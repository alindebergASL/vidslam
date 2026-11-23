"""Browser-safe preview copies of take/shot clips.

Video models sometimes return files a browser's <video> can't play: HEVC,
10-bit or 4:2:2 H.264, ProRes. The original stays untouched for renders
(the renderer reads anything ffmpeg does); the editor gets an 8-bit H.264
yuv420p copy made on first view and kept beside the clip as a hidden
`.<name>.<ext>.preview.mp4`, rebuilt when the clip is newer.

Coordination is by file locks, so it holds across the server's worker
processes as well as threads:
- one lock per clip: requests for a clip whose copy is being made wait for
  it (bounded by PREVIEW_TIMEOUT), so every range request of one <video>
  gets the same file;
- MAX_TRANSCODES slot locks: when every slot is busy, a request for another
  clip gets the original at once and the clip is queued for a background
  copy, ready on the next request (the endpoints send no-cache, so the
  browser asks again);
- a copy that ffmpeg rejected is not retried for FAILED_RETRY_SECONDS (a
  timeout or a kill is not remembered: the next request tries again).
The endpoints release their database connection before calling in here."""
from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import queue
import threading
import time
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Iterator, Optional

from . import renderer

log = logging.getLogger(__name__)

PREVIEW_TIMEOUT = 300.0
# Transcodes running at once across all workers (the host has 2 CPUs and
# renders share them); each one's ffmpeg is held to PREVIEW_THREADS threads.
MAX_TRANSCODES = 2
PREVIEW_THREADS = "2"
FAILED_RETRY_SECONDS = 600.0
_WAIT_STEP = 0.25

# (path, mtime_ns, size) -> when ffmpeg rejected the clip (per process; tried
# again after FAILED_RETRY_SECONDS, or as soon as the clip changes).
_FAILED: dict[tuple[str, int, int], float] = {}
_FAILED_GUARD = threading.Lock()


def preview_path(src: Path) -> Path:
    # The extension too: clip.mov and clip.mp4 get copies of their own.
    return src.with_name(f".{src.name}.preview.mp4")


def _legacy_path(src: Path) -> Path:
    return src.with_name(f".{src.stem}.preview.mp4")


def _clip_lock_path(src: Path) -> Path:
    return src.with_name(f".{src.name}.preview.lock")


def _slots_dir() -> Path:
    from ..config import get_settings

    d = get_settings().data_path / ".previews"
    d.mkdir(parents=True, exist_ok=True)
    return d


@contextmanager
def _flock(path: Path, *, wait: float = 0.0) -> Iterator[bool]:
    """An exclusive lock on `path` (created if missing), shared by every
    thread and process. Yields False when it isn't free within `wait`
    seconds (0: don't wait)."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        yield False
        return
    got = False
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EACCES) or time.monotonic() >= deadline:
                    break
                time.sleep(_WAIT_STEP)
        yield got
    finally:
        if got:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextmanager
def _slot(*, wait: float = 0.0) -> Iterator[bool]:
    """One of MAX_TRANSCODES transcode slots, across processes. Yields
    False when none is free within `wait` seconds."""
    deadline = time.monotonic() + wait
    d = _slots_dir()
    while True:
        for i in range(MAX_TRANSCODES):
            with _flock(d / f"slot-{i}.lock") as got:
                if got:
                    yield True
                    return
        if time.monotonic() >= deadline:
            yield False
            return
        time.sleep(_WAIT_STEP)


@lru_cache(maxsize=2048)
def _safe(path: str, mtime_ns: int, size: int) -> bool:
    """The file's first video stream is 8-bit H.264 yuv420p (keyed on
    mtime/size so a replaced file is probed again). Unreadable: True, so the
    original is served as before rather than a transcode attempted."""
    rc, out = renderer._run(["ffprobe", "-v", "quiet", "-print_format", "json", "-select_streams", "v:0",
                             "-show_entries", "stream=codec_name,pix_fmt", path], timeout=30)
    if rc != 0:
        return True
    try:
        st = json.loads(out)["streams"][0]
    except (KeyError, IndexError, TypeError, ValueError):
        return True
    return st.get("codec_name") == "h264" and st.get("pix_fmt") in ("yuv420p", "yuvj420p")


def _recently_failed(key: tuple[str, int, int]) -> bool:
    with _FAILED_GUARD:
        when = _FAILED.get(key)
        if when is None:
            return False
        if time.monotonic() - when >= FAILED_RETRY_SECONDS:
            del _FAILED[key]
            return False
        return True


def browser_safe(src: Path) -> Path:
    """The file to stream to the browser for `src`: itself when browsers
    play it, else its preview copy (made now if missing or stale). Falls
    back to `src` when the copy can't be made, or (queued for a background
    copy) when every transcode slot is busy with other clips."""
    try:
        st = src.stat()
    except OSError:
        return src
    if _safe(str(src), st.st_mtime_ns, st.st_size):
        return src
    dst = preview_path(src)
    if _fresh(dst, st.st_mtime_ns) or _adopt_legacy(src, dst, st.st_mtime_ns):
        return dst
    key = (str(src), st.st_mtime_ns, st.st_size)
    if _recently_failed(key):
        return src
    # Same clip: wait for a copy already being made (any worker), so all of
    # one <video>'s range requests get the same file.
    with _flock(_clip_lock_path(src), wait=PREVIEW_TIMEOUT) as got:
        if not got:
            return dst if _fresh(dst, st.st_mtime_ns) else src
        if _fresh(dst, st.st_mtime_ns):  # made while this request waited
            return dst
        if _recently_failed(key):
            return src
        # Other clips: never queue behind their transcodes.
        with _slot() as slot:
            if not slot:
                _BACKGROUND.add(src)  # the original now, its copy in the background
                return src
            return _make_and_note(src, dst, key)


def _make_and_note(src: Path, dst: Path, key: tuple[str, int, int]) -> Path:
    out, rc = _make(src, dst)
    if out == src and rc > 0:  # ffmpeg rejected it; a timeout or kill (rc < 0) is retried
        with _FAILED_GUARD:
            _FAILED[key] = time.monotonic()
    return out


def _adopt_legacy(src: Path, dst: Path, mtime_ns: int) -> bool:
    """A fresh copy named before the extension was kept (`.<stem>.preview.mp4`)
    is reused for a .mp4 clip rather than transcoded again."""
    if src.suffix.lower() != ".mp4":
        return False
    legacy = _legacy_path(src)
    if not _fresh(legacy, mtime_ns):
        return False
    try:
        os.replace(legacy, dst)
    except OSError:
        return False
    return True


def _fresh(dst: Path, src_mtime_ns: int) -> bool:
    try:
        return dst.stat().st_mtime_ns >= src_mtime_ns
    except OSError:
        return False


def _make(src: Path, dst: Path) -> tuple[Path, int]:
    """(path to serve, ffmpeg exit code): the new copy, or `src` on failure."""
    tmp = dst.with_name(f"{dst.stem}.{os.getpid()}.{threading.get_ident()}.tmp.mp4")
    conv = renderer.colour_chain(*renderer.probe_colour(src, timeout=30))
    vf = f"{conv},scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p"
    cmd = ["ffmpeg", "-y", "-threads", PREVIEW_THREADS, "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?",
           "-vf", vf, "-threads", PREVIEW_THREADS,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", *renderer.BT709_TAGS,
           "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(tmp)]
    rc, out = renderer._run(cmd, timeout=PREVIEW_TIMEOUT)
    if rc != 0:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass  # a read-only folder: the original is served all the same
        log.warning("preview copy of %s failed (rc %s): %s", src, rc, out[-500:])
        return src, rc
    try:
        os.replace(tmp, dst)
    except OSError:
        log.warning("could not keep the preview copy of %s", src)
        return src, -1
    return dst, rc


class _Background:
    """One thread that makes queued copies when a slot frees (deduplicated
    by path), so a clip that fell back to its original has its copy ready
    on the next request without another view. It waits for a slot before
    taking the clip's lock, so a request for that clip never waits behind
    the queue."""

    def __init__(self) -> None:
        self._q: "queue.Queue[Path]" = queue.Queue()
        self._pending: set[str] = set()
        self._guard = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def add(self, src: Path) -> None:
        with self._guard:
            if str(src) in self._pending:
                return
            self._pending.add(str(src))
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="preview-copies", daemon=True)
                self._thread.start()
        self._q.put(src)

    def _run(self) -> None:
        while True:
            src = self._q.get()
            try:
                self._copy(src)
            except Exception:  # noqa: BLE001 - a background copy must never take the thread down
                log.exception("background preview copy of %s failed", src)
            finally:
                with self._guard:
                    self._pending.discard(str(src))

    def _copy(self, src: Path) -> None:
        st = src.stat()
        dst = preview_path(src)
        key = (str(src), st.st_mtime_ns, st.st_size)
        if _fresh(dst, st.st_mtime_ns) or _recently_failed(key):
            return
        with _slot(wait=PREVIEW_TIMEOUT) as slot:
            if not slot:
                return
            with _flock(_clip_lock_path(src)) as got:
                if got and not _fresh(dst, st.st_mtime_ns):  # not made meanwhile by a request
                    _make_and_note(src, dst, key)


_BACKGROUND = _Background()


def remove(src: Path) -> None:
    """Delete `src`'s preview copy, if any (the clip itself is deleted)."""
    for p in (preview_path(src), _legacy_path(src), _clip_lock_path(src)):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            log.warning("could not delete %s", p)

"""Browser-safe preview copies of take/shot clips.

Video models sometimes return files a browser's <video> can't play: HEVC,
10-bit or 4:2:2 H.264, ProRes. The original stays untouched for renders
(the renderer reads anything ffmpeg does); the editor gets an 8-bit H.264
yuv420p copy made on first view and kept beside the clip as a hidden
`.<name>.<ext>.preview.mp4`, rebuilt when the clip is newer.

Requests never queue behind a transcode: while a clip's copy is being made,
or every transcode slot is busy, the original is served (the editor asks
again on the next view), and a copy that failed is not retried until the
clip changes. Each waiting request would otherwise hold a worker thread
and, in the endpoints, a pooled database connection."""
from __future__ import annotations

import json
import logging
import os
import threading
from functools import lru_cache
from pathlib import Path

from . import renderer

log = logging.getLogger(__name__)

PREVIEW_TIMEOUT = 300.0
# Transcodes running at once across clips (the host has 2 CPUs and renders
# share them); each one's ffmpeg is held to PREVIEW_THREADS threads.
MAX_TRANSCODES = 2
PREVIEW_THREADS = "2"
_SLOTS = threading.BoundedSemaphore(MAX_TRANSCODES)
# One lock per clip: a <video> sends several range requests at once, and
# each would otherwise start its own transcode of the same clip.
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
# (path, mtime_ns, size) of copies that failed: not retried until the clip changes.
_FAILED: set[tuple[str, int, int]] = set()


def preview_path(src: Path) -> Path:
    # The extension too: clip.mov and clip.mp4 get copies of their own.
    return src.with_name(f".{src.name}.preview.mp4")


def _legacy_path(src: Path) -> Path:
    return src.with_name(f".{src.stem}.preview.mp4")


def _lock_for(src: Path) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(str(src), threading.Lock())


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


def browser_safe(src: Path) -> Path:
    """The file to stream to the browser for `src`: itself when browsers
    play it, else its preview copy (made now if missing or stale). Falls
    back to `src` when the copy can't be made."""
    try:
        st = src.stat()
    except OSError:
        return src
    if _safe(str(src), st.st_mtime_ns, st.st_size):
        return src
    dst = preview_path(src)
    if _fresh(dst, st.st_mtime_ns):
        return dst
    key = (str(src), st.st_mtime_ns, st.st_size)
    if key in _FAILED:
        return src
    lock = _lock_for(src)
    if not lock.acquire(blocking=False):
        return src  # this clip's copy is being made right now
    try:
        if _fresh(dst, st.st_mtime_ns):  # made just before this request
            return dst
        if not _SLOTS.acquire(blocking=False):
            return src  # every transcode slot busy: serve the original, don't queue
        try:
            out = _make(src, dst)
        finally:
            _SLOTS.release()
        if out == src:
            _FAILED.add(key)
        return out
    finally:
        lock.release()


def _fresh(dst: Path, src_mtime_ns: int) -> bool:
    try:
        return dst.stat().st_mtime_ns >= src_mtime_ns
    except OSError:
        return False


def _make(src: Path, dst: Path) -> Path:
    tmp = dst.with_name(f"{dst.stem}.{os.getpid()}.tmp.mp4")  # pid: workers share the disk
    conv = renderer.colour_chain(*renderer.probe_colour(src, timeout=30))
    vf = f"{conv},scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p"
    cmd = ["ffmpeg", "-y", "-threads", PREVIEW_THREADS, "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?",
           "-vf", vf, "-threads", PREVIEW_THREADS,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", *renderer.BT709_TAGS,
           "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(tmp)]
    rc, out = renderer._run(cmd, timeout=PREVIEW_TIMEOUT)
    if rc != 0:
        tmp.unlink(missing_ok=True)
        log.warning("preview copy of %s failed: %s", src, out[-500:])
        return src
    os.replace(tmp, dst)
    return dst


def remove(src: Path) -> None:
    """Delete `src`'s preview copy, if any (the clip itself is deleted)."""
    try:
        preview_path(src).unlink(missing_ok=True)
        _legacy_path(src).unlink(missing_ok=True)  # named before the extension was kept
    except OSError:
        log.warning("could not delete preview copy of %s", src)

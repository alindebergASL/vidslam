"""Loop health: how well an ambient loop clip joins, and whether it
flickers, freezes or drifts in colour. Local measurement only (ffmpeg +
Pillow on small frames): free, and a 60 s loop takes a few seconds.

Readings (each with a verdict, a level for the coloured dot and a plain
sentence):
  seam     the last frame against the first, compared with the typical
           difference between neighbouring frames in the clip
  flicker  brightness flicker: std-dev of per-frame brightness after the
           slow drift is removed (a moving average), as a percentage
  frozen   longest stretch of near-identical frames, and the total
  drift    colour / brightness change from the first second to the last

Results are kept in the scene's JSON state under "_loop_health", keyed by
shot id, with the clip's path and mtime: a reading of a clip that has
since changed is stale and not shown.
"""
from __future__ import annotations

import logging
import math
import statistics
import subprocess
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from PIL import Image, ImageChops, ImageStat
from sqlalchemy.orm import Session

from .. import models

log = logging.getLogger("avs.loop_health")

STATE_KEY = "_loop_health"

# ---- thresholds (one place; tuned on synthetic clips and closed Wan/H3 loops) ----
# Frames are compared as 160 px wide greyscale; differences are mean
# absolute luma differences on a 0-1 scale.
ANALYSIS_WIDTH = 160
SAMPLE_FPS = 8.0          # sampled rate for flicker / freezing / drift
MAX_SAMPLES = 480         # long clips are sampled more sparsely (60 s → 8 fps)
WINDOW_SECONDS = 1.0      # native-rate frames read at each end for the join
# Join: seamless when the jump is no bigger than ~2.5 ordinary frame steps
# (or tiny in absolute terms); a visible jump beyond 6 steps.
TYPICAL_FLOOR = 0.004     # a near-still clip's typical step is noise: don't divide by ~0
SEAM_TINY = 0.008
SEAM_SEAMLESS_RATIO = 2.5
SEAM_SLIGHT_RATIO = 6.0
# Flicker, % of mean brightness: real calm scenes stay well under 1 %.
FLICKER_SLIGHT = 1.0
FLICKER_STRONG = 3.0
# Freezing: neighbouring samples closer than this are "the same frame";
# stretches shorter than FROZEN_MIN don't count (a calm scene can rest).
FROZEN_DIFF = 0.0015
FROZEN_MIN = 1.0
FROZEN_LONG = 3.0
# Colour drift: RGB distance between the first and last second's average
# colour, 0-1 (1 = black to white).
DRIFT_SLIGHT = 0.02
DRIFT_STRONG = 0.06

SEAM_PREVIEW_SECONDS = 3.0


# ---------- frame reading ----------


def _probe(clip: Path) -> tuple[float, tuple[int, int]]:
    from .renderer import _probe_duration, _probe_size

    return _probe_duration(clip), (_probe_size(clip) or (16, 9))


def _analysis_size(size: tuple[int, int]) -> tuple[int, int]:
    w, h = size
    aw = ANALYSIS_WIDTH
    ah = max(2, int(round(aw * h / max(w, 1) / 2.0)) * 2)
    return aw, ah


def _read_frames(cmd_in: list[str], vf_pre: str, size: tuple[int, int]) -> list[Image.Image]:
    w, h = size
    vf = ",".join(x for x in (vf_pre, f"scale={w}:{h}:flags=area", "format=rgb24") if x)
    cmd = ["ffmpeg", "-v", "error", *cmd_in, "-vf", vf, "-an", "-f", "rawvideo", "-"]
    proc = subprocess.run(cmd, capture_output=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"could not read the clip: {proc.stderr.decode(errors='replace')[-300:]}")
    n = w * h * 3
    data = proc.stdout
    return [Image.frombytes("RGB", (w, h), data[i:i + n]) for i in range(0, len(data) - n + 1, n)]


def _diff(a: Image.Image, b: Image.Image) -> float:
    return ImageStat.Stat(ImageChops.difference(a, b)).mean[0] / 255.0


def _median(xs: list[float]) -> float:
    return statistics.median(xs) if xs else 0.0


def _fmt_time(seconds: float) -> str:
    s = int(round(max(seconds, 0.0)))
    return f"{s // 60}:{s % 60:02d}"


# ---------- the readings ----------


def _seam(first: list[Image.Image], last: list[Image.Image]) -> dict:
    steps = [_diff(a, b) for win in (first, last) for a, b in zip(win, win[1:], strict=False)]
    typical = _median(steps)
    seam = _diff(last[-1], first[0])
    ratio = seam / max(typical, TYPICAL_FLOOR)
    if seam <= SEAM_TINY or ratio <= SEAM_SEAMLESS_RATIO:
        verdict, level, text = "seamless", "good", "The join is seamless"
    elif ratio <= SEAM_SLIGHT_RATIO:
        verdict, level, text = "slight_jump", "warn", "There's a slight jump at the join"
    else:
        verdict, level, text = "visible_jump", "bad", "There's a visible jump at the join"
    return {"verdict": verdict, "level": level, "text": text, "difference": round(seam, 4),
            "typical": round(typical, 4), "ratio": round(ratio, 2)}


def _moving_average(xs: list[float], window: int) -> list[float]:
    half = window // 2
    out = []
    for i in range(len(xs)):
        lo, hi = max(0, i - half), min(len(xs), i + half + 1)
        out.append(sum(xs[lo:hi]) / (hi - lo))
    return out


def _flicker(luma: list[float], fps: float) -> dict:
    if len(luma) < 4:
        pct = 0.0
    else:
        window = max(3, int(round(fps * 1.0)) | 1)
        resid = [y - m for y, m in zip(luma, _moving_average(luma, window), strict=True)]
        half = window // 2
        if len(resid) > 2 * half + 3:
            # The ends have a one-sided average, which reads a steady fade as flicker.
            resid = resid[half:len(resid) - half]
        pct = statistics.pstdev(resid) / max(statistics.fmean(luma), 0.1) * 100.0
    if pct < FLICKER_SLIGHT:
        verdict, level, text = "none", "good", "No flicker"
    elif pct < FLICKER_STRONG:
        verdict, level, text = "slight", "warn", f"Slight flicker ({pct:.1f}%)"
    else:
        verdict, level, text = "strong", "bad", f"Noticeable flicker ({pct:.1f}%)"
    return {"verdict": verdict, "level": level, "text": text, "percent": round(pct, 2)}


def _frozen(frames: list[Image.Image], fps: float, *, still: bool) -> dict:
    if still:
        return {"verdict": "not_checked", "level": "good",
                "text": "A still picture with slow camera drift, so freezing isn't checked",
                "longest_seconds": 0.0, "total_seconds": 0.0, "at_seconds": None}
    step = 1.0 / fps
    runs: list[tuple[int, int]] = []  # (start index, pairs)
    start, count = 0, 0
    for i, (a, b) in enumerate(zip(frames, frames[1:], strict=False)):
        if _diff(a, b) < FROZEN_DIFF:
            if count == 0:
                start = i
            count += 1
        else:
            if count:
                runs.append((start, count))
            count = 0
    if count:
        runs.append((start, count))
    runs = [(s, c) for s, c in runs if c * step >= FROZEN_MIN]
    total = sum(c for _, c in runs) * step
    if not runs:
        return {"verdict": "none", "level": "good", "text": "No frozen moments",
                "longest_seconds": 0.0, "total_seconds": 0.0, "at_seconds": None}
    s, c = max(runs, key=lambda r: r[1])
    longest, at = c * step, s * step
    text = f"Frozen for {longest:.1f} s around {_fmt_time(at)}"
    if total > longest + 0.5:
        text += f" ({total:.1f} s in all)"
    long_ = longest >= FROZEN_LONG
    return {"verdict": "long" if long_ else "some", "level": "bad" if long_ else "warn", "text": text,
            "longest_seconds": round(longest, 2), "total_seconds": round(total, 2), "at_seconds": round(at, 2)}


def _mean_rgb(frames: list[Image.Image]) -> tuple[float, float, float]:
    sums = [0.0, 0.0, 0.0]
    for f in frames:
        for i, v in enumerate(ImageStat.Stat(f).mean[:3]):
            sums[i] += v
    n = max(len(frames), 1)
    return (sums[0] / n, sums[1] / n, sums[2] / n)


def _drift(frames: list[Image.Image], fps: float) -> dict:
    k = max(1, int(round(fps)))
    a, b = _mean_rgb(frames[:k]), _mean_rgb(frames[-k:])
    dist = math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b, strict=True))) / (255.0 * math.sqrt(3))
    if dist < DRIFT_SLIGHT:
        verdict, level, text = "none", "good", "Colour stays the same from start to end"
    elif dist < DRIFT_STRONG:
        verdict, level, text = "slight", "warn", "Colour drifts slightly from start to end"
    else:
        verdict, level, text = "strong", "bad", "Colour changes noticeably from start to end"
    return {"verdict": verdict, "level": level, "text": text, "distance": round(dist, 4)}


def measure(clip: Path, *, still: bool = False) -> dict:
    """The four readings for `clip`. `still`: an image-drift loop (a still
    picture with slow camera motion), where freezing isn't meaningful."""
    duration, size = _probe(clip)
    if duration <= 0:
        raise RuntimeError("could not read the clip")
    asize = _analysis_size(size)
    fps = min(SAMPLE_FPS, MAX_SAMPLES / duration)
    frames = _read_frames(["-i", str(clip)], f"fps={fps:.4f}", asize)
    win = min(WINDOW_SECONDS, duration / 2.0)
    first = _read_frames(["-t", f"{win:.3f}", "-i", str(clip)], "", asize)
    last = _read_frames(["-sseof", f"-{win:.3f}", "-i", str(clip)], "", asize)
    if len(frames) < 2 or not first or not last:
        raise RuntimeError("the clip is too short to measure")
    luma = [ImageStat.Stat(f.convert("L")).mean[0] / 255.0 for f in frames]
    return {
        "clip_seconds": round(duration, 2),
        "sample_fps": round(fps, 2),
        "seam": _seam(first, last),
        "flicker": _flicker(luma, fps),
        "frozen": _frozen(frames, fps, still=still),
        "drift": _drift(frames, fps),
    }


# ---------- per scene ----------


def _clip_stamp(path: str) -> Optional[int]:
    try:
        return Path(path).stat().st_mtime_ns if path else None
    except OSError:
        return None


def clip_shots(project: models.VideoProject) -> list[models.VideoShot]:
    """The loop shot, then its variations (only those the scene uses)."""
    from .ambient import _config_of, _loop_shot, _variant_shots, variant_count

    loop = _loop_shot(project)
    out = [loop] if loop is not None else []
    out += _variant_shots(project)[:max(variant_count(_config_of(project)) - 1, 0)]
    return out


def _is_still(project: models.VideoProject) -> bool:
    gen = (project.ambient_config_json or {}).get("_generated") or {}
    cfg = project.ambient_config_json or {}
    return gen.get("strategy") == "drift" or cfg.get("source") == "image_drift"


def _stored(project: models.VideoProject) -> dict:
    return dict((project.ambient_config_json or {}).get(STATE_KEY) or {})


def current(project: models.VideoProject, shot: Optional[models.VideoShot]) -> Optional[dict]:
    """The reading of the shot's clip as it is now, or None (never measured,
    no clip, or the clip changed since)."""
    if shot is None or not shot.clip_path or shot.status != "completed":
        return None
    entry = _stored(project).get(str(shot.id))
    if not entry or entry.get("clip_path") != shot.clip_path:
        return None
    if entry.get("clip_mtime_ns") != _clip_stamp(shot.clip_path):
        return None
    return out(project, shot, entry)


def out(project: models.VideoProject, shot: models.VideoShot, entry: dict) -> dict:
    view = {k: v for k, v in entry.items() if k not in ("clip_path", "clip_mtime_ns")}
    view["shot_id"] = shot.id
    view["seam_preview_url"] = f"/api/ambient/{project.id}/seam-preview?shot_id={shot.id}"
    view["join_at_seconds"] = round(min(SEAM_PREVIEW_SECONDS, float(entry.get("clip_seconds") or 0) / 2.0), 2)
    return view


def measure_shot(db: Session, project: models.VideoProject, shot: models.VideoShot, *,
                 force: bool = False) -> Optional[dict]:
    """Measure the shot's clip (unless a current reading exists and not
    `force`), keep it in the scene's state, and return it."""
    if not force:
        have = current(project, shot)
        if have is not None:
            return have
    if not shot.clip_path or shot.status != "completed" or not Path(shot.clip_path).is_file():
        return None
    stamp = _clip_stamp(shot.clip_path)
    entry = measure(Path(shot.clip_path), still=_is_still(project))
    entry.update({"measured_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                  "clip_path": shot.clip_path, "clip_mtime_ns": stamp})
    db.refresh(project)  # the state may have moved on while we measured
    data = dict(project.ambient_config_json or {})
    health = dict(data.get(STATE_KEY) or {})
    live = {str(s.id) for s in clip_shots(project)}
    health = {k: v for k, v in health.items() if k in live}
    health[str(shot.id)] = entry
    data[STATE_KEY] = health
    project.ambient_config_json = data
    db.commit()
    return out(project, shot, entry)


def measure_scene(db: Session, project: models.VideoProject, *, force: bool = False) -> list[dict]:
    """Measure every clip the scene has (loop + variations). Never raises
    for one bad clip: it's logged and skipped."""
    results = []
    for shot in clip_shots(project):
        try:
            got = measure_shot(db, project, shot, force=force)
        except Exception as e:  # noqa: BLE001 — a reading is a nicety, never a failure
            db.rollback()
            log.warning("loop health of shot %s failed: %s", shot.id, e)
            got = None
        if got is not None:
            results.append(got)
    return results


def measure_quietly(db: Session, project_id: int) -> None:
    """After a loop is made: measure it. Never raises."""
    try:
        project = db.get(models.VideoProject, project_id)
        if project is not None:
            measure_scene(db, project)
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("loop health for scene %s failed", project_id)


# ---------- watch the seam ----------


_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


@contextmanager
def _clip_lock(clip: Path):
    """One seam preview is made at a time per clip: a thread lock (this
    process) plus a file lock next to the clip (other web / job workers)."""
    key = str(clip.resolve())
    with _locks_guard:
        lock = _locks.setdefault(key, threading.Lock())
    with lock:
        lock_path = clip.with_name(f".seam_{clip.stem}.lock")
        try:
            import fcntl
        except ImportError:  # not POSIX: the thread lock only
            yield
            return
        with open(lock_path, "a+b") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _playable(path: Path) -> bool:
    from .renderer import _probe_duration

    try:
        return path.is_file() and path.stat().st_size > 0 and _probe_duration(path) > 0
    except Exception:  # noqa: BLE001
        return False


def seam_preview(clip: Path) -> tuple[Path, float]:
    """A short MP4: the last few seconds of the loop, then its first few,
    so the join plays in the middle. Made once per version of the clip and
    kept next to it; returns (path, join time in seconds). Overlapping
    requests (a second tab, the player asking again) wait for the one
    making it; only a file that plays is kept."""
    stamp = clip.stat().st_mtime_ns
    from .renderer import _probe_duration

    duration = _probe_duration(clip)
    if duration <= 0:
        raise RuntimeError("could not read the clip")
    tail = round(min(SEAM_PREVIEW_SECONDS, duration / 2.0), 3)
    prefix = f"seam_{clip.stem}_"
    dst = clip.with_name(f"{prefix}{stamp}.mp4")
    if dst.is_file() and dst.stat().st_size > 0:
        return dst, tail
    with _clip_lock(clip):
        if dst.is_file() and dst.stat().st_size > 0:  # made while we waited
            return dst, tail
        # Under the lock nobody else is writing one for this clip: older
        # versions and leftovers of a crashed attempt can go.
        for old in clip.parent.glob(f"{prefix}*.mp4"):
            if old != dst:
                old.unlink(missing_ok=True)
        dst.unlink(missing_ok=True)
        tmp = dst.with_name(f"{dst.stem}.{uuid.uuid4().hex}.part.mp4")
        cmd = ["ffmpeg", "-y", "-v", "error", "-sseof", f"-{tail:.3f}", "-i", str(clip),
               "-t", f"{tail:.3f}", "-i", str(clip),
               "-filter_complex",
               "[0:v]setpts=PTS-STARTPTS[a];[1:v]setpts=PTS-STARTPTS[b];"
               "[a][b]concat=n=2:v=1:a=0,format=yuv420p[v]",
               "-map", "[v]", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
               "-movflags", "+faststart", str(tmp)]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=120)
        except subprocess.TimeoutExpired:
            tmp.unlink(missing_ok=True)
            raise RuntimeError("could not make the seam preview: ffmpeg took too long") from None
        if proc.returncode != 0 or not _playable(tmp):
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"could not make the seam preview: {proc.stderr.decode(errors='replace')[-300:]}")
        tmp.replace(dst)
        return dst, tail

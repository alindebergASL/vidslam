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
import re
import statistics
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageChops, ImageStat
from sqlalchemy.orm import Session

from .. import models

log = logging.getLogger("avs.loop_health")

STATE_KEY = "_loop_health"
# Bump when what a reading checks changes: older readings are measured again
# before they gate a render (2: the seam also reads the approach, so a take
# that snaps to its last frame isn't kept as "seamless").
MEASURE_VERSION = 2

# ---- thresholds (one place; tuned on synthetic clips and closed Wan/H3 loops) ----
# Frames are compared at 160 px wide; a difference is the mean absolute
# difference over all three colour channels on a 0-1 scale (so a change in
# green or blue alone counts as much as one in red; for grey pictures it is
# the luma difference).
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
# Snap (the host's take study, 37 takes): long first+last-frame takes often
# ignore the last-frame target until the final two frames, then jump to it.
# The last frame lands, so the join alone reads seamless; the approach
# doesn't. A take snaps when frame L-3 is far from the target (the loop's
# first frame) or from the last frame (block SSIM on grey, below 0.90), or
# when the last two frames move more than 8/255 on average.
SNAP_SSIM = 0.90
SNAP_STEP = 8.0 / 255.0
SNAP_BACK = 3
# Blend-source mismatch: how different the two pictures a loop blend mixes
# are (the clip's last second against its first, frame by frame; grey RMS,
# 0-255). Clean loop blends measured 20-27 and ghosted ones 34-50 at 320x180;
# at 160 px wide the numbers run a little lower. A note, not a gate (D5
# calibrates it).
MISMATCH_WARN = 30.0
# Flicker, % of mean brightness: real calm scenes stay well under 1 %.
FLICKER_SLIGHT = 1.0
FLICKER_STRONG = 3.0
# Freezing: samples closer than this to the first frame of the stretch are
# "the same frame" (against the first, not the neighbour, so slow steady
# motion builds up and ends the stretch); stretches shorter than FROZEN_MIN
# don't count (a calm scene can rest).
FROZEN_DIFF = 0.0015
FROZEN_MIN = 1.0
FROZEN_LONG = 3.0
# Colour drift: RGB distance between the first and last second's average
# colour, 0-1 (1 = black to white).
DRIFT_SLIGHT = 0.02
DRIFT_STRONG = 0.06

# The loop-point finder (services/loop_point.py) compares its candidate
# joins as if the clip ran at this rate: a 50 fps clip's steps are half a 25
# fps clip's for the same motion. The seam verdict above is not scaled (its
# thresholds were calibrated on real takes as measured).
REFERENCE_FPS = 24.0

SEAM_PREVIEW_SECONDS = 3.0
PART_ABANDONED_SECONDS = 600  # a half-written preview older than this is a crash's leftover


# ---------- frame reading ----------


def _probe(clip: Path, budget: Optional[Callable[[], float]] = None) -> tuple[float, tuple[int, int]]:
    """Duration and size. `budget`: asked before each ffprobe for the
    seconds it may take (a measurement under a deadline); None: no limit."""
    from .renderer import _probe_duration, _probe_size

    if budget is None:
        return _probe_duration(clip), (_probe_size(clip) or (16, 9))
    duration = _probe_duration(clip, timeout=budget())
    return duration, (_probe_size(clip, timeout=budget()) or (16, 9))


def _probe_fps(clip: Path) -> float:
    """The clip's frame rate (0 when ffprobe can't tell)."""
    from .renderer import _probe_video

    info = _probe_video(clip)
    return float(info.fps) if info is not None and info.fps > 0 else 0.0


def step_scale(fps: float) -> float:
    """Multiplier that turns a difference between neighbouring frames at
    `fps` into one at REFERENCE_FPS (1 when the rate is unknown)."""
    return fps / REFERENCE_FPS if fps > 0 else 1.0


def _analysis_size(size: tuple[int, int]) -> tuple[int, int]:
    w, h = size
    aw = ANALYSIS_WIDTH
    ah = max(2, int(round(aw * h / max(w, 1) / 2.0)) * 2)
    return aw, ah


MEASURE_TIMEOUT_SECONDS = 120.0


def _read_frames(cmd_in: list[str], vf_pre: str, size: tuple[int, int], *,
                 timeout: float = MEASURE_TIMEOUT_SECONDS) -> list[Image.Image]:
    w, h = size
    vf = ",".join(x for x in (vf_pre, f"scale={w}:{h}:flags=area", "format=rgb24") if x)
    cmd = ["ffmpeg", "-v", "error", *cmd_in, "-vf", vf, "-an", "-f", "rawvideo", "-"]
    proc = subprocess.run(cmd, capture_output=True, timeout=max(0.1, timeout))
    if proc.returncode != 0:
        raise RuntimeError(f"could not read the clip: {proc.stderr.decode(errors='replace')[-300:]}")
    n = w * h * 3
    data = proc.stdout
    return [Image.frombytes("RGB", (w, h), data[i:i + n]) for i in range(0, len(data) - n + 1, n)]


def _diff(a: Image.Image, b: Image.Image) -> float:
    """Mean absolute difference over every channel (0-1): a cut or a change
    in one colour only (a teal sea, a night sky) counts, not just red."""
    means = ImageStat.Stat(ImageChops.difference(a, b)).mean
    return sum(means) / len(means) / 255.0


def _median(xs: list[float]) -> float:
    return statistics.median(xs) if xs else 0.0


def _fmt_time(seconds: float) -> str:
    s = int(round(max(seconds, 0.0)))
    return f"{s // 60}:{s % 60:02d}"


# ---------- the readings ----------


_SSIM_BLOCK = 8
_C1 = (0.01 * 255) ** 2
_C2 = (0.03 * 255) ** 2


def _ssim(a: Image.Image, b: Image.Image) -> float:
    """Structural similarity of two frames on grey, averaged over 8x8
    blocks (1 = the same picture). Exact sums over the raw pixels (Pillow's
    multiply rounds each product, which is too coarse for a covariance)."""
    ga, gb = a.convert("L"), b.convert("L")
    w, h = ga.size
    pa, pb = ga.tobytes(), gb.tobytes()
    n = _SSIM_BLOCK * _SSIM_BLOCK
    vals: list[float] = []
    for y in range(0, h - _SSIM_BLOCK + 1, _SSIM_BLOCK):
        for x in range(0, w - _SSIM_BLOCK + 1, _SSIM_BLOCK):
            sa = sb = saa = sbb = sab = 0
            for row in range(y, y + _SSIM_BLOCK):
                off = row * w + x
                for va, vb in zip(pa[off:off + _SSIM_BLOCK], pb[off:off + _SSIM_BLOCK], strict=True):
                    sa += va
                    sb += vb
                    saa += va * va
                    sbb += vb * vb
                    sab += va * vb
            ma, mb = sa / n, sb / n
            var_a, var_b, cov = saa / n - ma * ma, sbb / n - mb * mb, sab / n - ma * mb
            vals.append(((2 * ma * mb + _C1) * (2 * cov + _C2))
                        / ((ma * ma + mb * mb + _C1) * (var_a + var_b + _C2)))
    return sum(vals) / len(vals) if vals else 1.0


def _grey_step(a: Image.Image, b: Image.Image) -> float:
    return ImageStat.Stat(ImageChops.difference(a.convert("L"), b.convert("L"))).mean[0] / 255.0


def _approach(first: list[Image.Image], last: list[Image.Image]) -> Optional[dict]:
    """Whether the clip snaps to its last frame (see SNAP_SSIM): the frame
    SNAP_BACK before the end against the target (the first frame) and the
    last frame. None when the end window is too short to tell."""
    if len(last) <= SNAP_BACK or not first:
        return None
    before = last[-SNAP_BACK]
    to_target = _ssim(before, first[0])
    to_last = _ssim(before, last[-1])
    step = _grey_step(last[-2], last[-1])
    snaps = to_target < SNAP_SSIM or to_last < SNAP_SSIM or step > SNAP_STEP
    return {"snaps": snaps, "to_target": round(to_target, 3), "to_last": round(to_last, 3),
            "last_step": round(step, 4)}


def _mismatch(first: list[Image.Image], last: list[Image.Image]) -> Optional[float]:
    """Grey RMS (0-255) between the pictures a loop blend over the end
    windows would mix: the last window's frames against the first's, in
    order."""
    n = min(len(first), len(last))
    if n == 0:
        return None
    total = 0.0
    for a, b in zip(last[-n:], first[:n], strict=True):
        diff = ImageChops.difference(a.convert("L"), b.convert("L"))
        total += ImageStat.Stat(diff).rms[0]
    return round(total / n, 2)


def _seam(first: list[Image.Image], last: list[Image.Image]) -> dict:
    steps = [_diff(a, b) for win in (first, last) for a, b in zip(win, win[1:], strict=False)]
    typical = _median(steps)
    seam = _diff(last[-1], first[0])
    ratio = seam / max(typical, TYPICAL_FLOOR)
    approach = _approach(first, last)
    if seam <= SEAM_TINY or ratio <= SEAM_SEAMLESS_RATIO:
        verdict, level, text = "seamless", "good", "The join is seamless"
    elif ratio <= SEAM_SLIGHT_RATIO:
        verdict, level, text = "slight_jump", "warn", "There's a slight jump at the join"
    else:
        verdict, level, text = "visible_jump", "bad", "There's a visible jump at the join"
    if verdict != "visible_jump" and approach and approach["snaps"]:
        # The last frame lands on the target, but only by jumping to it in
        # the final frames: a blend over that tail double-exposes, so it is
        # treated like a visible jump.
        verdict, level, text = "snap", "bad", "The clip jumps to its last frame at the very end"
    mismatch = _mismatch(first, last)
    return {"verdict": verdict, "level": level, "text": text, "difference": round(seam, 4),
            "typical": round(typical, 4), "ratio": round(ratio, 2), "approach": approach,
            "blend_mismatch": mismatch,
            "blend_mismatch_level": ("warn" if mismatch is not None and mismatch > MISMATCH_WARN else "good")}


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
    runs: list[tuple[int, int]] = []  # (start index, steps)
    # Each sample is compared with the first of the current stretch, so a
    # slow pan or drifting fog (tiny steps that add up) ends the stretch.
    anchor = 0
    for i in range(1, len(frames)):
        if _diff(frames[anchor], frames[i]) < FROZEN_DIFF:
            continue
        if i - 1 > anchor:
            runs.append((anchor, i - 1 - anchor))
        anchor = i
    if len(frames) - 1 > anchor:
        runs.append((anchor, len(frames) - 1 - anchor))
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


def measure(clip: Path, *, still: bool = False, timeout: float = MEASURE_TIMEOUT_SECONDS,
            deadline: Optional[float] = None) -> dict:
    """The four readings for `clip`. `still`: an image-drift loop (a still
    picture with slow camera motion), where freezing isn't meaningful.
    `timeout`: seconds each FFmpeg read may take (subprocess.TimeoutExpired
    past it). `deadline`: a time.monotonic() by which the whole measurement
    must be done (each read gets what is left; TimeoutError once it's gone)."""

    def budget() -> float:
        if deadline is None:
            return timeout
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("the measurement ran out of time")
        return min(timeout, left)

    duration, size = _probe(clip, budget if deadline is not None else None)
    if duration <= 0:
        budget()  # a probe that ran out of time: TimeoutError, not "unreadable"
        raise RuntimeError("could not read the clip")
    asize = _analysis_size(size)
    fps = min(SAMPLE_FPS, MAX_SAMPLES / duration)
    frames = _read_frames(["-i", str(clip)], f"fps={fps:.4f}", asize, timeout=budget())
    win = min(WINDOW_SECONDS, duration / 2.0)
    first = _read_frames(["-t", f"{win:.3f}", "-i", str(clip)], "", asize, timeout=budget())
    last = _read_frames(["-sseof", f"-{win:.3f}", "-i", str(clip)], "", asize, timeout=budget())
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


def current_entry(project: models.VideoProject, shot: Optional[models.VideoShot]) -> Optional[dict]:
    """The stored reading of the shot's clip as it is now (raw, as measured),
    or None (never measured, no clip, or the clip changed since)."""
    if shot is None or not shot.clip_path or shot.status != "completed":
        return None
    entry = _stored(project).get(str(shot.id))
    if not entry or entry.get("clip_path") != shot.clip_path:
        return None
    if entry.get("clip_mtime_ns") != _clip_stamp(shot.clip_path):
        return None
    if entry.get("version") != MEASURE_VERSION:
        return None  # read before the current checks: measure again
    return entry


def outdated(project: models.VideoProject, shot: Optional[models.VideoShot]) -> bool:
    """The shot's clip has a reading of the clip as it is now, but from an
    older version of the checks (not shown: the page asks to measure again)."""
    if shot is None or not shot.clip_path or shot.status != "completed":
        return False
    entry = _stored(project).get(str(shot.id))
    return bool(entry and entry.get("clip_path") == shot.clip_path
                and entry.get("clip_mtime_ns") == _clip_stamp(shot.clip_path)
                and entry.get("version") != MEASURE_VERSION)


def unmeasured(project: models.VideoProject) -> bool:
    """Whether any clip the scene uses (the loop or a variation) has no
    current reading: never measured, or changed since."""
    return any(current_entry(project, s) is None for s in clip_shots(project))


def current(project: models.VideoProject, shot: Optional[models.VideoShot]) -> Optional[dict]:
    """The reading of the shot's clip as it is now, or None (never measured,
    no clip, or the clip changed since)."""
    entry = current_entry(project, shot)
    return None if entry is None else out(project, shot, entry)


def out(project: models.VideoProject, shot: models.VideoShot, entry: dict) -> dict:
    view = {k: v for k, v in entry.items() if k not in ("clip_path", "clip_mtime_ns")}
    view["shot_id"] = shot.id
    # The reading is of the raw clip; the finished video dissolves across the
    # join, which hides a small jump. Say so, and soften the level only when
    # the blend covers it (a visible jump stays "bad": the render asks first).
    seam = dict(view.get("seam") or {})
    seam["raw_level"] = seam.get("level")
    try:
        from .ambient import seam_treatment

        treatment = seam_treatment(project)
    except Exception:  # noqa: BLE001 — a note, never a failure
        treatment = None
    blend = float(treatment["blend_seconds"]) if treatment else 0.0
    # Per clip: the scene's treatment is the worst join of all its clips, so
    # a variation's slight jump is still covered when another clip jumps
    # visibly, and a visible jump never is (whatever the blend).
    covers = seam.get("verdict") not in ("visible_jump", "snap")
    seam["treatment"] = treatment
    seam["blend_seconds"] = round(blend, 2)
    if seam.get("verdict") == "snap":
        seam["text"] = ("The raw clip jumps to its last frame in the final frames; a blend over that "
                        "tail would show two pictures at once, so the loop will jump")
    elif blend > 0 and seam.get("verdict") in ("slight_jump", "visible_jump"):
        jump = "a slight jump" if seam["verdict"] == "slight_jump" else "a visible jump"
        if covers:
            seam["text"] = f"The raw clip has {jump} at the join; the finished video blends it over {blend:g} s"
            if seam.get("level") == "bad":
                seam["level"] = "warn"
        else:
            seam["text"] = (f"The raw clip has {jump} at the join; a {blend:g} s blend won't hide it, "
                            "so the loop will jump")
    view["seam_preview_url"] = f"/api/ambient/{project.id}/seam-preview?shot_id={shot.id}"
    view["join_at_seconds"] = round(min(SEAM_PREVIEW_SECONDS, float(entry.get("clip_seconds") or 0) / 2.0), 2)
    lp = _loop_point_for(project, shot)
    if lp is not None and treatment:
        # The render loops a found stretch of this clip: the whole clip's
        # join no longer plays. Say which stretch, and how its join reads.
        verdict = treatment.get("verdict")
        level = {"seamless": "good", "slight_jump": "warn"}.get(verdict, "bad")
        if verdict == "visible_jump" and treatment.get("covers"):
            level = "warn"
        what = {"seamless": "joins seamlessly", "slight_jump": "has a slight jump at its join"}.get(
            verdict, "still jumps at its join")
        seam["level"] = level
        seam["text"] = (f"The render loops {lp['in']:.1f}–{lp['out']:.1f} s of the clip, which {what} "
                        f"(blended over {blend:g} s)")
        unit = float(lp["out"]) - float(lp["in"])
        view["join_at_seconds"] = round(min(SEAM_PREVIEW_SECONDS, unit / 2.0), 2)
        view["seam_preview_url"] += f"&lp={lp['in']:g}-{lp['out']:g}-{blend:g}"
    view["seam"] = seam
    return view


def _loop_point_for(project: models.VideoProject, shot: models.VideoShot) -> Optional[dict]:
    """The loop stretch the render uses for this shot's clip, if any."""
    try:
        from .ambient import _loop_shot, active_loop_point

        loop = _loop_shot(project)
        if loop is None or loop.id != shot.id:
            return None
        return active_loop_point(project)
    except Exception:  # noqa: BLE001 — a note, never a failure
        return None


def measure_shot(db: Session, project: models.VideoProject, shot: models.VideoShot, *,
                 force: bool = False, timeout: float = MEASURE_TIMEOUT_SECONDS,
                 deadline: Optional[float] = None) -> Optional[dict]:
    """Measure the shot's clip (unless a current reading exists and not
    `force`), keep it in the scene's state, and return it."""
    if not force:
        have = current(project, shot)
        if have is not None:
            return have
    if not shot.clip_path or shot.status != "completed" or not Path(shot.clip_path).is_file():
        return None
    stamp = _clip_stamp(shot.clip_path)
    entry = measure(Path(shot.clip_path), still=_is_still(project), timeout=timeout, deadline=deadline)
    entry.update({"version": MEASURE_VERSION,
                  "measured_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
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


def measure_scene(db: Session, project: models.VideoProject, *, force: bool = False,
                  timeout: float = MEASURE_TIMEOUT_SECONDS) -> list[dict]:
    """Measure every clip the scene has (loop + variations). Never raises
    for one bad clip: it's logged and skipped. `timeout`: per FFmpeg read
    (a request that waits on it passes a shorter one)."""
    results = []
    for shot in clip_shots(project):
        try:
            got = measure_shot(db, project, shot, force=force, timeout=timeout)
        except Exception as e:  # noqa: BLE001 — a reading is a nicety, never a failure
            db.rollback()
            log.warning("loop health of shot %s failed: %s", shot.id, e)
            got = None
        if got is not None:
            results.append(got)
    return results


def measure_loop(db: Session, project: models.VideoProject, *, within: float) -> Optional[dict]:
    """Measure just the loop clip (unless its reading is current), all of it
    within `within` seconds: a request waits on this, and only the loop
    clip's reading decides whether it can tell if the loop jumps. Never
    raises (None: no reading)."""
    from .ambient import _loop_shot

    shot = _loop_shot(project)
    if shot is None:
        return None
    try:
        return measure_shot(db, project, shot, timeout=within, deadline=time.monotonic() + within)
    except Exception as e:  # noqa: BLE001 — no reading: the caller asks
        db.rollback()
        log.warning("loop health of shot %s failed: %s", shot.id, e)
        return None


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


def _preview_graph(clip: Path, duration: float, loop: Optional[tuple[float, float]],
                   blend: float, usable_end: Optional[float] = None) -> tuple[str, float, str]:
    """(filter_complex, join time, cache key) for the seam preview: the raw
    join (the end, then the start), or with `loop` / `blend` the join as the
    render makes it: the loop unit's end, then its start."""
    if loop is None and blend <= 0:
        tail = round(min(SEAM_PREVIEW_SECONDS, duration / 2.0), 3)
        return ("[0:v]setpts=PTS-STARTPTS[a];[1:v]setpts=PTS-STARTPTS[b];"
                "[a][b]concat=n=2:v=1:a=0,format=yuv420p[v]"), tail, ""
    from .renderer import loop_frames, loop_unit_graph, seamless_unit_graph

    fps = _probe_fps(clip) or 30.0
    if loop is not None:
        frames = loop_frames(duration, loop[0], loop[1], blend, fps=fps, usable_end=usable_end)
        if frames is None:
            raise RuntimeError("the loop point is outside the clip")
        a, b, h = frames
        unit_graph = loop_unit_graph(start_frame=a, end_frame=b, half=h, out="u", fps=fps)
        unit_len = (b - a) / fps
        key = f"{a}-{b}-{h}"
    else:
        cf = min(blend, duration / 3.0)
        unit_graph = seamless_unit_graph(length=duration, crossfade=cf, out="u")
        unit_len = duration - cf
        key = f"0-{cf:.3f}"
    tail = round(min(SEAM_PREVIEW_SECONDS, unit_len / 2.0), 3)
    graph = (unit_graph + ";[u]split[u1][u2];"
             f"[u1]trim=start={unit_len - tail:.4f},setpts=PTS-STARTPTS[a];"
             f"[u2]trim=end={tail:.4f},setpts=PTS-STARTPTS[b];"
             "[a][b]concat=n=2:v=1:a=0,format=yuv420p[v]")
    import hashlib

    return graph, tail, "_b" + hashlib.sha1(key.encode()).hexdigest()[:8]


def seam_preview(clip: Path, *, loop: Optional[tuple[float, float]] = None,
                 blend: float = 0.0, usable_end: Optional[float] = None) -> tuple[Path, float]:
    """A short MP4: the last few seconds of the loop, then its first few,
    so the join plays in the middle. Made once per version of the clip and
    kept next to it; returns (path, join time in seconds). Overlapping
    requests (a second tab, the player asking again) wait for the one
    making it; only a file that plays is kept.

    `loop` (clip seconds in, out) and `blend` (seconds of dissolve): play
    the join as the render makes it (the found loop stretch, dissolved);
    without them, the raw clip's own join. `usable_end` (clip seconds):
    a snapping tail the render leaves out, so the preview does too.

    The length is the picture's (a sound track past it, as Wan's, would
    count frames that don't exist)."""
    stamp = clip.stat().st_mtime_ns
    from .loop_point import picture_seconds
    from .renderer import _probe_duration

    duration = picture_seconds(clip) or _probe_duration(clip)
    if duration <= 0:
        raise RuntimeError("could not read the clip")
    graph, tail, key = _preview_graph(clip, duration, loop, blend, usable_end)
    prefix = f"seam_{clip.stem}_"
    dst = clip.with_name(f"{prefix}{stamp}{key}.mp4")
    if _playable(dst):
        return dst, tail
    with _clip_lock(clip):
        if _playable(dst):  # made while we waited
            return dst, tail
        # Under the lock nobody else is writing one for this clip: older
        # versions (and a cached file that doesn't play) go. Only this clip's
        # own files ("seam_loop_<n>[_b<key>]", never "seam_loop_2_<n>"); a
        # .part file only once it is clearly a crashed attempt's leftover.
        mine = re.compile(rf"^{re.escape(prefix)}\d+(_b[0-9a-f]{{8}})?(\.[0-9a-f]{{32}}\.part)?\.mp4$")
        for old in clip.parent.glob(f"{prefix}*.mp4"):
            m = mine.match(old.name)
            if m is None:
                continue
            try:
                if m.group(2) and time.time() - old.stat().st_mtime < PART_ABANDONED_SECONDS:
                    continue
            except OSError:
                continue
            old.unlink(missing_ok=True)
        tmp = dst.with_name(f"{dst.stem}.{uuid.uuid4().hex}.part.mp4")
        if key:
            inputs = ["-i", str(clip)]
        else:
            inputs = ["-sseof", f"-{tail:.3f}", "-i", str(clip), "-t", f"{tail:.3f}", "-i", str(clip)]
        cmd = ["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", graph,
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

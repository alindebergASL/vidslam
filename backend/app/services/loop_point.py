"""Loop-point finder: the stretch of a loop clip whose end matches its start
best, so the finished video loops [in, out) instead of the whole clip. Free
and local (ffmpeg + Pillow on small frames, no numpy); a 30 s clip takes a
few seconds.

1. Coarse: frames at 8 fps, 160 px wide. Every pair (i, j) at least
   `min_loop` apart is scored on the mean difference of frames i+k and j+k
   for k in -2..2, so the motion matches as well as the picture.
2. Refine: the best few coarse pairs at the clip's own frame rate, within
   ±0.5 s, preferring a calm join (less motion to dissolve across).
3. Source mismatch: the dissolve mixes clip[out-h, out+h) into
   clip[in-h, in+h); the grey RMS between those two sources, frame by frame
   (loop_health's blend mismatch), above MISMATCH_MAX means two different
   pictures overlaid (a ghost), and the pair is rejected. Clean loop blends
   read 20-27, ghost ones 34-50 at 320 px grey.

A take that snaps to its last frame (loop_health's "snap": it ignores its
last-frame target until the final frames, then jumps to it) is searched
with `snap=True`: its last SNAP_BACK + 1 frames are never used, as an end
point or under the dissolve.

The result's ratio is the jump at the new join against the typical step
between neighbouring frames (scaled to 24 fps, like loop_health), and the
crossfade is sized from it: ≤2.5 → 0.5 s, ≤4 → 1 s, ≤6 → 2 s, +1 s for a
calm join; above 6 nothing is found.
"""
from __future__ import annotations

import logging
import math
import statistics
import time
from pathlib import Path
from typing import Optional

from PIL import Image

from . import loop_health as lh

log = logging.getLogger("avs.loop_point")

COARSE_FPS = 8.0
COARSE_MAX_SAMPLES = 480      # a long clip is sampled more sparsely
WINDOW = 2                    # frames either side matched with the pair
REFINE_SECONDS = 0.5
TOP_PAIRS = 3
MIN_LOOP_FLOOR = 8.0          # default min_loop = max(8 s, 40 % of the clip)
MIN_LOOP_SHARE = 0.4
EDGE_SECONDS = 1.5            # room kept at each end for the dissolve
MIN_CLIP_SECONDS = 4.0
MISMATCH_WIDTH = 320
MISMATCH_MAX = lh.MISMATCH_WARN   # grey RMS 0-255 between the dissolve's two sources
MISMATCH_SAMPLES = 5              # frames compared across the dissolve
NOMINAL_DISSOLVE = 0.5            # seconds, when the pair gets no crossfade of its own
CALM_SHARE = 0.8              # a join with motion below 0.8× typical is calm
CALM_PREFERENCE = 0.85        # a calm join wins over a slightly better busy one
# The crossfade rule (ratio → seconds); above the last, the pair is rejected.
CROSSFADE_RULE = ((lh.SEAM_SEAMLESS_RATIO, 0.5), (4.0, 1.0), (lh.SEAM_SLIGHT_RATIO, 2.0))
CALM_EXTRA = 1.0


class LoopPointError(RuntimeError):
    pass


def crossfade_for(ratio: float, calm: bool) -> Optional[float]:
    """Seconds of dissolve for a join of `ratio`; None above the last band."""
    for limit, seconds in CROSSFADE_RULE:
        if ratio <= limit:
            return seconds + (CALM_EXTRA if calm else 0.0)
    return None


def verdict_for(ratio: float) -> str:
    if ratio <= lh.SEAM_SEAMLESS_RATIO:
        return "seamless"
    return "slight_jump" if ratio <= lh.SEAM_SLIGHT_RATIO else "visible_jump"


def default_min_loop(duration: float) -> float:
    return min(max(MIN_LOOP_FLOOR, MIN_LOOP_SHARE * duration), 0.8 * duration)


def blend_mismatch(clip: Path, a: int, b: int, half: int, fps: float,
                   size: tuple[int, int]) -> Optional[float]:
    """Grey RMS (0-255) between the two sources a dissolve of `half` frames
    each side of the join mixes: clip[b-half, b+half) against clip[a-half,
    a+half), at MISMATCH_SAMPLES evenly spaced frames. None when the frames
    can't be read."""
    half = max(1, half)
    picks = sorted({int(round(k * (2 * half - 1) / max(1, MISMATCH_SAMPLES - 1)))
                    for k in range(MISMATCH_SAMPLES)})
    tails: list[Image.Image] = []
    heads: list[Image.Image] = []
    for off in picks:
        fb = _frame_at(clip, b - half + off, fps, size)
        fa = _frame_at(clip, a - half + off, fps, size)
        if fa is None or fb is None:
            return None
        tails.append(fb)
        heads.append(fa)
    return lh._mismatch(heads, tails)


def _frame_at(clip: Path, index: int, fps: float, size: tuple[int, int]) -> Optional[Image.Image]:
    start = max(0.0, (index - 0.5) / fps)
    got = lh._read_frames(["-ss", f"{start:.4f}", "-t", f"{1.5 / fps:.4f}", "-i", str(clip)], "", size)
    return got[0] if got else None


def _window(clip: Path, first: int, count: int, fps: float, size: tuple[int, int]) -> list[Image.Image]:
    """Native frames first..first+count-1 (seeking half a frame early, so the
    first decoded frame is `first`)."""
    start = max(0.0, (first - 0.5) / fps) if first > 0 else 0.0
    return lh._read_frames(["-ss", f"{start:.4f}", "-t", f"{(count + 0.5) / fps:.4f}", "-i", str(clip)],
                           "", size)[:count]


def _coarse_pairs(frames: list[Image.Image], *, lo: int, hi: int, gap: int) -> list[tuple[float, int, int]]:
    """(score, i, j) for every pair lo ≤ i, j ≤ hi with j − i ≥ gap; the
    score is the mean difference over the ±WINDOW frames around the pair.
    One difference per (frame, frame + offset), reused by every window."""
    out: list[tuple[float, int, int]] = []
    span = 2 * WINDOW + 1
    for off in range(gap, hi - lo + 1):
        a0, a1 = lo - WINDOW, hi - off + WINDOW
        if a1 < a0:
            break
        row = [lh._diff(frames[a], frames[a + off]) for a in range(a0, a1 + 1)]
        acc = sum(row[:span])
        for k, i in enumerate(range(lo, hi - off + 1)):
            if k:
                acc += row[k + span - 1] - row[k - 1]
            out.append((acc / span, i, i + off))
    return out


def _pick(pairs: list[tuple[float, int, int]], *, apart: int) -> list[tuple[float, int, int]]:
    chosen: list[tuple[float, int, int]] = []
    for p in sorted(pairs):
        if all(abs(p[1] - c[1]) > apart or abs(p[2] - c[2]) > apart for c in chosen):
            chosen.append(p)
            if len(chosen) == TOP_PAIRS:
                break
    return chosen


def _rank(r: dict) -> tuple:
    """Usable pairs first (no ghost, within the crossfade rule), then the
    calm preference: a calm pair just past the rule mustn't beat a usable one
    and leave the search with nothing found."""
    usable = r["mismatch_ok"] and crossfade_for(r["ratio"], r["calm"]) is not None
    return (not usable, not r["mismatch_ok"], r["ratio"] * (CALM_PREFERENCE if r["calm"] else 1.0))


def find_loop_point(clip: Path, min_loop: Optional[float] = None, *, snap: bool = False) -> dict:
    """The best loop stretch of `clip`: {in, out, ratio, raw_ratio,
    crossfade, calm, mismatch, mismatch_ok, verdict, found, snap,
    usable_end, ...} with times in the clip's own seconds. `found` is False
    when no stretch joins well enough (ratio above 6, or every candidate's
    dissolve would mix two different pictures); the best one tried is still
    described. `snap`: the take snaps to its last frame, so its last
    SNAP_BACK + 1 frames are left out (usable_end). Raises LoopPointError
    when the clip can't be read or is too short."""
    t0 = time.monotonic()
    duration, size = lh._probe(clip)
    if duration <= 0:
        raise LoopPointError("could not read the clip")
    if duration < MIN_CLIP_SECONDS:
        raise LoopPointError("the clip is too short to look for a loop point")
    fps = lh._probe_fps(clip) or 30.0
    scale = lh.step_scale(fps)
    asize = lh._analysis_size(size)
    min_loop = default_min_loop(duration) if min_loop is None else float(min_loop)
    edge = min(EDGE_SECONDS, 0.05 * duration)
    total_frames = max(1, int(math.floor(duration * fps + 1e-6)))
    # A snapping tail is never used: not as an end point, not under the dissolve.
    guard = (lh.SNAP_BACK + 1) if snap else 0
    usable_end = (total_frames - guard) / fps

    # 1. Coarse search.
    cfps = min(COARSE_FPS, COARSE_MAX_SAMPLES / duration)
    frames = lh._read_frames(["-i", str(clip)], f"fps={cfps:.4f}", asize)
    n = len(frames)
    lo = max(WINDOW, math.ceil(edge * cfps))
    hi = min(n - 1 - WINDOW, math.floor((usable_end - edge) * cfps))
    gap = max(1, math.ceil(min_loop * cfps))
    coarse_steps = [lh._diff(a, b) for a, b in zip(frames, frames[1:], strict=False)]
    coarse_typical = max(lh._median(coarse_steps), 1e-6)
    pairs = _coarse_pairs(frames, lo=lo, hi=hi, gap=gap) if hi - lo >= gap else []
    if not pairs:
        raise LoopPointError("the clip is too short for a loop of that length")
    candidates = _pick(pairs, apart=max(1, int(round(cfps))))
    coarse_seconds = time.monotonic() - t0

    # 2. Refine at the native rate. All native steps read feed one typical
    #    step, so every candidate's ratio (and the whole clip's) is measured
    #    against the same yardstick.
    last_index = max(1, total_frames - 1 - guard)
    head = _window(clip, 0, int(round(fps * 0.5)) + 1, fps, asize)
    tail = lh._read_frames(["-sseof", f"-{min(0.5, duration / 2):.3f}", "-i", str(clip)], "", asize)
    if guard:
        tail = tail[:-guard] or tail  # the snap itself isn't a typical step
    steps: list[float] = [lh._diff(a, b) for w in (head, tail) for a, b in zip(w, w[1:], strict=False)]
    reach = int(round(REFINE_SECONDS * fps))
    refined = []
    edge_frames = int(math.ceil(edge * fps))
    gap_frames = int(math.ceil(min_loop * fps))
    for _score, ci, cj in candidates:
        ca, cb = int(round(ci / cfps * fps)), int(round(cj / cfps * fps))
        a_first = max(0, ca - reach - WINDOW - 1)
        b_first = max(0, cb - reach - WINDOW - 1)
        count = 2 * (reach + WINDOW + 1) + 1
        wa = _window(clip, a_first, count, fps, asize)
        wb = _window(clip, b_first, count, fps, asize)
        if len(wa) < 2 * WINDOW + 2 or len(wb) < 2 * WINDOW + 2:
            continue
        steps += [lh._diff(x, y) for w in (wa, wb) for x, y in zip(w, w[1:], strict=False)]
        cache: dict[tuple[int, int], float] = {}

        def d(x: int, y: int, wa=wa, wb=wb, cache=cache) -> float:
            key = (x, y)
            if key not in cache:
                cache[key] = lh._diff(wa[x], wb[y])
            return cache[key]

        best = None
        for x in range(WINDOW + 1, len(wa) - WINDOW):
            a = a_first + x
            if a < edge_frames:
                continue
            for y in range(WINDOW + 1, len(wb) - WINDOW):
                b = b_first + y
                if b - a < gap_frames or b > last_index - edge_frames:
                    continue
                s = sum(d(x + k, y + k) for k in range(-WINDOW, WINDOW + 1)) / (2 * WINDOW + 1)
                if best is None or s < best[0]:
                    best = (s, a, b, x, y)
        if best is None:
            continue
        _s, a, b, x, y = best
        # The wrap: the frame before `out`, then `in`.
        wrap = lh._diff(wb[y - 1], wa[x])
        refined.append({"a": a, "b": b, "wrap": wrap, "score": _s})
    if not refined:
        raise LoopPointError("no loop point could be measured")

    typical = max(lh._median([s * scale for s in steps]), lh.TYPICAL_FLOOR)
    raw_ratio = lh._diff(tail[-1], head[0]) / typical if head and tail else float("inf")

    # 3. Calm joins and the source mismatch across the dissolve.
    def energy(t: float) -> float:
        k = int(round(t * cfps))
        near = coarse_steps[max(0, k - WINDOW):max(0, min(len(coarse_steps), k + WINDOW))]
        return statistics.fmean(near) if near else coarse_typical

    msize = (MISMATCH_WIDTH, max(2, int(round(MISMATCH_WIDTH * size[1] / max(size[0], 1) / 2.0)) * 2))
    for r in refined:
        r["ratio"] = r["wrap"] / typical
        t_in, t_out = r["a"] / fps, r["b"] / fps
        r["calm"] = (energy(t_in) + energy(t_out)) / 2.0 < CALM_SHARE * coarse_typical
        cf = crossfade_for(r["ratio"], r["calm"]) or NOMINAL_DISSOLVE
        half = min(int(round(cf * fps / 2.0)), r["a"], total_frames - guard - r["b"], (r["b"] - r["a"]) // 4)
        r["half"] = max(1, half)
        r["mismatch"] = blend_mismatch(clip, r["a"], r["b"], r["half"], fps, msize)
        r["mismatch_ok"] = r["mismatch"] is None or r["mismatch"] <= MISMATCH_MAX

    pick = min(refined, key=_rank)
    t_in, t_out = pick["a"] / fps, pick["b"] / fps
    ratio = pick["ratio"]
    cf = crossfade_for(ratio, pick["calm"])
    found = cf is not None and pick["mismatch_ok"]
    # Room for the dissolve: it is centred on the join, half before `in`
    # and half after `out` (never into a snapping tail).
    room = 2.0 * min(t_in, usable_end - t_out)
    cf_used = min(cf if cf is not None else crossfade_for(lh.SEAM_SLIGHT_RATIO, pick["calm"]), room)
    cf_used = math.floor(cf_used * fps) / fps if cf_used > 0 else 0.0
    result = {
        "in": round(t_in, 3), "out": round(t_out, 3),
        "ratio": round(ratio, 2), "raw_ratio": round(raw_ratio, 2) if math.isfinite(raw_ratio) else None,
        "crossfade": round(cf_used, 3), "calm": bool(pick["calm"]),
        "mismatch": pick["mismatch"], "mismatch_ok": bool(pick["mismatch_ok"]),
        # The dissolve width the mismatch check looked at: a wider blend would
        # mix frames it never compared (seam_treatment keeps within it).
        "checked_blend": round(2 * pick["half"] / fps, 3),
        "verdict": verdict_for(ratio), "found": bool(found),
        "snap": bool(snap), "usable_end": round(usable_end, 3) if snap else None,
        "clip_seconds": round(duration, 3), "fps": round(fps, 3),
        "coarse_seconds": round(coarse_seconds, 2), "seconds": round(time.monotonic() - t0, 2),
    }
    log.info("loop point of %s: %s", clip.name, result)
    return result

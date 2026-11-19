"""Grain pulse: a unit whose wrap (last frame -> first) steps far more than
its median frame step is rebuilt from the source with grain kept; others
keep the standard (smaller) encode."""
from __future__ import annotations

import subprocess
from pathlib import Path

from app.services import renderer as r


def _clip(path: Path, src: str) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src, "-c:v", "libx264", "-crf", "12",
                    "-preset", "veryfast", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def test_wrap_ratio_sees_a_jump_at_the_wrap(tmp_path):
    # Still picture whose last 5 frames are brighter: every repeat jumps.
    jump = _clip(tmp_path / "jump.mp4",
                 "color=c=gray:s=320x180:r=30:d=2,geq=lum='if(gte(N,55),140,100)':cb=128:cr=128")
    assert r.unit_wrap_ratio(jump) > 50
    # Steady motion that wraps onto itself (a 2 s period): no jump.
    smooth = _clip(tmp_path / "smooth.mp4",
                   "nullsrc=s=320x180:r=30:d=2,geq=lum='128+60*sin(2*PI*(X/W+T/2))':cb=128:cr=128")
    assert r.unit_wrap_ratio(smooth) < 3


def _run_build(tmp_path, monkeypatch, ratios: list[float]):
    clip = _clip(tmp_path / "c.mp4", "testsrc2=s=320x180:r=30:d=2")
    seen: list[list[str]] = []
    real = r._encode_normalized

    def spy(*a, x264=None, **k):
        seen.append(list(x264 or r._X264))
        return real(*a, x264=x264, **k)

    monkeypatch.setattr(r, "_encode_normalized", spy)
    it = iter(ratios)
    monkeypatch.setattr(r, "unit_wrap_ratio", lambda unit: next(it))
    logs: list[str] = []
    norm, unit = tmp_path / "n.mp4", tmp_path / "u.mp4"
    r._build_unit(clip, norm, unit, width=320, height=180, slow_factor=1.0, extra_vf="", crossfade=0.3,
                  logs=logs)
    return seen, logs, unit


def test_no_pulse_keeps_standard_encode(tmp_path, monkeypatch):
    seen, logs, _ = _run_build(tmp_path, monkeypatch, [1.2])
    assert seen == [r._X264]
    assert any("1.20x" in x and "kept standard" in x for x in logs)


def test_pulse_rebuilds_with_grain_kept(tmp_path, monkeypatch):
    seen, logs, unit = _run_build(tmp_path, monkeypatch, [16.0, 4.4])
    assert seen == [r._X264, r._X264_GRAIN]
    assert "-tune" in r._X264_GRAIN and "grain" in r._X264_GRAIN
    assert any("16.00x" in x and "4.40x" in x and "grain kept" in x for x in logs)
    assert unit.exists() and not list(tmp_path.glob("*_std*"))


def test_grain_encode_that_does_not_help_is_dropped(tmp_path, monkeypatch):
    """A jump that is the clip's own seam, not the encoder: the grain encode
    doesn't lower it, so the smaller standard unit is put back."""
    clip_seen: dict[str, bytes] = {}
    real_ratio = iter([7.0, 6.8])

    def ratio(unit):
        clip_seen.setdefault("first", unit.read_bytes())
        return next(real_ratio)

    clip = _clip(tmp_path / "c.mp4", "testsrc2=s=320x180:r=30:d=2")
    monkeypatch.setattr(r, "unit_wrap_ratio", ratio)
    logs: list[str] = []
    unit = tmp_path / "u.mp4"
    r._build_unit(clip, tmp_path / "n.mp4", unit, width=320, height=180, slow_factor=1.0, extra_vf="",
                  crossfade=0.3, logs=logs)
    assert unit.read_bytes() == clip_seen["first"]
    assert any("did not help" in x for x in logs)
    assert not list(tmp_path.glob("*_std*"))

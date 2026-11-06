"""Audio seams must not dip or drop out.

The nature-sound / music bed is made seamless by crossfading its tail into
its head, then looped (`-stream_loop -1`) for the whole video; long music
beds are built by crossfading separately generated parts. Two things went
wrong on the first live 45-minute render, once per loop (every ~28.5 s):

* a ~3 dB dip: both sides of a seam are uncorrelated sound, and a linear
  crossfade sums to half power mid-overlap;
* a ~20 ms hard dropout: the unit was AAC, whose encoder padding is replayed
  at every loop point;
* (found in review) the unit was cut at the container's reported duration,
  which for MP3/AAC includes encoder delay/padding, so 10–50 ms was replayed
  or skipped at every loop point — a click on tonal music.

These tests render real audio with ffmpeg and measure loudness across seams.
White noise is used because its short-term loudness is very steady (pink or
real nature sound swings by a dB or two per 0.1 s on its own).
"""
from __future__ import annotations

import array
import fnmatch
import math
import struct
import subprocess
import wave
from pathlib import Path

import pytest

from app.services import render_files, renderer

SR = 16000
MAX_DIP_DB = 1.0  # white noise: ~0.5 dB fixed, ~3 dB linear crossfade
DROPOUT_DB = 20.0  # a 10 ms window this far under the median is a gap


def _noise(path: Path, *, seconds: float, seed: int) -> Path:
    """Steady white noise: a stand-in for rain / a waterfall."""
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"anoisesrc=color=white:amplitude=0.3:seed={seed}:duration={seconds}:sample_rate=44100",
         "-c:a", "libmp3lame", "-b:a", "192k", str(path)],
        check=True,
    )
    return path


def _levels(path: Path, *, window: float, loops: int = 0) -> list[float]:
    """dBFS per `window` seconds of `path`, played `loops` extra times back to
    back the way the renderer loops a bed (`-stream_loop`). The first and last
    windows are dropped (encoder edges, not seams)."""
    wav = path.with_name(f"{path.stem}.levels{loops}.wav")
    cmd = ["ffmpeg", "-y", "-v", "error"]
    if loops:
        cmd += ["-stream_loop", str(loops)]
    cmd += ["-i", str(path), "-ac", "1", "-ar", str(SR), str(wav)]
    subprocess.run(cmd, check=True)
    with wave.open(str(wav)) as w:
        n = w.getnframes()
        samples = struct.unpack(f"<{n}h", w.readframes(n))
    win = int(SR * window)
    out = []
    for i in range(0, n - win, win):
        seg = samples[i:i + win]
        rms = math.sqrt(sum(x * x for x in seg) / win)
        out.append(20 * math.log10(rms / 32768 + 1e-12))
    return out[1:-1]


def _worst_dip(levels: list[float]) -> float:
    """dB by which the quietest window falls below the median (positive = dip)."""
    median = sorted(levels)[len(levels) // 2]
    return median - min(levels)


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_looped_unit_has_no_dip_or_dropout_at_the_seams(tmp_path, seed):
    src = _noise(tmp_path / "bed.mp3", seconds=12, seed=seed)
    unit = tmp_path / "unit.wav"
    _, unit_seconds = renderer._make_seamless_audio_unit(src, unit, crossfade=1.5)
    assert 10.0 < unit_seconds < 11.0  # 12 s minus the 1.5 s overlap
    # Three plays back to back: two loop points, each right after the
    # crossfade baked into the unit's tail.
    dip = _worst_dip(_levels(unit, window=0.1, loops=2))
    assert dip < MAX_DIP_DB, f"loudness dips {dip:.1f} dB at a seam"
    gap = _worst_dip(_levels(unit, window=0.01, loops=2))
    assert gap < DROPOUT_DB, f"audio drops out ({gap:.0f} dB) at a loop point"


def test_joined_music_parts_have_no_dip_at_the_joins(tmp_path):
    parts = [_noise(tmp_path / f"part{i}.mp3", seconds=10, seed=11 + i) for i in range(3)]
    dst = tmp_path / "bed.m4a"
    _, seconds = renderer.join_audio(parts, dst, crossfade=3.0)
    assert 23.0 < seconds < 25.0  # 3 × 10 s minus two 3 s overlaps
    dip = _worst_dip(_levels(dst, window=0.1))
    assert dip < MAX_DIP_DB, f"loudness dips {dip:.1f} dB at a join"


def test_every_audio_crossfade_uses_equal_power_curves():
    """Guard against a new acrossfade sneaking back in with linear curves."""
    src = Path(renderer.__file__).read_text(encoding="utf-8")
    assert "c1=tri" not in src and "c2=tri" not in src
    assert src.count("acrossfade=") == src.count("{AUDIO_XFADE_CURVES}") >= 2


def _tone(path: Path, *, seconds: float, codec: str) -> Path:
    """A steady tone (a stand-in for music): any jump at a loop point shows."""
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"sine=frequency=331:sample_rate=44100:duration={seconds}",
         "-c:a", codec, "-b:a", "192k", str(path)],
        check=True,
    )
    return path


def _samples(path: Path) -> array.array:
    """Decoded mono float samples at the file's own rate."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-f", "f32le", "-"],
                         check=True, capture_output=True).stdout
    return array.array("f", raw)


@pytest.mark.parametrize("codec,ext", [("libmp3lame", "mp3"), ("aac", "m4a")])
def test_unit_wraps_onto_its_own_first_sample(tmp_path, codec, ext):
    """MP3/AAC report a longer duration than they decode to (encoder delay
    and padding). The unit must follow the decoded audio: it starts at
    source[cf] and its last sample is source[cf - 1], so looping it plays
    the source continuously — nothing replayed, nothing skipped."""
    src = _tone(tmp_path / f"bed.{ext}", seconds=12, codec=codec)
    unit = tmp_path / "unit.wav"
    renderer._make_seamless_audio_unit(src, unit, crossfade=1.5)
    s, u = _samples(src), _samples(unit)
    n_cf = round(1.5 * 44100)
    assert len(u) == len(s) - n_cf, f"unit is {len(u) - (len(s) - n_cf):+d} samples off"
    assert max(abs(a - b) for a, b in zip(u[:512], s[n_cf:n_cf + 512], strict=True)) < 1e-6
    assert max(abs(a - b) for a, b in zip(u[-8:], s[n_cf - 8:n_cf], strict=True)) < 1e-3


@pytest.mark.parametrize("name", [
    "ambient_music_unit.wav", "ambient_sound_unit.wav", "ambient_music_unit.m4a",
    "ambient_sound_unit.m4a", "ambient_norm_1.mp4", "ambient_unit_2.mp4",
    "ambient_part_0_1.5.mp4", "ambient_trans_0_1.5_1_4.5.mp4",
])
def test_render_scratch_is_cleaned_up(name):
    assert any(fnmatch.fnmatch(name, pat) for pat in render_files._SCRATCH)


@pytest.mark.parametrize("name", ["shot_00_9.mp4", "music.m4a", "sound.mp3", "voice.m4a", "render_9.mp4"])
def test_render_inputs_and_outputs_are_kept(name):
    assert not any(fnmatch.fnmatch(name, pat) for pat in render_files._SCRATCH)

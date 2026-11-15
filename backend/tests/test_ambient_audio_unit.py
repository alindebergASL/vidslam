"""An empty audio unit must fail the render, never hang it.

Some ffmpeg builds make a WAV header with no samples from the crossfade
graph on certain PCM/FLAC beds while exiting 0. Looping that with
-stream_loop -1 never reaches -t, so the mux spun forever."""
from __future__ import annotations

import subprocess
import wave
from pathlib import Path

import pytest

from app.services import renderer


def _bed(path: Path, seconds: float = 12.0) -> Path:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                    "-i", f"sine=frequency=331:sample_rate=44100:duration={seconds}",
                    "-ac", "2", str(path)], check=True)
    return path


def _header_only(path: Path) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"")


def _empty_crossfade(monkeypatch, *, times: int = 10**6) -> list[Path]:
    """Make the crossfade graph exit 0 with an empty file, `times` times."""
    calls: list[Path] = []
    real = renderer._crossfade_unit

    def fake(src, dst, cf):
        calls.append(src)
        if len(calls) <= times:
            _header_only(dst)
            return "No filtered frames for output stream"
        return real(src, dst, cf)

    monkeypatch.setattr(renderer, "_crossfade_unit", fake)
    return calls


def test_a_header_only_wav_probes_as_empty(tmp_path):
    p = tmp_path / "empty.wav"
    _header_only(p)
    assert renderer._audio_unit_ok(p, 10.0) == 0.0


def test_an_empty_crossfade_is_retried_from_an_mp3_copy(tmp_path, monkeypatch):
    src = _bed(tmp_path / "bed.flac")
    calls = _empty_crossfade(monkeypatch, times=1)
    _, seconds = renderer._make_seamless_audio_unit(src, tmp_path / "unit.wav", crossfade=1.5)
    assert len(calls) == 2 and calls[1].suffix == ".mp3"
    assert 10.0 < seconds < 11.0
    assert not list(tmp_path.glob("*_src.mp3"))  # the copy is cleaned up


def test_when_every_crossfade_is_empty_the_bed_is_used_with_fades(tmp_path, monkeypatch):
    src = _bed(tmp_path / "bed.wav")
    _empty_crossfade(monkeypatch)
    unit = tmp_path / "unit.wav"
    _, seconds = renderer._make_seamless_audio_unit(src, unit, crossfade=1.5)
    assert 11.7 < seconds < 12.3
    assert renderer._probe_duration(unit) == pytest.approx(seconds)


def test_an_unrecoverable_empty_unit_raises(tmp_path, monkeypatch):
    src = _bed(tmp_path / "bed.flac")
    _empty_crossfade(monkeypatch)

    def empty_transcode(s, dst, **_):
        _header_only(dst)
        return ""

    monkeypatch.setattr(renderer, "_audio_transcode", empty_transcode)
    with pytest.raises(renderer.EmptyAudioUnit):
        renderer._make_seamless_audio_unit(src, tmp_path / "unit.wav", crossfade=1.5)


def test_a_bed_that_never_loops_is_only_transcoded(tmp_path, monkeypatch):
    src = _bed(tmp_path / "bed.flac", seconds=8)
    calls = _empty_crossfade(monkeypatch)
    _, seconds = renderer._make_seamless_audio_unit(src, tmp_path / "unit.wav", crossfade=1.5,
                                                    loops=False)
    assert calls == []  # no crossfade graph at all
    assert 7.8 < seconds < 8.2


def _tiny_video(path: Path) -> Path:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "color=c=black:s=64x64:d=1:r=24", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    str(path)], check=True)
    return path


def test_the_mux_refuses_an_empty_unit_instead_of_hanging(tmp_path):
    piece = _tiny_video(tmp_path / "p.mp4")
    unit = tmp_path / "unit.wav"
    _header_only(unit)
    with pytest.raises(renderer.EmptyAudioUnit):
        renderer._tile_and_mux([piece], tmp_path / "final.mp4", total=1.0, music_unit=unit,
                               music_volume=0.5, fade=0.0)


def test_the_mux_has_a_timeout_scaled_to_the_video(tmp_path, monkeypatch):
    piece = _tiny_video(tmp_path / "p.mp4")
    unit = _bed(tmp_path / "unit.wav", seconds=2)
    seen: list = []
    real = renderer._run

    def spy(cmd, timeout=None, should_stop=None):
        if "-stream_loop" in cmd:
            seen.append(timeout)
        return real(cmd, timeout=timeout, should_stop=should_stop)

    monkeypatch.setattr(renderer, "_run", spy)
    renderer._tile_and_mux([piece], tmp_path / "final.mp4", total=1.0, music_unit=unit,
                           music_volume=0.5, fade=0.0)
    assert seen == [renderer._mux_timeout(1.0)]
    assert renderer._mux_timeout(3600) == 3 * 3600 + 600

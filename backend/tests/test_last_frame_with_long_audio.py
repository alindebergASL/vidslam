"""The last frame of a clip whose sound track outlasts its picture.

Wan 3.0 returns an AAC track 31-80 ms longer than its video (10 s clips:
video 10.000 s, audio 10.077 s), even when the app asks for no sound.
renderer.extract_frame(last=True) seeked to the container's length minus
0.08 s, i.e. past the last video frame whenever the sound runs more than
0.08 - 1/fps past it (47 ms at 30 fps), and raised "ffmpeg frame extract
failed": an ambient chain stopped at its first join. When an earlier
chain had left the same frame file behind, ffmpeg wrote nothing and the
chain went on silently from that old picture."""
from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest
from PIL import Image


def _coded_clip(tmp_path: Path, *, fps: int, seconds: float, audio_extra: float | None) -> Path:
    """Frame n is a flat colour coding n (red 40*(n mod 6), blue 40*(n div 6
    mod 6)); with `audio_extra`, an AAC track that long past the picture."""
    path = tmp_path / f"coded_{fps}_{audio_extra}.mp4"
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
           f"color=black:s=64x48:r={fps}:d={seconds},"
           "geq=r='40*mod(N\\,6)':g='0':b='40*mod(floor(N/6)\\,6)'"]
    if audio_extra is not None:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={seconds + audio_extra}",
                "-map", "0:v", "-map", "1:a", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-crf", "1", "-pix_fmt", "yuv444p", str(path)]
    subprocess.run(cmd, check=True)
    return path


def _lengths(path: Path) -> tuple[float, float]:
    """(container length, video stream length)."""
    d = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration:"
                                   "format=duration", "-of", "json", str(path)],
                                  capture_output=True, text=True, check=True).stdout)
    video = next(float(s["duration"]) for s in d["streams"] if s["codec_type"] == "video")
    return float(d["format"]["duration"]), video


def _frame_number(png: Path) -> tuple[int, int]:
    r, _g, b = Image.open(png).convert("RGB").getpixel((32, 24))
    return round(r / 40), round(b / 40)


def _code(n: int) -> tuple[int, int]:
    return n % 6, (n // 6) % 6


@pytest.mark.parametrize("fps", [24, 30])
@pytest.mark.parametrize("audio_extra", [None, 0.03, 0.08, 0.5])
def test_last_frame_is_the_last_video_frame_whatever_the_sound(tmp_path, fps, audio_extra):
    from app.services.renderer import extract_frame

    seconds = 2.0
    video = _coded_clip(tmp_path, fps=fps, seconds=seconds, audio_extra=audio_extra)
    container, picture = _lengths(video)
    if audio_extra and audio_extra >= 0.08:
        # The case that broke: the container is longer than the picture by
        # more than the old 0.08 s back-off minus one frame.
        assert container - picture > 0.08 - 1 / fps
    out = tmp_path / "last.png"
    extract_frame(video, out, last=True)
    assert _frame_number(out) == _code(round(seconds * fps) - 1)
    extract_frame(video, out, last=False)
    assert _frame_number(out) == _code(0)


def _clear_leftover_frames() -> None:
    """renders/ is shared by every test in the run and project ids restart:
    a chain test after another would find that one's seg1_last.png."""
    from app.services import storage

    for p in (storage.settings.data_path / "renders").glob("*/frames/seg*_last.png"):
        p.unlink(missing_ok=True)


def _sound_past_picture(monkeypatch, tmp_path: Path, extra: float = 0.1) -> tuple[list[Path], list[dict]]:
    """The mock's clips carry a sound track `extra` seconds past the picture,
    as Wan's downloads do; the provider's submits are recorded."""
    from app.providers import mock_video

    real = mock_video._generate_clip
    made: list[Path] = []
    calls: list[dict] = []
    real_submit = mock_video.MockVideoProvider.submit

    def spy(self, **kw):
        calls.append(kw)
        return real_submit(self, **kw)

    def with_sound(prompt: str, duration: float, refs_count: int) -> bytes:
        silent = tmp_path / f"{uuid.uuid4().hex}.mp4"
        silent.write_bytes(real(prompt, duration, refs_count))
        out = tmp_path / f"{uuid.uuid4().hex}_a.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(silent), "-f", "lavfi", "-i",
                        f"sine=frequency=220:duration={duration + extra}", "-map", "0:v", "-map", "1:a",
                        "-c:v", "copy", "-c:a", "aac", str(out)], check=True)
        made.append(out)
        return out.read_bytes()

    monkeypatch.setattr(mock_video, "_generate_clip", with_sound)
    monkeypatch.setattr(mock_video.MockVideoProvider, "submit", spy)
    return made, calls


CHAIN = {"scene_preset": "waterfall", "loop_seconds": 20, "total_seconds": 60,
         "crossfade_seconds": 1.0, "fade_seconds": 1.0, "music_mood": "none"}


def test_chain_continues_when_a_clip_has_sound_past_its_picture(auth_client, monkeypatch, tmp_path):
    """End to end on the mock (10 s cap): a 20 s loop is two chained clips;
    each "download" carries a sound track 0.1 s longer than its picture, as
    Wan's do. The second clip starts on the first one's last frame."""
    _clear_leftover_frames()
    made, calls = _sound_past_picture(monkeypatch, tmp_path)
    scene = auth_client.post("/api/ambient", json={"config": CHAIN, "generate": True}).json()
    container, picture = _lengths(made[0])
    assert container - picture > 0.08 - 1 / 30  # the precondition: sound well past the picture
    assert scene["plan"]["segments"] == [10, 10]
    assert scene["loop_status"] == "completed", scene
    assert len(calls) == 2
    # The second clip starts on a frame of the first (not the anchor still).
    assert calls[1]["references"][0].url != calls[0]["references"][0].url


def test_a_leftover_frame_file_is_not_taken_for_the_new_chain(auth_client, monkeypatch, tmp_path):
    """An earlier chain's seg1_last.png in the scene's frames folder: the new
    chain writes its own last frame, never continues from the old picture
    (ffmpeg wrote nothing past the end and exited 0, so the old file passed)."""
    from app.services import storage

    _clear_leftover_frames()
    made, calls = _sound_past_picture(monkeypatch, tmp_path)
    scene = auth_client.post("/api/ambient", json={"config": CHAIN, "generate": False}).json()
    frames = storage.render_subdir(scene["id"]) / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    leftover = frames / "seg1_last.png"
    Image.new("RGB", (64, 36), (255, 0, 255)).save(leftover)
    r = auth_client.post(f"/api/ambient/{scene['id']}/generate-loop")
    assert r.status_code == 202, r.text
    out = auth_client.get(f"/api/ambient/{scene['id']}").json()
    assert out["loop_status"] == "completed", out
    assert len(calls) == 2
    assert Image.open(leftover).convert("RGB").getpixel((5, 5)) != (255, 0, 255)

"""Ambient overlays: a title over the opening, a corner watermark, and a
countdown / elapsed timer. Title and watermark ride on pieces that are
encoded anyway; the timer re-encodes the finished video once."""
from __future__ import annotations

import subprocess
from pathlib import Path

from PIL import Image, ImageChops

from .test_ambient import SHORT, _duration, _has_audio


def _black_clip(path: Path, seconds: float = 4.0) -> Path:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"color=c=black:s=640x360:r=30:d={seconds}", "-pix_fmt", "yuv420p", str(path)],
                   check=True)
    return path


def _frame(video: Path, t: float, tmp: Path) -> Image.Image:
    png = tmp / f"f_{t:.2f}.png"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(video),
                    "-frames:v", "1", str(png)], check=True)
    return Image.open(png).convert("L")


def _bright(img: Image.Image, box: tuple[float, float, float, float]) -> int:
    """Brightest pixel in a region given as fractions of the frame."""
    w, h = img.size
    return img.crop((int(box[0] * w), int(box[1] * h), int(box[2] * w), int(box[3] * h))).getextrema()[1]


BOTTOM_RIGHT = (0.55, 0.8, 1.0, 1.0)
TOP_LEFT = (0.0, 0.0, 0.45, 0.2)
TOP_RIGHT = (0.7, 0.0, 1.0, 0.2)
CENTER = (0.15, 0.35, 0.85, 0.65)


def _compose(tmp: Path, total: float = 20, **kw):
    from app.services.renderer import AmbientRenderInputs, compose_ambient

    return compose_ambient(AmbientRenderInputs(
        project_id=1, out_dir=tmp / "out", clip_path=_black_clip(tmp / "black.mp4"),
        total_seconds=total, crossfade_seconds=1.0, fade_seconds=0.0, **kw))


def test_watermark_is_on_every_tiled_copy(tmp_path):
    out = _compose(tmp_path, watermark="calm.example")
    final = out.final_video_path
    assert abs(_duration(final) - 20) < 0.2
    for t in (1.0, 9.0, 18.5):  # first unit, a stream-copied repeat, the end piece
        img = _frame(final, t, tmp_path)
        assert _bright(img, BOTTOM_RIGHT) > 100, t
        assert _bright(img, TOP_LEFT) < 30, t  # only where it was asked for


def test_title_shows_over_the_opening_only(tmp_path):
    out = _compose(tmp_path, overlay_title="Rain: 100% 'calm'", title_seconds=2.0)  # odd characters on purpose
    final = out.final_video_path
    assert _bright(_frame(final, 1.0, tmp_path), CENTER) > 150
    assert _bright(_frame(final, 6.0, tmp_path), CENTER) < 30


def test_countdown_timer_ticks(tmp_path):
    out = _compose(tmp_path, timer="countdown", timer_position="top_right")
    final = out.final_video_path
    assert abs(_duration(final) - 20) < 0.2
    a, b = _frame(final, 1.5, tmp_path), _frame(final, 7.5, tmp_path)
    assert _bright(a, TOP_RIGHT) > 150 and _bright(a, BOTTOM_RIGHT) < 30
    crop = (int(0.7 * a.width), 0, a.width, int(0.2 * a.height))
    assert ImageChops.difference(a.crop(crop), b.crop(crop)).getbbox() is not None  # the digits changed


def test_hour_long_timer_uses_hours():
    import tempfile

    from app.services.renderer import timer_filter

    with tempfile.TemporaryDirectory() as d:
        timer_filter(Path(d), "elapsed", total=3600, height=1080, position="top_left")
        assert (Path(d) / "overlay_timer.txt").read_text().count("%{eif") == 3  # h:mm:ss
        timer_filter(Path(d), "countdown", total=1800, height=1080, position="top_left")
        assert (Path(d) / "overlay_timer.txt").read_text().count("%{eif") == 2  # mm:ss
        assert timer_filter(Path(d), "none", total=60, height=1080, position="top_left") == ""


def test_overlays_render_end_to_end_and_keep_the_loop(auth_client):
    scene = auth_client.post("/api/ambient", json={"config": SHORT, "generate": True}).json()
    sid = scene["id"]
    assert scene["loop_status"] == "completed"
    cfg = {**SHORT, "overlay_title": "Forest rain", "watermark": "@calmchannel", "timer": "countdown"}
    r = auth_client.patch(f"/api/ambient/{sid}", json={"config": cfg})
    assert r.status_code == 200 and r.json()["loop_status"] == "completed"  # render-time only
    assert any("re-encoded once" in n for n in r.json()["plan"]["notes"])
    auth_client.post(f"/api/ambient/{sid}/render")
    rend = auth_client.get(f"/api/ambient/{sid}").json()["latest_render"]
    assert rend["status"] == "completed", rend
    assert "overlays=title+watermark+countdown" in rend["render_log"]
    final = Path(rend["final_video_path"])
    assert _has_audio(final) and 19.0 <= _duration(final) <= 21.0


def test_overlay_settings_validate(auth_client):
    bad = auth_client.post("/api/ambient/estimate", json={"config": {**SHORT, "timer": "stopwatch"}})
    assert bad.status_code == 422
    long = auth_client.post("/api/ambient/estimate", json={"config": {**SHORT, "watermark": "x" * 61}})
    assert long.status_code == 422
    ok = auth_client.post("/api/ambient/estimate", json={"config": {**SHORT, "watermark": "  hi  "}})
    assert ok.status_code == 200


def test_overlays_on_a_multi_variation_scene(tmp_path):
    """Title and watermark also ride on the variation path (units + dissolves)."""
    from app.services.renderer import AmbientRenderInputs, compose_ambient

    clips = [_black_clip(tmp_path / f"c{i}.mp4") for i in range(3)]
    out = compose_ambient(AmbientRenderInputs(
        project_id=1, out_dir=tmp_path / "out", clip_path=clips[0], extra_clips=clips[1:],
        total_seconds=18, crossfade_seconds=1.0, fade_seconds=0.5, variety="takes",
        transition_seconds=1.0, overlay_title="Night", title_seconds=2.0, watermark="wm"))
    final = out.final_video_path
    assert abs(_duration(final) - 18) < 0.2
    assert _bright(_frame(final, 1.2, tmp_path), CENTER) > 150
    for t in (1.2, 4.5, 10.0):  # inside a unit and inside dissolves
        assert _bright(_frame(final, t, tmp_path), BOTTOM_RIGHT) > 60, t

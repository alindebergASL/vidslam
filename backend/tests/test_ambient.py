"""Ambient mode: seamless-loop long-form scenes against mock providers.

Durations are kept tiny (5s loop → 20s output) so the suite stays fast; the
compositor's math is identical at 30s → 1h.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageStat


def _probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)


def _duration(path: Path) -> float:
    return float(_probe(path)["format"]["duration"])


def _has_audio(path: Path) -> bool:
    return any(s["codec_type"] == "audio" for s in _probe(path)["streams"])


def _frame_mean_red(video: Path, *, first: bool, tmp: Path) -> float:
    """Decode the first or last frame and return its mean red channel."""
    nb = None
    for s in _probe(video)["streams"]:
        if s["codec_type"] == "video":
            nb = int(s.get("nb_frames") or 0)
    assert nb and nb > 1, "need frame count"
    idx = 0 if first else nb - 1
    png = tmp / f"frame_{idx}.png"
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(video), "-vf", f"select='eq(n\\,{idx})'", "-vsync", "0",
         "-frames:v", "1", str(png)],
        capture_output=True, text=True, check=True,
    )
    with Image.open(png) as im:
        return ImageStat.Stat(im.convert("RGB")).mean[0]


SHORT = {
    "scene_preset": "waterfall",
    "loop_seconds": 5,
    "total_seconds": 20,
    "crossfade_seconds": 1.0,
    "fade_seconds": 1.0,
    "music_mood": "spa",
}


# ---------- presets + cost ----------


def test_presets_lists_core_scenes_and_moods(auth_client):
    r = auth_client.get("/api/ambient/presets")
    assert r.status_code == 200, r.text
    body = r.json()
    ids = {s["id"] for s in body["scenes"]}
    assert {"waterfall", "fireplace", "ocean", "custom"} <= ids
    assert {"spa", "zen", "none"} <= {m["id"] for m in body["music_moods"]}
    assert body["loop_options"] == [25, 30, 60]
    assert 3600 in body["total_options"]
    for s in body["scenes"]:
        assert len(s["palette"]) == 2


def test_estimate_is_independent_of_total_duration(auth_client):
    one_hour = auth_client.post(
        "/api/ambient/estimate",
        json={"config": {"scene_preset": "ocean", "loop_seconds": 30, "total_seconds": 3600}},
    ).json()
    three_hours = auth_client.post(
        "/api/ambient/estimate",
        json={"config": {"scene_preset": "ocean", "loop_seconds": 30, "total_seconds": 10800}},
    ).json()
    assert one_hour["total"] == three_hours["total"] > 0
    # The tiling line is explicit and free.
    tiling = [l for l in one_hour["lines"] if l["unit"] == "repeat"]
    assert tiling and tiling[0]["cost"] == 0.0
    # 30s loop − 2s crossfade = 28s unit → ceil(3600/28) = 129 repeats.
    assert tiling[0]["qty"] == 129


def test_estimate_image_drift_cheaper_and_no_music_drops_line(auth_client):
    video = auth_client.post(
        "/api/ambient/estimate", json={"config": {"loop_seconds": 30, "source": "video"}}
    ).json()
    still = auth_client.post(
        "/api/ambient/estimate",
        json={"config": {"loop_seconds": 30, "source": "image_drift", "music_mood": "none"}},
    ).json()
    assert still["total"] < video["total"]
    assert not any("Music" in l["item"] for l in still["lines"])
    assert any("Music" in l["item"] for l in video["lines"])


# ---------- lifecycle ----------


def test_create_draft_without_generating(auth_client):
    r = auth_client.post(
        "/api/ambient",
        json={"title": "", "aspect_ratio": "16:9", "config": SHORT, "generate": False},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["title"].startswith("Waterfall")
    assert body["status"] == "draft"
    assert body["loop_status"] == "pending"
    assert body["music_ready"] is False
    assert body["total_seconds"] == 20
    # 5s loop − 1s crossfade = 4s unit → 5 repeats.
    assert body["repeats"] == 5
    assert body["loop_shot"]["duration_seconds"] == 5.0
    assert "seamlessly" in body["loop_shot"]["prompt"]

    # Listed in the ambient index, and visible as a project with the right mode.
    ids = [s["id"] for s in auth_client.get("/api/ambient").json()]
    assert body["id"] in ids
    p = auth_client.get(f"/api/projects/{body['id']}").json()
    assert p["mode"] == "ambient_loop"
    assert p["ambient_config_json"]["scene_preset"] == "waterfall"
    assert p["voiceover_source"] == "silent"
    assert p["include_disclosure"] is False


def test_render_before_loop_is_rejected(auth_client):
    sid = auth_client.post("/api/ambient", json={"config": SHORT, "generate": False}).json()["id"]
    r = auth_client.post(f"/api/ambient/{sid}/render")
    assert r.status_code == 409


def test_generate_then_render_full_length_with_music(auth_client):
    r = auth_client.post("/api/ambient", json={"config": SHORT, "generate": True})
    assert r.status_code == 201, r.text
    scene = r.json()
    sid = scene["id"]
    # Inline queue → generation already finished by the time create returns.
    assert scene["status"] == "planned", scene
    assert scene["loop_status"] == "completed"
    assert scene["music_ready"] is True
    clip = Path(scene["loop_shot"]["clip_path"])
    assert clip.exists()
    assert 4.5 <= _duration(clip) <= 5.6

    # Loop clip and music bed stream through the generic project endpoints.
    assert auth_client.get(f"/api/projects/{sid}/shots/{scene['loop_shot']['id']}/clip").status_code == 200
    assert auth_client.get(f"/api/projects/{sid}/music").status_code == 200

    r = auth_client.post(f"/api/ambient/{sid}/render")
    assert r.status_code == 202, r.text
    scene = auth_client.get(f"/api/ambient/{sid}").json()
    assert scene["status"] == "completed", scene
    rend = scene["latest_render"]
    assert rend["status"] == "completed", rend
    final = Path(rend["final_video_path"])
    assert final.exists()
    # Tiled to the requested total (stream-copy cut lands within a frame).
    assert 19.0 <= _duration(final) <= 21.0
    assert _has_audio(final)
    assert Path(rend["thumbnail_path"]).exists()
    assert "repeats=5" in rend["render_log"]
    assert rend["actual_cost"] == 0.0  # mock mode bills nothing
    # Landscape output by default.
    v = [s for s in _probe(final)["streams"] if s["codec_type"] == "video"][0]
    assert (v["width"], v["height"]) == (1920, 1080)
    assert scene["render_count"] == 1


def test_image_drift_source_without_music(auth_client):
    cfg = {**SHORT, "source": "image_drift", "music_mood": "none", "aspect": None}
    cfg.pop("aspect")
    r = auth_client.post(
        "/api/ambient", json={"aspect_ratio": "9:16", "config": cfg, "generate": True}
    )
    assert r.status_code == 201, r.text
    scene = r.json()
    assert scene["loop_status"] == "completed", scene
    assert scene["music_ready"] is False
    clip = Path(scene["loop_shot"]["clip_path"])
    assert 4.5 <= _duration(clip) <= 5.5
    assert (Path(clip).parent / "ambient_still.png").exists()
    # Portrait dims honoured by the drift renderer.
    v = [s for s in _probe(clip)["streams"] if s["codec_type"] == "video"][0]
    assert (v["width"], v["height"]) == (1080, 1920)

    assert auth_client.post(f"/api/ambient/{scene['id']}/render").status_code == 202
    scene = auth_client.get(f"/api/ambient/{scene['id']}").json()
    final = Path(scene["latest_render"]["final_video_path"])
    assert scene["latest_render"]["status"] == "completed", scene["latest_render"]
    assert 19.0 <= _duration(final) <= 21.0
    assert not _has_audio(final)
    # Periodic clip → no crossfade → 5s unit → 4 repeats.
    assert "repeats=4" in scene["latest_render"]["render_log"]


def test_update_scene_prompt_invalidates_clip_but_fade_does_not(auth_client):
    scene = auth_client.post("/api/ambient", json={"config": SHORT, "generate": True}).json()
    sid = scene["id"]
    assert scene["loop_status"] == "completed"

    # Compositing-only change keeps the clip.
    r = auth_client.patch(
        f"/api/ambient/{sid}", json={"config": {**SHORT, "fade_seconds": 2.0, "total_seconds": 40}}
    )
    assert r.status_code == 200, r.text
    assert r.json()["loop_status"] == "completed"
    assert r.json()["total_seconds"] == 40
    assert r.json()["repeats"] == 10

    # Scene change → clip must be regenerated.
    r = auth_client.patch(
        f"/api/ambient/{sid}", json={"title": "Night", "config": {**SHORT, "scene_preset": "night_sky"}}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["title"] == "Night"
    assert body["loop_status"] == "pending"
    assert body["loop_shot"]["clip_path"] == ""
    assert "starry" in body["loop_shot"]["prompt"]
    assert auth_client.post(f"/api/ambient/{sid}/render").status_code == 409


def test_validation_errors(auth_client):
    bad_loop = auth_client.post("/api/ambient", json={"config": {**SHORT, "loop_seconds": 3}})
    assert bad_loop.status_code == 422
    bad_total = auth_client.post("/api/ambient", json={"config": {**SHORT, "total_seconds": 5}})
    assert bad_total.status_code == 422
    empty_custom = auth_client.post(
        "/api/ambient", json={"config": {**SHORT, "scene_preset": "custom", "scene_prompt": ""}}
    )
    assert empty_custom.status_code == 422
    assert auth_client.get("/api/ambient/999").status_code == 404
    # A storyboard project is not an ambient scene.
    pid = auth_client.post("/api/projects", json={"title": "x", "original_script": "hi"}).json()["id"]
    assert auth_client.get(f"/api/ambient/{pid}").status_code == 404


def test_requires_auth(client):
    assert client.get("/api/ambient").status_code == 401
    assert client.get("/api/ambient/presets").status_code == 401


def test_seed_creates_demo_ambient_scene(auth_client):
    assert auth_client.post("/api/system/seed").status_code == 200
    scenes = auth_client.get("/api/ambient").json()
    assert len(scenes) == 1
    assert scenes[0]["config"]["scene_preset"] == "fireplace"
    assert scenes[0]["status"] == "draft"
    assert scenes[0]["loop_shot"] is not None


def test_delete_scene_via_projects_endpoint(auth_client):
    sid = auth_client.post("/api/ambient", json={"config": SHORT, "generate": False}).json()["id"]
    assert auth_client.delete(f"/api/projects/{sid}").status_code == 204
    assert auth_client.get(f"/api/ambient/{sid}").status_code == 404


# ---------- compositor seam check (renderer, no HTTP) ----------


def test_seamless_unit_first_and_last_frames_match(tmp_path: Path):
    """A clip whose red channel ramps 0→255 over 4s is maximally non-loopable.
    After the tail→head crossfade + trim, the unit's last frame must land on
    (nearly) the same colour as its first — that's the whole trick."""
    from app.services.renderer import AmbientRenderInputs, compose_ambient

    ramp = tmp_path / "ramp.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", "color=c=black:s=320x180:d=4:r=30,format=rgb24,geq=r='255*T/4':g=0:b=0",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", str(ramp)],
        capture_output=True, text=True, check=True,
    )
    raw_first = _frame_mean_red(ramp, first=True, tmp=tmp_path)
    raw_last = _frame_mean_red(ramp, first=False, tmp=tmp_path)
    assert raw_last - raw_first > 150, "control: raw clip must have an obvious seam"

    out = compose_ambient(AmbientRenderInputs(
        project_id=0, out_dir=tmp_path / "out", clip_path=ramp,
        total_seconds=10, aspect_ratio="16:9",
        crossfade_seconds=1.0, fade_seconds=0.0, music_path=None,
    ))
    assert 2.8 <= out.unit_seconds <= 3.2  # 4s − 1s crossfade
    assert out.repeats == 4
    unit_first = _frame_mean_red(out.unit_path, first=True, tmp=tmp_path)
    unit_last = _frame_mean_red(out.unit_path, first=False, tmp=tmp_path)
    assert abs(unit_first - unit_last) < 25, (unit_first, unit_last)
    assert 9.0 <= _duration(out.final_video_path) <= 11.0
    assert not _has_audio(out.final_video_path)


def test_compose_ambient_rejects_bad_total(tmp_path: Path):
    from app.services.renderer import AmbientRenderInputs, compose_ambient

    with pytest.raises(ValueError):
        compose_ambient(AmbientRenderInputs(
            project_id=0, out_dir=tmp_path, clip_path=tmp_path / "x.mp4", total_seconds=0,
        ))

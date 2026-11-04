"""Ambient polish: long music beds, 4K, best-frame thumbnails, preset
previews, batch create, YouTube metadata drafts."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from PIL import Image

BASE = {"scene_preset": "ocean", "loop_seconds": 5, "total_seconds": 20,
        "crossfade_seconds": 1.0, "fade_seconds": 1.0, "music_mood": "spa"}


def _probe(p: Path) -> dict:
    return json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(p)],
        capture_output=True, text=True, check=True).stdout)


def _dur(p: Path) -> float:
    return float(_probe(p)["format"]["duration"])


def test_music_parts_respect_provider_caps():
    from app.schemas.ambient import AmbientConfig
    from app.services.ambient import music_parts

    ten = AmbientConfig(music_bed_seconds=600)
    parts = music_parts(ten, "elevenlabs")
    assert len(parts) == 3  # 5-min cap, 6 s overlaps → 3 calls
    assert all(p <= 300 for p in parts) and sum(parts) - 6 * (len(parts) - 1) >= 599
    # 30 s clips net 24 s each after the 6 s crossfade → 25 calls for 10 min.
    assert len(music_parts(ten, "google/lyria-3-clip-preview")) == 25
    assert music_parts(AmbientConfig(), "elevenlabs") == [60.0]  # auto bed: one call
    assert len(music_parts(AmbientConfig(music_bed_seconds=180), "google/lyria-3-pro-preview")) == 1


def test_long_music_bed_is_chained_and_crossfaded(auth_client, monkeypatch):
    from app.services import ambient

    monkeypatch.setattr(ambient, "music_cap_seconds", lambda model=None: 20.0)
    cfg = {**BASE, "music_bed_seconds": 50}
    scene = auth_client.post("/api/ambient", json={"config": cfg, "generate": True}).json()
    assert scene["music_ready"]
    p = auth_client.get(f"/api/projects/{scene['id']}").json()
    bed = Path(p["music_upload_path"])
    assert bed.name == "music.m4a" and 48 <= _dur(bed) <= 52
    assert not list(bed.parent.glob("music_part_*"))
    est = auth_client.post("/api/ambient/estimate", json={"config": cfg}).json()
    line = next(ln for ln in est["lines"] if ln["item"].startswith("Music bed"))
    assert line["qty"] == 4 and "4 tracks crossfaded" in line["item"]  # 20 s cap, 6 s overlaps
    # Dedicated regenerate endpoint uses the same path.
    r = auth_client.post(f"/api/ambient/{scene['id']}/music")
    assert r.status_code == 200 and r.json()["music_ready"]


def test_4k_render_and_best_frame_thumbnail(auth_client):
    cfg = {**BASE, "output_resolution": "4k", "music_mood": "none"}
    sid = auth_client.post("/api/ambient", json={"config": cfg, "generate": True}).json()["id"]
    assert auth_client.post(f"/api/ambient/{sid}/render").status_code == 202
    rend = auth_client.get(f"/api/ambient/{sid}").json()["latest_render"]
    assert rend["status"] == "completed", rend["error"]
    v = [s for s in _probe(Path(rend["final_video_path"]))["streams"] if s["codec_type"] == "video"][0]
    assert (v["width"], v["height"]) == (3840, 2160)
    assert "res=4k" in rend["render_log"]
    with Image.open(rend["thumbnail_path"]) as im:
        assert im.size == (1280, 720)


def test_best_frame_prefers_sharp_exposed_frame(tmp_path):
    from app.services.renderer import best_frame

    # 4 s: black for 3 s, then a sharp colourful test pattern for 1 s.
    clip = tmp_path / "c.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=320x180:d=3:r=30",
                    "-f", "lavfi", "-i", "testsrc2=s=320x180:d=1:r=30",
                    "-filter_complex", "[0][1]concat=n=2:v=1[v]", "-map", "[v]",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)], capture_output=True, check=True)
    out = best_frame(clip, tmp_path / "t.jpg", width=640, height=360, samples=8)
    with Image.open(out) as im:
        assert im.size == (640, 360)
        assert sum(im.convert("L").resize((1, 1)).getpixel((0, 0)) for _ in [0]) > 40  # not black


def test_preset_previews_generate_and_serve(auth_client):
    r = auth_client.post("/api/ambient/presets/previews").json()
    assert "ocean" in r["presets"] and "custom" not in r["presets"]
    presets = auth_client.get("/api/ambient/presets").json()["scenes"]
    ocean = next(p for p in presets if p["id"] == "ocean")
    assert ocean["preview_url"].startswith("/api/ambient/presets/ocean/preview")
    img = auth_client.get(ocean["preview_url"])
    assert img.status_code == 200 and img.content[:4] == b"\x89PNG"
    assert next(p for p in presets if p["id"] == "custom")["preview_url"] is None
    # Second call: nothing missing.
    assert auth_client.post("/api/ambient/presets/previews").json()["status"] == "nothing_to_do"


def test_batch_create_generates_and_renders(auth_client):
    r = auth_client.post("/api/ambient/batch", json={
        "presets": ["fireplace", "snowfall"], "config": {**BASE, "music_mood": "spa"}, "render": True})
    assert r.status_code == 201, r.text
    body = r.json()
    assert len(body["scene_ids"]) == 2 and body["estimated_total"] > 0
    for sid in body["scene_ids"]:
        s = auth_client.get(f"/api/ambient/{sid}").json()
        assert s["status"] == "completed" and s["latest_render"]["status"] == "completed"
    moods = [auth_client.get(f"/api/ambient/{i}").json()["config"]["music_mood"] for i in body["scene_ids"]]
    assert moods == ["piano", "piano"]  # each preset's own mood
    bad = auth_client.post("/api/ambient/batch", json={"presets": ["custom"], "config": BASE})
    assert bad.status_code == 422


def test_publish_meta_ambient_within_youtube_limits(auth_client):
    sid = auth_client.post("/api/ambient", json={
        "config": {**BASE, "total_seconds": 3600, "output_resolution": "4k"}, "generate": False}).json()["id"]
    m = auth_client.get(f"/api/projects/{sid}/publish-meta").json()
    assert m["title"].startswith("Ocean 4K — 1 Hour") and len(m["title"]) <= 100
    assert "created with AI" in m["description"] and len(m["description"].encode()) <= 5000
    assert "<" not in m["title"] + m["description"]
    assert "4K" in m["tags"] and sum(len(t) + (2 if " " in t else 0) for t in m["tags"]) + len(m["tags"]) - 1 <= 500
    assert m["category_id"] == "10" and m["contains_synthetic_media"] is True and m["made_for_kids"] is False


def test_fit_tags_caps_total_length():
    from app.services.publish_meta import fit_tags

    tags = fit_tags([f"tag number {i}" for i in range(100)] + ["a<b>"])
    assert sum(len(t) + 2 + 1 for t in tags) - 1 <= 500 + 1
    assert all("<" not in t for t in tags)

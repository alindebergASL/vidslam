"""Picture-to-scene: a scene can use its own picture (uploaded, generated as a
preview, or picked from Studio / Cast) as the loop's anchor frame, or as
the still of a Still + drift scene."""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from .conftest import make_png_bytes
from .test_ambient import SHORT


def _scene(client, *, aspect="16:9", generate=False, **cfg) -> int:
    r = client.post("/api/ambient", json={"aspect_ratio": aspect, "config": {**SHORT, **cfg},
                                          "generate": generate})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _upload(client, sid, data: bytes, mime="image/png"):
    return client.post(f"/api/ambient/{sid}/picture", files={"file": ("pic.png", data, mime)})


def _no_image_calls(monkeypatch):
    from app.providers import mock_image

    def boom(self, **kw):
        raise AssertionError("the image model must not be called when the scene has a picture")

    monkeypatch.setattr(mock_image.MockImageProvider, "generate", boom)


def test_upload_is_cropped_to_the_scene_aspect(auth_client):
    sid = _scene(auth_client, aspect="9:16")
    r = _upload(auth_client, sid, make_png_bytes((1600, 900), (20, 120, 200)))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["picture_ready"] is True and out["config"]["picture_origin"] == "upload"
    got = auth_client.get(f"/api/ambient/{sid}/picture")
    assert got.status_code == 200 and got.headers["content-type"] == "image/png"
    w, h = Image.open(io.BytesIO(got.content)).size
    assert abs(w / h - 9 / 16) < 0.01 and h == 900  # center crop, not a stretch


def test_bad_pictures_are_rejected(auth_client):
    sid = _scene(auth_client)
    assert _upload(auth_client, sid, make_png_bytes((300, 200))).status_code == 422  # too small
    assert _upload(auth_client, sid, b"not an image").status_code == 422
    assert _upload(auth_client, sid, b"GIF89a", mime="text/plain").status_code == 415
    assert auth_client.get(f"/api/ambient/{sid}").json()["picture_ready"] is False


def test_drift_scene_uses_the_picture_as_its_still(auth_client, monkeypatch):
    sid = _scene(auth_client, source="image_drift", music_mood="none")
    _upload(auth_client, sid, make_png_bytes((1920, 1080), (200, 40, 40)))
    _no_image_calls(monkeypatch)
    auth_client.post(f"/api/ambient/{sid}/generate-loop")
    scene = auth_client.get(f"/api/ambient/{sid}").json()
    assert scene["loop_status"] == "completed", scene
    still = Path(scene["loop_shot"]["clip_path"]).parent / "ambient_still.png"
    r, g, b = Image.open(still).convert("RGB").resize((1, 1)).getpixel((0, 0))
    assert r > 150 and g < 90 and b < 90  # it's the red picture we uploaded
    assert scene["loop_shot"]["provider_model"] == "your picture"


def test_video_scene_anchors_on_the_picture(auth_client, monkeypatch):
    sid = _scene(auth_client)
    _upload(auth_client, sid, make_png_bytes((1920, 1080), (30, 160, 60)))
    _no_image_calls(monkeypatch)
    auth_client.post(f"/api/ambient/{sid}/generate-loop")
    scene = auth_client.get(f"/api/ambient/{sid}").json()
    assert scene["loop_status"] == "completed", scene
    assert "Bring the given image to life" in scene["loop_shot"]["prompt"]
    frames = Path(scene["loop_shot"]["clip_path"]).parent / "frames"
    from app import models
    from app.db import SessionLocal
    from app.services import ambient

    db = SessionLocal()
    try:
        pic = ambient.picture_path(db.get(models.VideoProject, sid)).read_bytes()
    finally:
        db.close()
    assert (frames / "anchor.png").read_bytes() == pic


def test_generated_preview_studio_and_asset_sources(auth_client):
    sid = _scene(auth_client, look="watercolor")
    r = auth_client.post(f"/api/ambient/{sid}/picture/generate")
    assert r.status_code == 200 and r.json()["config"]["picture_origin"] == "generated"

    # Studio result
    aid = auth_client.post("/api/avatars", json={"name": "A"}).json()["id"]
    job = auth_client.post("/api/studio/generate-image", json={
        "owner_kind": "avatar", "owner_id": aid, "prompt": "misty lake at dawn"}).json()
    r = auth_client.post(f"/api/ambient/{sid}/picture/from", json={"studio_job_id": job["id"]})
    assert r.status_code == 200 and r.json()["config"]["picture_origin"] == "studio"

    # Cast asset
    up = auth_client.post(f"/api/avatars/{aid}/assets",
                          files={"file": ("h.png", make_png_bytes((1024, 1024)), "image/png")},
                          data={"asset_type": "hero", "rights_confirmed": "true"}).json()
    asset_id = up["id"] if "id" in up else up["assets"][0]["id"]
    r = auth_client.post(f"/api/ambient/{sid}/picture/from", json={"asset_id": asset_id})
    assert r.status_code == 200 and r.json()["config"]["picture_origin"] == "asset"

    assert auth_client.post(f"/api/ambient/{sid}/picture/from", json={"asset_id": 99999}).status_code == 404
    assert auth_client.post(f"/api/ambient/{sid}/picture/from", json={}).status_code == 422


def test_picture_changes_invalidate_the_loop_and_settings_keep_it(auth_client):
    sid = _scene(auth_client, generate=True)
    assert auth_client.get(f"/api/ambient/{sid}").json()["loop_status"] == "completed"
    r = _upload(auth_client, sid, make_png_bytes((1920, 1080)))
    assert r.json()["loop_status"] == "pending"  # made from a different image

    # A settings save from a page that never saw the picture keeps it.
    r = auth_client.patch(f"/api/ambient/{sid}", json={"config": {**SHORT, "picture_origin": ""}})
    assert r.json()["config"]["picture_origin"] == "upload" and r.json()["picture_ready"] is True

    r = auth_client.delete(f"/api/ambient/{sid}/picture")
    assert r.json()["picture_ready"] is False and r.json()["config"]["picture_origin"] == ""
    assert auth_client.get(f"/api/ambient/{sid}/picture").status_code == 404


def test_estimate_and_plan_with_a_picture(auth_client, monkeypatch):
    est = auth_client.post("/api/ambient/estimate", json={"config": {**SHORT, "picture_origin": "upload"}}).json()
    mine = [ln for ln in est["lines"] if ln["item"].startswith("Your picture")]
    assert mine and mine[0]["cost"] == 0.0
    drift = auth_client.post("/api/ambient/estimate", json={
        "config": {**SHORT, "source": "image_drift", "picture_origin": "upload"}}).json()
    assert drift["lines"][0]["item"] == "Your picture (no image call)" and drift["lines"][0]["cost"] == 0.0

    from app.providers.base import VideoCapabilities
    from app.schemas.ambient import AmbientConfig
    from app.services import ambient

    caps = VideoCapabilities(durations=[5, 10], first_frame=False, last_frame=False)
    plan = ambient.plan_loop(AmbientConfig(**SHORT, picture_origin="upload"), caps)
    assert any("can't start a clip from an image" in n for n in plan.notes)


def test_picture_endpoints_wait_while_generating(auth_client):
    from app import models
    from app.db import SessionLocal

    sid = _scene(auth_client)
    db = SessionLocal()
    db.get(models.VideoProject, sid).status = "generating"
    db.commit()
    db.close()
    assert _upload(auth_client, sid, make_png_bytes((1920, 1080))).status_code == 409
    assert auth_client.delete(f"/api/ambient/{sid}/picture").status_code == 409


@pytest.mark.parametrize("aspect,ratio", [("16:9", 16 / 9), ("1:1", 1.0)])
def test_crop_handles_tall_sources(aspect, ratio):
    from app.services.ambient import _crop_to_aspect

    out = _crop_to_aspect(make_png_bytes((800, 2000)), aspect)
    w, h = Image.open(io.BytesIO(out)).size
    assert abs(w / h - ratio) < 0.01 and w == 800


def test_drift_preview_uses_the_scene_image_model(auth_client, monkeypatch):
    """Review: the preview becomes the drift still, so it must use the
    scene's image model override, not the global one (video scenes keep the
    active image model, since their override is a video model)."""
    from app.providers import mock_image

    seen = []
    real = mock_image.MockImageProvider.generate

    def spy(self, **kw):
        seen.append(kw["model"])
        return real(self, **kw)

    monkeypatch.setattr(mock_image.MockImageProvider, "generate", spy)
    drift = _scene(auth_client, source="image_drift", model_override="acme/painter-2")
    auth_client.post(f"/api/ambient/{drift}/picture/generate")
    video = _scene(auth_client, model_override="acme/video-9")
    auth_client.post(f"/api/ambient/{video}/picture/generate")
    assert seen[0] == "acme/painter-2" and seen[1] != "acme/video-9"


def test_picture_version_changes_on_every_replacement(auth_client):
    """Review: regenerating twice keeps the same config, so the image URL
    needs a picture-specific version or the browser shows the old one."""
    import time

    sid = _scene(auth_client)
    v1 = auth_client.post(f"/api/ambient/{sid}/picture/generate").json()["picture_version"]
    time.sleep(0.01)
    v2 = auth_client.post(f"/api/ambient/{sid}/picture/generate").json()["picture_version"]
    assert v1 and v2 and v1 != v2
    assert auth_client.delete(f"/api/ambient/{sid}/picture").json()["picture_version"] == ""

"""Live-deploy finding: re-rolling a talking-head shot (single or bulk) must
go through lip-sync like a full render does, not the plain video model."""
from __future__ import annotations

from .test_review_fixes import _talking_project


def _lipsync_on(client):
    r = client.put("/api/models/active", json={"kind": "lipsync", "model": "heygen/avatar-iv", "allow_unlisted": True})
    assert r.status_code == 200, r.text


def _jobs(pid, purpose):
    from app import models
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        return db.query(models.VideoJob).filter_by(project_id=pid, purpose=purpose).count()
    finally:
        db.close()


def _talking_shot(client, pid):
    st = client.get(f"/api/projects/{pid}/status").json()
    return next(s for s in st["shots"] if s["shot_type"] == "talking_head")


def test_regenerating_a_talking_head_uses_lip_sync(auth_client):
    _lipsync_on(auth_client)
    pid = _talking_project(auth_client)
    auth_client.post(f"/api/projects/{pid}/generate-video?force=true")
    assert _talking_shot(auth_client, pid)["provider"] == "lipsync"
    before = _jobs(pid, "avatar")

    shot = _talking_shot(auth_client, pid)
    r = auth_client.post(f"/api/projects/{pid}/shots/{shot['id']}/regenerate?recompose=true")
    assert r.status_code == 202, r.text
    assert _jobs(pid, "avatar") == before + 1  # a new lip-sync job, not a plain video clip
    st = auth_client.get(f"/api/projects/{pid}/status").json()
    th = _talking_shot(auth_client, pid)
    assert th["provider"] == "lipsync" and th["status"] == "completed", th
    assert st["project_status"] == "completed"  # recomposed with the new clip


def test_bulk_regenerate_uses_lip_sync_for_talking_heads(auth_client):
    _lipsync_on(auth_client)
    pid = _talking_project(auth_client)
    auth_client.post(f"/api/projects/{pid}/generate-video?force=true")
    before = _jobs(pid, "avatar")
    st = auth_client.get(f"/api/projects/{pid}/status").json()
    ids = [s["id"] for s in st["shots"] if s["shot_type"] != "end_card"]
    assert auth_client.post(f"/api/projects/{pid}/shots/regenerate-bulk", json={"shot_ids": ids}).status_code == 202
    assert _jobs(pid, "avatar") == before + 1
    assert _talking_shot(auth_client, pid)["provider"] == "lipsync"


def test_regenerate_before_any_render_falls_back_to_the_video_model(auth_client):
    _lipsync_on(auth_client)
    """No render yet = no voiceover to lip-sync to: use the video model."""
    pid = _talking_project(auth_client)
    shot = _talking_shot(auth_client, pid)
    auth_client.post(f"/api/projects/{pid}/shots/{shot['id']}/regenerate?recompose=false")
    assert _jobs(pid, "avatar") == 0
    th = _talking_shot(auth_client, pid)
    assert th["provider"] != "lipsync" and th["status"] == "completed"


def test_video_cost_estimate_reflects_live_pricing():
    from app.config import Settings

    assert Settings(_env_file=None).cost_video_per_second == 0.15

from __future__ import annotations

from pathlib import Path

from .conftest import make_png_bytes


def _completed_project(auth_client) -> int:
    aid = auth_client.post("/api/avatars", json={"name": "X"}).json()["id"]
    auth_client.post(
        f"/api/avatars/{aid}/assets",
        files={"file": ("h.png", make_png_bytes(), "image/png")},
        data={"asset_type": "hero", "rights_confirmed": "true"},
    )
    pid = auth_client.post(
        "/api/projects",
        json={
            "title": "R",
            "original_script": "one two three four five",
            "cta_text": "Go",
            "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
        },
    ).json()["id"]
    auth_client.post(f"/api/projects/{pid}/generate-plan")
    auth_client.post(f"/api/projects/{pid}/generate-video")
    return pid


def test_retry_picks_recompose_when_everything_present(auth_client):
    pid = _completed_project(auth_client)
    r = auth_client.post(f"/api/projects/{pid}/retry")
    assert r.status_code == 202
    body = r.json()
    assert body["action"] == "recompose"
    # And the render actually completes (inline executor).
    st = auth_client.get(f"/api/projects/{pid}/status").json()
    assert st["project_status"] == "completed"


def test_retry_resumes_and_regenerates_failed_shot(auth_client):
    pid = _completed_project(auth_client)
    from app import models
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        shot = (
            db.query(models.VideoShot)
            .filter(models.VideoShot.project_id == pid)
            .order_by(models.VideoShot.shot_order)
            .first()
        )
        # Simulate a failed shot — clip on disk gone, status=failed.
        clip = Path(shot.clip_path)
        if clip.exists():
            clip.unlink()
        shot.clip_path = ""
        shot.status = "failed"
        shot.error = "simulated provider error"
        db.commit()
        failed_shot_id = shot.id
    finally:
        db.close()

    r = auth_client.post(f"/api/projects/{pid}/retry")
    assert r.status_code == 202
    assert r.json()["action"] == "resume"

    # The previously-failed shot is now completed with a clip on disk.
    proj = auth_client.get(f"/api/projects/{pid}").json()
    failed = next(s for s in proj["shots"] if s["id"] == failed_shot_id)
    assert failed["status"] == "completed"
    assert failed["clip_path"] and Path(failed["clip_path"]).exists()
    assert failed["error"] == ""

    st = auth_client.get(f"/api/projects/{pid}/status").json()
    assert st["project_status"] == "completed"


def test_retry_409_without_plan(auth_client):
    aid = auth_client.post("/api/avatars", json={"name": "X"}).json()["id"]
    pid = auth_client.post(
        "/api/projects",
        json={
            "title": "T",
            "original_script": "hi",
            "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
        },
    ).json()["id"]
    assert auth_client.post(f"/api/projects/{pid}/retry").status_code == 409


def test_retry_404_missing_project(auth_client):
    assert auth_client.post("/api/projects/999999/retry").status_code == 404

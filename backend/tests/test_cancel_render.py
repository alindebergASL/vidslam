from __future__ import annotations

from .conftest import make_png_bytes


def _planned(auth_client) -> int:
    aid = auth_client.post("/api/avatars", json={"name": "X"}).json()["id"]
    auth_client.post(
        f"/api/avatars/{aid}/assets",
        files={"file": ("h.png", make_png_bytes(), "image/png")},
        data={"asset_type": "hero", "rights_confirmed": "true"},
    )
    pid = auth_client.post(
        "/api/projects",
        json={
            "title": "C",
            "original_script": "one two three four five",
            "cta_text": "Go",
            "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
        },
    ).json()["id"]
    auth_client.post(f"/api/projects/{pid}/generate-plan")
    return pid


def test_cancel_404_for_missing_project(auth_client):
    assert auth_client.post("/api/projects/999999/cancel").status_code == 404


def test_cancel_409_when_no_render(auth_client):
    pid = _planned(auth_client)
    assert auth_client.post(f"/api/projects/{pid}/cancel").status_code == 409


def test_cancel_after_complete_is_noop(auth_client):
    pid = _planned(auth_client)
    auth_client.post(f"/api/projects/{pid}/generate-video")
    r = auth_client.post(f"/api/projects/{pid}/cancel").json()
    assert r["action"] == "noop"
    assert r["status"] == "completed"


def test_cancel_mid_render_bails_out_at_checkpoint(auth_client, monkeypatch):
    """Flip the render to 'cancelled' mid-shot-loop and confirm the pipeline
    short-circuits with project status='cancelled' rather than 'completed' or
    'failed', and that subsequent shots never run."""
    pid = _planned(auth_client)

    from app import models
    from app.db import SessionLocal
    from app.services import pipeline

    real_ensure = pipeline._ensure_clip_for_shot
    calls: list[int] = []

    def _spy(db, project, shot):
        calls.append(shot.id)
        # On the first shot, flip the latest render to 'cancelled' via a
        # separate session (mimicking the cancel endpoint running concurrently).
        if len(calls) == 1:
            probe = SessionLocal()
            try:
                latest = (
                    probe.query(models.Render)
                    .filter(models.Render.project_id == project.id)
                    .order_by(models.Render.created_at.desc())
                    .first()
                )
                latest.status = "cancelled"
                probe.commit()
            finally:
                probe.close()
        return real_ensure(db, project, shot)

    monkeypatch.setattr(pipeline, "_ensure_clip_for_shot", _spy)

    db = SessionLocal()
    try:
        pipeline.render_project(db, pid)
    finally:
        db.close()

    proj = auth_client.get(f"/api/projects/{pid}").json()
    assert proj["status"] == "cancelled"
    status = auth_client.get(f"/api/projects/{pid}/status").json()
    assert status["latest_render"]["status"] == "cancelled"
    assert status["latest_render"]["error"] == ""
    # The pipeline should NOT have continued past the cancel checkpoint:
    # exactly one shot got the _ensure_clip_for_shot call.
    body_shot_count = len([s for s in proj["shots"] if s["shot_type"] != "end_card"])
    assert body_shot_count >= 2
    assert len(calls) == 1, f"expected pipeline to bail after first shot, got {len(calls)} calls"


def test_retry_after_cancel_resumes_cleanly(auth_client, monkeypatch):
    """After a cancel, retry should still produce a completed video."""
    pid = _planned(auth_client)
    from app import models
    from app.db import SessionLocal
    from app.services import pipeline

    real_ensure = pipeline._ensure_clip_for_shot
    canceled_once = []

    def _spy(db, project, shot):
        if not canceled_once:
            canceled_once.append(True)
            probe = SessionLocal()
            try:
                latest = (
                    probe.query(models.Render)
                    .filter(models.Render.project_id == project.id)
                    .order_by(models.Render.created_at.desc())
                    .first()
                )
                latest.status = "cancelled"
                probe.commit()
            finally:
                probe.close()
        return real_ensure(db, project, shot)

    monkeypatch.setattr(pipeline, "_ensure_clip_for_shot", _spy)
    auth_client.post(f"/api/projects/{pid}/generate-video")
    assert auth_client.get(f"/api/projects/{pid}").json()["status"] == "cancelled"

    # Restore real behavior and retry — should pick the 'resume' path and finish.
    monkeypatch.setattr(pipeline, "_ensure_clip_for_shot", real_ensure)
    r = auth_client.post(f"/api/projects/{pid}/retry").json()
    assert r["action"] == "resume"
    final_status = auth_client.get(f"/api/projects/{pid}/status").json()
    assert final_status["project_status"] == "completed"

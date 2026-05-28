from __future__ import annotations

from .conftest import make_png_bytes


def _planned_project(auth_client) -> int:
    aid = auth_client.post("/api/avatars", json={"name": "X"}).json()["id"]
    auth_client.post(
        f"/api/avatars/{aid}/assets",
        files={"file": ("h.png", make_png_bytes(), "image/png")},
        data={"asset_type": "hero", "rights_confirmed": "true"},
    )
    pid = auth_client.post(
        "/api/projects",
        json={
            "title": "T",
            "original_script": "one two three four five",
            "cta_text": "Go",
            "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
        },
    ).json()["id"]
    auth_client.post(f"/api/projects/{pid}/generate-plan")
    return pid


def test_project_flips_to_generating_during_render(auth_client, monkeypatch):
    """The project status must read 'generating' while shots are being produced so
    clients polling project_status keep polling through a worker-backed render."""
    pid = _planned_project(auth_client)

    from app import models
    from app.db import SessionLocal
    from app.services import pipeline

    seen: list[str] = []
    real = pipeline._ensure_clip_for_shot

    def _spy(db, project, shot):
        # Capture the project's status as observed by a *separate* session,
        # mimicking a client polling while the render runs.
        probe = SessionLocal()
        try:
            seen.append(probe.get(models.VideoProject, project.id).status)
        finally:
            probe.close()
        return real(db, project, shot)

    monkeypatch.setattr(pipeline, "_ensure_clip_for_shot", _spy)

    db = SessionLocal()
    try:
        pipeline.render_project(db, pid)
    finally:
        db.close()

    assert seen, "no shots were generated"
    assert all(s == "generating" for s in seen), seen
    # And it ends at completed.
    assert auth_client.get(f"/api/projects/{pid}").json()["status"] == "completed"

from __future__ import annotations

from .conftest import make_png_bytes


def _rendered_project(auth_client) -> int:
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
    auth_client.post(f"/api/projects/{pid}/generate-video")
    return pid


def test_shot_clip_streams_after_render(auth_client):
    pid = _rendered_project(auth_client)
    shots = auth_client.get(f"/api/projects/{pid}").json()["shots"]
    body = [s for s in shots if s["shot_type"] != "end_card"]
    r = auth_client.get(f"/api/projects/{pid}/shots/{body[0]['id']}/clip")
    assert r.status_code == 200
    assert r.headers["content-type"] == "video/mp4"
    assert len(r.content) > 1000


def test_shot_clip_404_for_ungenerated_or_foreign(auth_client):
    pid = _rendered_project(auth_client)
    # Foreign shot id.
    assert auth_client.get(f"/api/projects/{pid}/shots/999999/clip").status_code == 404
    # End-card shot has no provider clip on disk.
    shots = auth_client.get(f"/api/projects/{pid}").json()["shots"]
    end = [s for s in shots if s["shot_type"] == "end_card"]
    if end:
        assert auth_client.get(f"/api/projects/{pid}/shots/{end[0]['id']}/clip").status_code == 404

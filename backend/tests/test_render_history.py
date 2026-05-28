from __future__ import annotations

from .conftest import make_png_bytes


def _project_with_hero(auth_client) -> int:
    aid = auth_client.post("/api/avatars", json={"name": "X"}).json()["id"]
    auth_client.post(
        f"/api/avatars/{aid}/assets",
        files={"file": ("h.png", make_png_bytes(), "image/png")},
        data={"asset_type": "hero", "rights_confirmed": "true"},
    )
    return auth_client.post(
        "/api/projects",
        json={
            "title": "T",
            "original_script": "one two three four five",
            "cta_text": "Go",
            "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
        },
    ).json()["id"]


def test_list_renders_returns_newest_first(auth_client):
    pid = _project_with_hero(auth_client)
    # No renders yet → empty list.
    assert auth_client.get(f"/api/projects/{pid}/renders").json() == []
    # Generate once, then recompose twice → three renders.
    auth_client.post(f"/api/projects/{pid}/generate-plan")
    auth_client.post(f"/api/projects/{pid}/generate-video")
    auth_client.post(f"/api/projects/{pid}/recompose")
    auth_client.post(f"/api/projects/{pid}/recompose")
    rs = auth_client.get(f"/api/projects/{pid}/renders").json()
    assert len(rs) == 3
    # Newest first by created_at.
    timestamps = [r["created_at"] for r in rs]
    assert timestamps == sorted(timestamps, reverse=True)
    # All completed.
    assert all(r["status"] == "completed" for r in rs)


def test_list_renders_404_when_project_missing(auth_client):
    assert auth_client.get("/api/projects/99999/renders").status_code == 404

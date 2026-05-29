from __future__ import annotations

from .conftest import make_png_bytes


def _render_project(auth_client, title: str) -> int:
    aid = auth_client.post("/api/avatars", json={"name": "X"}).json()["id"]
    auth_client.post(
        f"/api/avatars/{aid}/assets",
        files={"file": ("h.png", make_png_bytes(), "image/png")},
        data={"asset_type": "hero", "rights_confirmed": "true"},
    )
    pid = auth_client.post(
        "/api/projects",
        json={
            "title": title,
            "original_script": "one two three four five",
            "cta_text": "Go",
            "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
        },
    ).json()["id"]
    auth_client.post(f"/api/projects/{pid}/generate-plan")
    auth_client.post(f"/api/projects/{pid}/generate-video")
    return pid


def test_recent_renders_empty_when_none(auth_client):
    # Per-test DB isolation means this is genuinely empty.
    assert auth_client.get("/api/renders/recent").json() == []


def test_recent_renders_returns_completed_newest_first(auth_client):
    _render_project(auth_client, "Alpha")
    pid_b = _render_project(auth_client, "Beta")  # created after Alpha
    rows = auth_client.get("/api/renders/recent").json()
    assert len(rows) == 2
    assert rows[0]["project_id"] == pid_b  # newest first
    assert {r["project_title"] for r in rows} == {"Alpha", "Beta"}
    for r in rows:
        assert {"render_id", "project_id", "project_title", "share_token", "created_at"} <= set(r)


def test_recent_renders_excludes_unrendered_projects(auth_client):
    _render_project(auth_client, "Rendered")
    aid = auth_client.post("/api/avatars", json={"name": "Y"}).json()["id"]
    auth_client.post(
        "/api/projects",
        json={
            "title": "NoRenderYet",
            "original_script": "hi there",
            "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
        },
    )
    rows = auth_client.get("/api/renders/recent").json()
    assert [r["project_title"] for r in rows] == ["Rendered"]


def test_recent_renders_limit_and_route_ordering(auth_client):
    _render_project(auth_client, "One")
    _render_project(auth_client, "Two")
    assert len(auth_client.get("/api/renders/recent?limit=1").json()) == 1
    # The literal /recent route must not shadow /renders/{id}.
    rid = auth_client.get("/api/renders/recent?limit=1").json()[0]["render_id"]
    assert auth_client.get(f"/api/renders/{rid}").status_code == 200

from __future__ import annotations

from .conftest import make_png_bytes

# NOTE: the test harness shares one DB across a process (app.db binds the engine
# at import), so these tests assert *relative* behavior (baseline → delta) rather
# than global emptiness. The endpoint itself is global-across-projects by design.


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


def test_recent_renders_shape_and_newest_first(auth_client):
    pid_a = _render_project(auth_client, "RecentAlpha")
    pid_b = _render_project(auth_client, "RecentBeta")  # created after Alpha

    rows = auth_client.get("/api/renders/recent").json()
    mine = [r for r in rows if r["project_id"] in (pid_a, pid_b)]
    assert len(mine) == 2
    # Beta was rendered after Alpha → appears earlier (newest first).
    assert mine[0]["project_id"] == pid_b
    assert mine[1]["project_id"] == pid_a
    # Each row carries what the dashboard gallery needs.
    for r in mine:
        assert {"render_id", "project_id", "project_title", "share_token", "created_at"} <= set(r)
    titles = {r["project_id"]: r["project_title"] for r in mine}
    assert titles[pid_a] == "RecentAlpha" and titles[pid_b] == "RecentBeta"


def test_recent_renders_only_completed(auth_client):
    # A project with a plan but no render must not appear.
    aid = auth_client.post("/api/avatars", json={"name": "X"}).json()["id"]
    pid = auth_client.post(
        "/api/projects",
        json={
            "title": "NoRenderYet",
            "original_script": "hi there",
            "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
        },
    ).json()["id"]
    auth_client.post(f"/api/projects/{pid}/generate-plan")
    rows = auth_client.get("/api/renders/recent").json()
    assert all(r["project_id"] != pid for r in rows)


def test_recent_renders_limit_and_route_ordering(auth_client):
    _render_project(auth_client, "RecentGamma")
    # limit honored
    assert len(auth_client.get("/api/renders/recent?limit=1").json()) == 1
    # The literal /recent route does not shadow /renders/{id}.
    rid = auth_client.get("/api/renders/recent?limit=1").json()[0]["render_id"]
    assert auth_client.get(f"/api/renders/{rid}").status_code == 200

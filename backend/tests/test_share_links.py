from __future__ import annotations

from .conftest import make_png_bytes


def _completed_render(auth_client) -> dict:
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
    return auth_client.get(f"/api/projects/{pid}/status").json()["latest_render"]


def test_render_has_share_token_and_public_route_serves_video(auth_client):
    render = _completed_render(auth_client)
    token = render["share_token"]
    assert token

    # Public route works WITHOUT auth — use a bare client (no login cookie).
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anon:
        r = anon.get(f"/api/public-renders/{token}")
        assert r.status_code == 200
        assert r.headers["content-type"] == "video/mp4"
        assert len(r.content) > 1000
        t = anon.get(f"/api/public-renders/{token}/thumbnail")
        assert t.status_code == 200
        assert t.headers["content-type"] == "image/jpeg"


def test_public_route_404_for_unknown_token(auth_client):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anon:
        assert anon.get("/api/public-renders/not-a-real-token").status_code == 404


def test_share_tokens_are_unique_per_render(auth_client):
    r1 = _completed_render(auth_client)
    r2 = _completed_render(auth_client)
    assert r1["share_token"] != r2["share_token"]

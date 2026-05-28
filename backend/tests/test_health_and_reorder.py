from __future__ import annotations

from .conftest import make_png_bytes


def _project_with_plan(auth_client) -> int:
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
            "original_script": "one two three four five six seven eight",
            "cta_text": "Go",
            "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
        },
    ).json()["id"]
    auth_client.post(f"/api/projects/{pid}/generate-plan")
    return pid


def test_health_check_reports_mock_in_mock_mode(auth_client):
    r = auth_client.post("/api/providers/health-check")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    groups = {x["group"]: x for x in body["results"]}
    assert groups["openrouter"]["mode"] == "mock"
    assert groups["elevenlabs"]["mode"] == "mock"
    assert all(x["ok"] for x in body["results"])


def test_reorder_shots_sets_new_order(auth_client):
    pid = _project_with_plan(auth_client)
    shots = auth_client.get(f"/api/projects/{pid}").json()["shots"]
    assert len(shots) >= 2
    ids = [s["id"] for s in shots]
    reversed_ids = list(reversed(ids))
    r = auth_client.post(
        f"/api/projects/{pid}/shots/reorder", json={"shot_ids": reversed_ids}
    )
    assert r.status_code == 200
    new_shots = sorted(r.json()["shots"], key=lambda s: s["shot_order"])
    assert [s["id"] for s in new_shots] == reversed_ids
    # shot_order is 1..N with no gaps.
    assert [s["shot_order"] for s in new_shots] == list(range(1, len(new_shots) + 1))


def test_reorder_ignores_foreign_ids_and_keeps_omitted(auth_client):
    pid = _project_with_plan(auth_client)
    shots = auth_client.get(f"/api/projects/{pid}").json()["shots"]
    ids = [s["id"] for s in shots]
    # Only move the last shot to the front; omit the rest + include a bogus id.
    r = auth_client.post(
        f"/api/projects/{pid}/shots/reorder",
        json={"shot_ids": [ids[-1], 999999]},
    )
    assert r.status_code == 200
    new_shots = sorted(r.json()["shots"], key=lambda s: s["shot_order"])
    assert new_shots[0]["id"] == ids[-1]
    # All shots still present, contiguous order.
    assert {s["id"] for s in new_shots} == set(ids)
    assert [s["shot_order"] for s in new_shots] == list(range(1, len(new_shots) + 1))


def test_public_render_meta(auth_client):
    pid = _project_with_plan(auth_client)
    auth_client.post(f"/api/projects/{pid}/generate-video")
    render = auth_client.get(f"/api/projects/{pid}/status").json()["latest_render"]
    token = render["share_token"]
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anon:
        r = anon.get(f"/api/public-renders/{token}/meta")
        assert r.status_code == 200
        meta = r.json()
        assert meta["title"] == "T"
        assert meta["disclosure"]
        assert anon.get("/api/public-renders/bad-token/meta").status_code == 404

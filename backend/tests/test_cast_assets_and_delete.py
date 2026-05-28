from __future__ import annotations

from .conftest import make_png_bytes


def _hero(client, avatar_id: int) -> int:
    return client.post(
        f"/api/avatars/{avatar_id}/assets",
        files={"file": ("h.png", make_png_bytes(), "image/png")},
        data={"asset_type": "hero", "rights_confirmed": "true"},
    ).json()["id"]


def test_cast_assets_returns_all_owner_assets(auth_client):
    aid = auth_client.post("/api/avatars", json={"name": "Naina"}).json()["id"]
    hero_id = _hero(auth_client, aid)
    iid = auth_client.post(
        "/api/ingredients", json={"name": "Cafe", "kind": "scene"}
    ).json()["id"]
    ing_hero = auth_client.post(
        f"/api/ingredients/{iid}/assets",
        files={"file": ("s.png", make_png_bytes(color=(50, 50, 50)), "image/png")},
        data={"rights_confirmed": "true"},
    ).json()["id"]
    pid = auth_client.post(
        "/api/projects",
        json={
            "title": "T",
            "original_script": "hi",
            "cast": [
                {"member_kind": "avatar", "avatar_id": aid, "role": "host"},
                {"member_kind": "ingredient", "ingredient_id": iid, "role": "location"},
            ],
        },
    ).json()["id"]
    r = auth_client.get(f"/api/projects/{pid}/cast-assets")
    assert r.status_code == 200
    ids = {a["id"] for a in r.json()}
    assert hero_id in ids and ing_hero in ids


def test_shot_reference_override_persists(auth_client):
    aid = auth_client.post("/api/avatars", json={"name": "X"}).json()["id"]
    hero_id = _hero(auth_client, aid)
    pid = auth_client.post(
        "/api/projects",
        json={
            "title": "T",
            "original_script": "one two three four five six",
            "cta_text": "Go",
            "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
        },
    ).json()["id"]
    auth_client.post(f"/api/projects/{pid}/generate-plan")
    proj = auth_client.get(f"/api/projects/{pid}").json()
    shot_id = proj["shots"][0]["id"]
    r = auth_client.patch(
        f"/api/projects/{pid}/shots/{shot_id}",
        json={"reference_asset_ids_json": [hero_id]},
    )
    assert r.status_code == 200
    assert r.json()["reference_asset_ids_json"] == [hero_id]


def test_delete_project_cascades(auth_client):
    aid = auth_client.post("/api/avatars", json={"name": "X"}).json()["id"]
    pid = auth_client.post(
        "/api/projects",
        json={
            "title": "Doomed",
            "original_script": "x",
            "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
        },
    ).json()["id"]
    assert auth_client.get(f"/api/projects/{pid}").status_code == 200
    r = auth_client.delete(f"/api/projects/{pid}")
    assert r.status_code == 204
    assert auth_client.get(f"/api/projects/{pid}").status_code == 404

from __future__ import annotations

from .conftest import make_png_bytes


def _avatar(auth_client, name: str) -> int:
    aid = auth_client.post("/api/avatars", json={"name": name}).json()["id"]
    auth_client.post(
        f"/api/avatars/{aid}/assets",
        files={"file": ("h.png", make_png_bytes(), "image/png")},
        data={"asset_type": "hero", "rights_confirmed": "true"},
    )
    return aid


def test_deleting_avatar_removes_cast_membership_and_nulls_primary(auth_client):
    a1 = _avatar(auth_client, "Doomed")
    a2 = _avatar(auth_client, "Keeper")
    pid = auth_client.post(
        "/api/projects",
        json={
            "title": "P",
            "original_script": "one two three",
            "primary_avatar_id": a1,
            "cast": [
                {"member_kind": "avatar", "avatar_id": a1, "role": "host"},
                {"member_kind": "avatar", "avatar_id": a2, "role": "co-host"},
            ],
        },
    ).json()["id"]

    assert auth_client.delete(f"/api/avatars/{a1}").status_code == 204

    proj = auth_client.get(f"/api/projects/{pid}").json()
    # No dangling reference to the deleted avatar.
    assert proj["primary_avatar_id"] is None
    avatar_ids = [m["avatar_id"] for m in proj["cast_members"] if m["member_kind"] == "avatar"]
    assert avatar_ids == [a2]


def test_deleting_ingredient_removes_cast_membership(auth_client):
    aid = _avatar(auth_client, "Host")
    iid = auth_client.post("/api/ingredients", json={"name": "Scene", "kind": "scene"}).json()["id"]
    pid = auth_client.post(
        "/api/projects",
        json={
            "title": "P",
            "original_script": "one two three",
            "cast": [
                {"member_kind": "avatar", "avatar_id": aid, "role": "host"},
                {"member_kind": "ingredient", "ingredient_id": iid, "role": "location"},
            ],
        },
    ).json()["id"]

    assert auth_client.delete(f"/api/ingredients/{iid}").status_code == 204

    proj = auth_client.get(f"/api/projects/{pid}").json()
    kinds = [m["member_kind"] for m in proj["cast_members"]]
    assert kinds == ["avatar"]  # ingredient membership removed, avatar kept


def test_project_still_renders_after_cast_member_deleted(auth_client):
    """Deleting one of several avatars must leave the project in a still-renderable
    state (the survivor carries it)."""
    a1 = _avatar(auth_client, "A1")
    a2 = _avatar(auth_client, "A2")
    pid = auth_client.post(
        "/api/projects",
        json={
            "title": "P",
            "original_script": "one two three four five",
            "cta_text": "Go",
            "primary_avatar_id": a1,
            "cast": [
                {"member_kind": "avatar", "avatar_id": a1, "role": "host"},
                {"member_kind": "avatar", "avatar_id": a2, "role": "co-host"},
            ],
        },
    ).json()["id"]
    auth_client.delete(f"/api/avatars/{a1}")

    # Preflight should still pass (a2 has a hero) and a render should complete.
    assert auth_client.get(f"/api/projects/{pid}/preflight").json()["ok"] is True
    auth_client.post(f"/api/projects/{pid}/generate-plan")
    auth_client.post(f"/api/projects/{pid}/generate-video")
    st = auth_client.get(f"/api/projects/{pid}/status").json()
    assert st["project_status"] == "completed"

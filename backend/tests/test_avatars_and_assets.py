from __future__ import annotations

from .conftest import make_png_bytes


def test_unauthenticated_blocked(client):
    r = client.get("/api/avatars")
    assert r.status_code == 401


def test_login_then_crud_avatar(auth_client):
    r = auth_client.post("/api/avatars", json={"name": "Naina", "persona": "witty"})
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "Naina"
    aid = body["id"]

    r = auth_client.get(f"/api/avatars/{aid}")
    assert r.status_code == 200

    r = auth_client.patch(f"/api/avatars/{aid}", json={"persona": "deadpan"})
    assert r.json()["persona"] == "deadpan"

    r = auth_client.get("/api/avatars")
    assert any(a["id"] == aid for a in r.json())


def test_asset_upload_requires_rights_checkbox(auth_client):
    avatar_id = auth_client.post("/api/avatars", json={"name": "X"}).json()["id"]
    files = {"file": ("x.png", make_png_bytes(), "image/png")}
    r = auth_client.post(f"/api/avatars/{avatar_id}/assets", files=files,
                         data={"asset_type": "hero", "rights_confirmed": "false"})
    assert r.status_code == 400
    assert "rights" in r.json()["detail"].lower()

    r = auth_client.post(f"/api/avatars/{avatar_id}/assets", files=files,
                         data={"asset_type": "hero", "rights_confirmed": "true"})
    assert r.status_code == 201
    asset = r.json()
    assert asset["asset_type"] == "hero"
    assert asset["public_token"]
    assert asset["width"] == 64

    # Public token route serves the bytes.
    r = auth_client.get(f"/api/public-assets/{asset['public_token']}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")


def test_asset_upload_rejects_bad_mime(auth_client):
    avatar_id = auth_client.post("/api/avatars", json={"name": "X"}).json()["id"]
    files = {"file": ("x.gif", b"not an image", "image/gif")}
    r = auth_client.post(f"/api/avatars/{avatar_id}/assets", files=files,
                         data={"asset_type": "hero", "rights_confirmed": "true"})
    assert r.status_code == 400


def test_ingredient_crud_and_assets(auth_client):
    iid = auth_client.post("/api/ingredients",
                           json={"name": "Cafe", "kind": "scene"}).json()["id"]
    files = {"file": ("scene.png", make_png_bytes(color=(50, 50, 50)), "image/png")}
    r = auth_client.post(f"/api/ingredients/{iid}/assets", files=files,
                         data={"rights_confirmed": "true"})
    assert r.status_code == 201
    r = auth_client.get(f"/api/ingredients/{iid}/assets")
    assert len(r.json()) == 1

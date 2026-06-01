from __future__ import annotations


def test_seed_endpoint_populates_demo_content(auth_client):
    # Library starts empty.
    info = auth_client.get("/api/system/info").json()
    assert info["counts"]["avatars"] == 0
    assert info["counts"]["projects"] == 0

    r = auth_client.post("/api/system/seed")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["already_seeded"] is False
    assert body["counts"]["avatars"] >= 2  # Naina + Arjun
    assert body["counts"]["projects"] >= 1  # sample Kissmet project

    # /info reflects the new counts.
    info2 = auth_client.get("/api/system/info").json()
    assert info2["counts"]["avatars"] >= 2
    assert info2["counts"]["projects"] >= 1
    assert info2["counts"]["brand_kits"] >= 1
    assert info2["counts"]["ingredients"] >= 2

    # Seeded project is fully wired (has a cast and a brand kit) and ready to
    # generate-plan immediately.
    projects = auth_client.get("/api/projects").json()
    assert projects, "seed should produce at least one project"
    p = projects[0]
    assert len(p["cast_members"]) >= 1
    assert p["brand_kit_id"] is not None


def test_seed_is_idempotent(auth_client):
    auth_client.post("/api/system/seed")
    first = auth_client.get("/api/system/info").json()["counts"]
    r = auth_client.post("/api/system/seed")
    assert r.status_code == 200
    assert r.json()["already_seeded"] is True
    second = auth_client.get("/api/system/info").json()["counts"]
    # Re-running the seed must not duplicate rows.
    assert second == first


def test_seed_requires_auth(client):
    assert client.post("/api/system/seed").status_code == 401

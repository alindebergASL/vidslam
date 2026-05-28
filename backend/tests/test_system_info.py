from __future__ import annotations


def test_system_info_reports_mock_and_counts(auth_client):
    auth_client.post("/api/avatars", json={"name": "A"})
    auth_client.post("/api/brand-kits", json={"name": "B"})

    r = auth_client.get("/api/system/info")
    assert r.status_code == 200
    info = r.json()

    assert info["mock_providers"] is True
    assert info["ffmpeg_available"] is True  # installed in the test env
    # In mock mode, keys are absent and that's fine.
    assert info["keys"] == {"openrouter": False, "elevenlabs": False}
    assert info["counts"]["avatars"] >= 1
    assert info["counts"]["brand_kits"] >= 1
    # Never leak secret values — only model ids / booleans.
    assert "openrouter_api_key" not in str(info)
    assert isinstance(info["models"]["chat"], (str, type(None)))


def test_system_info_requires_auth(client):
    assert client.get("/api/system/info").status_code == 401

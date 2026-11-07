"""Settings → API keys: encrypted storage, resolver precedence, write-only API,
key test probes, and that providers actually use the resolved key."""
from __future__ import annotations

import base64

import httpx
import pytest
import respx

from .conftest import make_png_bytes

OR = "https://openrouter.ai/api/v1"
SOGNI = "https://api.sogni.ai"
APP_KEY = "sk-test-123456789"
ENV_KEY = "sk-test-env-987654321"
ENV_VARS = ("OPENROUTER_API_KEY", "ELEVENLABS_API_KEY", "GEMINI_API_KEY",
            "REPLICATE_API_TOKEN", "SOGNI_API_KEY")


@pytest.fixture(autouse=True)
def _clean_keys(monkeypatch):
    from app import config
    from app.services import provider_keys

    for v in ENV_VARS:
        monkeypatch.setenv(v, "")
    config.get_settings.cache_clear()
    provider_keys.invalidate()
    yield
    provider_keys.invalidate()


def _use_env(monkeypatch, var: str, value: str) -> None:
    from app import config

    monkeypatch.setenv(var, value)
    config.get_settings.cache_clear()


def _row(resp_json: dict, name: str) -> dict:
    return next(k for k in resp_json["keys"] if k["name"] == name)


def test_requires_auth(client):
    assert client.get("/api/settings/keys").status_code == 401
    assert client.put("/api/settings/keys/openrouter", json={"value": APP_KEY}).status_code == 401
    assert client.delete("/api/settings/keys/openrouter").status_code == 401
    assert client.post("/api/settings/keys/openrouter/test").status_code == 401


def test_set_get_clear_round_trip_never_returns_value(auth_client):
    from app import models
    from app.db import SessionLocal

    listing = auth_client.get("/api/settings/keys").json()
    assert listing["mock_providers"] is True
    assert [k["name"] for k in listing["keys"]] == ["openrouter", "elevenlabs", "gemini", "replicate", "sogni"]
    assert _row(listing, "sogni")["docs_url"] == "https://dashboard.sogni.ai/api-keys"
    assert _row(listing, "openrouter")["source"] == "none"

    r = auth_client.put("/api/settings/keys/openrouter", json={"value": f"  {APP_KEY}\n"})
    assert r.status_code == 200
    assert APP_KEY not in r.text
    row = r.json()
    assert row["set"] is True and row["source"] == "app" and row["last4"] == "6789"
    assert row["updated_at"]

    r = auth_client.get("/api/settings/keys")
    assert APP_KEY not in r.text
    assert _row(r.json(), "openrouter")["source"] == "app"

    # Stored sealed, not in plaintext.
    db = SessionLocal()
    try:
        stored = db.get(models.AppSetting, "secret.openrouter").value
    finally:
        db.close()
    assert APP_KEY not in str(stored) and stored["last4"] == "6789"

    from app.services import provider_keys

    assert provider_keys.resolve("openrouter") == APP_KEY

    r = auth_client.delete("/api/settings/keys/openrouter")
    assert r.status_code == 200 and r.json()["source"] == "none" and r.json()["set"] is False
    assert provider_keys.resolve("openrouter") == ""
    assert APP_KEY not in auth_client.get("/api/settings/keys").text


@pytest.mark.parametrize("body", [
    {"value": "sk-1"},  # too short
    {"value": "sk-test 123456789"},  # inner whitespace
    {"value": "sk-test-\n123456789"},  # inner newline
    {"value": "x" * 501},
    {"value": 123456789},
    {"nope": APP_KEY},
])
def test_put_rejects_bad_values_without_echo(auth_client, body):
    r = auth_client.put("/api/settings/keys/openrouter", json=body)
    assert r.status_code == 422
    for v in body.values():
        if isinstance(v, str):
            assert v not in r.text
            assert "123456789" not in r.text
    assert _row(auth_client.get("/api/settings/keys").json(), "openrouter")["source"] == "none"


def test_put_non_json_and_unknown_name(auth_client):
    r = auth_client.put("/api/settings/keys/openrouter", content=f"value={APP_KEY}",
                        headers={"Content-Type": "application/json"})
    assert r.status_code == 422 and APP_KEY not in r.text
    r = auth_client.put("/api/settings/keys/session_secret", json={"value": APP_KEY})
    assert r.status_code == 404 and APP_KEY not in r.text
    assert auth_client.delete("/api/settings/keys/mvp_password").status_code == 404
    assert auth_client.post("/api/settings/keys/youtube/test").status_code == 404


def test_env_fallback_and_app_overrides_env(auth_client, monkeypatch):
    from app.services import provider_keys

    _use_env(monkeypatch, "OPENROUTER_API_KEY", ENV_KEY)
    assert provider_keys.resolve("openrouter") == ENV_KEY
    r = auth_client.get("/api/settings/keys")
    row = _row(r.json(), "openrouter")
    assert row["source"] == "env" and row["set"] is True and row["last4"] == "4321"
    assert ENV_KEY not in r.text

    auth_client.put("/api/settings/keys/openrouter", json={"value": APP_KEY})
    assert provider_keys.resolve("openrouter") == APP_KEY
    assert _row(auth_client.get("/api/settings/keys").json(), "openrouter")["source"] == "app"

    # Clearing the app key falls back to env again.
    assert auth_client.delete("/api/settings/keys/openrouter").json()["source"] == "env"
    assert provider_keys.resolve("openrouter") == ENV_KEY


def test_unreadable_sealed_value_falls_back_and_is_reported(auth_client, monkeypatch, caplog):
    from app import config
    from app.services import provider_keys

    auth_client.put("/api/settings/keys/sogni", json={"value": APP_KEY})
    _use_env(monkeypatch, "SOGNI_API_KEY", ENV_KEY)
    # SESSION_SECRET rotated: the sealed value can't be opened any more.
    monkeypatch.setenv("SESSION_SECRET", "a-different-secret-entirely")
    config.get_settings.cache_clear()
    caplog.set_level("WARNING", logger="avs.provider_keys")
    assert provider_keys.resolve("sogni") == ENV_KEY
    row = provider_keys.row("sogni")
    assert row["source"] == "unreadable"
    assert APP_KEY not in str(row) and ENV_KEY not in str(row)
    warnings = [r for r in caplog.records if "can't be decrypted" in r.getMessage()]
    assert len(warnings) == 1
    assert APP_KEY not in caplog.text and ENV_KEY not in caplog.text
    # Logged once, not on every resolve.
    provider_keys.invalidate()
    provider_keys.resolve("sogni")
    assert len([r for r in caplog.records if "can't be decrypted" in r.getMessage()]) == 1

    # Without an env key it's simply unset.
    _use_env(monkeypatch, "SOGNI_API_KEY", "")
    provider_keys.invalidate()
    assert provider_keys.resolve("sogni") == ""
    assert provider_keys.has("sogni") is False


@respx.mock
def test_openrouter_image_provider_uses_stored_key(auth_client):
    from app.providers.openrouter_image import OpenRouterImageProvider
    from app.services import provider_keys

    provider_keys.set_key("openrouter", APP_KEY)
    b64 = base64.b64encode(make_png_bytes()).decode()
    route = respx.post(f"{OR}/images").mock(return_value=httpx.Response(
        200, json={"id": "gen-1", "data": [{"b64_json": b64, "media_type": "image/png"}]}))
    out = OpenRouterImageProvider(model="openai/gpt-image-2").generate(
        model="openai/gpt-image-2", prompt="a lake", references=[])
    assert out.startswith(b"\x89PNG")
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {APP_KEY}"


def test_mock_helpers_and_routing_follow_resolver(auth_client, monkeypatch):
    from app import config
    from app.services import model_registry, provider_keys

    monkeypatch.setenv("MOCK_PROVIDERS", "false")
    config.get_settings.cache_clear()
    s = config.get_settings()
    assert s.image_is_mocked() and s.tts_is_mocked()
    assert model_registry.provider_for("image", "openai/gpt-image-2") == "mock"

    provider_keys.set_key("openrouter", APP_KEY)
    provider_keys.set_key("elevenlabs", "el-test-123456789")
    assert not s.image_is_mocked() and not s.video_is_mocked() and not s.chat_is_mocked()
    assert not s.tts_is_mocked() and not s.music_is_mocked()
    assert model_registry.provider_for("image", "openai/gpt-image-2") == "openrouter"
    assert model_registry.provider_for("tts", "elevenlabs") == "elevenlabs"
    assert model_registry.provider_for("music", "google/lyria-3.5") == "mock"
    provider_keys.set_key("gemini", "gm-test-123456789")
    assert model_registry.provider_for("music", "google/lyria-3.5") == "gemini"

    from app.providers import get_training
    from app.providers.registry import provider_status

    assert provider_status()["training_lora"] == "mock"
    provider_keys.set_key("replicate", "r8-test-123456789")
    assert provider_status()["training_lora"] == "replicate"
    assert type(get_training("character_lora")).__name__ == "ReplicateTrainingProvider"


def test_ttl_cache_picks_up_other_process_writes(auth_client, monkeypatch):
    from app.services import model_registry, provider_keys, secret_box

    provider_keys.set_key("gemini", "gm-test-111111111")
    assert provider_keys.resolve("gemini") == "gm-test-111111111"
    # Another process (the web app, seen from the worker) writes directly.
    model_registry.put_setting("secret.gemini", {"sealed": secret_box.seal("gm-test-222222222"),
                                                 "last4": "2222", "updated_at": "x"})
    assert provider_keys.resolve("gemini") == "gm-test-111111111"  # within TTL
    real = provider_keys.time.monotonic
    monkeypatch.setattr(provider_keys.time, "monotonic",
                        lambda: real() + provider_keys.CACHE_TTL_SECONDS + 1)
    assert provider_keys.resolve("gemini") == "gm-test-222222222"
    monkeypatch.setattr(provider_keys.time, "monotonic", real)
    # A write in this process is visible at once.
    provider_keys.set_key("gemini", "gm-test-333333333")
    assert provider_keys.resolve("gemini") == "gm-test-333333333"


@respx.mock
def test_sogni_key_test_endpoint(auth_client):
    auth_client.put("/api/settings/keys/sogni", json={"value": APP_KEY})
    route = respx.get(f"{SOGNI}/v2/account/me").mock(return_value=httpx.Response(
        200, json={"status": "success", "data": {"username": "someone"}}))
    r = auth_client.post("/api/settings/keys/sogni/test")
    assert r.status_code == 200 and r.json()["ok"] is True
    req = route.calls.last.request
    assert req.headers["Authorization"] == f"Bearer {APP_KEY}"
    assert req.headers["User-Agent"] == "vidslam/1.0"
    assert APP_KEY not in r.text

    # A provider error that echoes the key never reaches the browser.
    route.mock(return_value=httpx.Response(401, json={
        "status": "error", "errorCode": 101, "message": f"Invalid API key {APP_KEY}"}))
    r = auth_client.post("/api/settings/keys/sogni/test")
    body = r.json()
    assert body["ok"] is False and "401" in body["detail"] and "101" in body["detail"]
    assert APP_KEY not in r.text and "Authorization" not in r.text


@respx.mock
def test_openrouter_key_test_endpoint_and_no_key(auth_client, monkeypatch):
    r = auth_client.post("/api/settings/keys/openrouter/test")
    assert r.json() == {"ok": False, "detail": "No key set."}

    _use_env(monkeypatch, "OPENROUTER_API_KEY", ENV_KEY)
    route = respx.get(f"{OR}/key").mock(return_value=httpx.Response(200, json={"data": {"usage": 1}}))
    r = auth_client.post("/api/settings/keys/openrouter/test")
    assert r.json()["ok"] is True
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {ENV_KEY}"
    route.mock(return_value=httpx.Response(500, text="boom"))
    r = auth_client.post("/api/settings/keys/openrouter/test")
    assert r.json()["ok"] is False and "500" in r.json()["detail"]
    assert ENV_KEY not in r.text


@respx.mock
def test_other_key_test_endpoints(auth_client):
    from app.services import provider_keys

    for name in ("elevenlabs", "gemini", "replicate"):
        provider_keys.set_key(name, f"{name[:2]}-test-123456789")
    el = respx.get("https://api.elevenlabs.io/v1/user").mock(return_value=httpx.Response(200, json={}))
    gm = respx.get("https://generativelanguage.googleapis.com/v1beta/models").mock(
        return_value=httpx.Response(403, json={"error": {"message": "API key not valid"}}))
    rp = respx.get("https://api.replicate.com/v1/account").mock(return_value=httpx.Response(200, json={}))
    assert auth_client.post("/api/settings/keys/elevenlabs/test").json()["ok"] is True
    assert el.calls.last.request.headers["xi-api-key"] == "el-test-123456789"
    g = auth_client.post("/api/settings/keys/gemini/test").json()
    assert g["ok"] is False and "API key not valid" in g["detail"]
    assert gm.calls.last.request.headers["x-goog-api-key"] == "ge-test-123456789"
    assert auth_client.post("/api/settings/keys/replicate/test").json()["ok"] is True
    assert rp.calls.last.request.headers["Authorization"] == "Bearer re-test-123456789"


def test_system_info_and_catalog_follow_keys(auth_client):
    from app.services import model_catalog, model_registry, provider_keys

    info = auth_client.get("/api/system/info").json()
    assert info["keys"] == {"openrouter": False, "elevenlabs": False, "gemini": False,
                            "replicate": False, "sogni": False}
    model_registry.put_setting(model_catalog.CATALOG_KEY, {"entries": [
        {"id": "openai/gpt-image-2", "kind": "image", "provider": "openrouter", "usable": False},
        {"id": "mock/x", "kind": "image", "provider": "mock", "usable": True},
    ]})
    provider_keys.set_key("sogni", APP_KEY)
    provider_keys.set_key("openrouter", APP_KEY)
    r = auth_client.get("/api/system/info")
    assert r.json()["keys"]["sogni"] is True and r.json()["keys"]["openrouter"] is True
    assert APP_KEY not in r.text
    entries = {e["id"]: e for e in model_catalog.get_snapshot()["entries"]}
    assert entries["openai/gpt-image-2"]["usable"] is True
    assert entries["mock/x"]["usable"] is True


def test_mock_providers_overrides_saved_replicate_key(auth_client):
    """MOCK_PROVIDERS=true (the test default) wins over a key saved in the
    app: routing and the reported status agree, and no paid training runs."""
    from app.providers import get_training
    from app.providers.registry import provider_status
    from app.services import provider_keys

    provider_keys.set_key("replicate", "r8-test-123456789")
    assert provider_keys.has("replicate")
    assert provider_status()["training_lora"] == "mock"
    for kind in ("character_lora", "style_lora"):
        assert type(get_training(kind)).__name__ == "MockTrainingProvider"


@respx.mock
def test_sogni_worker_key_is_flagged(auth_client, caplog):
    worker = "sogni_wkr_test123456789"
    with caplog.at_level("WARNING"):
        r = auth_client.put("/api/settings/keys/sogni", json={"value": worker})
    assert r.status_code == 200
    assert "worker key" in r.json()["warning"]
    assert worker not in r.text and worker not in caplog.text
    assert _row(auth_client.get("/api/settings/keys").json(), "sogni")["warning"]

    route = respx.get(f"{SOGNI}/v2/account/me").mock(return_value=httpx.Response(
        200, json={"status": "success", "data": {"username": "someone"}}))
    r = auth_client.post("/api/settings/keys/sogni/test")
    body = r.json()
    assert route.called
    assert body["ok"] is False and "worker key" in body["detail"] and "sogni_dev_" in body["detail"]
    assert worker not in r.text

    # A developer key has no warning and tests fine.
    r = auth_client.put("/api/settings/keys/sogni", json={"value": "sogni_dev_test123456789"})
    assert r.json()["warning"] is None
    assert auth_client.post("/api/settings/keys/sogni/test").json()["ok"] is True


@respx.mock
def test_key_test_does_not_follow_redirects(auth_client):
    from app.services import provider_keys

    provider_keys.set_key("elevenlabs", "el-test-123456789")
    el = respx.get("https://api.elevenlabs.io/v1/user").mock(return_value=httpx.Response(
        302, headers={"Location": "https://evil.example/collect"}))
    evil = respx.get("https://evil.example/collect").mock(return_value=httpx.Response(200, json={}))
    body = auth_client.post("/api/settings/keys/elevenlabs/test").json()
    assert el.called and not evil.called
    assert body["ok"] is False and "Unexpected response (HTTP 302)" in body["detail"]

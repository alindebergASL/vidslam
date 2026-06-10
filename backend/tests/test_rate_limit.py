from __future__ import annotations

from app.services.ratelimit import TokenBucketLimiter

from .conftest import make_png_bytes


def test_token_bucket_exhausts_and_refills_deterministically():
    clock = {"t": 0.0}
    limiter = TokenBucketLimiter(now=lambda: clock["t"])

    # Capacity 2, refill 60/min = 1 token/second.
    assert limiter.check("k", capacity=2, refill_per_minute=60)[0] is True
    assert limiter.check("k", capacity=2, refill_per_minute=60)[0] is True
    allowed, retry_after = limiter.check("k", capacity=2, refill_per_minute=60)
    assert allowed is False
    assert retry_after == 1  # one token/sec → ~1s until the next token

    # Advance the clock 1s → exactly one token refilled.
    clock["t"] = 1.0
    assert limiter.check("k", capacity=2, refill_per_minute=60)[0] is True
    assert limiter.check("k", capacity=2, refill_per_minute=60)[0] is False

    # Long idle never overfills past capacity.
    clock["t"] = 1000.0
    assert limiter.check("k", capacity=2, refill_per_minute=60)[0] is True
    assert limiter.check("k", capacity=2, refill_per_minute=60)[0] is True
    assert limiter.check("k", capacity=2, refill_per_minute=60)[0] is False


def test_token_bucket_keys_are_independent():
    clock = {"t": 0.0}
    limiter = TokenBucketLimiter(now=lambda: clock["t"])
    assert limiter.check("a", capacity=1, refill_per_minute=60)[0] is True
    assert limiter.check("a", capacity=1, refill_per_minute=60)[0] is False
    # A different client key still has a full bucket.
    assert limiter.check("b", capacity=1, refill_per_minute=60)[0] is True


def _project_for_limit_tests(auth_client) -> int:
    aid = auth_client.post("/api/avatars", json={"name": "X"}).json()["id"]
    auth_client.post(
        f"/api/avatars/{aid}/assets",
        files={"file": ("h.png", make_png_bytes(), "image/png")},
        data={"asset_type": "hero", "rights_confirmed": "true"},
    )
    return auth_client.post(
        "/api/projects",
        json={
            "title": "RL",
            "original_script": "one two three four five",
            "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
        },
    ).json()["id"]


def _tighten_limits(monkeypatch, burst: int, per_minute: float = 0.0001) -> None:
    """Dial the limiter down for one test; near-zero refill keeps it exhausted."""
    monkeypatch.setenv("RATE_LIMIT_GENERATION_BURST", str(burst))
    monkeypatch.setenv("RATE_LIMIT_GENERATION_PER_MINUTE", str(per_minute))
    from app import config as _c
    from app.services import ratelimit as _rl

    _c.get_settings.cache_clear()
    _rl._limiter = None


def test_generation_endpoints_429_when_bucket_empty(auth_client, monkeypatch):
    pid = _project_for_limit_tests(auth_client)
    _tighten_limits(monkeypatch, burst=2)

    assert auth_client.post(f"/api/projects/{pid}/generate-plan").status_code == 202
    assert auth_client.post(f"/api/projects/{pid}/generate-plan").status_code == 202
    r = auth_client.post(f"/api/projects/{pid}/generate-plan")
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    assert int(r.headers["Retry-After"]) >= 1
    assert "Rate limit exceeded" in r.json()["detail"]


def test_bucket_is_shared_across_generation_endpoints(auth_client, monkeypatch):
    """All budget-burning routes draw from the same per-client bucket, so a
    caller can't dodge the limit by alternating endpoints."""
    pid = _project_for_limit_tests(auth_client)
    _tighten_limits(monkeypatch, burst=2)

    assert auth_client.post(f"/api/projects/{pid}/generate-plan").status_code == 202
    # Second token spent on a *different* generation route.
    assert auth_client.post(f"/api/projects/{pid}/recompose").status_code in (202, 409)
    # Third call — any generation route — is throttled.
    assert auth_client.post(f"/api/projects/{pid}/generate-plan").status_code == 429


def test_cancel_is_never_rate_limited(auth_client, monkeypatch):
    """Cancel is the escape hatch for a runaway render; throttling it would be
    actively harmful. It must keep responding after the bucket is empty."""
    pid = _project_for_limit_tests(auth_client)
    # Render once while limits are still generous so a render row exists.
    auth_client.post(f"/api/projects/{pid}/generate-plan")
    auth_client.post(f"/api/projects/{pid}/generate-video?force=true")
    _tighten_limits(monkeypatch, burst=1)

    # Exhaust the bucket...
    assert auth_client.post(f"/api/projects/{pid}/generate-plan").status_code == 202
    assert auth_client.post(f"/api/projects/{pid}/generate-plan").status_code == 429
    # ...cancel still answers (noop here since the render already completed).
    assert auth_client.post(f"/api/projects/{pid}/cancel").status_code == 200


def test_studio_and_music_routes_are_limited(auth_client, monkeypatch):
    aid = auth_client.post("/api/avatars", json={"name": "S"}).json()["id"]
    _tighten_limits(monkeypatch, burst=1)

    body = {"owner_kind": "avatar", "owner_id": aid, "prompt": "p"}
    assert auth_client.post("/api/studio/generate-image", json=body).status_code == 202
    assert auth_client.post("/api/studio/generate-image", json=body).status_code == 429


def test_rate_limit_disabled_flag_bypasses(auth_client, monkeypatch):
    pid = _project_for_limit_tests(auth_client)
    _tighten_limits(monkeypatch, burst=1)
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    from app import config as _c

    _c.get_settings.cache_clear()

    for _ in range(4):
        assert auth_client.post(f"/api/projects/{pid}/generate-plan").status_code == 202

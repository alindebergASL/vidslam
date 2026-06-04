from __future__ import annotations


def test_health_and_healthz_are_unauthenticated(client):
    # Both probes must be reachable without logging in (so a load balancer can
    # poll them without credentials).
    assert client.get("/health").status_code == 200
    assert client.get("/healthz").status_code == 200
    assert client.get("/healthz").json() == {"status": "ok"}


def test_readyz_reports_each_check_in_happy_state(client):
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    checks = body["checks"]
    # All three probes are present and pass.
    assert set(checks) == {"database", "ffmpeg", "data_dir_writable"}
    assert all(c["ok"] for c in checks.values())
    # Database probe records a latency. ffmpeg is present in the test env.
    assert "latency_ms" in checks["database"]
    assert checks["ffmpeg"]["ok"] is True


def test_readyz_503_when_data_dir_is_unwritable(client, tmp_path, monkeypatch):
    # Point DATA_DIR at a path we deliberately make unwritable, then re-resolve
    # settings so the readyz handler sees it.
    unwritable = tmp_path / "ro"
    unwritable.mkdir()
    unwritable.chmod(0o500)  # read+execute only
    try:
        monkeypatch.setenv("DATA_DIR", str(unwritable))
        from app import config as _c

        _c.get_settings.cache_clear()

        r = client.get("/readyz")
        # Root inside Docker bypasses Unix perms — skip if so, otherwise verify.
        if r.status_code == 200 and r.json()["checks"]["data_dir_writable"]["ok"]:
            import pytest
            pytest.skip("running as root; DATA_DIR chmod cannot make it unwritable")

        assert r.status_code == 503
        body = r.json()
        assert body["status"] == "not_ready"
        assert body["checks"]["data_dir_writable"]["ok"] is False
        # Other checks still report; only the broken one flips overall status.
        assert body["checks"]["database"]["ok"] is True
    finally:
        unwritable.chmod(0o755)  # let pytest clean tmp_path
        from app import config as _c
        _c.get_settings.cache_clear()


def test_readyz_503_when_database_is_unreachable(client, monkeypatch):
    """If a DB query raises (e.g. file locked, engine misconfigured), /readyz
    must report not_ready instead of hiding it behind a generic 500."""
    from app import main as _m

    class _BrokenSession:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def execute(self, *_a, **_kw):
            raise RuntimeError("database is asleep")

    def _broken_factory(*a, **kw):
        return _BrokenSession()

    monkeypatch.setattr(_m, "SessionLocal", _broken_factory)
    r = client.get("/readyz")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"]["ok"] is False
    assert "database is asleep" in body["checks"]["database"]["error"]

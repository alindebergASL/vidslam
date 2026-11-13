from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from PIL import Image

_PG_MIGRATED = False


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path: Path) -> None:
    """Each test gets its own SQLite file + data dir, with full mock mode."""
    db = tmp_path / "test.db"
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("MOCK_PROVIDERS", "true")
    monkeypatch.setenv("MVP_PASSWORD", "test-pw")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-please-change")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://test.local")
    # Generous rate-limit defaults so ordinary tests never trip the limiter;
    # the dedicated rate-limit tests dial these down explicitly.
    monkeypatch.setenv("RATE_LIMIT_GENERATION_BURST", "10000")
    monkeypatch.setenv("RATE_LIMIT_GENERATION_PER_MINUTE", "10000")
    # Sogni's model list comes from the bundled snapshot (no network in tests).
    monkeypatch.setenv("SOGNI_TOOLS_MANIFEST_URL", "")
    from app.providers import sogni as _sogni

    _sogni._reset_listed()
    # Reset cached singletons (settings + mock video + rate limiter).
    from app import config as _c
    from app.providers import mock_video as _mv
    from app.services import ratelimit as _rl

    _c.get_settings.cache_clear()
    _mv._singleton = None
    _rl._limiter = None
    from app.services import alerts as _al

    _al.reset()
    # The safety check's in-process verdicts and the mock classifier's hooks.
    from app.providers import mock_chat as _mc
    from app.services import minor_check as _mcheck

    _mcheck.reset_cache()
    _mc.MINOR_REPLY = None
    _mc.MINOR_CALLS.clear()
    _mc.EXPLICIT_REPLY = None
    _mc.EXPLICIT_CALLS.clear()
    _mc.PICTURE_REPLY = None
    _mc.PICTURE_CALLS.clear()
    _mc.PICTURE_EXPLICIT_REPLY = None
    _mc.PICTURE_EXPLICIT_CALLS.clear()

    # Rebind the engine + SessionLocal to this test's own SQLite file so tests are
    # truly isolated (the engine is module-bound at import; without this every test
    # in a process would share the first-resolved DB).
    from app import db as _db

    pg = os.environ.get("TEST_DATABASE_URL")
    if pg:
        # Postgres mode (CI job / local check): schema comes from the real
        # Alembic migrations once per run; every table is emptied per test.
        monkeypatch.setenv("DATABASE_URL", pg)
        _c.get_settings.cache_clear()
        _db.reset_engine(pg)
        global _PG_MIGRATED
        if not _PG_MIGRATED:
            _db.migrate()
            _PG_MIGRATED = True
        from sqlalchemy import text

        with _db.engine.begin() as conn:
            names = ", ".join(t.name for t in _db.Base.metadata.sorted_tables)
            conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
        return

    _db.reset_engine(f"sqlite:///{db}")


@pytest.fixture(autouse=True)
def _drain_side_jobs(_isolated_env):
    """Side jobs (clip checks) run on a background thread without Redis:
    finish them before this test's database goes away."""
    yield
    from app.workers.queue import wait_background

    wait_background(60)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_client(client):
    r = client.post("/api/auth/login", json={"password": "test-pw"})
    assert r.status_code == 200, r.text
    return client


def make_png_bytes(size=(64, 64), color=(200, 30, 90)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()

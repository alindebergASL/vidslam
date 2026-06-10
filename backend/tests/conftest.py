from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image


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
    # Reset cached singletons (settings + mock video + rate limiter).
    from app import config as _c
    from app.providers import mock_video as _mv
    from app.services import ratelimit as _rl

    _c.get_settings.cache_clear()
    _mv._singleton = None
    _rl._limiter = None

    # Rebind the engine + SessionLocal to this test's own SQLite file so tests are
    # truly isolated (the engine is module-bound at import; without this every test
    # in a process would share the first-resolved DB).
    from app import db as _db

    _db.reset_engine(f"sqlite:///{db}")


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

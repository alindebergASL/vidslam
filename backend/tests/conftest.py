from __future__ import annotations

import io
import os
import tempfile
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
    # Reset cached singletons (settings + mock video singleton).
    from app import config as _c
    from app.providers import mock_video as _mv

    _c.get_settings.cache_clear()
    _mv._singleton = None


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

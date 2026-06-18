"""Direct unit tests for the production-secrets boot guard.

We don't want to actually crash app import inside the test process, so we
call _enforce_production_secrets() directly with synthetic Settings instead
of monkeypatching env + reimporting main."""
from __future__ import annotations

import pytest

from app.config import Settings
from app.main import _enforce_production_secrets


def _settings(**overrides) -> Settings:
    base = dict(
        mock_providers=True,
        mvp_password="strong-real-password-123",
        session_secret="x" * 48,
        public_base_url="http://localhost:8000",
        frontend_origin="http://localhost:3000",
        database_url="sqlite:///./data/app.db",
        data_dir="./data",
    )
    base.update(overrides)
    return Settings(**base)


def test_dev_localhost_with_mock_providers_passes(monkeypatch):
    # We're running under pytest, so the guard short-circuits unconditionally —
    # remove the marker for these specific tests so the actual checks run.
    monkeypatch.delitem(__import__("sys").modules, "pytest", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    _enforce_production_secrets(_settings())  # no raise


def test_non_mock_with_dev_session_secret_refuses(monkeypatch):
    monkeypatch.delitem(__import__("sys").modules, "pytest", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with pytest.raises(RuntimeError, match="dev default"):
        _enforce_production_secrets(
            _settings(
                mock_providers=False,
                session_secret="dev-session-secret-still-here-32-chars-please",
            )
        )


def test_short_session_secret_refuses(monkeypatch):
    monkeypatch.delitem(__import__("sys").modules, "pytest", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with pytest.raises(RuntimeError, match=r"only \d+ chars"):
        _enforce_production_secrets(
            _settings(mock_providers=False, session_secret="tooshort")
        )


def test_default_mvp_password_refuses(monkeypatch):
    monkeypatch.delitem(__import__("sys").modules, "pytest", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with pytest.raises(RuntimeError, match="MVP_PASSWORD"):
        _enforce_production_secrets(
            _settings(mock_providers=False, mvp_password="changeme")
        )


def test_non_localhost_public_url_triggers_guard(monkeypatch):
    monkeypatch.delitem(__import__("sys").modules, "pytest", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    # mock_providers=True but the URL is public — still gated.
    with pytest.raises(RuntimeError, match="MVP_PASSWORD"):
        _enforce_production_secrets(
            _settings(
                mock_providers=True,
                public_base_url="https://avs.example.com",
                mvp_password="test-pw",
            )
        )


def test_allow_dev_secrets_env_var_lets_through(monkeypatch):
    monkeypatch.delitem(__import__("sys").modules, "pytest", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("ALLOW_DEV_SECRETS", "true")
    # No raise even though everything is broken.
    _enforce_production_secrets(
        _settings(
            mock_providers=False,
            session_secret="dev-session-secret-still",
            mvp_password="changeme",
        )
    )


def test_running_under_pytest_skips_guard():
    # `pytest` is in sys.modules because we are pytest. Guard should no-op
    # even with a broken settings object.
    _enforce_production_secrets(
        _settings(
            mock_providers=False,
            session_secret="dev-session-secret-still",
            mvp_password="changeme",
        )
    )

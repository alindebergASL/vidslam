"""Unit tests for the production-secrets boot guard.

We exercise `_check_production_secrets` (pure function — returns problem
list) rather than `_enforce_production_secrets` (which has the
skip-under-pytest gate + raises). The wire-up between the two is trivial
and the boot path is exercised by the deploy walkthrough."""
from __future__ import annotations


def _settings(**overrides):
    from app.config import Settings

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


def _check(s):
    from app.main import _check_production_secrets

    return _check_production_secrets(s)


def test_dev_localhost_with_mock_providers_returns_no_problems():
    assert _check(_settings()) == []


def test_non_mock_with_dev_session_secret_flags_dev_default():
    problems = _check(
        _settings(
            mock_providers=False,
            session_secret="dev-session-secret-still-here-32-chars-please",
        )
    )
    assert any("dev default" in p for p in problems), problems


def test_short_session_secret_flags_length():
    problems = _check(_settings(mock_providers=False, session_secret="tooshort"))
    assert any("only" in p and "chars" in p for p in problems), problems


def test_default_mvp_password_flags_known_default():
    problems = _check(_settings(mock_providers=False, mvp_password="changeme"))
    assert any("MVP_PASSWORD" in p for p in problems), problems


def test_non_localhost_public_url_triggers_check_even_in_mock_mode():
    # Public URL + mock providers still counts as "looks prod" because the
    # app is reachable from outside localhost, so weak secrets are dangerous.
    problems = _check(
        _settings(
            mock_providers=True,
            public_base_url="https://avs.example.com",
            mvp_password="test-pw",
        )
    )
    assert any("MVP_PASSWORD" in p for p in problems), problems


def test_localhost_url_with_mock_providers_skips_all_checks():
    # The only "safe" combination — fully local dev — must not return any
    # problems even with weak secrets, so `make dev` works out of the box.
    assert _check(
        _settings(
            mock_providers=True,
            public_base_url="http://localhost:8000",
            session_secret="dev-session-secret-short",
            mvp_password="changeme",
        )
    ) == []


def test_127_loopback_url_also_treated_as_local():
    assert _check(
        _settings(
            mock_providers=True,
            public_base_url="http://127.0.0.1:8000",
            mvp_password="changeme",
        )
    ) == []

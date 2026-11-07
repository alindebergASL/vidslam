"""Provider API keys the user can set in Settings → API keys.

A key entered in the app is stored encrypted (``secret_box``, keyed by
SESSION_SECRET) in ``app_settings`` under ``secret.<name>`` and wins over the
server's .env value. Every place that needs a provider key calls
:func:`resolve` / :func:`has`, so a key saved through the web process is
picked up by the RQ worker too (within the cache TTL).

Values never leave the backend: :func:`status` reports presence, source and
the last four characters only. Session secret, MVP password, webhook secrets,
YouTube client secret and the alert webhook stay env-only on purpose.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from ..config import get_settings
from . import secret_box

log = logging.getLogger("avs.provider_keys")

SETTING_PREFIX = "secret."
CACHE_TTL_SECONDS = 15.0
MIN_LEN = 8
MAX_LEN = 500


@dataclass(frozen=True)
class KeySpec:
    name: str
    label: str
    env_var: str
    field: str  # attribute on config.Settings
    powers: str
    docs_url: str


KEYS: dict[str, KeySpec] = {
    k.name: k
    for k in (
        KeySpec("openrouter", "OpenRouter", "OPENROUTER_API_KEY", "openrouter_api_key",
                "Storyboard chat, images, video, lip-sync, OpenRouter voices and Lyria music",
                "https://openrouter.ai/settings/keys"),
        KeySpec("elevenlabs", "ElevenLabs", "ELEVENLABS_API_KEY", "elevenlabs_api_key",
                "Voices (TTS), music, sound effects and voice cloning",
                "https://elevenlabs.io/app/settings/api-keys"),
        KeySpec("gemini", "Google Gemini", "GEMINI_API_KEY", "gemini_api_key",
                "Lyria 3.5 music direct from Google",
                "https://aistudio.google.com/apikey"),
        KeySpec("replicate", "Replicate", "REPLICATE_API_TOKEN", "replicate_api_token",
                "Character and style LoRA training",
                "https://replicate.com/account/api-tokens"),
        KeySpec("sogni", "Sogni", "SOGNI_API_KEY", "sogni_api_key",
                "Sogni image and video models (sogni/…)",
                "https://dashboard.sogni.ai/api-keys"),
    )
}

_lock = threading.Lock()
# (engine id, session secret, name) -> (expires_at, value, source, record)
_cache: dict[tuple, tuple[float, str, str, Optional[dict]]] = {}
_warned_unreadable: set[tuple[str, str]] = set()


def _spec(name: str) -> KeySpec:
    try:
        return KEYS[name]
    except KeyError:
        raise KeyError(f"unknown provider key {name!r}") from None


def _setting_key(name: str) -> str:
    return SETTING_PREFIX + name


def _cache_key(name: str) -> tuple:
    from .. import db as _db

    # The engine id keeps per-test databases (and reset_engine) apart; the
    # session secret makes a rotation re-open the sealed value.
    eng = _db.engine
    return (id(eng), str(eng.url), get_settings().session_secret, name)


def invalidate(name: Optional[str] = None) -> None:
    """Drop cached values (all, or one key). Called on every write; other
    processes pick up changes when their TTL expires."""
    with _lock:
        if name is None:
            _cache.clear()
        else:
            for k in [k for k in _cache if k[-1] == name]:
                _cache.pop(k, None)


def _stored(name: str) -> tuple[str, str, Optional[dict]]:
    """(value, source, record) for the app-stored key: source is "app",
    "unreadable" or "none". Cached for CACHE_TTL_SECONDS."""
    ck = _cache_key(name)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(ck)
        if hit and hit[0] > now:
            return hit[1], hit[2], hit[3]
    from .model_registry import get_setting

    rec = get_setting(_setting_key(name))
    value, source = "", "none"
    if rec and rec.get("sealed"):
        opened = secret_box.open_(str(rec["sealed"]))
        if opened:
            value, source = opened, "app"
        else:
            source = "unreadable"
            marker = (name, str(rec.get("updated_at") or ""))
            if marker not in _warned_unreadable:
                _warned_unreadable.add(marker)
                log.warning(
                    "stored %s API key can't be decrypted (SESSION_SECRET changed?); "
                    "falling back to the environment. Re-enter it in Settings → API keys.",
                    name,
                )
    with _lock:
        _cache[ck] = (now + CACHE_TTL_SECONDS, value, source, rec)
    return value, source, rec


def _env(name: str) -> str:
    return (getattr(get_settings(), _spec(name).field, "") or "").strip()


def resolve(name: str) -> str:
    """The key to use: app-stored value, else the env value, else ""."""
    _spec(name)
    value, _source, _rec = _stored(name)
    return value or _env(name)


def has(name: str) -> bool:
    return bool(resolve(name))


def _row(name: str) -> dict:
    spec = _spec(name)
    value, source, rec = _stored(name)
    env = _env(name)
    row = {
        "name": spec.name,
        "label": spec.label,
        "env_var": spec.env_var,
        "powers": spec.powers,
        "docs_url": spec.docs_url,
        "set": False,
        "source": "none",
        "last4": None,
        "updated_at": None,
    }
    if source == "app" and value:
        row.update(set=True, source="app", last4=(rec or {}).get("last4") or value[-4:],
                   updated_at=(rec or {}).get("updated_at"))
    elif source == "unreadable":
        # The stored key can't be used; say so even when env covers it, so
        # the UI can ask the user to re-enter it.
        row.update(set=bool(env), source="unreadable", updated_at=(rec or {}).get("updated_at"),
                   last4=env[-4:] if env else None)
    elif env:
        row.update(set=True, source="env", last4=env[-4:])
    active = value if source == "app" and value else env
    row["warning"] = key_warning(name, active) if row["set"] and active else None
    return row


SOGNI_WORKER_PREFIX = "sogni_wkr_"
SOGNI_WORKER_WARNING = ("This is a Sogni worker key; workflows need a developer key "
                        "(sogni_dev_…) or the account's original key.")


def key_warning(name: str, value: str) -> str | None:
    """A problem we can see from the key's shape alone. Never echoes it."""
    if name == "sogni" and value.startswith(SOGNI_WORKER_PREFIX):
        return SOGNI_WORKER_WARNING
    return None


def status() -> list[dict]:
    return [_row(n) for n in KEYS]


def row(name: str) -> dict:
    return _row(name)


def validate_value(value: object) -> str:
    """Normalise a submitted key; raises ValueError with a message that never
    contains the value."""
    if not isinstance(value, str):
        raise ValueError("value must be a string")
    v = value.strip()
    if len(v) < MIN_LEN or len(v) > MAX_LEN:
        raise ValueError(f"key must be {MIN_LEN}–{MAX_LEN} characters")
    if any(c.isspace() for c in v):
        raise ValueError("key must not contain spaces or line breaks")
    if not v.isprintable():
        raise ValueError("key contains control characters")
    return v


def _after_change(name: str) -> None:
    invalidate(name)
    if name == "openrouter":
        try:
            from . import spend

            cache = getattr(spend, "_account_cache", None)
            if isinstance(cache, dict):
                cache.clear()
        except Exception:  # noqa: BLE001
            pass


def set_key(name: str, value: str) -> dict:
    _spec(name)
    v = validate_value(value)
    from .model_registry import put_setting

    put_setting(_setting_key(name), {
        "sealed": secret_box.seal(v),
        "last4": v[-4:],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    _after_change(name)
    log.info("provider key %s saved in app", name)
    if key_warning(name, v):
        log.warning("provider key %s saved in app looks like a worker key; workflows will be refused", name)
    return _row(name)


def clear_key(name: str) -> dict:
    """Remove the app-stored key; the env value (if any) applies again."""
    _spec(name)
    from .model_registry import put_setting

    put_setting(_setting_key(name), None)
    _after_change(name)
    log.info("provider key %s cleared from app", name)
    return _row(name)

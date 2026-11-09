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
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator, Optional

from ..config import get_settings
from . import secret_box

log = logging.getLogger("avs.provider_keys")

SETTING_PREFIX = "secret."
ENABLED_PREFIX = "provider."  # provider.<name> = {"enabled": bool, "updated_at": ...}
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
    # On/off in Settings → API keys until switched: True (on), or None (on
    # once a key is set: a provider the user adds later).
    default_enabled: Optional[bool] = True


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
                "https://replicate.com/account/api-tokens", default_enabled=None),
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
            _enabled_cache.clear()
        else:
            for k in [k for k in _cache if k[-1] == name]:
                _cache.pop(k, None)
            for k in [k for k in _enabled_cache if k[-1] == name]:
                _enabled_cache.pop(k, None)


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
    """A key is set (app or env). Says nothing about the on/off switch."""
    return bool(resolve(name))


# ---------- on/off ----------

_enabled_cache: dict[tuple, tuple[float, Optional[bool]]] = {}  # unused (kept for invalidate)
# Set while finishing work already under way (the next loop segment, a
# scene's music bed after its video): a provider switched off meanwhile
# still serves it. New work is refused.
_continuing: ContextVar[bool] = ContextVar("provider_continuing", default=False)


class ProviderUnavailable(Exception):
    """A call needs a provider that is switched off (or has no key and must
    not be faked). Raised before any provider request; the API answers 409."""

    def __init__(self, provider: str, *, kind: str = "", model: str = "", reason: str = "off") -> None:
        self.provider, self.kind, self.model, self.reason = provider, kind, model, reason
        label = KEYS[provider].label if provider in KEYS else provider.title()
        if reason == "off":
            msg = f"{label} is turned off in Settings → API keys"
        elif reason == "unsupported":
            msg = f"{label} models aren't supported by this version of the app"
        else:
            msg = f"{label} isn't set up yet (no API key in Settings → API keys)"
        super().__init__(msg + (f" ({model})" if model else "") + ".")


def explicit_enabled(name: str) -> Optional[bool]:
    """The switch as the user set it, or None when following the default.
    Read every time (one small row): a flip applies at once in every web
    worker and the job worker, not after a cache TTL."""
    _spec(name)
    from .model_registry import get_setting

    rec = get_setting(ENABLED_PREFIX + name)
    return bool(rec["enabled"]) if isinstance(rec, dict) and "enabled" in rec else None


def enabled(name: str) -> bool:
    """Whether the user has this provider on (the switch, else its default)."""
    spec = _spec(name)
    exp = explicit_enabled(name)
    if exp is not None:
        return exp
    return has(name) if spec.default_enabled is None else bool(spec.default_enabled)


def usable(name: str) -> bool:
    """On, and a key is set: its models are offered."""
    return has(name) and enabled(name)


def serves(name: str) -> bool:
    """On, or finishing work already under way (see continuing)."""
    return enabled(name) or _continuing.get()


def in_continuation() -> bool:
    return _continuing.get()


@contextmanager
def continuing() -> Iterator[None]:
    token = _continuing.set(True)
    try:
        yield
    finally:
        _continuing.reset(token)


def set_enabled(name: str, value: Optional[bool]) -> dict:
    """Switch a provider on/off; None goes back to its default."""
    _spec(name)
    from .model_registry import put_setting

    put_setting(ENABLED_PREFIX + name, None if value is None else {
        "enabled": bool(value), "updated_at": datetime.now(timezone.utc).isoformat()})
    invalidate(name)
    log.info("provider %s switched %s", name, "to its default" if value is None else ("on" if value else "off"))
    return _row(name)


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
    exp = explicit_enabled(name)
    row["enabled"] = enabled(name)
    row["enabled_default"] = spec.default_enabled  # True, or None: on once a key is set
    row["enabled_explicit"] = exp is not None
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

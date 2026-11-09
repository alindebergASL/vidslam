"""Which model is active for each capability, and which provider serves it.

Selections live in the `app_settings` table (key "model.<kind>") so they can
be switched from the Settings page without a redeploy; each kind falls back
to its .env default. Every generation path reads the active model at call
time, so a switch applies to the next job.

Model id conventions per kind:
  chat · image · video   OpenRouter model ids ("anthropic/claude-…");
                         image/video also "sogni/<selector>" (Sogni).
  tts                    "elevenlabs" (ElevenLabs voices) or an OpenRouter
                         audio-output model ("openai/gpt-audio-mini").
  music                  "elevenlabs" or a Google Lyria model
                         ("google/lyria-3.5", "google/lyria-3-clip-preview").
  lipsync                "" (off) or an OpenRouter avatar model
                         ("heygen/avatar-iv").
"""
from __future__ import annotations

from typing import Literal, Optional

from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError

from .. import models
from ..config import get_settings
from ..db import SessionLocal

Kind = Literal["chat", "image", "video", "tts", "music", "lipsync"]
KINDS: tuple[str, ...] = ("chat", "image", "video", "tts", "music", "lipsync")

MOCK_DEFAULTS = {
    "chat": "mock/chat-default",
    "image": "mock/image-default",
    "video": "mock/video-default",
    "tts": "elevenlabs",
    "music": "elevenlabs",
    "lipsync": "",
}

LYRIA_PREFIX = "google/lyria"
# Sogni image/video models: "sogni/<selector>" (providers/sogni.py).
SOGNI_PREFIX = "sogni/"
# Lyria models OpenRouter serves (openrouter.ai/google/lyria-3-*), used when
# the catalog hasn't been polled yet.
OPENROUTER_LYRIA = {"google/lyria-3-pro-preview", "google/lyria-3-clip-preview"}


def _catalog_provider(kind: str, model: str) -> Optional[str]:
    snap = get_setting("catalog.snapshot") or {}
    for e in snap.get("entries") or []:
        if e.get("kind") == kind and e.get("id") == model:
            return e.get("provider")
    return None


def env_default(kind: str) -> str:
    s = get_settings()
    return {
        "chat": s.openrouter_chat_model,
        "image": s.openrouter_image_model,
        "video": s.openrouter_video_model,
        "tts": "elevenlabs",
        "music": "elevenlabs",
        "lipsync": "",
    }[kind] or ""


def _key(kind: str) -> str:
    return f"model.{kind}"


def get_setting(key: str) -> Optional[dict]:
    db = SessionLocal()
    try:
        row = db.get(models.AppSetting, key)
        return row.value if row else None
    except (OperationalError, ProgrammingError):
        # Table not created yet (fresh DB before migrations / init_db):
        # behave as "no selection" so env defaults apply.
        return None
    finally:
        db.close()


def put_setting(key: str, value: Optional[dict]) -> None:
    db = SessionLocal()
    try:
        row = db.get(models.AppSetting, key)
        if value is None:
            if row is not None:
                db.delete(row)
        elif row is None:
            db.add(models.AppSetting(key=key, value=value))
        else:
            row.value = value
        try:
            db.commit()
        except IntegrityError:
            # Another process (e.g. a second web worker's catalog poller)
            # inserted the same key between our read and our insert: the row
            # exists now, so update it instead.
            db.rollback()
            row = db.get(models.AppSetting, key)
            if row is None:
                raise
            row.value = value
            db.commit()
    finally:
        db.close()


def selected(kind: str) -> Optional[str]:
    """The user's explicit selection, or None when following the default."""
    v = get_setting(_key(kind))
    return (v or {}).get("model") if v else None


def active_model(kind: str) -> str:
    sel = selected(kind)
    if sel is not None:
        return sel
    return env_default(kind) or ("" if kind == "lipsync" else MOCK_DEFAULTS[kind])


def active_options(kind: str) -> dict:
    """Extra per-kind options saved with the selection (e.g. tts voice)."""
    v = get_setting(_key(kind)) or {}
    return {k: val for k, val in v.items() if k != "model"}


def set_active(kind: str, model: Optional[str], options: Optional[dict] = None) -> None:
    """Select `model` for `kind`; None resets to the .env default."""
    if kind not in KINDS:
        raise ValueError(f"unknown model kind {kind!r}")
    if model is None:
        put_setting(_key(kind), None)
        return
    value = {"model": model.strip()}
    value.update({k: v for k, v in (options or {}).items() if v not in (None, "")})
    put_setting(_key(kind), value)


# Providers whose models are picked per call by an id prefix.
ROUTED_PREFIXES = {SOGNI_PREFIX: "sogni", "runware/": "runware"}
# Providers coming in later releases: their ids are refused cleanly (never
# sent to OpenRouter as unknown ids), e.g. after a rollback.
RESERVED_PREFIXES = {"replicate/": "replicate"}


def route_info(kind: str, model: Optional[str] = None) -> tuple[str, Optional[str]]:
    """(provider, blocked) for `kind` with the active (or given) model.
    provider: "mock" | "openrouter" | "elevenlabs" | "gemini" | "sogni" |
    "off" (no lip-sync) | "unavailable"; blocked names the provider that
    would serve it but is switched off (or isn't supported/set up)."""
    from .provider_keys import enabled, has, in_continuation

    s = get_settings()
    m = active_model(kind) if model is None else model
    if kind == "lipsync" and not m:
        return "off", None
    if s.mock_providers:
        return "mock", None
    for prefix, name in RESERVED_PREFIXES.items():
        if m.startswith(prefix):
            return "unavailable", name

    def pick(name: str) -> tuple[str, Optional[str]]:
        # No key: the placeholder provider, as always. A key but switched
        # off: refused, never faked (work already running still finishes).
        if not has(name):
            return "mock", None
        return (name, None) if (enabled(name) or in_continuation()) else ("unavailable", name)

    if kind == "tts":
        return pick("elevenlabs" if (m == "elevenlabs" or not m) else "openrouter")
    if kind == "music":
        if m.startswith(LYRIA_PREFIX):
            # Lyria 3 Pro / Clip are on OpenRouter; Lyria 3.5 is Google-only.
            on_openrouter = _catalog_provider("music", m) == "openrouter" or m in OPENROUTER_LYRIA
            cands = [c for c in (["openrouter"] if on_openrouter else []) + ["gemini"] if has(c)]
            for c in cands:  # a provider that's on first, even mid-run
                if enabled(c):
                    return c, None
            if cands and in_continuation():
                return cands[0], None
            return ("unavailable", cands[0]) if cands else ("mock", None)
        return pick("elevenlabs")
    if kind in ("image", "video"):
        for prefix, name in ROUTED_PREFIXES.items():
            if m.startswith(prefix):
                # Served by its own provider whatever else is active. A
                # provider you add later (Runware) without its key isn't set
                # up: refused, never faked with placeholder output.
                from .provider_keys import KEYS

                if not has(name) and name in KEYS and KEYS[name].default_enabled is None:
                    return "unavailable", name
                return pick(name)
    if m.startswith("mock/"):
        return "mock", None
    return pick("openrouter")


def provider_for(kind: str, model: Optional[str] = None) -> str:
    """Provider that will serve `kind` with the active (or given) model:
    "mock" | "openrouter" | "elevenlabs" | "gemini" | "sogni" | "off" |
    "unavailable" (switched off: see route_info)."""
    return route_info(kind, model)[0]


def switched_off(kind: str, model: Optional[str] = None) -> bool:
    """Its provider is switched off (ignoring work already running): used to
    try other models first, e.g. in a still model chain."""
    from .provider_keys import _continuing

    token = _continuing.set(False)
    try:
        return route_info(kind, model)[0] == "unavailable"
    finally:
        _continuing.reset(token)


def unavailable(kind: str, model: Optional[str] = None, blocked: Optional[str] = None):
    """The ProviderUnavailable to raise for `kind` with `model`."""
    from .provider_keys import KEYS, ProviderUnavailable, has

    m = active_model(kind) if model is None else model
    if blocked in RESERVED_PREFIXES.values() and any(m.startswith(p) for p in RESERVED_PREFIXES):
        reason = "unsupported"
    else:
        reason = "off" if blocked in KEYS and has(blocked) else "no_key"
    return ProviderUnavailable(blocked or "provider", kind=kind, model=m, reason=reason)


def require(kind: str, model: Optional[str] = None) -> None:
    """Raise ProviderUnavailable when `kind` with `model` can't be served
    (before queueing paid work)."""
    provider, blocked = route_info(kind, model)
    if provider == "unavailable":
        raise unavailable(kind, model, blocked)


def snapshot() -> dict:
    """Active model + serving provider for every kind (for status/UI)."""
    out = {}
    for k in KINDS:
        m = active_model(k)
        out[k] = {
            "model": m,
            "provider": provider_for(k, m),
            "default": env_default(k) or ("" if k == "lipsync" else MOCK_DEFAULTS[k]),
            "is_default": selected(k) is None,
            "options": active_options(k),
        }
    return out

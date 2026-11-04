"""Which model is active for each capability, and which provider serves it.

Selections live in the `app_settings` table (key "model.<kind>") so they can
be switched from the Settings page without a redeploy; each kind falls back
to its .env default. Every generation path reads the active model at call
time, so a switch applies to the next job.

Model id conventions per kind:
  chat · image · video   OpenRouter model ids ("anthropic/claude-…").
  tts                    "elevenlabs" (ElevenLabs voices) or an OpenRouter
                         audio-output model ("openai/gpt-audio-mini").
  music                  "elevenlabs" or a Google Lyria model
                         ("google/lyria-3.5", "google/lyria-3-clip-preview").
  lipsync                "" (off) or an OpenRouter avatar model
                         ("heygen/avatar-iv").
"""
from __future__ import annotations

from typing import Literal, Optional

from sqlalchemy.exc import OperationalError, ProgrammingError

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


def provider_for(kind: str, model: Optional[str] = None) -> str:
    """Provider that will serve `kind` with the active (or given) model:
    "mock" | "openrouter" | "elevenlabs" | "gemini" | "off"."""
    s = get_settings()
    m = active_model(kind) if model is None else model
    if kind == "lipsync" and not m:
        return "off"
    if s.mock_providers:
        return "mock"
    if kind == "tts":
        if m == "elevenlabs" or not m:
            return "elevenlabs" if s.elevenlabs_api_key else "mock"
        return "openrouter" if s.openrouter_api_key else "mock"
    if kind == "music":
        if m.startswith(LYRIA_PREFIX):
            return "gemini" if s.gemini_api_key else "mock"
        return "elevenlabs" if s.elevenlabs_api_key else "mock"
    if m.startswith("mock/"):
        return "mock"
    return "openrouter" if s.openrouter_api_key else "mock"


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

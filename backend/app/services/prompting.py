"""Prompting helpers: default negative ("avoid") lists, saved prompt
presets, and the planner-model "enhance my prompt" call.

Stored in app_settings:
  prompt.negative_defaults  {"image": str, "video": str}  (unset → built-ins)
  prompt.presets            {"items": [preset, ...]}       (newest first, cap 200)
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("avs.prompting")

NEGATIVE_KEY = "prompt.negative_defaults"
PRESETS_KEY = "prompt.presets"
NEGATIVE_MAX = 1000
PRESETS_CAP = 200
KINDS = ("image", "video")
ENHANCE_KINDS = ("image", "video", "script", "scene")

# No "text"/"logo": users ask for signs and end cards on purpose.
BUILTIN_NEGATIVES: dict[str, str] = {
    "image": ("malformed hands, extra fingers, missing fingers, bad anatomy, distorted face, deformed, "
              "low quality, worst quality, blurry, jpeg artifacts, watermark, oversaturated"),
    "video": ("slow motion, slo-mo, jittery motion, warping, morphing, flicker, malformed hands, "
              "bad anatomy, distorted face, low quality, jpeg artifacts, watermark"),
}


class PresetLimitError(Exception):
    """The preset list is full."""


# ---------- negative defaults ----------


def _get(key: str) -> Optional[dict]:
    from . import model_registry

    try:
        return model_registry.get_setting(key)
    except Exception:  # noqa: BLE001 — a settings read never fails a job
        log.warning("could not read %s; using built-ins", key)
        return None


def _put(key: str, value: Optional[dict]) -> None:
    from . import model_registry

    model_registry.put_setting(key, value)


def negative_defaults() -> dict[str, str]:
    """The default avoid lists, {"image": str, "video": str}: saved values
    where set (an empty string is a deliberate "none"), else the built-ins."""
    saved = _get(NEGATIVE_KEY) or {}
    out: dict[str, str] = {}
    for kind in KINDS:
        v = saved.get(kind) if isinstance(saved, dict) else None
        out[kind] = v.strip()[:NEGATIVE_MAX] if isinstance(v, str) else BUILTIN_NEGATIVES[kind]
    return out


def set_negative_defaults(image: str, video: str) -> dict[str, str]:
    _put(NEGATIVE_KEY, {"image": (image or "").strip()[:NEGATIVE_MAX],
                        "video": (video or "").strip()[:NEGATIVE_MAX]})
    return negative_defaults()


def reset_negative_defaults() -> dict[str, str]:
    """Forget the saved lists: the built-ins apply again (and follow any
    future change to them, unlike a saved copy)."""
    _put(NEGATIVE_KEY, None)
    return negative_defaults()


def _terms(text: str) -> list[str]:
    return [t for t in (re.sub(r"\s+", " ", p).strip() for p in re.split(r"[,\n;]", text or "")) if t]


def effective_negative(kind: str, user: str = "") -> str:
    """The user's avoid text, then the defaults for `kind` ("image"/"video"),
    comma-joined, de-duplicated case-insensitively, trimmed to 1000 chars
    (whole terms where possible). An unknown kind adds no defaults. Never raises."""
    try:
        defaults = negative_defaults().get(kind, "") if kind in KINDS else ""
    except Exception:  # noqa: BLE001
        defaults = ""
    seen: set[str] = set()
    out = ""
    for term in _terms(user) + _terms(defaults):
        k = term.casefold()
        if k in seen:
            continue
        seen.add(k)
        candidate = f"{out}, {term}" if out else term
        if len(candidate) > NEGATIVE_MAX:
            if not out:  # a single over-long term: cut it
                out = term[:NEGATIVE_MAX].rstrip()
            break
        out = candidate
    return out


# ---------- presets ----------

_PRESET_FIELDS = ("name", "kind", "prompt", "negative_prompt", "model", "aspect_ratio")


def list_presets() -> list[dict]:
    saved = _get(PRESETS_KEY) or {}
    items = saved.get("items") if isinstance(saved, dict) else None
    return [dict(p) for p in (items or []) if isinstance(p, dict) and p.get("id")]


def _save(items: list[dict]) -> None:
    _put(PRESETS_KEY, {"items": items})


def _clean(data: dict) -> dict:
    return {k: data.get(k, "") for k in _PRESET_FIELDS}


def create_preset(data: dict) -> dict:
    items = list_presets()
    if len(items) >= PRESETS_CAP:
        raise PresetLimitError(f"You have {PRESETS_CAP} presets; delete one to save another.")
    preset = {"id": uuid.uuid4().hex, **_clean(data),
              "created_at": datetime.now(timezone.utc).isoformat()}
    _save([preset, *items])
    return preset


def get_preset(preset_id: str) -> Optional[dict]:
    return next((p for p in list_presets() if p["id"] == preset_id), None)


def update_preset(preset_id: str, data: dict) -> Optional[dict]:
    items = list_presets()
    for i, p in enumerate(items):
        if p["id"] == preset_id:
            items[i] = {**p, **_clean(data), "id": p["id"], "created_at": p.get("created_at", "")}
            _save(items)
            return items[i]
    return None


def delete_preset(preset_id: str) -> bool:
    items = list_presets()
    kept = [p for p in items if p["id"] != preset_id]
    if len(kept) == len(items):
        return False
    _save(kept)
    return True


# ---------- enhance ----------

ENHANCE_MODEL_KEY = "prompting.enhance_model"  # {"model": str} ("" = the planner)
ENHANCE_MODEL_MAX = 200
_MODEL_ID = re.compile(r"^~?[A-Za-z0-9][\w.\-]*/[\w.:@\-]+$")

# Offered in Settings → Prompting (any OpenRouter chat model id also works).
ENHANCE_CHOICES = (
    {"id": "deepseek/deepseek-v4-flash", "label": "DeepSeek V4 Flash",
     "note": "Fast and nearly free. Rewrote every adult prompt in a test where the planner refused or toned them down."},
    {"id": "deepseek/deepseek-v4-pro", "label": "DeepSeek V4 Pro",
     "note": "Keeps the most detail; slower (up to about 45 s)."},
    {"id": "", "label": "The planner model",
     "note": "The storyboard planner (Settings → Models). It may refuse or tone down adult prompts; "
             "a refusal is retried with DeepSeek V4 Flash."},
)

# A reply that declines instead of rewriting ("I can't write a prompt for
# explicit sexual content, but here's a version…"): never offered as the
# user's prompt.
_DECLINE_VERBS = (r"(?:help(?!\s+but)|assist|write|create|provide|generate|produce|fulfill|comply|"
                  r"do that|make (?:that|this))")
_REFUSAL = re.compile(
    r"\bi(?:\s+(?:can[’']?t|cannot|won[’']?t|will not|am (?:not able|unable))|[’']m (?:not able|unable))"
    rf"\s+(?:to\s+)?{_DECLINE_VERBS}"
    r"|\bi must decline|\bagainst (?:my|our|the) (?:guidelines|polic(?:y|ies)|content polic\w*)"
    r"|\bnon-explicit version|\ba (?:safer|tamer|more tasteful|non-explicit) version",
    re.I,
)


class EnhanceRefused(RuntimeError):
    """The model declined to rewrite the prompt (the user's text stands)."""

    def __init__(self, model: str) -> None:
        super().__init__(f"{model} declined to rewrite this prompt")
        self.model = model


def looks_refused(text: str) -> bool:
    """The reply declines (judged on its opening, where models say so)."""
    return bool(_REFUSAL.search(" ".join((text or "").split())[:400]))


def enhance_model() -> str:
    """The model "✨ Enhance" uses: as set in Settings → Prompting, else
    PROMPT_ENHANCE_MODEL. "" = the planner (Settings → Models)."""
    saved = _get(ENHANCE_MODEL_KEY)
    if isinstance(saved, dict) and isinstance(saved.get("model"), str):
        return saved["model"]
    from ..config import get_settings

    return get_settings().prompt_enhance_model or ""


def set_enhance_model(model: Optional[str]) -> dict:
    """Choose the enhance model ("" = the planner); None = back to the default."""
    if model is not None:
        model = model.strip()
        if len(model) > ENHANCE_MODEL_MAX or (model and not _MODEL_ID.match(model)):
            raise ValueError("model must be an OpenRouter model id like deepseek/deepseek-v4-flash, or empty")
    _put(ENHANCE_MODEL_KEY, None if model is None else {"model": model})
    return enhance_settings()


def enhance_settings() -> dict:
    from ..config import get_settings
    from . import model_registry

    return {
        "model": enhance_model(),
        "default": get_settings().prompt_enhance_model or "",
        "planner": model_registry.active_model("chat"),
        "choices": [dict(c) for c in ENHANCE_CHOICES],
    }


def _chat_for(model: str):
    """The chat provider for an enhance model ("" = the planner), routed like
    any chat call: placeholder in mock mode or without a key, refused (409)
    when OpenRouter is switched off."""
    from ..providers import get_chat

    if not model:
        return get_chat()
    from . import model_registry

    provider, blocked = model_registry.route_info("chat", model)
    if provider == "unavailable":
        raise model_registry.unavailable("chat", model, blocked)
    if provider == "mock":
        from ..providers.mock_chat import MockChatProvider

        return MockChatProvider()
    from ..providers.openrouter_chat import OpenRouterChatProvider

    return OpenRouterChatProvider(model=model)


def enhance(text: str, kind: str, model: str = "") -> dict:
    """A better prompt from the enhance model: {"text", "model"} where model
    is the one that wrote it. `model` is the target generation model (for a
    model-family hint). A reply that declines is retried once with the
    default enhance model, then refused (EnhanceRefused): it is never handed
    back as the user's prompt. Prompts involving minors are refused before
    any call (safety.UnsafePromptError), and so is such a rewrite."""
    if kind not in ENHANCE_KINDS:
        raise ValueError(f"unknown prompt kind {kind!r}")
    from ..config import get_settings
    from . import model_registry
    from .safety import check_prompt

    check_prompt(text)

    def run(m: str) -> tuple[str, str]:
        chat = _chat_for(m)
        out = chat.enhance_prompt(text, kind, model or "")
        return out, getattr(chat, "model", "") or m or model_registry.active_model("chat")

    out, used = run(enhance_model())
    if looks_refused(out):
        fallback = get_settings().prompt_enhance_model or ""
        if fallback and fallback != used:
            log.info("enhance: %s declined; retrying with %s", used, fallback)
            out, used = run(fallback)
        if looks_refused(out):
            raise EnhanceRefused(used)
    check_prompt(out)
    return {"text": out, "model": used}

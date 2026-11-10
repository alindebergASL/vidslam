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

# What the enhance instructions (openrouter_chat._ENHANCE_COMMON) tell a
# model to reply when it won't rewrite: exactly one of these words.
DECLINED = "DECLINED"
REFUSED_MINOR = "REFUSED_MINOR"

# A model that ignores that instruction declines in words instead. Judged on
# the reply's opening only, and never when the user's own text says the
# same thing (a script's "I can't help it…" hook).
_DECLINE_VERBS = (r"(?:help(?!\s+(?:but|it)\b)|assist|write|create|provide|generate|produce|fulfil+|comply|"
                  r"rewrite|improve|enhance|edit|tighten|describe|depict|include|do (?:that|this|it))")
_REFUSAL_START = re.compile(
    r"^\W*(?:(?:i[’']?m\s+)?sorry[,.!]?\s*(?:but\s+)?|unfortunately[,]?\s*)?"
    r"i(?:\s+(?:can[’']?t|cannot|won[’']?t(?:\s+be able to)?|will not|am (?:not able|unable)|don[’']?t|do not)"
    r"|[’']m (?:not able|unable|not comfortable))"
    rf"\s+(?:to\s+)?{_DECLINE_VERBS}\b"
    r"|^\W*i must decline",
    re.I,
)
_REFUSAL_ANYWHERE = re.compile(
    r"\bagainst (?:my|our|the) (?:guidelines|polic(?:y|ies)|content polic\w*)"
    r"|\b(?:a |an )?(?:non-explicit|tasteful|toned[- ]down|non-graphic|safer|tamer|more tasteful|suggestive) "
    r"(?:version|alternative|take)\b",
    re.I,
)
_MINOR_WORDS = re.compile(r"\b(?:minors?|child(?:ren|like)?|under-?age|teen(?:ager)?s?|young-looking|ages?)\b", re.I)


class EnhanceRefused(RuntimeError):
    """The rewrite didn't happen: the model declined ("declined"), the model
    judged it to sexualise a minor ("minor"), or the rewrite failed the
    minors check ("unsafe_rewrite"). The user's text stands."""

    def __init__(self, model: str, reason: str, adult: bool) -> None:
        super().__init__(f"{model}: {reason}")
        self.model = model
        self.reason = reason
        self.adult = adult


def refusal(out: str, text: str = "") -> Optional[str]:
    """None for a rewrite; "minor" or "declined" for a reply that refuses."""
    reply = " ".join((out or "").split())
    head = reply.strip("\"'`*_.: ").upper()
    if head.startswith(REFUSED_MINOR):
        return "minor"
    if head.startswith(DECLINED):
        return "declined"
    opening = reply[:300]
    mine = " ".join((text or "").split()).lower()
    for pat in (_REFUSAL_START, _REFUSAL_ANYWHERE):
        m = pat.search(opening)
        if m and m.group(0).strip().lower() not in mine:
            return "minor" if _MINOR_WORDS.search(reply[:400]) else "declined"
    return None


def looks_refused(text: str) -> bool:
    return refusal(text) is not None


def _check(kind: str, text: str) -> None:
    """The check generation applies to this kind: the script check for
    scripts (what POST /generate-plan runs), the prompt check otherwise."""
    from .safety import check_prompt, validate_script

    (validate_script if kind == "script" else check_prompt)(text)


def _chat_for(model: str):
    """The chat provider for a model ("" = get_chat(): the planner, or the
    adult text model in adult mode), routed like any chat call."""
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
    """A better prompt from the text model (the planner; DeepSeek in adult
    mode): {"text", "model"} with the model that wrote it. `model` is the
    target generation model (for a model-family hint).

    Never returns a refusal as the prompt. In adult mode a model that
    declines is retried once with ADULT_TEXT_FALLBACK_MODEL; outside adult
    mode declining is the intended answer. A prompt sexualising a minor is
    refused before any call (safety.UnsafePromptError), never retried, and a
    rewrite that fails the check is refused too (EnhanceRefused)."""
    if kind not in ENHANCE_KINDS:
        raise ValueError(f"unknown prompt kind {kind!r}")
    from ..config import get_settings
    from . import content_mode, model_registry

    _check(kind, text)
    adult = content_mode.adult_mode()

    def run(m: str) -> tuple[str, str]:
        chat = _chat_for(m)
        out = chat.enhance_prompt(text, kind, model or "")
        return out, getattr(chat, "model", "") or m or model_registry.active_model("chat")

    out, used = run("")
    why = refusal(out, text)
    if why == "declined" and adult:
        fallback = get_settings().adult_text_fallback_model or ""
        if fallback and fallback != used:
            log.info("enhance: %s declined; retrying with %s", used, fallback)
            out, used = run(fallback)
            why = refusal(out, text)
    if why:
        raise EnhanceRefused(used, why, adult)
    try:
        _check(kind, out)
    except ValueError:
        raise EnhanceRefused(used, "unsafe_rewrite", adult) from None
    return {"text": out, "model": used}

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
# the reply's opening only, and never when the user's own text opens the
# same way (a script's "I can't help it…" hook).
_DECLINE_VERBS = (r"(?:help(?!\s+(?:but|it)\b)|assist|write|create|provide|generate|produce|fulfil+|comply|"
                  r"rewrite|improve|enhance|edit|tighten|describe|depict|include|continue|do (?:that|this|it))")
_LEAD_IN = (r"(?:i[’']m sorry|i am sorry|sorry|i apologi[sz]e|apologies|my apologies|"
            r"i (?:understand|appreciate|see)[^.!?\n]{0,80}?|unfortunately|as an ai(?: (?:model|assistant))?|however)")
_REFUSAL_START = re.compile(
    rf"^\W*(?:{_LEAD_IN}[\s,;:!.—–-]*(?:but\s+)?){{0,2}}"
    r"(?:i(?:\s+(?:can[’']?t|can not|cannot|won[’']?t(?:\s+be able to)?|will not|"
    r"am (?:not able|unable|not going|not comfortable)|don[’']?t(?:\s+feel comfortable)?|do not|would rather not)"
    r"|[’']m (?:not able|unable|not going|not comfortable))"
    rf"\s+(?:to\s+)?(?:\w+\s+)?{_DECLINE_VERBS}\b"
    r"|i must decline"
    r"|(?:that|this)(?:[’']s| is) not something i (?:can|will|am able to)"
    r"|(?:this|that|the|your) (?:request|prompt)[^.!?\n]{0,40}\bagainst (?:my|our|the) "
    r"(?:guidelines|polic(?:y|ies)|content polic\w*))",
    re.I,
)
# A sentinel reply: the word alone, perhaps with a short reason after it.
_SENTINEL = re.compile(rf"^\W*({DECLINED}|{REFUSED_MINOR})\b(?:\W*$|\s*[:—–-]\s*.{{0,240}}$)", re.S)
# The refusal names a minor (only then is it a minors refusal).
_MINOR_REASON = re.compile(r"\b(?:minors?|child(?:ren|like)?|under-?age|teen(?:ager)?s?|young-looking|"
                           r"under\s+18|underaged)\b", re.I)


def _norm(t: str) -> str:
    t = (t or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return " ".join(re.sub(r"^\W+", "", t).split()).lower()


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
    """None for a rewrite; "minor" or "declined" for a reply that refuses.
    "minor" only when the refusal itself names a minor or the user's text
    mentions one (then no other model is asked)."""
    from .safety import mentions_minor

    reply = " ".join((out or "").split())
    def names_minor(r: str) -> bool:
        return bool(_MINOR_REASON.search(r)) or mentions_minor(r) or mentions_minor(text)

    m = _SENTINEL.match(reply)
    if m:
        return "minor" if m.group(1) == REFUSED_MINOR or names_minor(reply) else "declined"
    m = _REFUSAL_START.search(reply[:300])
    if m and not _norm(text).startswith(_norm(m.group(0))):
        return "minor" if names_minor(reply[:400]) else "declined"
    return None


def looks_refused(text: str) -> bool:
    return refusal(text) is not None


def _check(kind: str, text: str, *, rewrite: bool = False) -> None:
    """What generating this kind checks: scripts the script check (what
    POST /generate-plan runs) plus the minors check for scripts; other kinds
    the prompt check. A rewrite gets only the minors check."""
    from .safety import check_prompt, check_script, validate_script

    if kind != "script":
        check_prompt(text)
        return
    if not rewrite:
        validate_script(text)
    check_script(text)


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
    if not out.strip():
        return {"text": out, "model": used}  # the API's "empty prompt; try again"
    why = refusal(out, text)
    if why == "declined" and adult:
        fallback = get_settings().adult_text_fallback_model or ""
        if fallback and fallback != used:
            log.info("enhance: %s declined; retrying with %s", used, fallback)
            out, used = run(fallback)
            why = refusal(out, text) if out.strip() else None
    if why:
        raise EnhanceRefused(used, why, adult)
    try:
        _check(kind, out, rewrite=True)
    except ValueError:
        raise EnhanceRefused(used, "unsafe_rewrite", adult) from None
    return {"text": out, "model": used}

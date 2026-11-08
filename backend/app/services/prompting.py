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


def enhance(text: str, kind: str, model: str = "") -> dict:
    """Ask the planner model for a better prompt: {"text", "model"} where
    model is the planner model used. `model` is the target generation model
    (for a model-family hint)."""
    if kind not in ENHANCE_KINDS:
        raise ValueError(f"unknown prompt kind {kind!r}")
    from ..providers import get_chat
    from . import model_registry

    chat = get_chat()
    out = chat.enhance_prompt(text, kind, model or "")
    used = getattr(chat, "model", "") or model_registry.active_model("chat")
    return {"text": out, "model": used}

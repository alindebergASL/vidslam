"""Adult mode: one switch (Settings → Adult content) for adult content.

On:
- text work (storyboard planning, ✨ Enhance, ambient scene rewrites) uses
  ADULT_TEXT_MODEL (DeepSeek V4 Flash: it rewrote and planned the owner's
  adult prompts where the planner refused or toned them down, Nov 2026);
- picture/video filters follow their own settings: Runware's content check
  option, SOGNI_SAFE_CONTENT_FILTER, vendor safety knobs at their loosest.

Off (the default):
- text work uses the planner chosen in Settings → Models;
- Runware's content check and Sogni's safe filter are on, and vendor safety
  knobs stay at the vendor's defaults.

In both: prompts sexualising minors are refused (services/safety.py), and a
picture of a Cast character keeps Sogni's safe filter and Runware's content
check on, unless adult mode is on and the character is marked "adult
content allowed" (then every picture on its card skips them, however it
was made).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from ..config import get_settings

log = logging.getLogger("avs.content_mode")

KEY = "content.adult_mode"  # {"enabled": bool, "updated_at": iso}


def explicit() -> Optional[bool]:
    """The switch as set in Settings, None when following ADULT_MODE. Read
    every time (one small row), so a flip applies at once in every web and
    job worker. A failed read counts as off (filters on), never as on."""
    from sqlalchemy.exc import SQLAlchemyError

    from .. import models
    from ..db import SessionLocal

    try:
        db = SessionLocal()
    except Exception:  # noqa: BLE001 — no database configured (scripts)
        return False
    try:
        row = db.get(models.AppSetting, KEY)
        rec = row.value if row else None
    except SQLAlchemyError as e:
        log.warning("adult mode unreadable (%s); treating it as off", type(e).__name__)
        return False
    finally:
        db.close()
    return bool(rec["enabled"]) if isinstance(rec, dict) and "enabled" in rec else None


def adult_mode() -> bool:
    exp = explicit()
    return bool(get_settings().adult_mode) if exp is None else exp


def chat_model() -> str:
    """The text model that will actually run (gates and labels use this)."""
    from .model_registry import active_model

    return text_model() or active_model("chat")


def text_model() -> str:
    """The text model adult mode uses ("" when it's off: the planner)."""
    return (get_settings().adult_text_model or "") if adult_mode() else ""


# Cast characters marked "adult content allowed": an adult (18+), fictional
# or a real adult who consented. In adult mode every picture on their card
# (not a logo) skips the forced content filter, however it was made
# (pipeline._exempt_asset). A mark needs the character's own text to pass
# the minors word filter and the AI check (adult_ok_problem); an edit that
# fails either takes it off.
# One row per character, so marking two at once can't overwrite each other.
ADULT_OK_PREFIX = "content.adult_ok.avatar."  # + id → {"enabled": true, "at": iso}


def adult_ok_avatars() -> set[int]:
    """Marked character ids. A failed read marks nobody (filters stay on)."""
    from .. import models
    from ..db import SessionLocal

    try:
        db = SessionLocal()
    except Exception:  # noqa: BLE001
        return set()
    try:
        rows = db.query(models.AppSetting).filter(models.AppSetting.key.like(f"{ADULT_OK_PREFIX}%")).all()
    except Exception:  # noqa: BLE001 — unreadable: nobody is exempt
        return set()
    finally:
        db.close()
    out = set()
    for r in rows:
        tail = r.key[len(ADULT_OK_PREFIX):]
        if tail.isdigit() and isinstance(r.value, dict) and r.value.get("enabled") is True:
            out.add(int(tail))
    return out


CARD_FIELDS = ("name", "description", "persona", "visual_identity")


def describes_minor(avatar) -> bool:  # noqa: ANN001 — models.Avatar
    """The character's own text names a minor (word filter, so it's cheap
    enough for every render): never marked, and a mark is ignored."""
    from .safety import character_minor

    return character_minor(*(str(getattr(avatar, f, "") or "") for f in CARD_FIELDS))


# Why a character can't be ticked, or lost its tick on an edit.
DESCRIBED_AS_MINOR = "This character is described as a minor, so adult content can't be allowed."
_UNTICKED = {
    "words": "the new text describes a minor.",
    "minor": "the safety check thinks the new text describes someone under 18.",
    "unavailable": "the safety check couldn't run on the new text. Tick it again once it can.",
}


def adult_ok_problem(avatar) -> Optional[str]:  # noqa: ANN001 — models.Avatar or a copy of its fields
    """Why this character can't be marked "adult content allowed", or None.
    "words": its own text names a minor (describes_minor). "minor": the AI
    check thinks it's, or would be shown as, under 18. "unavailable": the AI
    check couldn't run, which never counts as a yes."""
    if describes_minor(avatar):
        return "words"
    from .minor_check import character_card, check_character

    verdict = check_character(character_card(avatar))
    return None if verdict == "ok" else verdict


def tick_refusal(problem: str) -> str:
    """The message for a refused tick (adult_ok_problem's answer)."""
    from .minor_check import CHARACTER_REFUSED, CHARACTER_UNAVAILABLE

    return {"minor": CHARACTER_REFUSED, "unavailable": CHARACTER_UNAVAILABLE}.get(problem, DESCRIBED_AS_MINOR)


def untick_notice(name: str, problem: str) -> str:
    """The message when an edit takes the tick off (adult_ok_problem's answer)."""
    why = _UNTICKED.get(problem, _UNTICKED["words"])
    return f"Adult content is no longer allowed for {name or 'this character'}: {why}"


def set_avatar_adult_ok(avatar_id: int, value: bool) -> dict:
    from .model_registry import put_setting

    put_setting(f"{ADULT_OK_PREFIX}{int(avatar_id)}", {
        "enabled": True, "at": datetime.now(timezone.utc).isoformat()} if value else None)
    log.info("cast character %s: adult content %s", avatar_id, "allowed" if value else "not allowed")
    return status()


def exempt(ref) -> bool:  # noqa: ANN001 — ImageRef
    """A picture that skips the forced content filter: a Cast character
    marked "adult content allowed", while adult mode is on."""
    return bool(getattr(ref, "adult_ok", False)) and adult_mode()


def forces_filter(ref) -> bool:  # noqa: ANN001 — ImageRef
    """A picture that turns the content filter on: a Cast character's
    picture (role "character", or `cast_picture` for one sent as a scene or
    a frame) that isn't exempt."""
    cast = getattr(ref, "role", "") == "character" or bool(getattr(ref, "cast_picture", False))
    return cast and not exempt(ref)


def set_adult_mode(value: Optional[bool]) -> dict:
    """Switch adult mode on/off; None goes back to ADULT_MODE."""
    from .model_registry import put_setting

    put_setting(KEY, None if value is None else {
        "enabled": bool(value), "updated_at": datetime.now(timezone.utc).isoformat()})
    log.info("adult mode %s", "back to its default" if value is None else ("on" if value else "off"))
    return status()


def status() -> dict:
    from .model_registry import active_model

    s = get_settings()
    return {
        "adult": adult_mode(),
        "default": bool(s.adult_mode),
        "explicit": explicit() is not None,
        "text_model": s.adult_text_model or "",
        "planner": active_model("chat"),
        "safety_check": _safety_check(),
        "adult_ok_avatars": sorted(adult_ok_avatars()),
    }


def _safety_check() -> dict:
    """{"model", "available"}: the planner checks prompts for minors before
    they're sent (services/minor_check.py)."""
    from .minor_check import availability

    try:
        return availability()
    except Exception:  # noqa: BLE001 — a status line, never a failed page
        from .model_registry import active_model

        return {"model": active_model("chat"), "available": False,
                "note": "It can't run right now, so sexual prompts are paused until it can."}

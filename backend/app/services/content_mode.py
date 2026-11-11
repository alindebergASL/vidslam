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

In both: prompts sexualising minors are refused (services/safety.py), and
Runware always checks requests carrying a Cast member's picture.
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


# Cast characters marked "adult content allowed": fictional, or the person
# consented. Their pictures skip the forced content filter in adult mode.
ADULT_OK_KEY = "content.adult_ok_avatars"  # {"ids": [avatar id, ...]}


def adult_ok_avatars() -> set[int]:
    try:
        from .model_registry import get_setting

        rec = get_setting(ADULT_OK_KEY)
    except Exception:  # noqa: BLE001 — unreadable: nobody is exempt
        return set()
    ids = rec.get("ids") if isinstance(rec, dict) else None
    return {int(i) for i in ids or [] if isinstance(i, int) or str(i).isdigit()}


def set_avatar_adult_ok(avatar_id: int, value: bool) -> dict:
    from .model_registry import put_setting

    ids = adult_ok_avatars()
    (ids.add if value else ids.discard)(int(avatar_id))
    put_setting(ADULT_OK_KEY, {"ids": sorted(ids)} if ids else None)
    log.info("cast character %s: adult content %s", avatar_id, "allowed" if value else "not allowed")
    return status()


def exempt(ref) -> bool:  # noqa: ANN001 — ImageRef
    """A picture that skips the forced content filter: a Cast character
    marked "adult content allowed", while adult mode is on."""
    return bool(getattr(ref, "adult_ok", False)) and adult_mode()


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

        return {"model": active_model("chat"), "available": False}

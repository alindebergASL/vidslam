"""Adult mode: one switch (Settings → Adult content) for adult content.

On:
- text work (storyboard planning, ✨ Enhance, ambient scene rewrites) uses
  ADULT_TEXT_MODEL (DeepSeek V4 Flash: it rewrote and planned the owner's
  adult prompts where the planner refused or toned them down, Nov 2026);
- picture/video filters follow their switches in Settings → API keys
  (Runware's content check, Sogni's safe filter); Runware's vendor safety
  settings are loosened where a model offers them.

Off (the default):
- text work uses the planner chosen in Settings → Models;
- Runware's content check and Sogni's safe filter are on, and vendor safety
  knobs stay at the vendor's defaults.

In both: prompts sexualising minors are refused (services/safety.py), and a
picture of a Cast character keeps Sogni's safe filter and Runware's content
check on, unless adult mode is on and the character is marked "adult
content allowed" (then the pictures on its card that the safety check
looked at skip them, however they were made).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable, Optional

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
# or a real adult who consented. In adult mode the pictures on their card
# (not logos) that the safety check looked at skip the forced content
# filter, however they were made (pipeline._exempt_asset).
#
# A tick needs the card's text to pass the minors word filter and the AI
# check, and each picture to pass the AI check (minor_check). The tick row
# keeps what was checked: {"enabled": true, "at": iso, "card": the card's
# hash, "assets": [picture ids]}. It holds only while the card is still the
# text that was checked (the hash includes minor_check.CHARACTER_POLICY, so
# a new policy needs a new tick), and covers only those pictures: one added
# later gets the filter until it is checked (tick again, or "Check new
# pictures" on the Cast card). Ticks from before this (no card hash) don't
# hold. One row per character, so marking two at once can't overwrite each
# other.
ADULT_OK_PREFIX = "content.adult_ok.avatar."  # + id → the row above


def _tick_rows() -> dict[int, dict]:
    """Every tick row that is switched on, by character id. A failed read
    marks nobody (filters stay on)."""
    from .. import models
    from ..db import SessionLocal

    try:
        db = SessionLocal()
    except Exception:  # noqa: BLE001
        return {}
    try:
        rows = db.query(models.AppSetting).filter(models.AppSetting.key.like(f"{ADULT_OK_PREFIX}%")).all()
    except Exception:  # noqa: BLE001 — unreadable: nobody is exempt
        return {}
    finally:
        db.close()
    out: dict[int, dict] = {}
    for r in rows:
        tail = r.key[len(ADULT_OK_PREFIX):]
        if tail.isdigit() and isinstance(r.value, dict) and r.value.get("enabled") is True:
            out[int(tail)] = r.value
    return out


def card_hash(avatar) -> str:  # noqa: ANN001 — models.Avatar or a copy of its fields
    """The hash a tick keeps of the card it checked (policy included)."""
    from .minor_check import character_card, character_hash

    return character_hash(character_card(avatar))


def _holding(rows: dict[int, dict]) -> tuple[dict[int, frozenset[int]], set[int]]:
    """(the ticks that hold: {id: checked picture ids}, ids whose tick no
    longer holds). See the note above."""
    if not rows:
        return {}, set()
    from .. import models
    from ..db import SessionLocal

    held: dict[int, frozenset[int]] = {}
    try:
        db = SessionLocal()
    except Exception:  # noqa: BLE001
        return {}, set(rows)
    try:
        for a in db.query(models.Avatar).filter(models.Avatar.id.in_(list(rows))).all():
            rec = rows[a.id]
            card = rec.get("card")
            if isinstance(card, str) and card == card_hash(a) and not describes_minor(a):
                ids = rec.get("assets") if isinstance(rec.get("assets"), list) else []
                held[a.id] = frozenset(int(i) for i in ids if isinstance(i, int))
    except Exception:  # noqa: BLE001 — unreadable: nobody is exempt
        return {}, set(rows)
    finally:
        db.close()
    return held, set(rows) - set(held)


def adult_ok_avatars() -> dict[int, frozenset[int]]:
    """Ticked characters whose tick holds, each with the ids of the
    pictures the check looked at. `id in adult_ok_avatars()` says whether
    a character counts as ticked. A failed read marks nobody."""
    return _holding(_tick_rows())[0]


def tick_pictures(avatar) -> list:  # noqa: ANN001 — models.Avatar
    """The pictures a tick covers once checked: every image on the card
    that isn't a logo."""
    return [x for x in (avatar.assets or [])
            if x.owner_kind == "avatar" and x.asset_type != "logo" and (x.mime_type or "").startswith("image/")]


CARD_FIELDS = ("name", "description", "persona", "visual_identity")


def describes_minor(avatar) -> bool:  # noqa: ANN001 — models.Avatar
    """The character's own text names a minor (word filter, so it's cheap
    enough for every render): never marked, and a mark is ignored."""
    from .safety import character_minor

    return character_minor(*(str(getattr(avatar, f, "") or "") for f in CARD_FIELDS))


def minor_words(avatar) -> Optional[str]:  # noqa: ANN001 — models.Avatar
    """The words on the card that the word filter reads as a minor, or None."""
    from .safety import character_minor_reason

    return character_minor_reason(*(str(getattr(avatar, f, "") or "") for f in CARD_FIELDS))


# Why a character can't be ticked, or lost its tick on an edit.
DESCRIBED_AS_MINOR = ("The card mentions an age under 18, a child or a school ({words}), so adult content can't be "
                      "allowed. If that isn't about this character now, reword the card (Edit card).")
REMEMBERED = ("This answer is remembered. If it's wrong, clear it in Settings → Adult content (Safety check "
              "memory), then tick again.")
_UNTICKED = {
    "words": "the card now mentions an age under 18, a child or a school ({words}).",
    "minor": "the safety check thinks the new text describes someone under 18.",
    "declined": "the safety check model declined to judge the new text. Tick it again, or try another planner.",
    "too_long": "the card is now too long for the safety check to read. Shorten it, then tick it again.",
    "unavailable": "the safety check couldn't run on the new text. Tick it again once it can.",
}


def adult_ok_problem(avatar) -> Optional[tuple[str, str]]:  # noqa: ANN001 — models.Avatar or a copy of its fields
    """Why this character's text can't be marked "adult content allowed",
    or None: (code, detail). "words": the word filter reads a minor (detail:
    the words). "minor": the AI check thinks it's, or would be shown as,
    under 18 (detail: its reason). "declined": the model wouldn't judge it.
    "too_long": the card is longer than the check reads. "unavailable": the
    check couldn't run. None of those counts as a yes."""
    words = minor_words(avatar)
    if words:
        return "words", words
    from .minor_check import character_card, check_character

    verdict, reason = check_character(character_card(avatar))
    return None if verdict == "ok" else (verdict, reason)


def tick_refusal(problem: tuple[str, str]) -> str:
    """The message for a refused tick (adult_ok_problem's answer)."""
    from .minor_check import (
        CARD_LIMIT,
        CHARACTER_DECLINED,
        CHARACTER_REFUSED,
        CHARACTER_TOO_LONG,
        CHARACTER_UNAVAILABLE,
    )

    code, detail = problem
    if code == "words":
        return DESCRIBED_AS_MINOR.format(words=f'"{detail}"')
    if code == "minor":
        why = f" It said: {detail.rstrip('.')}." if detail else ""
        return f"{CHARACTER_REFUSED}{why} {REMEMBERED}"
    if code == "declined":
        return CHARACTER_DECLINED
    if code == "too_long":
        return CHARACTER_TOO_LONG.format(limit=CARD_LIMIT)
    return CHARACTER_UNAVAILABLE


def untick_notice(name: str, problem: tuple[str, str]) -> str:
    """The message when an edit takes the tick off (adult_ok_problem's answer)."""
    code, detail = problem
    why = _UNTICKED.get(code, _UNTICKED["unavailable"]).format(words=f'"{detail}"')
    return f"Adult content is no longer allowed for {name or 'this character'}: {why}"


def set_avatar_adult_ok(avatar_id: int, value: bool, *, card: Optional[str] = None,
                        assets: Iterable[int] = ()) -> dict:
    """Tick (with the card hash and the picture ids that were checked) or
    untick. A tick without a card hash never holds."""
    from .model_registry import put_setting

    put_setting(f"{ADULT_OK_PREFIX}{int(avatar_id)}", {
        "enabled": True, "at": datetime.now(timezone.utc).isoformat(), "card": card,
        "assets": sorted({int(a) for a in assets})} if value else None)
    log.info("cast character %s: adult content %s", avatar_id, "allowed" if value else "not allowed")
    return status()


def move_tick(avatar_id: int, old_card: str, new_card: str) -> bool:
    """An edit whose new text passed the checks: the tick now holds for the
    new text, with the same pictures. Only when the tick is still the one
    the edit saw (not unticked or ticked again meanwhile)."""
    rec = _tick_rows().get(int(avatar_id))
    if not rec or rec.get("card") != old_card:
        return False
    set_avatar_adult_ok(avatar_id, True, card=new_card, assets=rec.get("assets") or [])
    return True


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
        **_tick_status(),
    }


def _tick_status() -> dict:
    """For the Cast cards: "adult_ok_avatars" (ticks that hold),
    "adult_ok_unchecked" ({id: pictures added since the check, which get
    the filter until they're checked}) and "adult_ok_stale" (ticks that no
    longer hold: the card changed outside the app, or it was ticked before
    the current check; tick again)."""
    held, stale = _holding(_tick_rows())
    unchecked: dict[str, int] = {}
    if held:
        from .. import models
        from ..db import SessionLocal

        try:
            db = SessionLocal()
            try:
                for a in db.query(models.Avatar).filter(models.Avatar.id.in_(list(held))).all():
                    n = sum(1 for x in tick_pictures(a) if x.id not in held[a.id])
                    if n:
                        unchecked[str(a.id)] = n
            finally:
                db.close()
        except Exception:  # noqa: BLE001 — a status line, never a failed page
            pass
    return {"adult_ok_avatars": sorted(held), "adult_ok_unchecked": unchecked, "adult_ok_stale": sorted(stale)}


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

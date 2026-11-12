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
from types import SimpleNamespace
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
# check (the planner), and the pictures to be looked at by the picture
# model (minor_check.check_pictures). The tick row keeps what was checked:
# {"enabled": true, "at": iso, "card": the card's hash, "assets": [picture
# ids judged adult], "declined": [picture ids the picture model wouldn't
# judge]}. It holds only while the card is still the text that was checked
# (the hash includes minor_check.CHARACTER_POLICY, so a new policy needs a
# new tick), and covers only the pictures judged adult: a declined one, or
# one added later, gets the filter until it is checked. A picture added to a
# ticked card is checked on its own (services/cast_pictures.py); "Check new
# pictures" on the Cast card (a tick again) asks about every picture not
# covered. Ticks from before this (no card hash) don't hold. One row per
# character, so marking two at once can't overwrite each other.
ADULT_OK_PREFIX = "content.adult_ok.avatar."  # + id → the row above
# Why a check of an added picture took a tick off (the Cast card says so
# until the owner ticks or unticks): + id → {"text", "at"}.
NOTICE_PREFIX = "content.adult_ok.notice."
# A check of added pictures is queued or running: + id → {"at": iso}
# (services/cast_pictures.py). Older than CHECKING_TTL: it died.
CHECKING_PREFIX = "content.adult_ok.checking."
CHECKING_TTL_SECONDS = 15 * 60


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


def is_tick_picture(asset) -> bool:  # noqa: ANN001 — models.Asset
    """A picture a tick covers once checked: an image on a Cast card that
    isn't a logo."""
    return (getattr(asset, "owner_kind", "") == "avatar" and getattr(asset, "asset_type", "") != "logo"
            and (getattr(asset, "mime_type", "") or "").startswith("image/"))


def tick_pictures(avatar) -> list:  # noqa: ANN001 — models.Avatar
    """The pictures a tick covers once checked: every image on the card
    that isn't a logo."""
    return [x for x in (avatar.assets or []) if is_tick_picture(x)]


def tick_row(avatar_id: int) -> Optional[dict]:
    """This character's tick row when it is switched on (held or not), else
    None. A failed read is None."""
    from .model_registry import get_setting

    try:
        rec = get_setting(f"{ADULT_OK_PREFIX}{int(avatar_id)}")
    except Exception:  # noqa: BLE001
        return None
    return rec if isinstance(rec, dict) and rec.get("enabled") is True else None


def holds(avatar_id: int) -> bool:
    """Whether this character counts as ticked now (as adult_ok_avatars)."""
    rec = tick_row(avatar_id)
    return rec is not None and int(avatar_id) in _holding({int(avatar_id): rec})[0]


def card_copy(avatar_id: int) -> tuple[Optional[SimpleNamespace], list[SimpleNamespace]]:
    """A copy of the character's text and the pictures a tick covers, so no
    database session is held while the AI check runs. (None, []) when the
    character is gone."""
    from .. import models
    from ..db import SessionLocal

    db = SessionLocal()
    try:
        avatar = db.get(models.Avatar, int(avatar_id))
        if avatar is None:
            return None, []
        text = SimpleNamespace(**{f: getattr(avatar, f) or "" for f in CARD_FIELDS})
        pictures = [SimpleNamespace(id=a.id, file_path=a.file_path, checksum=a.checksum or "",
                                    original_filename=a.original_filename or "")
                    for a in tick_pictures(avatar)]
        return text, pictures
    finally:
        db.close()


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
PICTURE_UNTICKED = ("Adult content is no longer allowed for {name}: the safety check thinks a picture added to the "
                    "card ({picture}) shows someone under 18.{why} Remove that picture, then tick again. "
                    + REMEMBERED)


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
                        assets: Iterable[int] = (), declined: Iterable[int] = (),
                        notice: Optional[str] = None) -> dict:
    """Tick (with the card hash, the ids of the pictures judged adult and of
    those the picture model wouldn't judge) or untick. A tick without a card
    hash never holds. `notice`: why a check took the tick off (the Cast card
    shows it); any other change clears an earlier notice."""
    from .model_registry import put_setting

    ids = {int(a) for a in assets}
    put_setting(f"{ADULT_OK_PREFIX}{int(avatar_id)}", {
        "enabled": True, "at": datetime.now(timezone.utc).isoformat(), "card": card,
        "assets": sorted(ids), "declined": sorted({int(a) for a in declined} - ids)} if value else None)
    try:
        put_setting(f"{NOTICE_PREFIX}{int(avatar_id)}", {
            "text": notice, "at": datetime.now(timezone.utc).isoformat()} if notice and not value else None)
    except Exception as e:  # noqa: BLE001 — a note on a card, never a failed tick
        log.warning("cast character %s: notice not saved (%s)", avatar_id, type(e).__name__)
    log.info("cast character %s: adult content %s", avatar_id, "allowed" if value else "not allowed")
    return status()


def move_tick(avatar_id: int, old_card: str, new_card: str) -> bool:
    """An edit whose new text passed the checks: the tick now holds for the
    new text, with the same pictures. Only when the tick is still the one
    the edit saw (not unticked or ticked again meanwhile)."""
    rec = _tick_rows().get(int(avatar_id))
    if not rec or rec.get("card") != old_card:
        return False
    set_avatar_adult_ok(avatar_id, True, card=new_card, assets=rec.get("assets") or [],
                        declined=picture_ids(rec.get("declined")))
    return True


def picture_ids(value: object) -> set[int]:
    """The picture ids a tick row keeps under "assets" or "declined"."""
    return {int(i) for i in value if isinstance(i, int)} if isinstance(value, list) else set()


def merge_tick(avatar_id: int, seen: dict, *, covered: Iterable[int] = (), declined: Iterable[int] = ()) -> bool:
    """Add what a later check of the card's pictures found to the tick
    `seen` (the row as read before that check): `covered`, pictures judged
    adult; `declined`, pictures the picture model wouldn't judge (they keep
    the filter). Compare-and-set: only while the tick is still that one
    (same card hash and time: not unticked, ticked again or moved by an edit
    meanwhile) and the card's text is still what it was checked as.
    Merged with the row as it is now (under a row lock on Postgres), so two
    checks finishing together both count. Only pictures still on the card
    are added. True when written."""
    from .. import models
    from ..db import SessionLocal

    key = f"{ADULT_OK_PREFIX}{int(avatar_id)}"
    db = SessionLocal()
    try:
        row = (db.query(models.AppSetting).filter(models.AppSetting.key == key)
               .with_for_update().one_or_none())
        rec = row.value if row is not None else None
        if not (isinstance(rec, dict) and rec.get("enabled") is True and isinstance(rec.get("card"), str)
                and rec.get("card") == seen.get("card") and rec.get("at") == seen.get("at")):
            db.rollback()
            return False
        avatar = db.get(models.Avatar, int(avatar_id))
        if avatar is None or card_hash(avatar) != rec["card"] or describes_minor(avatar):
            db.rollback()
            return False
        on_card = {int(x.id) for x in tick_pictures(avatar)}
        ids = picture_ids(rec.get("assets")) | ({int(i) for i in covered} & on_card)
        out = (picture_ids(rec.get("declined")) | ({int(i) for i in declined} & on_card)) - ids
        row.value = {**rec, "assets": sorted(ids), "declined": sorted(out)}
        db.commit()
        return True
    except Exception as e:  # noqa: BLE001 — not written: those pictures keep the filter
        db.rollback()
        log.warning("cast character %s: picture check not saved (%s)", avatar_id, type(e).__name__)
        return False
    finally:
        db.close()


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
    "adult_ok_unchecked" ({id: pictures not checked yet, which get the
    filter until they are: added since, or the check couldn't run}),
    "adult_ok_declined" ({id: [{"id", "name"}]: pictures the picture model
    wouldn't judge, which keep the filter}), "adult_ok_checking" (ids whose
    added pictures are being checked now), "adult_ok_notices" ({id: why a
    check of an added picture took the tick off}) and "adult_ok_stale"
    (ticks that no longer hold: the card changed outside the app, or it was
    ticked before the current check; tick again)."""
    rows = _tick_rows()
    held, stale = _holding(rows)
    unchecked: dict[str, int] = {}
    declined: dict[str, list[dict]] = {}
    checking: list[int] = []
    notices: dict[str, str] = {}
    from .. import models
    from ..db import SessionLocal

    try:
        db = SessionLocal()
        try:
            for a in (db.query(models.Avatar).filter(models.Avatar.id.in_(list(held))).all() if held else []):
                said_no = picture_ids(rows[a.id].get("declined"))
                for x in tick_pictures(a):
                    if x.id in held[a.id]:
                        continue
                    if x.id in said_no:
                        declined.setdefault(str(a.id), []).append(
                            {"id": int(x.id), "name": (x.original_filename or "").strip() or f"picture {x.id}"})
                    else:
                        unchecked[str(a.id)] = unchecked.get(str(a.id), 0) + 1
            now = datetime.now(timezone.utc)
            for r in db.query(models.AppSetting).filter(models.AppSetting.key.like("content.adult_ok.%")).all():
                tail = r.key.rsplit(".", 1)[-1]
                if not tail.isdigit() or not isinstance(r.value, dict):
                    continue
                if r.key == f"{CHECKING_PREFIX}{tail}" and int(tail) in held:
                    try:
                        fresh = (now - datetime.fromisoformat(str(r.value.get("at")))).total_seconds()
                    except (ValueError, TypeError):
                        continue
                    if fresh < CHECKING_TTL_SECONDS:
                        checking.append(int(tail))
                elif r.key == f"{NOTICE_PREFIX}{tail}" and int(tail) not in held and r.value.get("text"):
                    notices[tail] = str(r.value["text"])
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — a status line, never a failed page
        pass
    return {"adult_ok_avatars": sorted(held), "adult_ok_unchecked": unchecked, "adult_ok_declined": declined,
            "adult_ok_checking": sorted(checking), "adult_ok_notices": notices, "adult_ok_stale": sorted(stale)}


def _safety_check() -> dict:
    """{"model", "available", "note", "picture_model", "picture_available"}:
    the planner checks prompts (and Cast cards' text) for minors before
    they're sent, the picture model the pictures on Cast cards
    (services/minor_check.py)."""
    from .minor_check import availability, picture_model

    try:
        return availability()
    except Exception:  # noqa: BLE001 — a status line, never a failed page
        from .model_registry import active_model

        return {"model": active_model("chat"), "available": False,
                "note": "It can't run right now, so sexual prompts are paused until it can.",
                "picture_model": picture_model(), "picture_available": False}

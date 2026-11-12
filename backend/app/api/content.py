"""Adult mode (Settings → Adult content), see services/content_mode.py.

GET /api/settings/content   {"adult", "default", "explicit", "text_model", "planner",
                             "safety_check": {"model", "available", "note", "picture_model",
                             "picture_available"}, "adult_ok_avatars",
                             "adult_ok_unchecked": {id: pictures not checked yet},
                             "adult_ok_declined": {id: [{"id", "name"}]: not judged},
                             "adult_ok_checking": [ids being checked], "adult_ok_notices":
                             {id: why a check took the tick off},
                             "adult_ok_stale": [ticks that no longer hold]}
PUT /api/settings/content   {"adult": true | false | null} → same (null = back to ADULT_MODE)
PUT /api/settings/content/avatars/{id}   {"adult_ok": true | false} → same (+ "notice" when
                             the picture model wouldn't judge some pictures: they keep the
                             filter). Ticking runs the minors word filter and the AI check (the
                             planner) on the character's text, and the picture check
                             (CHARACTER_PICTURE_CHECK_MODEL) on each picture on the card: a 422
                             says why it was refused (or that a check couldn't run). Ticking a
                             ticked character again checks the pictures not covered yet; a
                             picture added to a ticked card is checked on its own
                             (services/cast_pictures.py).
GET /api/settings/content/safety-memory   {"refusals", "oks"}: verdicts the AI check remembers
POST /api/settings/content/safety-memory/clear-refusals   → {"cleared", "refusals", "oks"}
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from ..services import content_mode, minor_check
from .auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("")
def get_content_mode() -> dict:
    return content_mode.status()


@router.put("")
async def put_content_mode(request: Request) -> dict:
    try:
        body = json.loads((await request.body()) or b"null")
    except (ValueError, UnicodeDecodeError):
        body = None
    if not isinstance(body, dict) or "adult" not in body or body["adult"] not in (True, False, None):
        raise HTTPException(422, 'body must be JSON: {"adult": true | false | null}')
    return content_mode.set_adult_mode(body["adult"])


@router.put("/avatars/{avatar_id}")
async def put_avatar_adult_ok(avatar_id: int, request: Request) -> dict:
    try:
        body = json.loads((await request.body()) or b"null")
    except (ValueError, UnicodeDecodeError):
        body = None
    if not isinstance(body, dict) or body.get("adult_ok") is not True and body.get("adult_ok") is not False:
        raise HTTPException(422, 'body must be JSON: {"adult_ok": true | false}')
    if not body["adult_ok"]:
        return content_mode.set_avatar_adult_ok(avatar_id, False)  # clearing always works
    card, pictures, declined = await run_in_threadpool(_check_tick, avatar_id)
    out = content_mode.set_avatar_adult_ok(avatar_id, True, card=card, assets=pictures,
                                           declined=[d["id"] for d in declined])
    if declined:
        out["notice"] = minor_check.picture_declined([d["picture"] for d in declined])
    return out


def _card(avatar_id: int) -> tuple[SimpleNamespace, list[SimpleNamespace]]:
    """A copy of the character's text and pictures (no database session is
    held while the AI check runs)."""
    text, pictures = content_mode.card_copy(avatar_id)
    if text is None:
        raise HTTPException(404, "cast character not found")
    return text, pictures


def _check_tick(avatar_id: int) -> tuple[str, list[int], list[dict]]:
    """The card's hash, the ids of the pictures judged adult and the
    pictures the picture model wouldn't judge ({"id", "picture"}: they keep
    the filter, the tick still holds for the rest). Raises a 422 unless the
    character's text passes the word filter and the AI check (the planner),
    and the picture model could look at the pictures (it must run: no answer
    is a no). A picture it thinks shows someone under 18 refuses the tick,
    and takes off a tick the character already had."""
    card, pictures = _card(avatar_id)
    problem = content_mode.adult_ok_problem(card)
    if problem:
        raise HTTPException(422, content_mode.tick_refusal(problem))
    seen = minor_check.check_pictures(card.name, pictures)
    if seen["verdict"] == "minor":
        content_mode.set_avatar_adult_ok(avatar_id, False)
        why = f" It said: {seen['reason'].rstrip('.')}." if seen["reason"] else ""
        raise HTTPException(422, minor_check.PICTURE_REFUSED.format(picture=f'"{seen["picture"]}"') + why
                            + " " + content_mode.REMEMBERED)
    if seen["verdict"] != "ok":
        raise HTTPException(422, minor_check.PICTURE_UNAVAILABLE.format(
            model=minor_check.short_model(minor_check.picture_model())))
    if _card(avatar_id)[0] != card:  # edited while it was checked: the check read the old text
        raise HTTPException(409, "This character was changed while it was being checked. Try again.")
    return content_mode.card_hash(card), seen["checked"], seen["declined"]


@router.get("/safety-memory")
def get_safety_memory() -> dict:
    """How many AI check verdicts are remembered (word-filter refusals
    aren't: they're worked out again every time)."""
    return minor_check.memory()


@router.post("/safety-memory/clear-refusals")
def clear_safety_refusals() -> dict:
    """Forget the AI check's refusals, so each prompt (or character) is
    asked about again next time. Nothing else changes: the word filter
    still refuses what it refused before."""
    return {"cleared": minor_check.clear_refusals(), **minor_check.memory()}

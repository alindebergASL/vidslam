"""Adult mode (Settings → Adult content), see services/content_mode.py.

GET /api/settings/content   {"adult", "default", "explicit", "text_model", "planner",
                             "safety_check": {"model", "available", "note"}, "adult_ok_avatars"}
PUT /api/settings/content   {"adult": true | false | null} → same (null = back to ADULT_MODE)
PUT /api/settings/content/avatars/{id}   {"adult_ok": true | false} → same. Ticking runs
                             the minors word filter and the AI check on the character's text:
                             a 422 says why it was refused (or that the check couldn't run).
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
    if body["adult_ok"]:
        await run_in_threadpool(_check_tick, avatar_id)
    return content_mode.set_avatar_adult_ok(avatar_id, body["adult_ok"])  # clearing always works


def _card(avatar_id: int) -> SimpleNamespace:
    """A copy of the character's text (no database session is held while
    the AI check runs)."""
    from .. import models
    from ..db import SessionLocal

    db = SessionLocal()
    try:
        avatar = db.get(models.Avatar, avatar_id)
        if avatar is None:
            raise HTTPException(404, "cast character not found")
        return SimpleNamespace(**{f: getattr(avatar, f) or "" for f in content_mode.CARD_FIELDS})
    finally:
        db.close()


def _check_tick(avatar_id: int) -> None:
    """Raise a 422 unless the character's text passes the word filter and
    the AI check (which must run: no answer is a no)."""
    card = _card(avatar_id)
    problem = content_mode.adult_ok_problem(card)
    if problem:
        raise HTTPException(422, content_mode.tick_refusal(problem))
    if _card(avatar_id) != card:  # edited while it was checked: the check read the old text
        raise HTTPException(409, "This character was changed while it was being checked. Try again.")


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

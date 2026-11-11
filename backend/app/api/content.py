"""Adult mode (Settings → Adult content), see services/content_mode.py.

GET /api/settings/content   {"adult", "default", "explicit", "text_model", "planner",
                             "safety_check": {"model", "available"}}
PUT /api/settings/content   {"adult": true | false | null} → same (null = back to ADULT_MODE)
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request

from ..services import content_mode
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
    from .. import models
    from ..db import SessionLocal

    try:
        body = json.loads((await request.body()) or b"null")
    except (ValueError, UnicodeDecodeError):
        body = None
    if not isinstance(body, dict) or body.get("adult_ok") not in (True, False):
        raise HTTPException(422, 'body must be JSON: {"adult_ok": true | false}')
    db = SessionLocal()
    try:
        if db.get(models.Avatar, avatar_id) is None:
            raise HTTPException(404, "cast character not found")
    finally:
        db.close()
    return content_mode.set_avatar_adult_ok(avatar_id, body["adult_ok"])

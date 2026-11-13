from __future__ import annotations

from types import SimpleNamespace
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..schemas import AvatarCreate, AvatarOut, AvatarUpdate
from .auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])


class AvatarEditOut(AvatarOut):
    """An edited character. `adult_ok_notice` says why the edit took
    "Adult content allowed" off (the new text fails the minors checks);
    None when the mark didn't change."""

    adult_ok_notice: Optional[str] = None


def _check_look(*texts: str | None) -> None:
    """A Cast member's description and look go into its pictures' prompts
    (character sheets, storyboard shots): the safety check runs on save."""
    from ..services.minor_check import check_or_422

    text = " ".join(t.strip() for t in texts if t and t.strip())
    if text:
        check_or_422(text, kind="image")


@router.get("/avatars", response_model=list[AvatarOut])
def list_avatars(db: Session = Depends(get_db)) -> list[models.Avatar]:
    return db.query(models.Avatar).order_by(models.Avatar.created_at.desc()).all()


@router.post("/avatars", response_model=AvatarOut, status_code=201)
def create_avatar(body: AvatarCreate, db: Session = Depends(get_db)) -> models.Avatar:
    data = body.model_dump()
    _check_look(data.get("visual_identity"), data.get("description"))
    a = models.Avatar(**data)
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


@router.get("/avatars/{avatar_id}", response_model=AvatarOut)
def get_avatar(avatar_id: int, db: Session = Depends(get_db)) -> models.Avatar:
    a = db.get(models.Avatar, avatar_id)
    if a is None:
        raise HTTPException(404, "avatar not found")
    return a


@router.patch("/avatars/{avatar_id}", response_model=AvatarEditOut)
def update_avatar(
    avatar_id: int, body: AvatarUpdate, db: Session = Depends(get_db)
) -> AvatarEditOut:
    from ..services import content_mode

    a = db.get(models.Avatar, avatar_id)
    if a is None:
        raise HTTPException(404, "avatar not found")
    changes = body.model_dump(exclude_none=True)
    if "visual_identity" in changes or "description" in changes:
        _check_look(changes.get("visual_identity", a.visual_identity), changes.get("description", a.description))
    # A character marked "adult content allowed" whose text changes is
    # checked again (word filter, then the AI check). If it fails, or the
    # check can't run, the mark comes off before the new text is saved. If
    # it passes, the mark moves to the new text once it's saved (a tick
    # holds only for the text that was checked: content_mode).
    notice = None
    moved: Optional[tuple[str, str]] = None
    card = {f: changes.get(f, getattr(a, f)) or "" for f in content_mode.CARD_FIELDS}
    if (any(card[f] != (getattr(a, f) or "") for f in card)
            and avatar_id in content_mode.adult_ok_avatars()):
        problem = content_mode.adult_ok_problem(SimpleNamespace(**card))
        if problem:
            content_mode.set_avatar_adult_ok(avatar_id, False)
            notice = content_mode.untick_notice(card["name"], problem)
        else:
            moved = (content_mode.card_hash(a), content_mode.card_hash(SimpleNamespace(**card)))
    for k, v in changes.items():
        setattr(a, k, v)
    db.commit()
    db.refresh(a)
    if moved:
        content_mode.move_tick(avatar_id, *moved)
    out = AvatarEditOut.model_validate(a)
    out.adult_ok_notice = notice
    return out


@router.delete("/avatars/{avatar_id}", status_code=204)
def delete_avatar(avatar_id: int, db: Session = Depends(get_db)) -> None:
    a = db.get(models.Avatar, avatar_id)
    if a is None:
        raise HTTPException(404, "avatar not found")
    # Clean up references so projects never point at a deleted avatar (SQLite
    # doesn't enforce FKs, so these would otherwise dangle).
    db.query(models.ProjectCastMember).filter(
        models.ProjectCastMember.member_kind == "avatar",
        models.ProjectCastMember.avatar_id == avatar_id,
    ).delete(synchronize_session=False)
    db.query(models.VideoProject).filter(
        models.VideoProject.primary_avatar_id == avatar_id
    ).update({models.VideoProject.primary_avatar_id: None}, synchronize_session=False)
    # Its pictures go with it: clips made from them remember they were its.
    from ..services.pipeline import remember_deleted_pictures

    remember_deleted_pictures(db, list(a.assets))
    db.delete(a)
    db.commit()
    from ..services import content_mode

    content_mode.set_avatar_adult_ok(avatar_id, False)  # a reused id must not inherit the mark

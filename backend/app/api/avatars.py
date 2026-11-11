from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..schemas import AvatarCreate, AvatarOut, AvatarUpdate
from .auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])


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


@router.patch("/avatars/{avatar_id}", response_model=AvatarOut)
def update_avatar(
    avatar_id: int, body: AvatarUpdate, db: Session = Depends(get_db)
) -> models.Avatar:
    a = db.get(models.Avatar, avatar_id)
    if a is None:
        raise HTTPException(404, "avatar not found")
    changes = body.model_dump(exclude_none=True)
    if "visual_identity" in changes or "description" in changes:
        _check_look(changes.get("visual_identity", a.visual_identity), changes.get("description", a.description))
    for k, v in changes.items():
        setattr(a, k, v)
    db.commit()
    db.refresh(a)
    from ..services import content_mode

    if avatar_id in content_mode.adult_ok_avatars() and content_mode.describes_minor(a):
        content_mode.set_avatar_adult_ok(avatar_id, False)  # now described as a minor: never marked
    return a


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
    db.delete(a)
    db.commit()
    from ..services import content_mode

    content_mode.set_avatar_adult_ok(avatar_id, False)  # a reused id must not inherit the mark

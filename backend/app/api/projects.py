from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..schemas import (
    AssetOut,
    CastMemberIn,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
    ShotOut,
    ShotUpdate,
)
from .auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])


def _apply_cast(db: Session, project: models.VideoProject, cast: list[CastMemberIn]) -> None:
    for cm in list(project.cast_members):
        db.delete(cm)
    db.flush()
    for c in cast:
        if c.member_kind == "avatar" and c.avatar_id:
            db.add(
                models.ProjectCastMember(
                    project_id=project.id,
                    member_kind="avatar",
                    avatar_id=c.avatar_id,
                    role=c.role or "host",
                )
            )
        elif c.member_kind == "ingredient" and c.ingredient_id:
            db.add(
                models.ProjectCastMember(
                    project_id=project.id,
                    member_kind="ingredient",
                    ingredient_id=c.ingredient_id,
                    role=c.role or "scene",
                )
            )


@router.get("/projects", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db)) -> list[models.VideoProject]:
    return (
        db.query(models.VideoProject).order_by(models.VideoProject.created_at.desc()).all()
    )


@router.post("/projects", response_model=ProjectOut, status_code=201)
def create_project(body: ProjectCreate, db: Session = Depends(get_db)) -> models.VideoProject:
    primary_id = body.primary_avatar_id
    if primary_id is None:
        for c in body.cast:
            if c.member_kind == "avatar" and c.avatar_id:
                primary_id = c.avatar_id
                break
    p = models.VideoProject(
        title=body.title or "Untitled Project",
        original_script=body.original_script,
        mode=body.mode,
        aspect_ratio=body.aspect_ratio,
        target_duration_seconds=body.target_duration_seconds,
        cta_text=body.cta_text,
        caption_style=body.caption_style,
        include_disclosure=body.include_disclosure,
        disclosure_text=body.disclosure_text,
        creative_direction=body.creative_direction,
        voiceover_source=body.voiceover_source,
        music_volume=body.music_volume,
        primary_avatar_id=primary_id,
        status="draft",
    )
    db.add(p)
    db.flush()
    _apply_cast(db, p, body.cast)
    db.commit()
    db.refresh(p)
    return p


@router.get("/projects/{project_id}", response_model=ProjectOut)
def get_project(project_id: int, db: Session = Depends(get_db)) -> models.VideoProject:
    p = db.get(models.VideoProject, project_id)
    if p is None:
        raise HTTPException(404, "project not found")
    return p


@router.patch("/projects/{project_id}", response_model=ProjectOut)
def update_project(
    project_id: int, body: ProjectUpdate, db: Session = Depends(get_db)
) -> models.VideoProject:
    p = db.get(models.VideoProject, project_id)
    if p is None:
        raise HTTPException(404, "project not found")
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(p, k, v)
    db.commit()
    db.refresh(p)
    return p


@router.patch("/projects/{project_id}/cast", response_model=ProjectOut)
def update_cast(
    project_id: int, cast: list[CastMemberIn], db: Session = Depends(get_db)
) -> models.VideoProject:
    p = db.get(models.VideoProject, project_id)
    if p is None:
        raise HTTPException(404, "project not found")
    _apply_cast(db, p, cast)
    db.commit()
    db.refresh(p)
    return p


@router.patch("/projects/{project_id}/shots/{shot_id}", response_model=ShotOut)
def update_shot(
    project_id: int, shot_id: int, body: ShotUpdate, db: Session = Depends(get_db)
) -> models.VideoShot:
    s = db.get(models.VideoShot, shot_id)
    if s is None or s.project_id != project_id:
        raise HTTPException(404, "shot not found in project")
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(s, k, v)
    db.commit()
    db.refresh(s)
    return s


class ReorderIn(BaseModel):
    shot_ids: list[int]


@router.post("/projects/{project_id}/shots/reorder", response_model=ProjectOut)
def reorder_shots(
    project_id: int, body: ReorderIn, db: Session = Depends(get_db)
) -> models.VideoProject:
    """Set shot_order from the given ordering of shot ids. Ids not belonging to
    the project are ignored; omitted shots keep a stable order after the listed ones."""
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    by_id = {s.id: s for s in project.shots}
    order = 1
    for sid in body.shot_ids:
        shot = by_id.pop(sid, None)
        if shot is not None:
            shot.shot_order = order
            order += 1
    # Any shots not mentioned keep their relative order at the end.
    for shot in sorted(by_id.values(), key=lambda s: s.shot_order):
        shot.shot_order = order
        order += 1
    db.commit()
    db.refresh(project)
    return project


@router.get("/projects/{project_id}/cast-assets", response_model=list[AssetOut])
def cast_assets(project_id: int, db: Session = Depends(get_db)) -> list[models.Asset]:
    """Flat list of every asset owned by any cast member of this project,
    used to populate per-shot reference pickers in the UI."""
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    out: list[models.Asset] = []
    for cm in project.cast_members:
        if cm.member_kind == "avatar" and cm.avatar is not None:
            out.extend(cm.avatar.assets)
        elif cm.member_kind == "ingredient" and cm.ingredient is not None:
            out.extend(cm.ingredient.assets)
    return out


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: int, db: Session = Depends(get_db)) -> None:
    p = db.get(models.VideoProject, project_id)
    if p is None:
        raise HTTPException(404, "project not found")
    db.delete(p)
    db.commit()

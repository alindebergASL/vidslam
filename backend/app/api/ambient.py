"""Ambient section API — calm / landscape / spa long-form loops.

Mounted at /api/ambient. Scenes are VideoProjects under the hood, so the
generic project endpoints (delete, renders, cancel, music upload/preview,
shot clip preview, share links) keep working on an ambient id; this router
only adds the ambient-specific lifecycle.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..schemas.ambient import (
    AmbientCreate,
    AmbientEstimateRequest,
    AmbientOut,
    AmbientPresetsOut,
    AmbientUpdate,
)
from ..services import ambient as svc
from ..services.ratelimit import rate_limit
from ..workers.jobs import generate_ambient_loop_job, render_ambient_job
from ..workers.queue import enqueue
from .auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])


def _get_scene(db: Session, scene_id: int) -> models.VideoProject:
    project = db.get(models.VideoProject, scene_id)
    if project is None or project.mode != svc.AMBIENT_MODE:
        raise HTTPException(404, "ambient scene not found")
    return project


@router.get("/presets", response_model=AmbientPresetsOut)
def get_presets() -> AmbientPresetsOut:
    return svc.presets()


@router.post("/estimate")
def estimate(body: AmbientEstimateRequest) -> dict:
    return svc.estimate_ambient_cost(body.config)


@router.get("", response_model=list[AmbientOut])
def list_scenes(db: Session = Depends(get_db)) -> list[AmbientOut]:
    return [svc.to_out(db, p) for p in svc.list_scenes(db)]


@router.post("", response_model=AmbientOut, status_code=201,
             dependencies=[Depends(rate_limit("generation"))])
def create_scene(body: AmbientCreate, db: Session = Depends(get_db)) -> AmbientOut:
    try:
        project = svc.create_scene(db, body)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    if body.generate:
        enqueue(generate_ambient_loop_job, project.id)
        db.expire_all()
        project = _get_scene(db, project.id)
    return svc.to_out(db, project)


@router.get("/{scene_id}", response_model=AmbientOut)
def get_scene(scene_id: int, db: Session = Depends(get_db)) -> AmbientOut:
    return svc.to_out(db, _get_scene(db, scene_id))


@router.patch("/{scene_id}", response_model=AmbientOut)
def update_scene(scene_id: int, body: AmbientUpdate, db: Session = Depends(get_db)) -> AmbientOut:
    project = _get_scene(db, scene_id)
    if project.status == "generating":
        raise HTTPException(409, "scene is generating; wait or cancel first")
    try:
        project = svc.update_scene(db, project, body)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return svc.to_out(db, project)


@router.post("/{scene_id}/generate-loop", status_code=202,
             dependencies=[Depends(rate_limit("generation"))])
def generate_loop(
    scene_id: int,
    regenerate_music: bool = True,
    db: Session = Depends(get_db),
) -> dict:
    project = _get_scene(db, scene_id)
    if project.status == "generating":
        raise HTTPException(409, "scene is already generating")
    job_id = enqueue(generate_ambient_loop_job, scene_id, regenerate_music)
    return {"scene_id": scene_id, "job_id": job_id, "status": "enqueued"}


@router.post("/{scene_id}/render", status_code=202,
             dependencies=[Depends(rate_limit("generation"))])
def render_scene(scene_id: int, db: Session = Depends(get_db)) -> dict:
    project = _get_scene(db, scene_id)
    if project.status == "generating":
        raise HTTPException(409, "scene is already generating")
    shot = svc._loop_shot(project)
    if shot is None or not shot.clip_path or not Path(shot.clip_path).exists():
        raise HTTPException(409, "generate the loop clip first")
    job_id = enqueue(render_ambient_job, scene_id)
    return {"scene_id": scene_id, "job_id": job_id, "status": "enqueued"}

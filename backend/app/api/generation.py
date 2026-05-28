from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..schemas import ProjectStatusOut, RenderOut, ShotOut
from ..services.safety import UnsafeScriptError, validate_script
from ..workers.jobs import generate_plan_job, generate_video_job, regenerate_shot_job
from ..workers.queue import enqueue
from .auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])


@router.post("/projects/{project_id}/generate-plan", status_code=202)
def generate_plan(project_id: int, db: Session = Depends(get_db)) -> dict:
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    try:
        validate_script(project.original_script or "")
    except UnsafeScriptError as e:
        raise HTTPException(422, str(e)) from e
    job_id = enqueue(generate_plan_job, project_id)
    return {"project_id": project_id, "job_id": job_id, "status": "enqueued"}


@router.post("/projects/{project_id}/generate-video", status_code=202)
def generate_video(project_id: int, db: Session = Depends(get_db)) -> dict:
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    if not project.generated_plan_json:
        raise HTTPException(409, "generate a storyboard plan first")
    job_id = enqueue(generate_video_job, project_id)
    return {"project_id": project_id, "job_id": job_id, "status": "enqueued"}


@router.get("/projects/{project_id}/status", response_model=ProjectStatusOut)
def project_status(project_id: int, db: Session = Depends(get_db)) -> ProjectStatusOut:
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    latest = (
        db.query(models.Render)
        .filter(models.Render.project_id == project_id)
        .order_by(models.Render.created_at.desc())
        .first()
    )
    return ProjectStatusOut(
        project_id=project.id,
        project_status=project.status,
        latest_render=RenderOut.model_validate(latest) if latest else None,
        shots=[ShotOut.model_validate(s) for s in project.shots],
    )


@router.post("/projects/{project_id}/shots/{shot_id}/regenerate", status_code=202)
def regenerate_shot(project_id: int, shot_id: int, db: Session = Depends(get_db)) -> dict:
    shot = db.get(models.VideoShot, shot_id)
    if shot is None or shot.project_id != project_id:
        raise HTTPException(404, "shot not found in project")
    job_id = enqueue(regenerate_shot_job, project_id, shot_id)
    return {"shot_id": shot_id, "job_id": job_id, "status": "enqueued"}


@router.get("/renders/{render_id}", response_model=RenderOut)
def get_render(render_id: int, db: Session = Depends(get_db)) -> models.Render:
    r = db.get(models.Render, render_id)
    if r is None:
        raise HTTPException(404, "render not found")
    return r


@router.get("/renders/{render_id}/download")
def download_render(render_id: int, db: Session = Depends(get_db)) -> FileResponse:
    r = db.get(models.Render, render_id)
    if r is None or not r.final_video_path or not Path(r.final_video_path).exists():
        raise HTTPException(404, "render not ready")
    return FileResponse(
        r.final_video_path,
        media_type="video/mp4",
        filename=f"avatar-video-{render_id}.mp4",
    )


@router.get("/renders/{render_id}/thumbnail")
def download_thumbnail(render_id: int, db: Session = Depends(get_db)) -> FileResponse:
    r = db.get(models.Render, render_id)
    if r is None or not r.thumbnail_path or not Path(r.thumbnail_path).exists():
        raise HTTPException(404, "thumbnail not ready")
    return FileResponse(r.thumbnail_path, media_type="image/jpeg")

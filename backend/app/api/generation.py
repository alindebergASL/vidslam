from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..schemas import ProjectStatusOut, RenderOut, ShotOut
from ..services.preflight import run_preflight
from ..services.safety import UnsafeScriptError, validate_script
from ..workers.jobs import (
    generate_plan_job,
    generate_video_job,
    recompose_project_job,
    regenerate_shot_job,
)
from ..workers.queue import enqueue
from .auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])

# Public, token-gated router (no auth) for sharing finished renders.
public_router = APIRouter()


@public_router.get("/public-renders/{token}")
def public_render(token: str, db: Session = Depends(get_db)) -> FileResponse:
    r = db.query(models.Render).filter(models.Render.share_token == token).first()
    if r is None or r.status != "completed" or not r.final_video_path:
        raise HTTPException(404, "render not found")
    if not Path(r.final_video_path).exists():
        raise HTTPException(404, "render file missing")
    return FileResponse(r.final_video_path, media_type="video/mp4")


@public_router.get("/public-renders/{token}/thumbnail")
def public_render_thumbnail(token: str, db: Session = Depends(get_db)) -> FileResponse:
    r = db.query(models.Render).filter(models.Render.share_token == token).first()
    if r is None or not r.thumbnail_path or not Path(r.thumbnail_path).exists():
        raise HTTPException(404, "thumbnail not found")
    return FileResponse(r.thumbnail_path, media_type="image/jpeg")


@public_router.get("/public-renders/{token}/meta")
def public_render_meta(token: str, db: Session = Depends(get_db)) -> dict:
    """Minimal metadata for the public share landing page (no auth)."""
    r = db.query(models.Render).filter(models.Render.share_token == token).first()
    if r is None or r.status != "completed":
        raise HTTPException(404, "render not found")
    project = r.project
    plan = project.generated_plan_json or {}
    return {
        "title": project.title or "AI-generated video",
        "aspect_ratio": project.aspect_ratio,
        "created_at": r.created_at.isoformat(),
        "disclosure": plan.get("disclosure_text") or "AI-generated virtual creator",
    }


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


@router.get("/projects/{project_id}/preflight")
def preflight(project_id: int, db: Session = Depends(get_db)) -> dict:
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    return run_preflight(db, project)


@router.post("/projects/{project_id}/generate-video", status_code=202)
def generate_video(
    project_id: int,
    force: bool = False,
    db: Session = Depends(get_db),
) -> dict:
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    if not project.generated_plan_json:
        raise HTTPException(409, "generate a storyboard plan first")
    if not force:
        result = run_preflight(db, project)
        if not result["ok"]:
            raise HTTPException(422, detail={"preflight": result})
    job_id = enqueue(generate_video_job, project_id)
    return {"project_id": project_id, "job_id": job_id, "status": "enqueued"}


@router.post("/projects/{project_id}/recompose", status_code=202)
def recompose_video(project_id: int, db: Session = Depends(get_db)) -> dict:
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    if not project.generated_plan_json:
        raise HTTPException(409, "generate a storyboard plan first")
    job_id = enqueue(recompose_project_job, project_id)
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
def regenerate_shot(
    project_id: int,
    shot_id: int,
    recompose: bool = True,
    db: Session = Depends(get_db),
) -> dict:
    shot = db.get(models.VideoShot, shot_id)
    if shot is None or shot.project_id != project_id:
        raise HTTPException(404, "shot not found in project")
    job_id = enqueue(regenerate_shot_job, project_id, shot_id, recompose)
    return {
        "shot_id": shot_id,
        "job_id": job_id,
        "status": "enqueued",
        "will_recompose": recompose,
    }


@router.get("/projects/{project_id}/renders", response_model=list[RenderOut])
def list_renders(project_id: int, db: Session = Depends(get_db)) -> list[models.Render]:
    if db.get(models.VideoProject, project_id) is None:
        raise HTTPException(404, "project not found")
    return (
        db.query(models.Render)
        .filter(models.Render.project_id == project_id)
        .order_by(models.Render.created_at.desc())
        .all()
    )


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

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from slugify import slugify
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
    regenerate_shots_bulk_job,
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


@router.get("/projects/{project_id}/cost-estimate")
def cost_estimate(project_id: int, db: Session = Depends(get_db)) -> dict:
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    if not project.generated_plan_json:
        raise HTTPException(409, "generate a storyboard plan first")
    from ..schemas.storyboard import StoryboardPlan
    from ..services.cost import estimate_plan_cost

    plan = StoryboardPlan.model_validate(project.generated_plan_json)
    project_dict = {
        "voiceover_source": project.voiceover_source,
        # If the project already has uploaded music, no generation cost; otherwise
        # we can't know intent here, so treat music as already-handled (0) for the
        # estimate. The Studio/AudioPanel surfaces music-gen cost separately.
        "music_will_generate": False,
    }
    return estimate_plan_cost(project_dict, plan)


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


class BulkRegenIn(BaseModel):
    shot_ids: list[int]


@router.post("/projects/{project_id}/shots/regenerate-bulk", status_code=202)
def regenerate_shots_bulk(
    project_id: int, body: BulkRegenIn, db: Session = Depends(get_db)
) -> dict:
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    valid = {s.id for s in project.shots if s.shot_type != "end_card"}
    targets = [sid for sid in body.shot_ids if sid in valid]
    if not targets:
        raise HTTPException(422, "no regenerable shots in the selection")
    job_id = enqueue(regenerate_shots_bulk_job, project_id, targets)
    return {
        "project_id": project_id,
        "job_id": job_id,
        "status": "enqueued",
        "shot_ids": targets,
    }


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


@router.get("/projects/{project_id}/shots/{shot_id}/clip")
def shot_clip(project_id: int, shot_id: int, db: Session = Depends(get_db)) -> FileResponse:
    """Stream a single shot's generated clip for in-editor preview."""
    shot = db.get(models.VideoShot, shot_id)
    if shot is None or shot.project_id != project_id:
        raise HTTPException(404, "shot not found in project")
    if not shot.clip_path or not Path(shot.clip_path).exists():
        raise HTTPException(404, "clip not generated yet")
    return FileResponse(shot.clip_path, media_type="video/mp4")


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


@router.get("/projects/{project_id}/renders/export")
def export_renders(project_id: int, db: Session = Depends(get_db)) -> StreamingResponse:
    """Bundle every completed render's MP4 (+ thumbnail) for a project into a
    single downloadable zip, newest version first."""
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    renders = (
        db.query(models.Render)
        .filter(models.Render.project_id == project_id, models.Render.status == "completed")
        .order_by(models.Render.created_at.desc())
        .all()
    )
    completed = [r for r in renders if r.final_video_path and Path(r.final_video_path).exists()]
    if not completed:
        raise HTTPException(404, "no completed renders to export")

    slug = slugify(project.title or f"project-{project_id}") or f"project-{project_id}"
    buf = io.BytesIO()
    total = len(completed)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for idx, r in enumerate(completed):
            version = total - idx  # newest = highest version number
            base = f"{slug}_v{version}"
            zf.write(r.final_video_path, arcname=f"{base}.mp4")
            if r.thumbnail_path and Path(r.thumbnail_path).exists():
                zf.write(r.thumbnail_path, arcname=f"{base}.jpg")
    buf.seek(0)
    headers = {"Content-Disposition": f'attachment; filename="{slug}_renders.zip"'}
    return StreamingResponse(buf, media_type="application/zip", headers=headers)


@router.get("/renders/recent")
def recent_renders(limit: int = 12, db: Session = Depends(get_db)) -> list[dict]:
    """Most-recent completed renders across all projects, for the dashboard gallery."""
    limit = max(1, min(limit, 48))
    rows = (
        db.query(models.Render, models.VideoProject.title)
        .join(models.VideoProject, models.Render.project_id == models.VideoProject.id)
        .filter(models.Render.status == "completed", models.Render.final_video_path != "")
        .order_by(models.Render.created_at.desc())
        .limit(limit)
        .all()
    )
    out: list[dict] = []
    for r, title in rows:
        if not (r.final_video_path and Path(r.final_video_path).exists()):
            continue
        out.append({
            "render_id": r.id,
            "project_id": r.project_id,
            "project_title": title or "Untitled",
            "share_token": r.share_token,
            "created_at": r.created_at.isoformat(),
        })
    return out


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

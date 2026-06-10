from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..schemas import AssetOut, StudioJobOut, StudioRequest
from ..services.pipeline import save_studio_result_as_asset
from ..services.ratelimit import rate_limit
from ..workers.jobs import generate_asset_job
from ..workers.queue import enqueue
from .auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])


def _owner_exists(db: Session, kind: str, owner_id: int) -> bool:
    if kind == "avatar":
        return db.get(models.Avatar, owner_id) is not None
    if kind == "ingredient":
        return db.get(models.Ingredient, owner_id) is not None
    return False


@router.post("/generate-image", response_model=StudioJobOut, status_code=202, dependencies=[Depends(rate_limit("generation"))])
def generate_image(body: StudioRequest, db: Session = Depends(get_db)) -> models.AssetGenerationJob:
    if not _owner_exists(db, body.owner_kind, body.owner_id):
        raise HTTPException(404, "owner not found")
    job = models.AssetGenerationJob(
        owner_kind=body.owner_kind,
        owner_id=body.owner_id,
        output_kind="image",
        prompt=body.prompt,
        negative_prompt=body.negative_prompt,
        reference_asset_ids_json=body.reference_asset_ids,
        provider_model=body.model or "",
        status="pending",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    enqueue(generate_asset_job, job.id)
    return job


@router.post("/generate-clip", response_model=StudioJobOut, status_code=202, dependencies=[Depends(rate_limit("generation"))])
def generate_clip(body: StudioRequest, db: Session = Depends(get_db)) -> models.AssetGenerationJob:
    if not _owner_exists(db, body.owner_kind, body.owner_id):
        raise HTTPException(404, "owner not found")
    job = models.AssetGenerationJob(
        owner_kind=body.owner_kind,
        owner_id=body.owner_id,
        output_kind="video_clip",
        prompt=body.prompt,
        negative_prompt=body.negative_prompt,
        reference_asset_ids_json=body.reference_asset_ids,
        provider_model=body.model or "",
        status="pending",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    enqueue(generate_asset_job, job.id)
    return job


@router.get("/jobs", response_model=list[StudioJobOut])
def list_jobs(db: Session = Depends(get_db)) -> list[models.AssetGenerationJob]:
    return (
        db.query(models.AssetGenerationJob)
        .order_by(models.AssetGenerationJob.created_at.desc())
        .limit(50)
        .all()
    )


@router.get("/jobs/{job_id}", response_model=StudioJobOut)
def get_job(job_id: int, db: Session = Depends(get_db)) -> models.AssetGenerationJob:
    j = db.get(models.AssetGenerationJob, job_id)
    if j is None:
        raise HTTPException(404, "job not found")
    return j


@router.get("/jobs/{job_id}/preview")
def preview_job(job_id: int, db: Session = Depends(get_db)) -> FileResponse:
    j = db.get(models.AssetGenerationJob, job_id)
    if j is None or not j.result_path or not Path(j.result_path).exists():
        raise HTTPException(404, "result not ready")
    media = "image/png" if j.output_kind == "image" else "video/mp4"
    return FileResponse(j.result_path, media_type=media)


@router.post("/jobs/{job_id}/save", response_model=AssetOut)
def save_job(job_id: int, asset_type: str = "lifestyle", db: Session = Depends(get_db)) -> models.Asset:
    j = db.get(models.AssetGenerationJob, job_id)
    if j is None:
        raise HTTPException(404, "job not found")
    try:
        return save_studio_result_as_asset(db, j, asset_type=asset_type)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

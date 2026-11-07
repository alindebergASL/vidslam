from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..schemas import AssetOut, StudioJobOut, StudioRequest
from ..schemas.api import ASSET_TYPES, StudioSaveRequest
from ..services.pipeline import save_studio_result_as_asset
from ..services.ratelimit import rate_limit
from ..services.spend import budget_guard
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


@router.post("/generate-image", response_model=StudioJobOut, status_code=202, dependencies=[Depends(rate_limit("generation")), Depends(budget_guard)])
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


@router.post("/generate-clip", response_model=StudioJobOut, status_code=202, dependencies=[Depends(rate_limit("generation")), Depends(budget_guard)])
def generate_clip(body: StudioRequest, db: Session = Depends(get_db)) -> models.AssetGenerationJob:
    if not _owner_exists(db, body.owner_kind, body.owner_id):
        raise HTTPException(404, "owner not found")
    job = models.AssetGenerationJob(
        owner_kind=body.owner_kind,
        owner_id=body.owner_id,
        output_kind="video_clip",
        duration_seconds=body.duration_seconds,
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
def save_job(
    job_id: int,
    asset_type: Optional[str] = None,
    body: Optional[StudioSaveRequest] = Body(None),
    db: Session = Depends(get_db),
) -> models.Asset:
    j = db.get(models.AssetGenerationJob, job_id)
    if j is None:
        raise HTTPException(404, "job not found")
    body = body or StudioSaveRequest()
    kind = body.owner_kind or j.owner_kind
    if body.owner_id is not None:
        owner_id = body.owner_id
    elif kind == j.owner_kind:
        owner_id = j.owner_id
    else:
        # An id from the job's own owner means nothing under another kind.
        raise HTTPException(422, "owner_id is required when owner_kind changes")
    # The body wins over the legacy ?asset_type= query param.
    chosen = body.asset_type or asset_type
    if chosen is not None and chosen not in ASSET_TYPES.get(kind, ()):
        raise HTTPException(422, f"asset_type must be one of {', '.join(ASSET_TYPES.get(kind, ()))}")
    if j.output_kind != "image":
        raise HTTPException(400, "only image studio results can be saved as cast assets")
    if j.status not in ("completed", "saved") or not j.result_path or not Path(j.result_path).exists():
        raise HTTPException(400, "studio job is not completed")
    if not _owner_exists(db, kind, owner_id):
        raise HTTPException(404, "owner not found")
    try:
        return save_studio_result_as_asset(
            db, j, asset_type=chosen, owner_kind=kind, owner_id=owner_id
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/jobs/{job_id}", status_code=204)
def delete_job(job_id: int, db: Session = Depends(get_db)) -> None:
    j = db.get(models.AssetGenerationJob, job_id)
    if j is None:
        raise HTTPException(404, "job not found")
    path = j.result_path
    # Assets keep their file and just lose the provenance link; on Postgres
    # the FK would otherwise refuse the job delete.
    db.query(models.Asset).filter(models.Asset.generation_job_id == job_id).update(
        {models.Asset.generation_job_id: None}, synchronize_session="fetch"
    )
    # SQLite hands a deleted max id to the next row, so a clip still being
    # rendered for this job would later fill whatever new job took the id.
    # Point it at id 0 (never used) so the completion handlers no-op; the
    # VideoJob stays active so the sweeper still records its provider spend.
    clip_jobs = db.query(models.VideoJob).filter(
        models.VideoJob.purpose == "studio", models.VideoJob.ref_id == job_id
    )
    stale_clips = [v.result_path for v in clip_jobs if v.result_path and v.result_path != path]
    clip_jobs.update({models.VideoJob.ref_id: 0}, synchronize_session="fetch")
    db.delete(j)
    db.commit()
    for clip in stale_clips:
        try:
            Path(clip).unlink(missing_ok=True)
        except OSError:
            pass
    # Assets saved before saves made copies point at the job's own file.
    if path and not db.query(models.Asset).filter(models.Asset.file_path == path).first():
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass

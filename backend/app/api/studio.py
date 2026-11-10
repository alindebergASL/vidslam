from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import String, case, cast, func, literal, or_
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..schemas import AssetOut, StudioJobOut, StudioRequest
from ..schemas.api import (
    ASSET_TYPES,
    CHARACTER_SHEET_VIEWS,
    CharacterSheetOut,
    CharacterSheetRequest,
    CharacterSheetView,
    StudioBulkDeleteOut,
    StudioBulkFavorite,
    StudioBulkFavoriteOut,
    StudioBulkIds,
    StudioBulkSave,
    StudioBulkSaveOut,
    StudioJobUpdate,
    StudioRetryRequest,
    StudioSaveRequest,
    StudioSkipped,
)
from ..services import provider_gate
from ..services.pipeline import STUDIO_DELETED, save_studio_result_as_asset
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


def _enqueue_jobs(db: Session, jobs: list[models.AssetGenerationJob]) -> list[models.AssetGenerationJob]:
    """Store new jobs (one commit) and enqueue each on its own."""
    db.add_all(jobs)
    db.commit()
    for job in jobs:
        db.refresh(job)
    for job in jobs:
        enqueue(generate_asset_job, job.id)
    return jobs


def _create_jobs(db: Session, body: StudioRequest, output_kind: str, count: int) -> StudioJobOut:
    """`count` separate jobs for one request (same prompt, refs, model and
    aspect), each enqueued on its own. Answers with the first job plus the
    ids of all of them."""
    if not _owner_exists(db, body.owner_kind, body.owner_id):
        raise HTTPException(404, "owner not found")
    # The model's provider switched off in Settings: 409 before anything is queued.
    provider_gate.require_model("image" if output_kind == "image" else "video", body.model or None, db)
    jobs = _enqueue_jobs(db, [
        models.AssetGenerationJob(
            owner_kind=body.owner_kind,
            owner_id=body.owner_id,
            output_kind=output_kind,
            duration_seconds=body.duration_seconds,
            prompt=body.prompt,
            negative_prompt=body.negative_prompt,
            reference_asset_ids_json=list(body.reference_asset_ids),
            aspect_ratio=body.aspect_ratio or "",
            provider_model=body.model or "",
            status="pending",
        )
        for _ in range(count)
    ])
    out = _with_costs(db, [jobs[0]])[0]
    out.batch_ids = [j.id for j in jobs]
    return out


def _with_costs(db: Session, jobs: list[models.AssetGenerationJob]) -> list[StudioJobOut]:
    """The jobs as StudioJobOut with what the spend ledger holds for each:
    one query for the images ("studio_job:<id>") and one for the clips
    (their studio VideoJobs' "video_job:<vid>" entries), whatever the count."""
    Spend, Video = models.SpendEntry, models.VideoJob
    costs: dict[int, tuple[float, bool]] = {}
    estimate = func.max(case((Spend.is_estimate.is_(True), 1), else_=0))
    image_ids = [j.id for j in jobs if j.output_kind == "image"]
    if image_ids:
        by_ref = {f"studio_job:{i}": i for i in image_ids}
        rows = (
            db.query(Spend.ref, func.sum(Spend.cost_usd), estimate)
            .filter(Spend.ref.in_(list(by_ref)))
            .group_by(Spend.ref)
            .all()
        )
        for ref, total, est in rows:
            costs[by_ref[ref]] = (float(total or 0.0), bool(est))
    clip_ids = [j.id for j in jobs if j.output_kind != "image"]
    if clip_ids:
        rows = (
            db.query(Video.ref_id, func.sum(Spend.cost_usd), estimate)
            .join(Spend, Spend.ref == literal("video_job:") + cast(Video.id, String))
            .filter(Video.purpose == "studio", Video.ref_id.in_(clip_ids))
            .group_by(Video.ref_id)
            .all()
        )
        for ref_id, total, est in rows:
            costs[ref_id] = (float(total or 0.0), bool(est))
    out = []
    for j in jobs:
        o = StudioJobOut.model_validate(j)
        if j.id in costs:
            o.cost_usd, o.cost_is_estimate = costs[j.id]
        out.append(o)
    return out


@router.post("/generate-image", response_model=StudioJobOut, status_code=202, dependencies=[Depends(rate_limit("generation")), Depends(budget_guard)])
def generate_image(body: StudioRequest, db: Session = Depends(get_db)) -> StudioJobOut:
    return _create_jobs(db, body, "image", body.variations)


@router.post("/generate-clip", response_model=StudioJobOut, status_code=202, dependencies=[Depends(rate_limit("generation")), Depends(budget_guard)])
def generate_clip(body: StudioRequest, db: Session = Depends(get_db)) -> StudioJobOut:
    if body.variations > 1:
        # Clips are the expensive output; one per request, never N by accident.
        raise HTTPException(422, "variations apply to images only; make one clip at a time")
    return _create_jobs(db, body, "video_clip", 1)


def _live_job(db: Session, job_id: int) -> Optional[models.AssetGenerationJob]:
    """The job, or None when it doesn't exist or was deleted (tombstoned)."""
    j = db.get(models.AssetGenerationJob, job_id)
    return None if j is None or j.status == STUDIO_DELETED else j


# Statuses a job leaves for good; anything else is still being worked on.
_TERMINAL = ("completed", "saved", "failed", STUDIO_DELETED)


@router.get("/jobs", response_model=list[StudioJobOut])
def list_jobs(
    q: Optional[str] = None,
    favorite: Optional[bool] = None,
    output_kind: Optional[Literal["image", "video_clip"]] = None,
    owner_kind: Optional[Literal["avatar", "ingredient"]] = None,
    owner_id: Optional[int] = None,
    status: Optional[Literal["completed", "failed", "active"]] = None,
    limit: int = Query(50, ge=1, le=200),
    before_id: Optional[int] = None,
    db: Session = Depends(get_db),
) -> list[StudioJobOut]:
    """Newest first (by id). Every filter is optional and they combine;
    `before_id` pages back from the last id the caller has."""
    if (owner_kind is None) != (owner_id is None):
        raise HTTPException(422, "owner_kind and owner_id go together")
    Job = models.AssetGenerationJob
    query = db.query(Job).filter(Job.status != STUDIO_DELETED)
    text = (q or "").strip()
    if text:
        pattern = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.filter(or_(Job.prompt.ilike(pattern, escape="\\"), Job.label.ilike(pattern, escape="\\")))
    if favorite:
        query = query.filter(Job.favorite.is_(True))
    if output_kind:
        query = query.filter(Job.output_kind == output_kind)
    if owner_kind is not None:
        query = query.filter(Job.owner_kind == owner_kind, Job.owner_id == owner_id)
    if status == "completed":
        query = query.filter(Job.status.in_(("completed", "saved")))
    elif status == "failed":
        query = query.filter(Job.status == "failed")
    elif status == "active":
        query = query.filter(Job.status.notin_(_TERMINAL))
    if before_id is not None:
        query = query.filter(Job.id < before_id)
    return _with_costs(db, query.order_by(Job.id.desc()).limit(limit).all())


@router.get("/jobs/{job_id}", response_model=StudioJobOut)
def get_job(job_id: int, db: Session = Depends(get_db)) -> StudioJobOut:
    j = _live_job(db, job_id)
    if j is None:
        raise HTTPException(404, "job not found")
    return _with_costs(db, [j])[0]


@router.patch("/jobs/{job_id}", response_model=StudioJobOut)
def update_job(job_id: int, body: StudioJobUpdate, db: Session = Depends(get_db)) -> StudioJobOut:
    j = _live_job(db, job_id)
    if j is None:
        raise HTTPException(404, "job not found")
    if body.favorite is not None:
        j.favorite = body.favorite
    if body.label is not None:
        j.label = body.label
    db.commit()
    db.refresh(j)
    return _with_costs(db, [j])[0]


@router.get("/jobs/{job_id}/preview")
def preview_job(job_id: int, db: Session = Depends(get_db)) -> FileResponse:
    j = _live_job(db, job_id)
    if j is None or not j.result_path or not Path(j.result_path).exists():
        raise HTTPException(404, "result not ready")
    media = "image/png" if j.output_kind == "image" else "video/mp4"
    return FileResponse(j.result_path, media_type=media)


def _check_asset_type(kind: str, asset_type: Optional[str]) -> None:
    if asset_type is not None and asset_type not in ASSET_TYPES.get(kind, ()):
        raise HTTPException(422, f"asset_type must be one of {', '.join(ASSET_TYPES.get(kind, ()))}")


def _unsavable(j: models.AssetGenerationJob) -> Optional[str]:
    """Why a job's result can't be filed as an asset, or None when it can."""
    if j.output_kind != "image":
        return "only image studio results can be saved as cast assets"
    if j.status not in ("completed", "saved") or not j.result_path or not Path(j.result_path).exists():
        return "studio job is not completed"
    return None


@router.post("/jobs/{job_id}/save", response_model=AssetOut)
def save_job(
    job_id: int,
    asset_type: Optional[str] = None,
    body: Optional[StudioSaveRequest] = Body(None),
    db: Session = Depends(get_db),
) -> models.Asset:
    j = _live_job(db, job_id)
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
    _check_asset_type(kind, chosen)
    reason = _unsavable(j)
    if reason:
        raise HTTPException(400, reason)
    if not _owner_exists(db, kind, owner_id):
        raise HTTPException(404, "owner not found")
    try:
        return save_studio_result_as_asset(
            db, j, asset_type=chosen, owner_kind=kind, owner_id=owner_id
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/jobs/bulk-save", response_model=StudioBulkSaveOut)
def bulk_save(body: StudioBulkSave, db: Session = Depends(get_db)) -> StudioBulkSaveOut:
    """File several results to one owner. A job that can't be saved is
    reported in `skipped`; only a bad owner or asset type fails the batch."""
    _check_asset_type(body.owner_kind, body.asset_type)
    if not _owner_exists(db, body.owner_kind, body.owner_id):
        raise HTTPException(404, "owner not found")
    saved: list[AssetOut] = []
    skipped: list[StudioSkipped] = []
    for job_id in body.ids:
        j = _live_job(db, job_id)
        reason = "not found" if j is None else _unsavable(j)
        if reason is None:
            try:
                asset = save_studio_result_as_asset(
                    db, j, asset_type=body.asset_type, owner_kind=body.owner_kind, owner_id=body.owner_id
                )
                saved.append(AssetOut.model_validate(asset))
                continue
            except ValueError as e:
                db.rollback()
                reason = str(e)
        skipped.append(StudioSkipped(id=job_id, reason=reason))
    return StudioBulkSaveOut(saved=saved, skipped=skipped)


def _tombstone(db: Session, j: models.AssetGenerationJob) -> list[str]:
    """Delete one job in the open transaction (the caller commits). Answers
    the files to clean up once committed: stale clip renders, and the job's
    own result (only removed when no asset still uses it)."""
    path = j.result_path
    # Assets keep their file and just lose the provenance link; on Postgres
    # the FK would otherwise refuse the job delete.
    db.query(models.Asset).filter(models.Asset.generation_job_id == j.id).update(
        {models.Asset.generation_job_id: None}, synchronize_session="fetch"
    )
    # SQLite hands a deleted max id to the next row, so a clip still being
    # rendered for this job would later fill whatever new job took the id.
    # Point it at id 0 (never used) so the completion handlers no-op; the
    # VideoJob stays active so the sweeper still records its provider spend.
    clip_jobs = db.query(models.VideoJob).filter(
        models.VideoJob.purpose == "studio", models.VideoJob.ref_id == j.id
    )
    stale_clips = [v.result_path for v in clip_jobs if v.result_path and v.result_path != path]
    clip_jobs.update({models.VideoJob.ref_id: 0}, synchronize_session="fetch")
    # Tombstone rather than delete: on SQLite a deleted max id is handed to
    # the next job, and an image still being generated for this one would
    # then fill that new job. The row stays (hidden everywhere) so the id is
    # never reused; the worker drops a late result for a deleted job.
    j.status = STUDIO_DELETED
    j.result_path = ""
    j.result_asset_id = None
    return stale_clips + ([path] if path else [])


def _remove_files(db: Session, paths: list[str]) -> None:
    """After the commit: unlink each file no asset points at (assets saved
    before saves made copies point at the job's own file)."""
    for p in paths:
        if db.query(models.Asset).filter(models.Asset.file_path == p).first():
            continue
        try:
            Path(p).unlink(missing_ok=True)
        except OSError:
            pass


@router.delete("/jobs/{job_id}", status_code=204)
def delete_job(job_id: int, db: Session = Depends(get_db)) -> None:
    j = _live_job(db, job_id)
    if j is None:
        raise HTTPException(404, "job not found")
    files = _tombstone(db, j)
    db.commit()
    _remove_files(db, files)


@router.post("/jobs/bulk-delete", response_model=StudioBulkDeleteOut)
def bulk_delete(body: StudioBulkIds, db: Session = Depends(get_db)) -> StudioBulkDeleteOut:
    deleted: list[int] = []
    missing: list[int] = []
    files: list[str] = []
    for job_id in body.ids:
        j = _live_job(db, job_id)
        if j is None:
            missing.append(job_id)
            continue
        files += _tombstone(db, j)
        deleted.append(job_id)
    db.commit()
    _remove_files(db, files)
    return StudioBulkDeleteOut(deleted=deleted, missing=missing)


@router.post("/jobs/bulk-favorite", response_model=StudioBulkFavoriteOut)
def bulk_favorite(body: StudioBulkFavorite, db: Session = Depends(get_db)) -> StudioBulkFavoriteOut:
    updated: list[int] = []
    missing: list[int] = []
    for job_id in body.ids:
        j = _live_job(db, job_id)
        if j is None:
            missing.append(job_id)
            continue
        j.favorite = body.favorite
        updated.append(job_id)
    db.commit()
    return StudioBulkFavoriteOut(updated=updated, missing=missing)


@router.post("/jobs/{job_id}/retry", response_model=StudioJobOut, status_code=202,
             dependencies=[Depends(rate_limit("generation")), Depends(budget_guard)])
def retry_job(job_id: int, body: Optional[StudioRetryRequest] = Body(None),
              db: Session = Depends(get_db)) -> StudioJobOut:
    """Run a job again as a new job (failed or completed alike), with any
    of model, prompt, avoid text and aspect changed."""
    src = _live_job(db, job_id)
    if src is None:
        raise HTTPException(404, "job not found")
    body = body or StudioRetryRequest()
    if not _owner_exists(db, src.owner_kind, src.owner_id):
        raise HTTPException(404, "owner not found")
    model = body.model or src.provider_model or ""
    provider_gate.require_model("image" if src.output_kind == "image" else "video", model or None, db)
    job = models.AssetGenerationJob(
        owner_kind=src.owner_kind,
        owner_id=src.owner_id,
        output_kind=src.output_kind,
        duration_seconds=src.duration_seconds,
        prompt=body.prompt if body.prompt is not None else src.prompt,
        negative_prompt=body.negative_prompt if body.negative_prompt is not None else (src.negative_prompt or ""),
        reference_asset_ids_json=list(src.reference_asset_ids_json or []),
        aspect_ratio=body.aspect_ratio or src.aspect_ratio or "",
        label=src.label or "",
        provider_model=model,
        status="pending",
    )
    _enqueue_jobs(db, [job])
    out = _with_costs(db, [job])[0]
    out.batch_ids = [job.id]
    return out


# ---------- character sheet ----------

# An avatar's own pictures that show the character best, in this order.
_SHEET_REF_TYPES = ("hero", "reference_sheet", "expression_sheet", "outfit_sheet", "lifestyle", "other")
_SHEET_MAX_REFS = 4


def _sheet_references(db: Session, avatar_id: int) -> list[int]:
    """Up to four of the avatar's image assets: hero and reference sheets
    first, then the other picture types (never the logo), newest first."""
    assets = (
        db.query(models.Asset)
        .filter(models.Asset.avatar_id == avatar_id, models.Asset.mime_type.like("image/%"),
                models.Asset.asset_type.in_(_SHEET_REF_TYPES))
        .order_by(models.Asset.created_at.desc(), models.Asset.id.desc())
        .all()
    )
    assets.sort(key=lambda a: _SHEET_REF_TYPES.index(a.asset_type))  # stable: newest first per type
    return [a.id for a in assets[:_SHEET_MAX_REFS]]


def _sheet_prompt(avatar: models.Avatar, view: str, style: str) -> str:
    _label, _aspect, shot = CHARACTER_SHEET_VIEWS[view]
    parts = [
        f"Character reference picture of {avatar.name}: {shot}.",
        "The same person as in the reference pictures: same face, hair, body and outfit.",
    ]
    look = " ".join(x.strip() for x in (avatar.visual_identity, avatar.description) if x and x.strip())
    if look:
        parts.append(look if look.endswith(".") else look + ".")
    style = style.strip()
    if "background" not in style.lower():
        # One backdrop for every view, so the sheet reads as a set.
        parts.append("Plain, evenly lit light grey background, the same in every picture.")
    if style:
        parts.append(style if style.endswith(".") else style + ".")
    return " ".join(parts)


@router.get("/character-sheet/views", response_model=list[CharacterSheetView])
def character_sheet_views() -> list[CharacterSheetView]:
    return [CharacterSheetView(id=k, label=label, aspect_ratio=aspect)
            for k, (label, aspect, _shot) in CHARACTER_SHEET_VIEWS.items()]


@router.post("/character-sheet", response_model=CharacterSheetOut, status_code=202,
             dependencies=[Depends(rate_limit("generation")), Depends(budget_guard)])
def character_sheet(body: CharacterSheetRequest, db: Session = Depends(get_db)) -> CharacterSheetOut:
    """One image job per view of a Cast member, all from the same
    references, each labelled with its view."""
    avatar = db.get(models.Avatar, body.avatar_id)
    if avatar is None:
        raise HTTPException(404, "owner not found")
    refs = list(body.reference_asset_ids or []) or _sheet_references(db, avatar.id)
    if not refs:
        raise HTTPException(422, f"add a picture of {avatar.name} first")
    provider_gate.require_model("image", body.model or None, db)
    jobs = _enqueue_jobs(db, [
        models.AssetGenerationJob(
            owner_kind="avatar",
            owner_id=avatar.id,
            output_kind="image",
            prompt=_sheet_prompt(avatar, view, body.style),
            negative_prompt=body.negative_prompt,
            reference_asset_ids_json=refs,
            aspect_ratio=CHARACTER_SHEET_VIEWS[view][1],
            label=CHARACTER_SHEET_VIEWS[view][0],
            provider_model=body.model or "",
            status="pending",
        )
        for view in body.views
    ])
    return CharacterSheetOut(batch_ids=[j.id for j in jobs], jobs=_with_costs(db, jobs))

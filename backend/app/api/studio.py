from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import case, func, or_
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
    ImageOpEstimateOut,
    ImageOpModelOut,
    StudioBulkDeleteOut,
    StudioBulkFavorite,
    StudioBulkFavoriteOut,
    StudioBulkIds,
    StudioBulkSave,
    StudioBulkSaveOut,
    StudioContinueInfo,
    StudioContinueRequest,
    StudioFrameRequest,
    StudioJobUpdate,
    StudioJoinRequest,
    StudioRemoveBackgroundRequest,
    StudioRetryRequest,
    StudioSaveRequest,
    StudioSkipped,
    StudioUpscaleRequest,
)
from ..services import provider_gate
from ..services.model_registry import LOCAL_PREFIX
from ..services.pipeline import (
    STUDIO_DELETED,
    options_with_audio,
    save_studio_result_as_asset,
    studio_clip_audio,
)
from ..services.ratelimit import rate_limit
from ..services.spend import budget_guard
from ..services.storage import drop_reference_copy
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
    """Store new jobs (one commit) and enqueue each on its own. When the
    queue refuses one, it and the rest are marked failed (nothing would
    ever run them) before the error goes back to the caller."""
    db.add_all(jobs)
    db.commit()
    for job in jobs:
        db.refresh(job)
    for i, job in enumerate(jobs):
        try:
            qid = enqueue(generate_asset_job, job.id)
            # The sweeper leaves a pending job alone while this entry waits.
            db.query(models.AssetGenerationJob).filter(
                models.AssetGenerationJob.id == job.id, models.AssetGenerationJob.status == "pending",
            ).update({"queue_job_id": str(qid)[:80]}, synchronize_session=False)
            db.commit()
        except Exception:
            db.rollback()
            left = [j.id for j in jobs[i:]]
            db.query(models.AssetGenerationJob).filter(
                models.AssetGenerationJob.id.in_(left),
                models.AssetGenerationJob.status == "pending",
            ).update({"status": "failed", "error": "Couldn't queue this job"}, synchronize_session=False)
            db.commit()
            raise
    return jobs


def _video_model(db: Session, model: Optional[str]) -> str:
    from ..services.model_registry import active_model
    from ..services.pipeline import _resolve_model

    return _resolve_model(db, model or active_model("video"))


def _check_first_frame(db: Session, output_kind: str, model: Optional[str],
                       job_id: Optional[int], asset_id: Optional[int]) -> None:
    """A clip that starts from a picture: the picture is usable and the
    video model can start from one (422 before anything is queued)."""
    if job_id is None and asset_id is None:
        return
    if output_kind != "video_clip":
        raise HTTPException(422, "Only clips can start from a picture")
    if job_id is not None and asset_id is not None:
        raise HTTPException(422, "Pick one picture to start from, not two")
    if job_id is not None:
        src = _live_job(db, job_id)
        if src is None:
            raise HTTPException(404, "The picture to start from was deleted")
        if (src.output_kind != "image" or src.status not in ("completed", "saved")
                or not src.result_path or not Path(src.result_path).exists()):
            raise HTTPException(422, "Only a finished Studio picture can start a clip")
    else:
        asset = db.get(models.Asset, asset_id)
        if asset is None:
            raise HTTPException(404, "The picture to start from was deleted")
        if not (asset.mime_type or "").startswith("image/"):
            raise HTTPException(422, "Only a picture can start a clip")
    from ..providers import video_capabilities

    resolved = _video_model(db, model)
    if not video_capabilities(resolved).first_frame:
        raise HTTPException(422, f"{resolved or 'This model'} can't start from a picture; pick another model")


def _check_safety(prompt: str, negative: str, output_kind: str) -> None:
    """The safety check now, as the worker will run it (same avoid list,
    so the worker's check is a cache hit): a refusal is a 422."""
    from ..services.minor_check import check_or_422
    from ..services.prompting import effective_negative

    kind = "image" if output_kind == "image" else "video"
    check_or_422(prompt or "", effective_negative(kind, negative or ""), kind=kind)


def _create_jobs(db: Session, body: StudioRequest, output_kind: str, count: int, *,
                 options: Optional[dict] = None) -> StudioJobOut:
    """`count` separate jobs for one request (same prompt, refs, model and
    aspect), each enqueued on its own. Answers with the first job plus the
    ids of all of them. `options`: each job's options_json (a continuation's
    intent)."""
    if not _owner_exists(db, body.owner_kind, body.owner_id):
        raise HTTPException(404, "owner not found")
    # The model's provider switched off in Settings: 409 before anything is queued.
    provider_gate.require_model("image" if output_kind == "image" else "video", body.model or None, db)
    _check_first_frame(db, output_kind, body.model or None, body.first_frame_job_id, body.first_frame_asset_id)
    _check_safety(body.prompt, body.negative_prompt, output_kind)
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
            first_frame_job_id=body.first_frame_job_id,
            first_frame_asset_id=body.first_frame_asset_id,
            # A clip keeps its sound choice (Retry and Try another model reuse
            # it), beside `options` (a continuation's intent).
            options_json=(options_with_audio(options, body.audio) if output_kind == "video_clip"
                          else dict(options) if options else None),
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
        # Two indexed lookups: the clips' video jobs, then their ledger rows.
        owner = {f"video_job:{vid}": ref_id for vid, ref_id in
                 db.query(Video.id, Video.ref_id).filter(Video.purpose == "studio", Video.ref_id.in_(clip_ids))}
        if owner:
            rows = (
                db.query(Spend.ref, func.sum(Spend.cost_usd), estimate)
                .filter(Spend.ref.in_(list(owner)))
                .group_by(Spend.ref)
                .all()
            )
            for ref, total, est in rows:
                prev_total, prev_est = costs.get(owner[ref], (0.0, False))
                costs[owner[ref]] = (prev_total + float(total or 0.0), prev_est or bool(est))
    # Reference pictures deleted since the job was made (Remix drops them),
    # and what the others are (for ref_intent).
    wanted = {i for j in jobs for i in _ref_ids(j)}
    found = ({a: (kind, atype) for a, kind, atype in db.query(
        models.Asset.id, models.Asset.owner_kind, models.Asset.asset_type).filter(models.Asset.id.in_(wanted))}
             if wanted else {})
    out = []
    for j in jobs:
        o = StudioJobOut.model_validate(j)
        if j.id in costs:
            o.cost_usd, o.cost_is_estimate = costs[j.id]
        ids = _ref_ids(j)
        o.missing_reference_ids = [i for i in ids if i not in found]
        if j.output_kind == "image" and not j.operation:
            o.ref_intent = _ref_intent([found[i] for i in ids if i in found])
        out.append(o)
    return out


def _ref_intent(pictures: list[tuple]) -> Optional[str]:
    """What a picture's reference pictures (owner kind, asset type) are for
    when Sogni picks an editor, as sogni.reference_intent decides from
    pipeline._asset_refs' roles: a Cast character's picture, not its logo,
    keeps a likeness ("identity"); any other picture guides ("guide")."""
    if not pictures:
        return None
    return "identity" if any(kind == "avatar" and atype != "logo" for kind, atype in pictures) else "guide"


def _ref_ids(j: models.AssetGenerationJob) -> list[int]:
    out = []
    for x in j.reference_asset_ids_json or []:
        text = str(x).strip()
        if text.isascii() and text.isdecimal():
            out.append(int(text))
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
    ids: Optional[str] = Query(None, description="comma-separated job ids (max 200); ignores other filters"),
    db: Session = Depends(get_db),
) -> list[StudioJobOut]:
    """Newest first (by id). Every filter is optional and they combine;
    `before_id` pages back from the last id the caller has. `ids` fetches
    just those jobs (the page's batched poll), whatever the other filters."""
    Job = models.AssetGenerationJob
    if ids is not None:
        wanted: list[int] = []
        for part in ids.split(","):
            part = part.strip()
            if not part:
                continue
            if not (part.isascii() and part.isdecimal()):
                raise HTTPException(422, "ids must be comma-separated numbers")
            wanted.append(int(part))
        wanted = list(dict.fromkeys(wanted))
        if len(wanted) > 200:
            raise HTTPException(422, "at most 200 ids")
        if not wanted:
            return []
        rows = (db.query(Job).filter(Job.id.in_(wanted), Job.status != STUDIO_DELETED)
                .order_by(Job.id.desc()).all())
        return _with_costs(db, rows)
    if (owner_kind is None) != (owner_id is None):
        raise HTTPException(422, "owner_kind and owner_id go together")
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
    return FileResponse(j.result_path, media_type=media_type_of(j.result_path, j.output_kind))


_MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
                ".gif": "image/gif", ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime"}


def media_type_of(path: str, output_kind: str = "image") -> str:
    """From the file's real suffix (providers return PNG, JPEG or WebP)."""
    return _MEDIA_TYPES.get(Path(path).suffix.lower(), "image/png" if output_kind == "image" else "video/mp4")


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
        drop_reference_copy(Path(p))
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


@router.post("/jobs/{job_id}/cancel", response_model=StudioJobOut)
def cancel_job(job_id: int, db: Session = Depends(get_db)) -> StudioJobOut:
    """Stop waiting for a running job: it is marked failed ("Cancelled").
    There is no provider cancel, so a clip already sent may still finish
    and be billed; its video job stays tracked so that spend is recorded.
    A job that already finished is answered unchanged."""
    from ..services import events
    from ..services.pipeline import STUDIO_RUNNING

    j = _live_job(db, job_id)
    if j is None:
        raise HTTPException(404, "job not found")
    done = db.query(models.AssetGenerationJob).filter(
        models.AssetGenerationJob.id == job_id,
        models.AssetGenerationJob.status.in_(STUDIO_RUNNING),
    ).update({"status": "failed", "error": "Cancelled"}, synchronize_session=False)
    if done:
        db.refresh(j)
        events.studio_event(db, j, "cancelled")
    db.commit()
    db.refresh(j)
    return _with_costs(db, [j])[0]


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
             dependencies=[Depends(rate_limit("generation"))])
def retry_job(job_id: int, response: Response, body: Optional[StudioRetryRequest] = Body(None),
              db: Session = Depends(get_db)) -> StudioJobOut:
    """Run a job again as a new job (failed or completed alike), with any
    of model, prompt, avoid text, aspect and (a clip's) sound changed."""
    src = _live_job(db, job_id)
    if src is None:
        raise HTTPException(404, "job not found")
    body = body or StudioRetryRequest()
    if not _owner_exists(db, src.owner_kind, src.owner_id):
        raise HTTPException(404, "owner not found")
    if src.operation == "extract_frame":
        # Grabs the same frame of the same clip again (free, no model).
        if src.source_job_id is None:
            raise HTTPException(409, "The uploaded video isn't kept; upload it again to save another frame")
        from ..services import video_ops

        return _frame_from_clip(db, src.source_job_id, video_ops.at_of(src), response)
    if src.operation == "upload_video":
        raise HTTPException(409, "An uploaded video can't be made again; upload it again")
    if src.operation == "join_clips":
        # Joins the same clips again, the same way (free, no model).
        from ..services import video_ops

        ids, transition, crossfade = video_ops.join_options(src)
        try:
            return _join(db, ids, transition, crossfade, response, replaces=src)
        except HTTPException as e:
            if e.status_code == 404:
                # A clip it joined was deleted since: it can't be made again.
                raise HTTPException(409, f"{e.detail}, so these clips can't be joined again") from e
            raise
    if src.operation:
        # Runs the same operation again on the same picture (the prompt,
        # avoid text and aspect don't apply); a model given replaces it.
        from ..services import image_ops

        if src.source_job_id is None and src.source_asset_id is None:
            raise HTTPException(404, "The original picture was deleted")
        return _queue_operation(db, src.operation, body.model or src.provider_model or None,
                                image_ops.factor_of(src), source_job_id=src.source_job_id,
                                source_asset_id=src.source_asset_id, label=src.label or None)
    from ..services import video_ops

    budget_guard()
    model = body.model or src.provider_model or ""
    provider_gate.require_model("image" if src.output_kind == "image" else "video", model or None, db)
    _check_first_frame(db, src.output_kind, model or None, src.first_frame_job_id, src.first_frame_asset_id)
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
        first_frame_job_id=src.first_frame_job_id,
        first_frame_asset_id=src.first_frame_asset_id,
        # A clip asks for sound as the one it remakes did, unless the body
        # says. A continuation run again stays one (and joins with its
        # original once finished, when it was asked to).
        options_json=(options_with_audio(video_ops.continuation_of(src),
                                         studio_clip_audio(src) if body.audio is None else body.audio)
                      if src.output_kind == "video_clip" else None),
        status="pending",
    )
    _check_safety(job.prompt, job.negative_prompt, job.output_kind)
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


def _checked_sheet_references(db: Session, avatar: models.Avatar, ids: list[int]) -> list[int]:
    """The caller's own reference list, held to the same rules as the
    default pick: each id is a picture of this avatar, never its logo."""
    found = {a.id: a for a in db.query(models.Asset).filter(models.Asset.id.in_(ids)).all()}
    for i in ids:
        a = found.get(i)
        if a is None:
            raise HTTPException(422, f"reference picture {i} not found")
        if not (a.mime_type or "").startswith("image/"):
            raise HTTPException(422, f"reference {i} is not a picture")
        if a.avatar_id != avatar.id:
            raise HTTPException(422, f"reference picture {i} is not a picture of {avatar.name}")
        if a.asset_type == "logo":
            raise HTTPException(422, f"reference {i} is a logo, not a picture of {avatar.name}")
    return list(dict.fromkeys(ids))


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
    if body.reference_asset_ids:
        refs = _checked_sheet_references(db, avatar, list(body.reference_asset_ids))
    else:
        refs = _sheet_references(db, avatar.id)
    if not refs:
        raise HTTPException(422, f"add a picture of {avatar.name} first")
    provider_gate.require_model("image", body.model or None, db)
    for view in body.views:
        _check_safety(_sheet_prompt(avatar, view, body.style), body.negative_prompt, "image")
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


# ---------- picture operations: remove background, upscale ----------


def _queue_operation(db: Session, op: str, model: Optional[str], factor: int, *,
                     source_job_id: Optional[int] = None, source_asset_id: Optional[int] = None,
                     label: Optional[str] = None) -> StudioJobOut:
    """Check and queue one operation job; it lands in Studio as a new
    pending tile owned by the source's owner."""
    from ..services import image_ops

    try:
        job = image_ops.new_job(db, op, model, factor, source_job_id=source_job_id,
                                source_asset_id=source_asset_id, label=label)
    except image_ops.OpError as e:
        raise HTTPException(e.status, e.message) from e
    if not _owner_exists(db, job.owner_kind, job.owner_id):
        raise HTTPException(404, "owner not found")
    # The free upscale costs nothing, so the monthly cap doesn't stop it (the
    # rate limit still applies: it uses this machine's CPU).
    if not (job.provider_model or "").startswith(LOCAL_PREFIX):
        budget_guard()
    _enqueue_jobs(db, [job])
    out = _with_costs(db, [job])[0]
    out.batch_ids = [job.id]
    return out


@router.post("/jobs/{job_id}/remove-background", response_model=StudioJobOut, status_code=202,
             dependencies=[Depends(rate_limit("generation"))])
def remove_background_job(job_id: int, body: Optional[StudioRemoveBackgroundRequest] = Body(None),
                          db: Session = Depends(get_db)) -> StudioJobOut:
    """A new job: this finished Studio picture with its background removed
    (transparent PNG)."""
    body = body or StudioRemoveBackgroundRequest()
    return _queue_operation(db, "remove_background", body.model or None, 1, source_job_id=job_id)


@router.post("/jobs/{job_id}/upscale", response_model=StudioJobOut, status_code=202,
             dependencies=[Depends(rate_limit("generation"))])
def upscale_job(job_id: int, body: Optional[StudioUpscaleRequest] = Body(None),
                db: Session = Depends(get_db)) -> StudioJobOut:
    """A new job: this finished Studio picture ×2 or ×4 larger."""
    body = body or StudioUpscaleRequest()
    return _queue_operation(db, "upscale", body.model or None, body.factor, source_job_id=job_id)


@router.post("/assets/{asset_id}/remove-background", response_model=StudioJobOut, status_code=202,
             dependencies=[Depends(rate_limit("generation"))])
def remove_background_asset(asset_id: int, body: Optional[StudioRemoveBackgroundRequest] = Body(None),
                            db: Session = Depends(get_db)) -> StudioJobOut:
    """A Cast (or ingredient) picture with its background removed; the
    result is a Studio job owned by that cast member."""
    body = body or StudioRemoveBackgroundRequest()
    return _queue_operation(db, "remove_background", body.model or None, 1, source_asset_id=asset_id)


@router.post("/assets/{asset_id}/upscale", response_model=StudioJobOut, status_code=202,
             dependencies=[Depends(rate_limit("generation"))])
def upscale_asset(asset_id: int, body: Optional[StudioUpscaleRequest] = Body(None),
                  db: Session = Depends(get_db)) -> StudioJobOut:
    """A Cast (or ingredient) picture ×2 or ×4 larger, as a Studio job."""
    body = body or StudioUpscaleRequest()
    return _queue_operation(db, "upscale", body.model or None, body.factor, source_asset_id=asset_id)


@router.get("/operations/estimate", response_model=ImageOpEstimateOut)
def operation_estimate(
    op: Literal["remove_background", "upscale"],
    model: Optional[str] = Query(None, max_length=300),
    factor: int = Query(2, description="2 or 4 (upscale only)"),
    source_job_id: Optional[int] = None,
    source_asset_id: Optional[int] = None,
    db: Session = Depends(get_db),
) -> ImageOpEstimateOut:
    """Rough cost and output size of a run, before it runs (nothing is
    queued or billed). Without a source the price is for a 1 MP picture."""
    from ..services import image_ops

    if source_job_id is not None and source_asset_id is not None:
        raise HTTPException(422, "Pick one picture, not two")
    try:
        return ImageOpEstimateOut(**image_ops.estimate(
            db, op, model or None, int(factor), source_job_id=source_job_id, source_asset_id=source_asset_id))
    except image_ops.OpError as e:
        raise HTTPException(e.status, e.message) from e


@router.get("/operations/models", response_model=list[ImageOpModelOut])
def operation_models(op: Literal["remove_background", "upscale"]) -> list[ImageOpModelOut]:
    """The models offered for an operation, each with its price in plain
    words (Try another model, the Upscale dialog)."""
    from ..services import image_ops

    return [ImageOpModelOut(**m) for m in image_ops.models_for(op)]


# ---------- save a frame of a video as a picture ----------


def _frame_from_clip(db: Session, job_id: int, at, response: Response) -> StudioJobOut:
    """A new Studio picture: the frame of a finished Studio clip at `at`.
    Grabbed here and finished before answering (ffmpeg is in the API
    image), else queued for the worker (202). Free: no model, no spend, no
    budget cap. No new safety check, except for a clip with pictures of an
    uploaded video (video_ops.checked_grab)."""
    from ..services import video_ops

    try:
        src = video_ops.source_clip(db, job_id)
    except video_ops.OpError as e:
        raise HTTPException(e.status, e.message) from e
    if not _owner_exists(db, src.owner_kind, src.owner_id):
        raise HTTPException(404, "owner not found")
    if not video_ops.ffmpeg_here():
        job = video_ops.new_clip_job(db, src, at)
        _enqueue_jobs(db, [job])
        response.status_code = 202
    else:
        job = _grab_now(db, src, at)
        response.status_code = 201
    out = _with_costs(db, [job])[0]
    out.batch_ids = [job.id]
    return out


def _grab_now(db: Session, src: models.AssetGenerationJob, at) -> models.AssetGenerationJob:
    """The frame of `src` at `at`, grabbed here and stored as a finished
    Studio picture (ffmpeg must be here)."""
    from ..services import video_ops

    try:
        data, seconds = video_ops.checked_grab(db, src, at)
    except RuntimeError as e:
        raise HTTPException(422, "Couldn't read a frame from that clip") from e
    except video_ops.OpError as e:
        raise HTTPException(e.status, e.message) from e
    job = video_ops.new_clip_job(db, src, at, seconds=seconds)
    job.status = "submitted"
    db.add(job)
    db.commit()
    db.refresh(job)
    return video_ops.finish(db, job, data, seconds)


@router.post("/jobs/{job_id}/frame-to-picture", response_model=StudioJobOut, status_code=201,
             dependencies=[Depends(rate_limit("generation"))])
def frame_to_picture(job_id: int, response: Response, body: Optional[StudioFrameRequest] = Body(None),
                     db: Session = Depends(get_db)) -> StudioJobOut:
    """Save a frame of a finished Studio clip ("first", "last" or a time in
    seconds) as a new Studio picture owned by the clip's owner (free)."""
    from ..services import video_ops

    body = body or StudioFrameRequest()
    try:
        at = video_ops.parse_at(body.at)
    except video_ops.OpError as e:
        raise HTTPException(e.status, e.message) from e
    return _frame_from_clip(db, job_id, at, response)


@router.post("/frame-from-video", response_model=StudioJobOut, status_code=201,
             dependencies=[Depends(rate_limit("generation"))])
def frame_from_video(
    file: UploadFile = File(...),
    owner_kind: Literal["avatar", "ingredient"] = Form(...),
    owner_id: int = Form(...),
    at: str = Form("first", max_length=20),
    db: Session = Depends(get_db),
) -> StudioJobOut:
    """Save a frame of a video uploaded from this device ("first", "last" or
    a time in seconds) as a new Studio picture owned by `owner` (free).
    The video isn't kept. The frame comes from outside the app, so the
    picture safety check runs on it before it is stored, and refuses it
    when it can't run (see services/video_ops.py)."""
    from ..config import get_settings
    from ..services import video_ops

    if not _owner_exists(db, owner_kind, owner_id):
        raise HTTPException(404, "owner not found")
    if not video_ops.looks_like_video(file.content_type or "", file.filename or ""):
        raise HTTPException(422, video_ops.NOT_A_VIDEO)
    try:
        job = video_ops.frame_from_upload(
            db, owner_kind=owner_kind, owner_id=owner_id, file=file.file, filename=file.filename or "",
            at=video_ops.parse_at(at), limit=get_settings().max_upload_bytes)
    except video_ops.OpError as e:
        raise HTTPException(e.status, e.message) from e
    out = _with_costs(db, [job])[0]
    out.batch_ids = [job.id]
    return out


# ---------- join clips ----------


def _join(db: Session, job_ids: list[int], transition: str, crossfade: float, response: Response, *,
          replaces: Optional[models.AssetGenerationJob] = None) -> StudioJobOut:
    """Check the clips and queue one job that joins them (202): a new
    Studio clip owned by the first clip's owner. Free: no model, no spend,
    no budget cap, but at most JOIN_MAX_RUNNING at once (429). No new
    safety check: see services/video_ops.py. `replaces`: the failed join
    this one retries; a continuation's auto-join stays linked to it."""
    from ..services import video_ops

    try:
        sources = video_ops.join_sources(db, job_ids)
    except video_ops.OpError as e:
        raise HTTPException(e.status, e.message) from e
    first = sources[0]
    if not _owner_exists(db, first.owner_kind, first.owner_id):
        raise HTTPException(404, "owner not found")
    busy = HTTPException(429, f"{video_ops.JOIN_MAX_RUNNING} joins are already running; "
                              "try again when one has finished")
    if video_ops.join_busy(db):
        raise busy
    job = video_ops.new_join_job(sources, transition, crossfade)
    # Stored first, then all the joins holding a place counted again (this
    # one among them): of requests that passed the check above together,
    # any that sees the cap passed is removed before it is ever queued.
    # Counting every join, not only those with a lower id (on Postgres ids
    # aren't handed out in commit order), two racing requests may both be
    # refused; never both queued past the cap.
    db.add(job)
    db.commit()
    if video_ops.joins_running(db) > video_ops.JOIN_MAX_RUNNING:
        db.delete(job)
        db.commit()
        raise busy
    if replaces is not None:
        # Committed with the queueing below.
        video_ops.relink_auto_join(db, replaces, job)
    _enqueue_jobs(db, [job])
    db.refresh(job)
    response.status_code = 202
    out = _with_costs(db, [job])[0]
    out.batch_ids = [job.id]
    return out


@router.post("/join-clips", response_model=StudioJobOut, status_code=202,
             dependencies=[Depends(rate_limit("generation"))])
def join_clips(body: StudioJoinRequest, response: Response, db: Session = Depends(get_db)) -> StudioJobOut:
    """One new Studio clip that plays 2 to 12 finished Studio clips back to
    back, in the order given (a clip may repeat), with a cut or a short
    dissolve between them. Free (made here by ffmpeg)."""
    from ..services import video_ops

    try:
        crossfade = video_ops.parse_crossfade(body.transition, body.crossfade)
    except video_ops.OpError as e:
        raise HTTPException(e.status, e.message) from e
    return _join(db, list(body.job_ids), body.transition, crossfade, response)


# ---------- continue a clip ----------


def _continue_model(db: Session, src: models.AssetGenerationJob) -> str:
    """The model a continuation of `src` uses when none is picked: the model
    of the clip it follows on from when that one can start from a picture
    and its provider is on, else the default video model when it can; ""
    when neither can start from a picture. (A default whose provider is off
    is still answered, so the request gets the plain "switched off" 409.)"""
    from ..providers import video_capabilities
    from ..services import video_ops
    from ..services.provider_keys import ProviderUnavailable

    def first_frame(model: str) -> bool:
        try:
            return bool(video_capabilities(_video_model(db, model)).first_frame)
        except Exception:  # noqa: BLE001 — an old or unknown model: not usable
            return False

    def gate_open(model: str) -> bool:
        try:
            provider_gate.require_model("video", model, db)
        except ProviderUnavailable:
            return False
        return True

    origin = video_ops.continue_origin(db, src)
    own = (origin.provider_model if origin is not None else "") or ""
    if own and not own.startswith(LOCAL_PREFIX) and first_frame(own) and gate_open(own):
        return own
    default = _video_model(db, None)
    return default if first_frame(default) else ""


def _continue_source(db: Session, job_id: int) -> models.AssetGenerationJob:
    from ..services import video_ops

    try:
        src = video_ops.source_clip(db, job_id)
    except video_ops.OpError as e:
        raise HTTPException(e.status, e.message) from e
    if not _owner_exists(db, src.owner_kind, src.owner_id):
        raise HTTPException(404, "owner not found")
    return src


@router.get("/jobs/{job_id}/continue", response_model=StudioContinueInfo)
def continue_info(job_id: int, db: Session = Depends(get_db)) -> StudioContinueInfo:
    """What "Continue this clip" starts from (nothing is made)."""
    from ..services import video_ops

    src = _continue_source(db, job_id)
    origin = video_ops.continue_origin(db, src)
    return StudioContinueInfo(
        prompt=video_ops.continue_prompt(db, src),
        negative_prompt=(origin.negative_prompt or "") if origin is not None else "",
        aspect_ratio=video_ops.continue_aspect(src),
        model=_continue_model(db, src),
        duration_seconds=float(origin.duration_seconds or 5.0) if origin is not None else 5.0,
        audio=video_ops.continue_audio(db, src),
    )


@router.post("/jobs/{job_id}/continue", response_model=StudioJobOut, status_code=202,
             dependencies=[Depends(rate_limit("generation")), Depends(budget_guard)])
def continue_clip(job_id: int, body: StudioContinueRequest, db: Session = Depends(get_db)) -> StudioJobOut:
    """A new clip that carries on from this finished Studio clip (generated,
    joined, continued or uploaded): its last frame is saved as a Studio
    picture and the new clip starts from it, from `prompt` (paid like any
    clip). With `auto_join`, once the new clip is finished it is joined
    after this one as one more clip (see services/video_ops.py). It asks
    for sound as the clip it continues does unless `audio` says (priced
    that way). Every check a clip's prompt gets applies; nothing gets a
    Cast exemption."""
    from ..providers import video_capabilities
    from ..services import video_ops

    src = _continue_source(db, job_id)
    model = body.model or _continue_model(db, src)
    if not model:
        raise HTTPException(422, "The default video model can't start from a picture; pick a model that can")
    # Every check before anything is made (the frame included).
    provider_gate.require_model("video", model, db)
    resolved = _video_model(db, model)
    if not video_capabilities(resolved).first_frame:
        raise HTTPException(422, f"{resolved or 'This model'} can't start from a picture; pick another model")
    _check_safety(body.prompt, body.negative_prompt, "video_clip")
    if not video_ops.ffmpeg_here():
        raise HTTPException(503, video_ops.NO_FFMPEG)
    frame = _grab_now(db, src, "last")
    try:
        if frame.status not in ("completed", "saved"):
            raise HTTPException(409, "Couldn't save the clip's last frame; try again")
        request = StudioRequest(
            owner_kind=src.owner_kind, owner_id=src.owner_id, output_kind="video_clip", prompt=body.prompt,
            negative_prompt=body.negative_prompt, model=model, duration_seconds=body.duration_seconds,
            aspect_ratio=video_ops.continue_aspect(src) or None, first_frame_job_id=frame.id,
            audio=video_ops.continue_audio(db, src) if body.audio is None else body.audio,
        )
        return _create_jobs(db, request, "video_clip", 1,
                            options=video_ops.continue_options(src.id, body.auto_join))
    except Exception:
        # Nothing was started (a check refused it, the queue is down): the
        # frame saved for it, and a continuation stored but never queued,
        # are removed, so a retry doesn't leave one more frame behind.
        _drop_continue_frame(db, frame.id)
        raise


def _drop_continue_frame(db: Session, frame_id: int) -> None:
    """Tombstone the frame a failed Continue saved and the clip stored from
    it in that request when it was never run (its queue entry was refused,
    so it is marked failed). Kept as they are when that clip was queued."""
    db.rollback()
    Job = models.AssetGenerationJob
    clips = db.query(Job).filter(Job.first_frame_job_id == frame_id, Job.status != STUDIO_DELETED).all()
    if any(c.status != "failed" for c in clips):
        return
    frame = _live_job(db, frame_id)
    files: list[str] = []
    for j in clips + ([frame] if frame is not None else []):
        files += _tombstone(db, j)
    db.commit()
    _remove_files(db, files)


# ---------- a video uploaded as a clip ----------


@router.post("/upload-video", response_model=StudioJobOut, status_code=201,
             dependencies=[Depends(rate_limit("generation"))])
def upload_video(
    file: UploadFile = File(...),
    owner_kind: Literal["avatar", "ingredient"] = Form(...),
    owner_id: int = Form(...),
    db: Session = Depends(get_db),
) -> StudioJobOut:
    """Keep a video from this device as a finished Studio clip owned by
    `owner` (free), so it can be continued, joined or have its frames
    saved. Frames across it go through the picture safety checks before
    anything is stored, and it is refused when they can't run (see
    services/video_ops.py)."""
    from ..config import get_settings
    from ..services import video_ops

    if not _owner_exists(db, owner_kind, owner_id):
        raise HTTPException(404, "owner not found")
    if not video_ops.looks_like_video(file.content_type or "", file.filename or ""):
        raise HTTPException(422, video_ops.NOT_A_VIDEO)
    try:
        job = video_ops.video_from_upload(
            db, owner_kind=owner_kind, owner_id=owner_id, file=file.file, filename=file.filename or "",
            limit=get_settings().max_upload_bytes)
    except video_ops.OpError as e:
        raise HTTPException(e.status, e.message) from e
    out = _with_costs(db, [job])[0]
    out.batch_ids = [job.id]
    return out

"""Top-level job functions usable both inline and from an RQ worker.

These functions open their own DB session because RQ workers run in a separate
process and cannot share a FastAPI request's session.
"""
from __future__ import annotations

import logging

from sqlalchemy import update

from ..db import SessionLocal
from ..services import events, pipeline, provider_keys, spend

log = logging.getLogger("avs.jobs")


def generate_plan_job(project_id: int, preserve_edits: bool = False, claim: str = "",
                      claim_token: str = "") -> None:
    """`claim`: the status the API claimed the project with when it queued
    this job ("" for older queue entries)."""
    db = SessionLocal()
    try:
        with spend.scope(project_id=project_id):
            pipeline.generate_plan(db, project_id, preserve_edits=preserve_edits, claim=claim or None,
                                   claim_token=claim_token or None)
    finally:
        db.close()


def generate_video_job(project_id: int, claim: str = "", claim_token: str = "") -> None:
    db = SessionLocal()
    try:
        with spend.scope(project_id=project_id), provider_keys.continuing():  # checked when queued
            pipeline.render_project(db, project_id, claim=claim or None, claim_token=claim_token or None)
    finally:
        db.close()


def regenerate_shot_job(project_id: int, shot_id: int, recompose: bool = True) -> None:
    db = SessionLocal()
    try:
        with spend.scope(project_id=project_id), provider_keys.continuing():  # checked when queued
            pipeline.regenerate_shot(db, project_id, shot_id, recompose=recompose)
    finally:
        db.close()


def recompose_project_job(project_id: int, claim: str = "", claim_token: str = "") -> None:
    db = SessionLocal()
    try:
        if claim:
            if not pipeline.consume_claim(db, project_id, claim, claim_token or None):
                db.rollback()
                return
            db.commit()
        pipeline.recompose_project(db, project_id)
    finally:
        db.close()


def regenerate_shots_bulk_job(project_id: int, shot_ids: list[int]) -> None:
    db = SessionLocal()
    try:
        with spend.scope(project_id=project_id), provider_keys.continuing():  # checked when queued
            pipeline.regenerate_shots_bulk(db, project_id, shot_ids)
    finally:
        db.close()


def generate_asset_job(job_id: int) -> None:
    from .. import models

    db = SessionLocal()
    try:
        # The job may have been deleted from Studio while it sat in the queue,
        # and on SQLite a new job can then reuse its id. Only a still-pending
        # job runs, claimed atomically, so a stale task never runs (and pays
        # for) a reused id a second time.
        claimed = db.execute(
            update(models.AssetGenerationJob)
            .where(models.AssetGenerationJob.id == job_id,
                   models.AssetGenerationJob.status == "pending")
            .values(status="submitted")
        ).rowcount
        db.commit()
        if claimed != 1:
            log.info("studio job %s is gone or already started; skipping", job_id)
            return
        with spend.scope(ref=f"studio_job:{job_id}"), provider_keys.continuing():  # checked when queued
            try:
                pipeline.run_studio_job(db, job_id)
            except Exception:
                # Deleted mid-run: the final status write hits a missing row.
                # Nothing is left to report the failure on, so don't raise.
                db.rollback()
                if db.get(models.AssetGenerationJob, job_id) is None:
                    log.info("studio job %s was deleted while running", job_id)
                    return
                raise
    finally:
        db.close()


def train_custom_model_job(job_id: int) -> None:
    db = SessionLocal()
    try:
        pipeline.run_training_job(db, job_id)
    finally:
        db.close()


def generate_ambient_loop_job(project_id: int, regenerate_music: bool = True, claim_token: str = "") -> None:
    from ..services import ambient

    db = SessionLocal()
    try:
        # Cancelled or replaced while queued: do nothing (and pay nothing).
        if claim_token:
            if not pipeline.consume_claim(db, project_id, "generating", claim_token):
                db.rollback()
                return
            db.commit()
        with spend.scope(project_id=project_id), provider_keys.continuing():  # checked when queued
            ambient.generate_loop(db, project_id, regenerate_music=regenerate_music)
    finally:
        db.close()


def render_ambient_job(project_id: int, claim_token: str = "") -> None:
    from ..services import ambient

    db = SessionLocal()
    try:
        # Cancelled or replaced while queued: do nothing.
        if claim_token:
            if not pipeline.consume_claim(db, project_id, "generating", claim_token):
                db.rollback()
                return
            db.commit()
        ambient.render_scene(db, project_id)
    finally:
        db.close()


def generate_previews_job(preset_ids: list[str]) -> None:
    from ..services import ambient

    db = SessionLocal()
    try:
        for pid in preset_ids:
            # A second request queued behind this one (or a retry) finds the
            # preview already made from the current prompt: nothing to pay for.
            if ambient.preview_is_current(pid):
                continue
            preset = ambient._PRESET_BY_ID.get(pid)
            label = preset.label if preset is not None else pid
            try:
                with spend.scope(ref=f"preview:{pid}"):
                    ambient.generate_preview(db, pid)
                events.emit(db, "ambient_loop", "preset_preview", 0, "completed", f"Preview ready: {label}",
                            detail=pid, url="/ambient/new")
                db.commit()
            except Exception as e:  # noqa: BLE001 — one bad preset shouldn't stop the rest
                log.exception("preview generation failed for %s", pid)
                db.rollback()
                events.emit(db, "ambient_loop", "preset_preview", 0, "failed",
                            f"Preview failed: {label}", detail=pid + ": " + str(e)[:300], url="/ambient/new")
                db.commit()
    finally:
        db.close()


def ambient_batch_job(project_ids: list[int], render: bool = True) -> None:
    """Generate (and render) a batch of scenes sequentially, so a batch never
    floods the provider with parallel jobs."""
    from ..services import ambient

    for pid in project_ids:
        db = SessionLocal()
        try:
            # Video loops finish asynchronously; render_after renders each
            # scene when its loop is ready.
            # Checked when the batch was queued: finish it even if a provider
            # is switched off meanwhile.
            with spend.scope(project_id=pid), provider_keys.continuing():
                ambient.generate_loop(db, pid, render_after=render)
        except Exception:  # noqa: BLE001 — failures are recorded on the scene
            log.exception("batch item %s failed", pid)
        finally:
            db.close()


def publish_youtube_job(job_id: int) -> None:
    from .. import models
    from ..services import alerts, youtube

    db = SessionLocal()
    try:
        # Claim the job atomically: only a queued/failed/stale job moves to
        # "uploading", so a duplicate enqueue can't start a second upload.
        from datetime import datetime, timedelta

        stale = datetime.utcnow() - timedelta(minutes=15)
        claimed = (
            db.query(models.PublishJob)
            .filter(models.PublishJob.id == job_id)
            .filter((models.PublishJob.status.in_(("queued", "failed")))
                    | ((models.PublishJob.status == "uploading") & (models.PublishJob.updated_at < stale)))
            .update({"status": "uploading", "updated_at": datetime.utcnow()}, synchronize_session=False)
        )
        db.commit()
        if not claimed:
            log.info("publish job %s already handled or in progress; skipping", job_id)
            return
        job = db.get(models.PublishJob, job_id)
        try:
            youtube.upload(db, job)
        except Exception as e:  # noqa: BLE001
            log.exception("youtube upload %s failed", job_id)
            db.rollback()
            job = db.get(models.PublishJob, job_id)
            job.status = "failed"
            job.error = str(e)[:800]
            from ..services import events

            events.publish_event(db, job, "failed", error=str(e))
            db.commit()
            alerts.send(f"publish-failed:{job_id}", f"YouTube upload failed: “{job.title}”", str(e)[:600])
    finally:
        db.close()


def check_video_job(job_id: int) -> None:
    from ..services import video_jobs

    db = SessionLocal()
    try:
        video_jobs.check(db, job_id)
    finally:
        db.close()


def complete_video_job(job_id: int, cost: float | None = None, result_url: str | None = None) -> None:
    from ..services import video_jobs

    db = SessionLocal()
    try:
        video_jobs.complete(db, job_id, cost=cost, result_url=result_url)
    finally:
        db.close()


def fail_video_job(job_id: int, error: str) -> None:
    from ..services import video_jobs

    db = SessionLocal()
    try:
        video_jobs.fail(db, job_id, error)
    finally:
        db.close()


def compose_render_job(render_id: int) -> None:
    db = SessionLocal()
    try:
        pipeline.compose_render(db, render_id)
    finally:
        db.close()


def recompose_after_regen_job(project_id: int) -> None:
    db = SessionLocal()
    try:
        pipeline.recompose_project(db, project_id)
    finally:
        db.close()


def make_hook_variants_job(project_id: int, variant_ids: list[int], claim: str = "", claim_token: str = "",
                           previous: str = "completed") -> None:
    """Hook lab "Make videos": the variants' audio and clips (paid), each
    composed when its parts are in. `claim`/`claim_token`: the project
    claim the API made; `previous`: the status it goes back to."""
    from ..services import hook_lab

    db = SessionLocal()
    try:
        with spend.scope(project_id=project_id), provider_keys.continuing():  # checked when queued
            hook_lab.make_variants(db, project_id, variant_ids, claim=claim or None,
                                   claim_token=claim_token or None, previous=previous)
    finally:
        db.close()


def compose_hook_variant_job(variant_id: int) -> None:
    from ..services import hook_lab

    db = SessionLocal()
    try:
        hook_lab.compose_variant(db, variant_id)
    finally:
        db.close()


def inspect_take_job(take_id: int) -> None:
    """The automatic clip check of a take that just landed (advisory: it
    never touches the render)."""
    from ..services import inspector

    db = SessionLocal()
    try:
        inspector.run_auto(db, take_id)
    except Exception:  # noqa: BLE001
        log.exception("clip check of take %s failed", take_id)
    finally:
        db.close()


def check_cast_pictures_job(avatar_id: int) -> None:
    """The safety check of pictures added to the card of a Cast character
    ticked "Adult content allowed" (services/cast_pictures.py)."""
    from ..services import cast_pictures

    try:
        cast_pictures.run(avatar_id)
    except Exception:  # noqa: BLE001 — the pictures keep the filter; "Check new pictures" retries
        log.exception("picture check of cast character %s failed", avatar_id)

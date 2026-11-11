"""Background repair, about once a minute from the video-job sweeper loop:
Studio jobs and renders that would otherwise stay "running" forever (a
lost queue entry, a killed worker, a completion handler that raised), and
event retention once a day. Every step is a few cheap indexed queries, and
every change is a conditional UPDATE, so two web processes sweeping at
once never double-act."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from sqlalchemy import update
from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from . import events

log = logging.getLogger("avs.sweepers")

STUDIO_REQUEUE_AFTER = timedelta(minutes=10)
STUDIO_FAIL_PENDING_AFTER = timedelta(minutes=30)
STUDIO_POLLING_ORPHAN_AFTER = timedelta(minutes=10)
# A finished clip the normal handler should have applied by now.
STUDIO_APPLY_GRACE = timedelta(minutes=1)
RENDER_GRACE = timedelta(minutes=10)
COMPOSE_GRACE = timedelta(minutes=2)
STUCK_RENDER = ("pending", "planning", "generating_audio", "rendering")
DONE_RENDER = ("completed", "failed", "cancelled")
STOPPED = "Stopped unexpectedly; Retry continues from where it stopped"

_last_prune: float = 0.0


def _now() -> datetime:
    return datetime.utcnow()


# ---------- Studio ----------


def _fail_studio(db: Session, job_id: int, from_status: str, error: str) -> bool:
    res = db.execute(update(models.AssetGenerationJob)
                     .where(models.AssetGenerationJob.id == job_id,
                            models.AssetGenerationJob.status == from_status)
                     .values(status="failed", error=error, updated_at=_now()))
    if res.rowcount == 1:
        job = db.get(models.AssetGenerationJob, job_id)
        db.refresh(job)
        events.studio_event(db, job, "failed", error=error)
    db.commit()
    return res.rowcount == 1


def sweep_studio(db: Session, *, now: Optional[datetime] = None) -> dict:
    """Studio jobs stuck in pending / submitted / polling."""
    from ..workers.jobs import generate_asset_job
    from ..workers.queue import enqueue, queue_entry_state
    from . import pipeline, video_jobs

    now = now or _now()
    Job = models.AssetGenerationJob
    out = {"requeued": 0, "failed": 0, "applied": 0}

    # pending: the queue entry was lost (or never made it). Re-enqueue once
    # (the worker's claim is atomic, so a duplicate entry is a no-op); give
    # up after 30 minutes. A job whose entry is still queued (one worker
    # busy with a training run or a long render) is only waiting: left
    # alone, and so is everything while Redis doesn't answer.
    for job in (db.query(Job).filter(Job.status == "pending", Job.created_at <= now - STUDIO_REQUEUE_AFTER)
                .order_by(Job.id).limit(50).all()):
        if queue_entry_state(job.queue_job_id or "") != "gone":
            continue
        if job.created_at <= now - STUDIO_FAIL_PENDING_AFTER:
            if _fail_studio(db, job.id, "pending", "Couldn't start this job; try again"):
                out["failed"] += 1
            continue
        touched = job.updated_at is not None and job.updated_at > job.created_at + timedelta(seconds=30)
        if touched:
            continue  # already re-enqueued once
        res = db.execute(update(Job).where(Job.id == job.id, Job.status == "pending",
                                           Job.updated_at == job.updated_at)
                         .values(updated_at=now))
        db.commit()
        if res.rowcount == 1:
            try:
                qid = enqueue(generate_asset_job, job.id)
                db.execute(update(Job).where(Job.id == job.id, Job.status == "pending")
                           .values(queue_job_id=str(qid)[:80], updated_at=now))
                db.commit()
                out["requeued"] += 1
            except Exception:  # noqa: BLE001 — failed for good at 30 minutes
                log.exception("re-enqueueing studio job %s failed", job.id)

    # submitted: the worker died mid-generation (a timeout inside it is
    # caught and recorded, so only a hard kill leaves this).
    limit = timedelta(seconds=get_settings().queue_job_timeout_seconds) + timedelta(minutes=5)
    for (jid,) in (db.query(Job.id).filter(Job.status == "submitted", Job.updated_at <= now - limit)
                   .order_by(Job.id).limit(50).all()):
        if _fail_studio(db, jid, "submitted", "Stopped unexpectedly"):
            out["failed"] += 1

    # polling: a clip at the provider. Its video job finishes it, unless the
    # worker died before the video job existed, or the completion handler
    # raised (disk full, storage error).
    V = models.VideoJob
    for job in (db.query(Job).filter(Job.status == "polling", Job.updated_at <= now - STUDIO_APPLY_GRACE)
                .order_by(Job.id).limit(50).all()):
        vjob = (db.query(V).filter(V.purpose == "studio", V.ref_id == job.id)
                .order_by(V.id.desc()).first())
        if vjob is None:
            if job.updated_at <= now - STUDIO_POLLING_ORPHAN_AFTER:
                if _fail_studio(db, job.id, "polling", "Stopped unexpectedly"):
                    out["failed"] += 1
            continue
        if vjob.status in video_jobs.ACTIVE or vjob.status == "completing":
            continue  # still being made (a stuck download is failed by video_jobs)
        if vjob.completed_at and vjob.completed_at > now - STUDIO_APPLY_GRACE:
            continue  # its own handler may still be running
        if vjob.status == "completed" and vjob.result_path and Path(vjob.result_path).is_file():
            try:
                pipeline.on_studio_clip_ready(db, vjob)  # retry applying it, once
                out["applied"] += 1
                continue
            except Exception:  # noqa: BLE001
                db.rollback()
                log.exception("studio job %s: applying the finished clip failed again", job.id)
            if _fail_studio(db, job.id, "polling", "The clip was made but couldn't be saved; try again"):
                out["failed"] += 1
            continue
        if vjob.status == "failed":
            try:
                pipeline.on_studio_clip_failed(db, vjob)
                out["failed"] += 1
                continue
            except Exception:  # noqa: BLE001
                db.rollback()
        if _fail_studio(db, job.id, "polling", (vjob.error or "The clip couldn't be made")[:500]):
            out["failed"] += 1
    return out


# ---------- renders ----------


def _fail_stuck_render(db: Session, render: models.Render) -> bool:
    res = db.execute(update(models.Render)
                     .where(models.Render.id == render.id, models.Render.status == render.status,
                            models.Render.updated_at == render.updated_at)
                     .values(status="failed", error=STOPPED, updated_at=_now()))
    if res.rowcount != 1:
        db.rollback()
        return False
    db.refresh(render)
    project = db.get(models.VideoProject, render.project_id)
    if project is not None and project.status not in ("draft", "planned", "completed", "failed", "cancelled"):
        project.status = "failed"  # so Retry works
    events.render_event(db, render, project, "failed", error=STOPPED)
    db.commit()
    return True


def sweep_renders(db: Session, *, now: Optional[datetime] = None) -> dict:
    """Renders (and plans) a dead worker left running, and renders whose
    clips are all back but whose compose was never queued."""
    from ..workers.queue import queue_entry_state
    from . import pipeline, video_jobs

    now = now or _now()
    out = {"failed": 0, "composed": 0, "projects": 0}
    limit = timedelta(seconds=get_settings().queue_job_timeout_seconds) + RENDER_GRACE
    R = models.Render
    for render in (db.query(R).filter(R.status.in_(STUCK_RENDER), R.updated_at <= now - limit)
                   .order_by(R.id).limit(50).all()):
        if _fail_stuck_render(db, render):
            out["failed"] += 1

    # generating_shots: every clip is back (or failed) but nothing composed.
    for render in (db.query(R).filter(R.status == "generating_shots", R.updated_at <= now - COMPOSE_GRACE)
                   .order_by(R.id).limit(50).all()):
        project = db.get(models.VideoProject, render.project_id)
        if project is None or project.mode == "ambient_loop":
            continue
        body = [s for s in project.shots if s.shot_type not in ("end_card", "ambient_segment")]
        if not body or any(s.status not in ("completed", "failed") for s in body):
            continue
        if video_jobs.pending_for(db, project_id=project.id) > 0:
            continue
        try:
            pipeline.maybe_compose(db, render.id)
            db.refresh(render)
            if render.status != "generating_shots":
                out["composed"] += 1
        except Exception:  # noqa: BLE001
            db.rollback()
            log.exception("composing stuck render %s failed", render.id)

    # Projects claimed (planning / generating) whose job never ran or died
    # before it made a render row.
    P = models.VideoProject
    for project in (db.query(P).filter(P.status.in_(("planning", "generating")), P.updated_at <= now - limit)
                    .order_by(P.id).limit(50).all()):
        running = db.query(R.id).filter(R.project_id == project.id, R.status.notin_(DONE_RENDER)).first()
        if running is not None or video_jobs.pending_for(
                db, project_id=project.id, purposes=("shot", "avatar", "ambient_segment")) > 0:
            continue
        if queue_entry_state(project.queue_job_id or "") != "gone":
            continue  # still waiting in a busy queue (or Redis didn't answer)
        was = project.status
        res = db.execute(update(P).where(P.id == project.id, P.status == was,
                                         P.updated_at == project.updated_at)
                         .values(status="failed", updated_at=_now()))
        if res.rowcount != 1:
            db.rollback()
            continue
        db.refresh(project)
        if was == "planning":
            events.plan_event(db, project, "failed", error="Stopped unexpectedly; try again")
        elif project.mode == "ambient_loop":
            events.ambient_loop_event(db, project, "failed", error="Stopped unexpectedly; try again")
        else:
            events.emit(db, "render", "project", project.id, "failed",
                        f"Render failed: {STOPPED}", url=events.project_url(project), project_id=project.id)
        db.commit()
        out["projects"] += 1
    return out


# ---------- the once-a-minute step ----------


def run_once(*, now: Optional[datetime] = None) -> dict:
    """Studio and render repair, plus event retention once a day. Never raises."""
    from ..db import SessionLocal

    global _last_prune
    result: dict = {}
    db = SessionLocal()
    try:
        for name, step in (("studio", sweep_studio), ("renders", sweep_renders)):
            try:
                result[name] = step(db, now=now)
            except Exception:  # noqa: BLE001
                db.rollback()
                log.exception("%s sweep failed", name)
        if time.monotonic() - _last_prune > 24 * 3600 or not _last_prune:
            try:
                result["pruned_events"] = events.prune(db)
                _last_prune = time.monotonic()
            except Exception:  # noqa: BLE001
                db.rollback()
                log.exception("event retention failed")
    finally:
        db.close()
    return result

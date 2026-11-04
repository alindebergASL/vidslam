"""Top-level job functions usable both inline and from an RQ worker.

These functions open their own DB session because RQ workers run in a separate
process and cannot share a FastAPI request's session.
"""
from __future__ import annotations

import logging

from ..db import SessionLocal
from ..services import pipeline

log = logging.getLogger("avs.jobs")


def generate_plan_job(project_id: int, preserve_edits: bool = False) -> None:
    db = SessionLocal()
    try:
        pipeline.generate_plan(db, project_id, preserve_edits=preserve_edits)
    finally:
        db.close()


def generate_video_job(project_id: int) -> None:
    db = SessionLocal()
    try:
        pipeline.render_project(db, project_id)
    finally:
        db.close()


def regenerate_shot_job(project_id: int, shot_id: int, recompose: bool = True) -> None:
    db = SessionLocal()
    try:
        pipeline.regenerate_shot(db, project_id, shot_id, recompose=recompose)
    finally:
        db.close()


def recompose_project_job(project_id: int) -> None:
    db = SessionLocal()
    try:
        pipeline.recompose_project(db, project_id)
    finally:
        db.close()


def regenerate_shots_bulk_job(project_id: int, shot_ids: list[int]) -> None:
    db = SessionLocal()
    try:
        pipeline.regenerate_shots_bulk(db, project_id, shot_ids)
    finally:
        db.close()


def generate_asset_job(job_id: int) -> None:
    db = SessionLocal()
    try:
        pipeline.run_studio_job(db, job_id)
    finally:
        db.close()


def train_custom_model_job(job_id: int) -> None:
    db = SessionLocal()
    try:
        pipeline.run_training_job(db, job_id)
    finally:
        db.close()


def generate_ambient_loop_job(project_id: int, regenerate_music: bool = True) -> None:
    from ..services import ambient

    db = SessionLocal()
    try:
        ambient.generate_loop(db, project_id, regenerate_music=regenerate_music)
    finally:
        db.close()


def render_ambient_job(project_id: int) -> None:
    from ..services import ambient

    db = SessionLocal()
    try:
        ambient.render_scene(db, project_id)
    finally:
        db.close()


def generate_previews_job(preset_ids: list[str]) -> None:
    from ..services import ambient

    db = SessionLocal()
    try:
        for pid in preset_ids:
            try:
                ambient.generate_preview(db, pid)
            except Exception:  # noqa: BLE001 — one bad preset shouldn't stop the rest
                log.exception("preview generation failed for %s", pid)
    finally:
        db.close()


def ambient_batch_job(project_ids: list[int], render: bool = True) -> None:
    """Generate (and render) a batch of scenes sequentially, so a batch never
    floods the provider with parallel jobs."""
    from ..services import ambient

    for pid in project_ids:
        db = SessionLocal()
        try:
            ambient.generate_loop(db, pid)
            if render:
                ambient.render_scene(db, pid)
        except Exception:  # noqa: BLE001 — failures are recorded on the scene
            log.exception("batch item %s failed", pid)
        finally:
            db.close()


def publish_youtube_job(job_id: int) -> None:
    from .. import models
    from ..services import alerts, youtube

    db = SessionLocal()
    try:
        job = db.get(models.PublishJob, job_id)
        if job is None:
            return
        try:
            youtube.upload(db, job)
        except Exception as e:  # noqa: BLE001
            log.exception("youtube upload %s failed", job_id)
            db.rollback()
            job = db.get(models.PublishJob, job_id)
            job.status = "failed"
            job.error = str(e)[:800]
            db.commit()
            alerts.send(f"publish-failed:{job_id}", f"YouTube upload failed: “{job.title}”", str(e)[:600])
    finally:
        db.close()

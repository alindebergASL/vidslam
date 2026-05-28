"""Top-level job functions usable both inline and from an RQ worker.

These functions open their own DB session because RQ workers run in a separate
process and cannot share a FastAPI request's session.
"""
from __future__ import annotations

import logging

from ..db import SessionLocal
from ..services import pipeline

log = logging.getLogger("avs.jobs")


def generate_plan_job(project_id: int) -> None:
    db = SessionLocal()
    try:
        pipeline.generate_plan(db, project_id)
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

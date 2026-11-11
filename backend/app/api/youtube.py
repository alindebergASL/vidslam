"""YouTube connection + publishing.

GET  /api/youtube/status                 configured? connected? channel
GET  /api/youtube/connect                → {url} for Google's consent screen
GET  /api/youtube/callback               OAuth redirect target (browser)
POST /api/youtube/disconnect             revoke + forget the refresh token
POST /api/renders/{id}/publish/youtube   queue an upload
GET  /api/renders/{id}/publish-jobs      uploads of one render
GET  /api/publish-jobs                   recent uploads
POST /api/publish-jobs/{id}/retry        resume a failed upload ({confirm_new_upload} for a new video)
(overview, YouTube refresh and results: api/publishing.py)
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from ..db import get_db
from ..services import youtube as yt
from ..services.publish_meta import fit_tags
from ..services.publish_state import on_youtube
from ..services.publishing import duplicate_of
from ..services.ratelimit import rate_limit
from ..workers.jobs import publish_youtube_job
from ..workers.queue import enqueue
from .auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])


class PublishRequest(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=5000)
    tags: list[str] = Field(default_factory=list)
    category_id: str = Field(default="22", max_length=10)
    privacy: Literal["private", "unlisted", "public"] = "private"
    publish_at: Optional[datetime] = None
    made_for_kids: bool = False
    contains_synthetic_media: bool = True
    set_thumbnail: bool = True
    # Required to upload a render that's already on YouTube (or uploading).
    confirm_duplicate: bool = False


class PublishJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    render_id: int
    platform: str
    status: str
    title: str
    privacy: str
    publish_at: Optional[datetime]
    contains_synthetic_media: bool
    bytes_total: int
    bytes_sent: int
    external_id: str
    external_url: str
    error: str
    created_at: datetime
    updated_at: datetime
    uploaded_at: Optional[datetime] = None
    # What the creator should read (uploading | published | scheduled |
    # private | processing | removed | failed) and, when YouTube did
    # something other than what was asked, why.
    state: str = ""
    warning: str = ""
    yt_privacy_status: str = ""
    yt_upload_status: str = ""
    yt_publish_at: Optional[datetime] = None
    yt_title: str = ""
    yt_removed: bool = False
    yt_checked_at: Optional[datetime] = None


@router.get("/youtube/status")
def yt_status() -> dict:
    return yt.status()


@router.get("/youtube/connect")
def yt_connect() -> dict:
    try:
        return {"url": yt.auth_url()}
    except yt.YouTubeError as e:
        raise HTTPException(409, str(e)) from e


@router.get("/youtube/callback")
def yt_callback(code: str = "", state: str = "", error: str = "") -> RedirectResponse:
    front = get_settings().frontend_origin.rstrip("/")
    if error:
        return RedirectResponse(f"{front}/settings?{urlencode({'youtube': 'error', 'reason': error})}")
    try:
        yt.handle_callback(code, state)
    except yt.YouTubeError as e:
        return RedirectResponse(f"{front}/settings?{urlencode({'youtube': 'error', 'reason': str(e)[:200]})}")
    return RedirectResponse(f"{front}/settings?youtube=connected")


@router.post("/youtube/disconnect")
def yt_disconnect() -> dict:
    yt.disconnect()
    return yt.status()


def _lock_render(db: Session, render_id: int) -> None:
    """Hold the render's row until this transaction ends: publish and retry
    for one render run one at a time. Postgres: SELECT … FOR UPDATE. SQLite
    has no row locks; a no-op write takes its database write lock instead."""
    if db.get_bind().dialect.name == "postgresql":
        db.query(models.Render.id).filter(models.Render.id == render_id).with_for_update().one_or_none()
    else:
        db.execute(text("UPDATE renders SET id = id WHERE id = :rid"), {"rid": render_id})


def _refuse_duplicate(db: Session, render_id: int, exclude_id: Optional[int] = None) -> None:
    dup = duplicate_of(db, render_id, exclude_id=exclude_id)
    if dup is not None:
        where = f" ({dup.external_url})" if dup.external_url else ""
        what = "already on YouTube" if dup.external_url else "already uploading to YouTube"
        raise HTTPException(409, f"This version is {what}{where}. Upload it again as a separate video?",
                            headers={"X-Duplicate-Of": str(dup.id)})


@router.post("/renders/{render_id}/publish/youtube", response_model=PublishJobOut, status_code=202,
             dependencies=[Depends(rate_limit("generation"))])
def publish(render_id: int, body: PublishRequest, db: Session = Depends(get_db)) -> models.PublishJob:
    render = db.get(models.Render, render_id)
    if render is None or render.status != "completed" or not render.final_video_path \
            or not Path(render.final_video_path).exists():
        raise HTTPException(409, "render isn't completed (or its file expired)")
    if not yt.status()["connected"]:
        raise HTTPException(409, "connect a YouTube channel in Settings first")
    try:
        when = yt.validate_publish_at(body.publish_at)
    except yt.YouTubeError as e:
        raise HTTPException(422, str(e)) from e
    if any(c in body.title + body.description for c in "<>"):
        raise HTTPException(422, "YouTube doesn't allow '<' or '>' in titles or descriptions")
    # Check-then-insert under a lock on the render row, so two requests at
    # once (two tabs, a client retry, a double click) can't both pass.
    _lock_render(db, render.id)
    if not body.confirm_duplicate:
        _refuse_duplicate(db, render.id)
    job = models.PublishJob(
        render_id=render.id, platform="youtube", status="queued",
        title=body.title.strip(), description=body.description, tags_json=fit_tags(body.tags),
        category_id=body.category_id, privacy="private" if when else body.privacy, publish_at=when,
        made_for_kids=body.made_for_kids, contains_synthetic_media=body.contains_synthetic_media,
        set_thumbnail=body.set_thumbnail,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    enqueue(publish_youtube_job, job.id)
    db.refresh(job)
    return job


@router.get("/renders/{render_id}/publish-jobs", response_model=list[PublishJobOut])
def render_jobs(render_id: int, db: Session = Depends(get_db)) -> list[models.PublishJob]:
    return (db.query(models.PublishJob).filter(models.PublishJob.render_id == render_id)
            .order_by(models.PublishJob.id.desc()).all())


@router.get("/publish-jobs", response_model=list[PublishJobOut])
def recent_jobs(limit: int = 20, db: Session = Depends(get_db)) -> list[models.PublishJob]:
    return db.query(models.PublishJob).order_by(models.PublishJob.id.desc()).limit(min(limit, 100)).all()


class RetryRequest(BaseModel):
    # Required to retry a job that would make a new YouTube video: one YouTube
    # rejected, or one whose render is already on YouTube by another job.
    confirm_new_upload: bool = False


@router.post("/publish-jobs/{job_id}/retry", response_model=PublishJobOut, status_code=202)
def retry(job_id: int, body: Optional[RetryRequest] = None, db: Session = Depends(get_db)) -> models.PublishJob:
    confirm = bool(body and body.confirm_new_upload)
    job = db.get(models.PublishJob, job_id)
    if job is None:
        raise HTTPException(404, "publish job not found")
    _lock_render(db, job.render_id)
    db.refresh(job)
    # failed → retry. uploading with no progress for 15 min → the worker died
    # mid-upload; the saved session URI lets it resume. queued is refused so
    # a job can't be enqueued twice (two workers = two YouTube videos).
    stale = job.status == "uploading" and job.updated_at < datetime.utcnow() - timedelta(minutes=15)
    if job.status != "failed" and not stale:
        raise HTTPException(409, f"job is {job.status}" + ("" if job.status != "uploading" else " (still active)"))
    if not confirm:
        if on_youtube(job):
            # YouTube has (and rejected or couldn't process) this job's video:
            # retrying can only upload a second, separate video.
            raise HTTPException(409, "YouTube already has this upload (it rejected or couldn't process it). "
                                     "Retrying uploads it again as a new video.",
                                headers={"X-Needs-Confirm": "new-upload"})
        if not job.upload_url:
            # Nothing to resume: this is a fresh upload of the render.
            _refuse_duplicate(db, job.render_id, exclude_id=job.id)
    job.status, job.error = "queued", ""
    db.commit()
    enqueue(publish_youtube_job, job.id)
    db.refresh(job)
    return job

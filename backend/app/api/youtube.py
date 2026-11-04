"""YouTube connection + publishing.

GET  /api/youtube/status                 configured? connected? channel
GET  /api/youtube/connect                → {url} for Google's consent screen
GET  /api/youtube/callback               OAuth redirect target (browser)
POST /api/youtube/disconnect             revoke + forget the refresh token
POST /api/renders/{id}/publish/youtube   queue an upload
GET  /api/renders/{id}/publish-jobs      uploads of one render
GET  /api/publish-jobs                   recent uploads
POST /api/publish-jobs/{id}/retry        resume a failed upload
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from ..db import get_db
from ..services import youtube as yt
from ..services.publish_meta import fit_tags
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


@router.post("/publish-jobs/{job_id}/retry", response_model=PublishJobOut, status_code=202)
def retry(job_id: int, db: Session = Depends(get_db)) -> models.PublishJob:
    job = db.get(models.PublishJob, job_id)
    if job is None:
        raise HTTPException(404, "publish job not found")
    if job.status not in ("failed", "queued"):
        raise HTTPException(409, f"job is {job.status}")
    job.status, job.error = "queued", ""
    db.commit()
    enqueue(publish_youtube_job, job.id)
    db.refresh(job)
    return job

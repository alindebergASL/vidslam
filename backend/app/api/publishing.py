"""Publishing overview + results loop.

GET  /api/publishing/jobs            every publish job, filters + pagination (?ids= for some)
GET  /api/publishing/not-posted      latest finished render per project with no upload
POST /api/publish-jobs/{id}/refresh  read YouTube's state for one job now
POST /api/publish-jobs/refresh       … for many jobs ({ids}), ≤50 ids per YouTube call
GET  /api/results                    one row per uploaded video + roll-ups (?group_by=)
POST /api/results/refresh            fetch today's YouTube numbers now (rate-limited)
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..services import publishing, video_stats
from ..services import youtube as yt
from ..services.ratelimit import rate_limit
from .auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])

StatusFilter = Literal["all", "scheduled", "published", "failed", "uploading"]
GroupBy = Literal["type", "preset", "time_weather", "length", "character", "model", "aspect"]


class PublishOverviewItem(BaseModel):
    id: int
    render_id: int
    render_version: Optional[int]
    project_id: int
    project_title: str
    project_kind: Literal["storyboard", "ambient"]
    project_url: str
    render_url: str
    title: str
    youtube_title: str
    thumbnail_url: Optional[str]
    status: str
    state: str
    warning: str
    error: str
    privacy: str
    youtube_privacy: str
    youtube_upload_status: str
    publish_at: Optional[datetime]
    youtube_publish_at: Optional[datetime]
    youtube_reason: str
    removed: bool
    external_id: str
    external_url: str
    bytes_total: int
    bytes_sent: int
    uploaded_at: Optional[datetime]
    checked_at: Optional[datetime]
    created_at: datetime


class PublishOverviewPage(BaseModel):
    items: list[PublishOverviewItem]
    next_before_id: Optional[int]
    counts: dict[str, int]


class NotPostedItem(BaseModel):
    render_id: int
    render_version: Optional[int]
    project_id: int
    project_title: str
    project_kind: Literal["storyboard", "ambient"]
    project_url: str
    publish_url: str
    thumbnail_url: Optional[str]
    file_available: bool
    failed_attempts: int
    created_at: datetime


class NotPostedPage(BaseModel):
    items: list[NotPostedItem]
    next_before_id: Optional[int]


class RefreshMany(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=200)


class RefreshResult(BaseModel):
    items: list[PublishOverviewItem]
    checked: int
    removed: int
    skipped: int


class ResultRow(BaseModel):
    publish_job_id: int
    render_id: int
    render_version: Optional[int]
    project_id: int
    project_title: str
    project_kind: Literal["storyboard", "ambient"]
    project_url: str
    title: str
    external_url: str
    thumbnail_url: Optional[str]
    state: str
    warning: str
    published_at: datetime
    age_days: int
    views: Optional[int]
    likes: Optional[int]
    comments: Optional[int]
    views_per_day: Optional[float]
    stats_fetched_at: Optional[datetime]
    cost_usd: float
    cost_is_estimate: bool
    cost_per_1k_views: Optional[float]
    views_before: Optional[int]
    views_change: Optional[int]
    trend_days: Optional[int]
    sparkline: list[int]
    duration_seconds: Optional[float]
    length_bucket: str
    preset: str
    time_weather: str
    character: str
    model: str
    aspect_ratio: str


class ResultGroup(BaseModel):
    key: str
    videos: int
    # Videos that have YouTube numbers; views, median, cost per 1k and
    # too_few are based on these only.
    videos_with_numbers: int
    total_views: int
    median_views: Optional[float]
    total_cost_usd: float
    cost_is_estimate: bool
    cost_per_1k_views: Optional[float]
    too_few: bool


class ResultsOut(BaseModel):
    configured: bool
    connected: bool
    # Last successful fetch; the last attempt is reported separately so a
    # failed one doesn't pass for fresh numbers.
    last_refreshed_at: Optional[datetime]
    last_attempt_at: Optional[datetime] = None
    last_attempt_failed: bool = False
    last_attempt_error: str = ""
    has_numbers: bool
    rows: list[ResultRow]
    group_by: GroupBy
    group_label: str
    groups: list[ResultGroup]


class StatsRefreshOut(BaseModel):
    videos: int
    snapshots: int
    removed: int
    skipped: str
    refreshed_at: datetime


# ---------- overview ----------


@router.get("/publishing/jobs", response_model=PublishOverviewPage)
def overview_jobs(status: StatusFilter = "all", q: str = "", limit: int = Query(30, ge=1, le=100),
                  before_id: Optional[int] = None,
                  ids: Optional[list[int]] = Query(None, max_length=100),
                  db: Session = Depends(get_db)) -> dict:
    return publishing.list_jobs(db, status=status, q=q, limit=limit, before_id=before_id, ids=ids)


@router.get("/publishing/not-posted", response_model=NotPostedPage)
def overview_not_posted(limit: int = Query(30, ge=1, le=100), before_id: Optional[int] = None,
                        db: Session = Depends(get_db)) -> dict:
    return publishing.not_posted(db, limit=limit, before_id=before_id)


def _refresh(db: Session, ids: list[int]) -> dict:
    if not yt.can_read():
        raise HTTPException(409, "Connect YouTube in Settings first")
    jobs = db.query(models.PublishJob).filter(models.PublishJob.id.in_(ids)).all()
    if not jobs:
        raise HTTPException(404, "publish job not found")
    try:
        summary = yt.refresh_jobs(db, jobs)
    except yt.UPSTREAM_ERRORS as e:
        db.rollback()
        raise HTTPException(502, yt.upstream_message(e)) from e
    return {"items": _items_for(db, {j.id for j in jobs}), **summary}


def _items_for(db: Session, ids: set[int]) -> list[dict]:
    J = models.PublishJob
    rows = publishing._jobs_query(db).filter(J.id.in_(ids)).order_by(J.id.desc()).all()
    versions = publishing.render_versions(db, [p.id for _, _, p in rows])
    return [publishing.job_item(j, r, p, versions.get(r.id)) for j, r, p in rows]


@router.post("/publish-jobs/{job_id}/refresh", response_model=PublishOverviewItem)
def refresh_one(job_id: int, db: Session = Depends(get_db)) -> dict:
    if db.get(models.PublishJob, job_id) is None:
        raise HTTPException(404, "publish job not found")
    return _refresh(db, [job_id])["items"][0]


@router.post("/publish-jobs/refresh", response_model=RefreshResult)
def refresh_many(body: RefreshMany, db: Session = Depends(get_db)) -> dict:
    return _refresh(db, list(dict.fromkeys(body.ids)))


# ---------- results ----------


@router.get("/results", response_model=ResultsOut)
def results(group_by: GroupBy = "type", db: Session = Depends(get_db)) -> dict:
    st = yt.status()
    attempt = video_stats.last_attempt()
    rows = publishing.result_rows(db)
    return {
        "configured": st["configured"],
        "connected": st["connected"],
        "last_refreshed_at": video_stats.last_success(),
        "last_attempt_at": attempt["at"],
        "last_attempt_failed": attempt["failed"],
        "last_attempt_error": attempt["error"],
        "has_numbers": any(r["views"] is not None for r in rows),
        "rows": rows,
        "group_by": group_by,
        "group_label": publishing.GROUPS[group_by][1],
        "groups": publishing.group_rows(rows, group_by),
    }


@router.post("/results/refresh", response_model=StatsRefreshOut,
             dependencies=[Depends(rate_limit("youtube_stats"))])
def refresh_numbers(db: Session = Depends(get_db)) -> dict:
    if not yt.can_read():
        raise HTTPException(409, "Connect YouTube in Settings first")
    wait = video_stats.manual_wait_seconds()
    if wait:
        raise HTTPException(429, f"Numbers were refreshed a moment ago — try again in about {wait} s",
                            headers={"Retry-After": str(wait)})
    now = datetime.utcnow()
    video_stats.mark_manual(now)
    try:
        out = video_stats.snapshot(db, now=now)
    except yt.UPSTREAM_ERRORS as e:
        db.rollback()
        msg = yt.upstream_message(e)
        video_stats.record_outcome(now, msg)
        raise HTTPException(502, msg) from e
    video_stats.record_outcome(now, "YouTube isn't connected" if out.get("skipped") else "")
    return {**out, "refreshed_at": now}

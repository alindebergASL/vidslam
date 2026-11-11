"""Publishing overview + results: every publish job across storyboard
projects and ambient scenes, the renders not posted yet, and per-video
YouTube numbers with cost and roll-ups."""
from __future__ import annotations

import statistics as stats_mod
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from .. import models
from . import publish_state

AMBIENT_MODE = "ambient_loop"

FILTERS = {
    "all": None,
    "scheduled": ("scheduled",),
    "published": ("published", "processing"),
    "failed": ("failed",),
    "uploading": ("queued", "uploading"),
}


# ---------- project facts ----------


def project_kind(p: models.VideoProject) -> str:
    return "ambient" if p.mode == AMBIENT_MODE else "storyboard"


def project_url(p: models.VideoProject) -> str:
    """Where the project (and its publish form) lives in the app."""
    return f"/ambient/{p.id}" if p.mode == AMBIENT_MODE else f"/projects/{p.id}/render"


def thumbnail_url(r: models.Render) -> Optional[str]:
    if r.thumbnail_path and Path(r.thumbnail_path).exists():
        return f"/api/renders/{r.id}/thumbnail"
    return None


def render_versions(db: Session, project_ids: Iterable[int]) -> dict[int, int]:
    """render id → version number within its project (1 = first completed
    render), the same numbering as the project's export zip."""
    ids = list(set(project_ids))
    if not ids:
        return {}
    rows = (db.query(models.Render.id, models.Render.project_id)
            .filter(models.Render.project_id.in_(ids), models.Render.status == "completed")
            .order_by(models.Render.project_id, models.Render.created_at, models.Render.id).all())
    out: dict[int, int] = {}
    seen: Counter = Counter()
    for rid, pid in rows:
        seen[pid] += 1
        out[rid] = seen[pid]
    return out


def render_url(p: models.VideoProject, render_id: int) -> str:
    """The page with the publish form for this exact render."""
    if p.mode == AMBIENT_MODE:
        return f"/ambient/{p.id}?render={render_id}"
    return f"/projects/{p.id}/render?id={render_id}"


def uploaded_at(job: models.PublishJob) -> Optional[datetime]:
    if job.uploaded_at:
        return job.uploaded_at
    if job.external_id:
        # Never updated_at: every YouTube check bumps it. Migration 0008
        # backfills uploaded_at, so this is only a last resort.
        return job.created_at
    return None


# ---------- overview ----------


def job_item(job: models.PublishJob, render: models.Render, project: models.VideoProject,
             version: Optional[int]) -> dict:
    state, warning = publish_state.display_state(job)
    return {
        "id": job.id,
        "render_id": render.id,
        "render_version": version,
        "project_id": project.id,
        "project_title": project.title or "",
        "project_kind": project_kind(project),
        "project_url": project_url(project),
        "render_url": render_url(project, render.id),
        "title": job.title,
        "youtube_title": job.yt_title or "",
        "thumbnail_url": thumbnail_url(render),
        "status": job.status,
        "state": state,
        "warning": warning,
        "error": job.error,
        "privacy": job.privacy,
        "youtube_privacy": job.yt_privacy_status or "",
        "youtube_upload_status": job.yt_upload_status or "",
        "publish_at": job.publish_at,
        "youtube_publish_at": job.yt_publish_at,
        "youtube_reason": publish_state.reason_text(job.yt_failure_reason) if job.yt_failure_reason else "",
        "removed": bool(job.yt_removed),
        "external_id": job.external_id,
        "external_url": job.external_url,
        "bytes_total": job.bytes_total,
        "bytes_sent": job.bytes_sent,
        "uploaded_at": uploaded_at(job),
        "checked_at": job.yt_checked_at,
        "created_at": job.created_at,
    }


def _escape_like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _jobs_query(db: Session, q: str = ""):
    J, R, P = models.PublishJob, models.Render, models.VideoProject
    query = (db.query(J, R, P).join(R, J.render_id == R.id).join(P, R.project_id == P.id))
    text = (q or "").strip()
    if text:
        pat = _escape_like(text)
        query = query.filter(or_(J.title.ilike(pat, escape="\\"), J.yt_title.ilike(pat, escape="\\"),
                                 P.title.ilike(pat, escape="\\")))
    return query


def list_jobs(db: Session, *, status: str = "all", q: str = "", limit: int = 30,
              before_id: Optional[int] = None, ids: Optional[Iterable[int]] = None) -> dict:
    """One page of jobs, newest first. `ids` limits it to those jobs (the
    overview polls only the rows still uploading)."""
    J = models.PublishJob
    base = _jobs_query(db, q)
    counts = {"all": base.count()}
    for name, sts in FILTERS.items():
        if sts:
            counts[name] = base.filter(J.status.in_(sts)).count()
    query = base
    if FILTERS.get(status):
        query = query.filter(J.status.in_(FILTERS[status]))
    if before_id is not None:
        query = query.filter(J.id < before_id)
    if ids is not None:
        query = query.filter(J.id.in_(list(ids)))
    rows = query.order_by(J.id.desc()).limit(limit + 1).all()
    more = len(rows) > limit
    rows = rows[:limit]
    versions = render_versions(db, [p.id for _, _, p in rows])
    items = [job_item(j, r, p, versions.get(r.id)) for j, r, p in rows]
    return {"items": items, "next_before_id": items[-1]["id"] if more and items else None, "counts": counts}


def not_posted(db: Session, *, limit: int = 30, before_id: Optional[int] = None) -> dict:
    """The latest finished render of each project (storyboard and ambient)
    that has no publish job (failed attempts don't count as posted)."""
    R, P, J = models.Render, models.VideoProject, models.PublishJob
    latest = (db.query(R.project_id, func.max(R.id).label("rid"))
              .filter(R.status == "completed", R.final_video_path != "")
              .group_by(R.project_id).subquery())
    posted = select(J.render_id).where(J.status != "failed")
    query = (db.query(R, P).join(latest, R.id == latest.c.rid).join(P, R.project_id == P.id)
             .filter(R.id.notin_(posted)))
    if before_id is not None:
        query = query.filter(R.id < before_id)
    rows = query.order_by(R.id.desc()).limit(limit + 1).all()
    more = len(rows) > limit
    rows = rows[:limit]
    versions = render_versions(db, [p.id for _, p in rows])
    failed = dict(db.query(J.render_id, func.count(J.id))
                  .filter(J.render_id.in_([r.id for r, _ in rows]), J.status == "failed")
                  .group_by(J.render_id).all()) if rows else {}
    items = [{
        "render_id": r.id,
        "render_version": versions.get(r.id),
        "project_id": p.id,
        "project_title": p.title or "",
        "project_kind": project_kind(p),
        "project_url": project_url(p),
        "publish_url": render_url(p, r.id),
        "thumbnail_url": thumbnail_url(r),
        "file_available": bool(r.final_video_path and Path(r.final_video_path).exists()),
        "failed_attempts": int(failed.get(r.id, 0)),
        "created_at": r.created_at,
    } for r, p in rows]
    return {"items": items, "next_before_id": items[-1]["render_id"] if more and items else None}


def duplicate_of(db: Session, render_id: int, exclude_id: Optional[int] = None) -> Optional[models.PublishJob]:
    """A job that already put (or is putting) this render on YouTube."""
    J = models.PublishJob
    q = db.query(J).filter(J.render_id == render_id, J.status.in_(publish_state.LIVE_STATUSES),
                           J.yt_removed.is_(False))
    if exclude_id is not None:
        q = q.filter(J.id != exclude_id)
    return q.order_by(J.id.desc()).first()


# ---------- results ----------


def length_bucket(seconds: Optional[float]) -> str:
    if not seconds:
        return "Unknown length"
    if seconds < 60:
        return "Under 1 min"
    if seconds < 600:
        return "1–10 min"
    if seconds < 3600:
        return "10–60 min"
    return "Over 1 hour"


def _duration(p: models.VideoProject) -> Optional[float]:
    if p.mode == AMBIENT_MODE:
        cfg = p.ambient_config_json or {}
        try:
            return float(cfg.get("total_seconds") or 3600)
        except (TypeError, ValueError):
            return None
    total = sum(float(s.duration_seconds or 0) for s in p.shots)
    return total or float(p.target_duration_seconds or 0) or None


def _character(p: models.VideoProject, avatars: dict[int, str]) -> str:
    if p.mode == AMBIENT_MODE:
        return "No character (ambient)"
    if p.primary_avatar_id and p.primary_avatar_id in avatars:
        return avatars[p.primary_avatar_id]
    for m in p.cast_members:
        if m.avatar is not None:
            return m.avatar.name
    return "No character"


def _job_models(db: Session, project_ids: list[int]) -> dict[int, str]:
    """project id → the video model most of its generation jobs used, in one
    GROUP BY for every project."""
    if not project_ids:
        return {}
    V = models.VideoJob
    rows = (db.query(V.project_id, V.model, func.count(V.id))
            .filter(V.project_id.in_(project_ids), V.model != "",
                    V.purpose.in_(("shot", "avatar", "ambient_segment")))
            .group_by(V.project_id, V.model).all())
    best: dict[int, tuple[int, str]] = {}
    for pid, model, n in rows:
        if pid not in best or (n, model) > best[pid]:
            best[pid] = (n, model)
    return {pid: m for pid, (_, m) in best.items()}


def _model(p: models.VideoProject, from_jobs: dict[int, str]) -> str:
    if p.id in from_jobs:
        return from_jobs[p.id]
    used = Counter(s.provider_model for s in p.shots if s.provider_model)
    used.update(t.provider_model for s in p.shots for t in s.takes if t.provider_model)
    if used:
        return used.most_common(1)[0][0]
    if p.mode == AMBIENT_MODE and (p.ambient_config_json or {}).get("model_override"):
        return p.ambient_config_json["model_override"]
    return "Unknown"


def project_costs(db: Session, project_ids: Iterable[int]) -> dict[int, tuple[float, bool]]:
    """project id → (total ledger spend, any part estimated)."""
    ids = list(set(project_ids))
    if not ids:
        return {}
    S = models.SpendEntry
    est = case((or_(S.is_estimate.is_(True), S.source == "pending"), 1), else_=0)
    rows = (db.query(S.project_id, func.coalesce(func.sum(S.cost_usd), 0.0), func.max(est))
            .filter(S.project_id.in_(ids)).group_by(S.project_id).all())
    return {pid: (round(float(total or 0), 4), bool(e)) for pid, total, e in rows}


def per_1k(cost: Optional[float], views: Optional[int]) -> Optional[float]:
    if cost is None or not views:
        return None
    return round(cost / views * 1000, 4)


def _trend(snaps: list[models.VideoStat]) -> dict:
    """views now vs the snapshot ~7 days earlier (or the oldest other one)."""
    if len(snaps) < 2:
        return {"views_before": None, "views_change": None, "trend_days": None}
    latest = snaps[-1]
    cutoff = latest.fetched_at - timedelta(days=7)
    older = [s for s in snaps[:-1] if s.fetched_at <= cutoff]
    base = older[-1] if older else snaps[0]
    days = max(round((latest.fetched_at - base.fetched_at).total_seconds() / 86400), 1)
    return {"views_before": base.views, "views_change": latest.views - base.views, "trend_days": days}


STATS_WINDOW = timedelta(days=31)  # enough for the 30-point sparkline and the 7-day trend


def _snapshots(db: Session, job_ids: list[int], now: datetime) -> dict[int, list[models.VideoStat]]:
    """Each job's snapshots from the last 31 days, plus its latest one even
    when older, oldest first. Never the whole history."""
    S = models.VideoStat
    out: dict[int, list[models.VideoStat]] = defaultdict(list)
    if not job_ids:
        return out
    latest = (select(S.publish_job_id, func.max(S.fetched_at).label("mx"))
              .where(S.publish_job_id.in_(job_ids)).group_by(S.publish_job_id).subquery())
    rows = (db.query(S).join(latest, S.publish_job_id == latest.c.publish_job_id)
            .filter(or_(S.fetched_at >= now - STATS_WINDOW, S.fetched_at == latest.c.mx))
            .order_by(S.fetched_at, S.id).all())
    for s in rows:
        out[s.publish_job_id].append(s)
    return out


def _project_facts(db: Session, projects: list[models.VideoProject]) -> dict[int, dict]:
    """Grouping facts per project, with a fixed number of queries however
    many projects there are (relations are eager-loaded by the caller)."""
    avatar_ids = {p.primary_avatar_id for p in projects if p.primary_avatar_id}
    avatars = dict(db.query(models.Avatar.id, models.Avatar.name)
                   .filter(models.Avatar.id.in_(avatar_ids)).all()) if avatar_ids else {}
    from_jobs = _job_models(db, [p.id for p in projects])
    facts: dict[int, dict] = {}
    for p in projects:
        cfg = p.ambient_config_json or {}
        ambient = p.mode == AMBIENT_MODE
        dur = _duration(p)
        facts[p.id] = {
            "duration_seconds": dur,
            "length_bucket": length_bucket(dur),
            "preset": (cfg.get("scene_preset") or "custom") if ambient else "Storyboard (no preset)",
            "time_weather": (f"{cfg.get('time_of_day') or 'any'} · {cfg.get('weather') or 'any'}"
                             if ambient else "Storyboard (no time or weather)"),
            "character": _character(p, avatars),
            "model": _model(p, from_jobs),
            "aspect_ratio": p.aspect_ratio or "",
        }
    return facts


def result_rows(db: Session, now: Optional[datetime] = None) -> list[dict]:
    now = now or datetime.utcnow()
    J, R, P = models.PublishJob, models.Render, models.VideoProject
    rows = (db.query(J, R, P).join(R, J.render_id == R.id).join(P, R.project_id == P.id)
            .options(selectinload(P.shots).selectinload(models.VideoShot.takes),
                     selectinload(P.cast_members).selectinload(models.ProjectCastMember.avatar))
            .filter(J.external_id != "", J.status != "failed").order_by(J.id.desc()).all())
    if not rows:
        return []
    snaps = _snapshots(db, [j.id for j, _, _ in rows], now)
    costs = project_costs(db, [p.id for _, _, p in rows])
    versions = render_versions(db, [p.id for _, _, p in rows])
    facts = _project_facts(db, list({p.id: p for _, _, p in rows}.values()))
    out = []
    for job, render, p in rows:
        f = facts[p.id]
        published = job.yt_publish_at or job.publish_at or uploaded_at(job) or job.created_at
        if job.yt_publish_at and job.yt_publish_at > now:
            published = job.yt_publish_at
        age = max((now - published).total_seconds() / 86400, 0.0)
        js = snaps.get(job.id, [])
        last = js[-1] if js else None
        cost, est = costs.get(p.id, (0.0, False))
        views = last.views if last else None
        live_days = max(age, 1.0)
        state, warning = publish_state.display_state(job)
        out.append({
            "publish_job_id": job.id,
            "render_id": render.id,
            "render_version": versions.get(render.id),
            "project_id": p.id,
            "project_title": p.title or "",
            "project_kind": project_kind(p),
            "project_url": project_url(p),
            "title": job.yt_title or job.title,
            "external_url": job.external_url,
            "thumbnail_url": thumbnail_url(render),
            "state": state,
            "warning": warning,
            "published_at": published,
            "age_days": int(age),
            "views": views,
            "likes": last.likes if last else None,
            "comments": last.comments if last else None,
            "views_per_day": round(views / live_days, 2) if views is not None else None,
            "stats_fetched_at": last.fetched_at if last else None,
            "cost_usd": cost,
            "cost_is_estimate": est,
            "cost_per_1k_views": per_1k(cost, views),
            **_trend(js),
            "sparkline": [s.views for s in js[-30:]],
            **f,
        })
    return out


GROUPS = {
    "type": ("project_kind", "Video type"),
    "preset": ("preset", "Ambient preset"),
    "time_weather": ("time_weather", "Time of day + weather"),
    "length": ("length_bucket", "Video length"),
    "character": ("character", "Main character"),
    "model": ("model", "Video model"),
    "aspect": ("aspect_ratio", "Aspect ratio"),
}
TOO_FEW = 3


def group_rows(rows: list[dict], by: str) -> list[dict]:
    field = GROUPS[by][0]
    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        key = r.get(field) or "Unknown"
        if field == "project_kind":
            key = "Ambient" if key == "ambient" else "Storyboard"
        buckets[key].append(r)
    out = []
    for key, items in buckets.items():
        # Videos with no numbers yet (just uploaded, never fetched, YouTube
        # not connected) are left out of views, median and cost per 1k:
        # they'd count as 0 views.
        counted = [r for r in items if r["views"] is not None]
        views = [r["views"] for r in counted]
        # Whole-project spend: count each project once, even with several uploads.
        per_project = {r["project_id"]: (r["cost_usd"], r["cost_is_estimate"]) for r in items}
        total_cost = round(sum(c for c, _ in per_project.values()), 4)
        counted_cost = round(sum(per_project[pid][0] for pid in {r["project_id"] for r in counted}), 4)
        total_views = sum(views)
        out.append({
            "key": key,
            "videos": len(items),
            "videos_with_numbers": len(counted),
            "total_views": total_views,
            "median_views": float(stats_mod.median(views)) if views else None,
            "total_cost_usd": total_cost,
            "cost_is_estimate": any(e for _, e in per_project.values()),
            "cost_per_1k_views": per_1k(counted_cost, total_views),
            "too_few": len(counted) < TOO_FEW,
        })
    out.sort(key=lambda g: (-g["total_views"], g["key"]))
    return out

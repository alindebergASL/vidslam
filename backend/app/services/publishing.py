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
    render; a hook-lab variant has its base render's number), the same
    numbering as the render page and the export zip."""
    from . import render_versions as rv

    return rv.numbers(db, project_ids)


def version_labels(db: Session, renders: Iterable[models.Render]) -> dict[int, str]:
    """render id → "Version 3" / "Version 3 · opening B"."""
    from . import render_versions as rv

    return {rid: d["version_label"] for rid, d in rv.describe(db, list(renders)).items()}


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
             version: Optional[int], version_label: str = "") -> dict:
    state, warning = publish_state.display_state(job)
    return {
        "id": job.id,
        "render_id": render.id,
        "render_version": version,
        "render_version_label": version_label,
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
    labels = version_labels(db, [r for _, r, _ in rows])
    items = [job_item(j, r, p, versions.get(r.id), labels.get(r.id, "")) for j, r, p in rows]
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
    labels = version_labels(db, [r for r, _ in rows])
    failed = dict(db.query(J.render_id, func.count(J.id))
                  .filter(J.render_id.in_([r.id for r, _ in rows]), J.status == "failed")
                  .group_by(J.render_id).all()) if rows else {}
    items = [{
        "render_id": r.id,
        "render_version": versions.get(r.id),
        "render_version_label": labels.get(r.id, ""),
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
    # The body shots plus the end card compose draws (only with a CTA, and
    # at its own length, not the end-card shot's planned one).
    from .pipeline import END_CARD_SECONDS

    total = sum(float(s.duration_seconds or 0) for s in p.shots
                if s.shot_type not in ("end_card", "ambient_segment"))
    if total and (p.cta_text or "").strip():
        total += END_CARD_SECONDS
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


def variant_render_costs(db: Session, project_ids: Iterable[int]) -> dict[int, tuple[int, float, bool]]:
    """Hook-lab variant render id → (project id, its own ledger spend, any
    part estimated): what making that opening cost."""
    ids = list(set(project_ids))
    if not ids:
        return {}
    S, R = models.SpendEntry, models.Render
    est = case((or_(S.is_estimate.is_(True), S.source == "pending"), 1), else_=0)
    variants = dict(db.query(R.id, R.project_id).filter(R.project_id.in_(ids), R.hook_variant_id.isnot(None)).all())
    out = {rid: (pid, 0.0, False) for rid, pid in variants.items()}
    if variants:
        for rid, total, e in (db.query(S.render_id, func.coalesce(func.sum(S.cost_usd), 0.0), func.max(est))
                              .filter(S.render_id.in_(list(variants))).group_by(S.render_id).all()):
            out[rid] = (variants[rid], round(float(total or 0), 4), bool(e))
    return out


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


_seconds_cache: dict[tuple[str, int], float] = {}


def _file_seconds(path: Optional[str]) -> float:
    """A video file's length (ffprobe once per file version; 0 when gone)."""
    from .renderer import _probe_duration

    if not path:
        return 0.0
    p = Path(path)
    try:
        key = (str(p), p.stat().st_mtime_ns)
    except OSError:
        return 0.0
    if key not in _seconds_cache:
        if len(_seconds_cache) > 5000:
            _seconds_cache.clear()
        _seconds_cache[key] = float(_probe_duration(p) or 0.0)
    return _seconds_cache[key]


def _render_facts(render: models.Render, base: dict, avatars: dict[int, str]) -> dict:
    """The project's facts, overridden by what this render was actually
    made from (its composition_json) when it was recorded: the real length,
    the shots' video models, the aspect ratio and the main character."""
    comp = render.composition_json or {}
    if not comp:
        # Made before compositions were recorded: its file's real length
        # (cached per file), not the project's current planned shots.
        dur = _file_seconds(render.final_video_path)
        if not dur:
            return base
        return {**base, "duration_seconds": round(dur, 2), "length_bucket": length_bucket(dur)}
    facts = dict(base)
    dur = comp.get("duration_seconds")
    if not dur and comp.get("body_seconds"):
        dur = float(comp["body_seconds"]) + float(comp.get("end_card_seconds") or 0)
    if dur:
        facts["duration_seconds"] = round(float(dur), 2)
        facts["length_bucket"] = length_bucket(float(dur))
    used = Counter(str(sh.get("provider_model") or "") for sh in comp.get("shots") or []
                   if sh.get("provider_model"))
    if used:
        facts["model"] = used.most_common(1)[0][0]
    if comp.get("aspect_ratio"):
        facts["aspect_ratio"] = comp["aspect_ratio"]
    if comp.get("avatar_id") in avatars:
        facts["character"] = avatars[comp["avatar_id"]]
    return facts


_NO_HOOK = {"hook_variant_id": None, "hook_base_render_id": None, "hook_label": "", "hook_style_key": "",
            "hook_style": "No spoken opening", "hook_text": ""}


def _hook_facts(db: Session, pairs: list[tuple[models.Render, models.VideoProject]]) -> dict[int, dict]:
    """render id → its opening: a hook-lab variant's label, style and text
    (grouped with its base render), or for a storyboard render its own
    opening ("A", original)."""
    from . import hook_lab

    vids = {r.hook_variant_id for r, _ in pairs if r.hook_variant_id}
    variants = {v.id: v for v in db.query(models.HookVariant).filter(models.HookVariant.id.in_(vids))} if vids else {}
    out: dict[int, dict] = {}
    for r, p in pairs:
        if p.mode == AMBIENT_MODE:
            continue
        v = variants.get(r.hook_variant_id or 0)
        if v is not None:
            out[r.id] = {"hook_variant_id": v.id, "hook_base_render_id": v.base_render_id, "hook_label": v.label,
                         "hook_style_key": v.style, "hook_style": hook_lab.STYLE_LABELS.get(v.style, v.style),
                         "hook_text": v.hook_text}
            continue
        comp = r.composition_json or {}
        text = "" if comp.get("spoken_unknown") else (
            str(comp.get("hook") or "") or hook_lab.plan_hook(p.generated_plan_json))
        if not text:
            continue
        out[r.id] = {"hook_variant_id": None, "hook_base_render_id": r.id, "hook_label": "A",
                     "hook_style_key": "original", "hook_style": hook_lab.STYLE_LABELS["original"],
                     "hook_text": text}
    return out


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
    labels = version_labels(db, [r for _, r, _ in rows])
    variant_costs = variant_render_costs(db, [p.id for _, _, p in rows])
    hooks = _hook_facts(db, [(r, p) for _, r, p in rows])
    facts = _project_facts(db, list({p.id: p for _, _, p in rows}.values()))
    comp_avatars = {(r.composition_json or {}).get("avatar_id") for _, r, _ in rows} - {None}
    avatars = dict(db.query(models.Avatar.id, models.Avatar.name)
                   .filter(models.Avatar.id.in_(comp_avatars)).all()) if comp_avatars else {}
    out = []
    for job, render, p in rows:
        f = _render_facts(render, facts[p.id], avatars)
        published = job.yt_publish_at or job.publish_at or uploaded_at(job) or job.created_at
        if job.yt_publish_at and job.yt_publish_at > now:
            published = job.yt_publish_at
        age = max((now - published).total_seconds() / 86400, 0.0)
        js = snaps.get(job.id, [])
        last = js[-1] if js else None
        if render.hook_variant_id:
            # A hook-lab variant: what its own opening cost.
            _, cost, est = variant_costs.get(render.id, (p.id, 0.0, False))
            cost_scope = "render"
        else:
            # The project's spend, without what its other openings cost.
            cost, est = costs.get(p.id, (0.0, False))
            cost = round(max(cost - sum(c for pid, c, _ in variant_costs.values() if pid == p.id), 0.0), 4)
            cost_scope = "project"
        views = last.views if last else None
        live_days = max(age, 1.0)
        state, warning = publish_state.display_state(job)
        out.append({
            "publish_job_id": job.id,
            "render_id": render.id,
            "render_version": versions.get(render.id),
            "render_version_label": labels.get(render.id, ""),
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
            "cost_scope": cost_scope,
            **hooks.get(render.id, _NO_HOOK),
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
    "hook": ("hook_style", "Opening (hook) style"),
}
TOO_FEW = 3


def _cost_key(r: dict) -> str:
    return f"r{r['render_id']}" if r.get("cost_scope") == "render" else f"p{r['project_id']}"


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
        # Whole-project spend: count each project once, even with several
        # uploads; a hook-lab variant's own spend once per variant render.
        per_project = {_cost_key(r): (r["cost_usd"], r["cost_is_estimate"]) for r in items}
        total_cost = round(sum(c for c, _ in per_project.values()), 4)
        counted_cost = round(sum(per_project[k][0] for k in {_cost_key(r) for r in counted}), 4)
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


# ---------- hook tests ----------

HOOK_TEST_MIN_DAYS = 3


def _all_snapshots(db: Session, job_ids: list[int]) -> dict[int, list[models.VideoStat]]:
    S = models.VideoStat
    out: dict[int, list[models.VideoStat]] = defaultdict(list)
    if job_ids:
        for st in db.query(S).filter(S.publish_job_id.in_(job_ids)).order_by(S.fetched_at, S.id):
            out[st.publish_job_id].append(st)
    return out


def views_at_age(snaps: list[models.VideoStat], published: datetime, days: int) -> Optional[int]:
    """Views `days` days after `published` from the daily snapshots: the
    last one taken by then, when one was taken close to that age."""
    if days < 1:
        return None
    cutoff = published + timedelta(days=days, hours=12)
    by_then = [st for st in snaps if published <= st.fetched_at <= cutoff]
    if not by_then or (by_then[-1].fetched_at - published) < timedelta(days=days, hours=-12):
        return None
    return int(by_then[-1].views)


def _verdict(entries: list[dict], days: Optional[int]) -> tuple[str, Optional[str], bool]:
    """(plain verdict, leader label, ready)."""
    if len(entries) < 2:
        return "Too early to tell: publish at least two openings to compare them.", None, False
    if days is None or days < HOOK_TEST_MIN_DAYS or any(e["views_per_day_equal_age"] is None for e in entries):
        return (f"Too early to tell: each needs at least {HOOK_TEST_MIN_DAYS} days of numbers.", None, False)
    ranked = sorted(entries, key=lambda e: -e["views_per_day_equal_age"])
    top, second = ranked[0], ranked[1]
    if top["views_per_day_equal_age"] <= 0:
        return "No views yet on any of them.", None, True
    if second["views_per_day_equal_age"] <= 0:
        return f"{top['hook_label']} is ahead: the others have no views yet.", top["hook_label"], True
    ratio = top["views_per_day_equal_age"] / second["views_per_day_equal_age"]
    if ratio < 1.1:
        return "Too close to call: about the same views per day.", None, True
    return (f"{top['hook_label']} is ahead: {ratio:.1f}× the views per day.", top["hook_label"], True)


def hook_tests(db: Session, rows: list[dict], now: Optional[datetime] = None) -> list[dict]:
    """Each base video with other openings that are on YouTube: views per
    day at equal age (the same number of days since upload, from the daily
    snapshots), likes per 1,000 views, cost (the base: the project's, once;
    a variant: its own) and a plain verdict once each has 3 days of numbers."""
    now = now or datetime.utcnow()
    groups: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        if r.get("hook_base_render_id"):
            groups[r["hook_base_render_id"]].append(r)
    groups = {b: items for b, items in groups.items() if any(i.get("hook_variant_id") for i in items)}
    if not groups:
        return []
    snaps = _all_snapshots(db, [r["publish_job_id"] for items in groups.values() for r in items])
    bases = {r.id: r for r in db.query(models.Render).filter(models.Render.id.in_(list(groups)))}
    base_labels = version_labels(db, list(bases.values()))
    out: list[dict] = []
    for base_id, items in groups.items():
        # One entry per video: a render uploaded twice (a re-upload, or a
        # public and an unlisted test) is one opening, never compared with
        # itself; its most-viewed upload stands for it.
        per_render: dict[int, dict] = {}
        uploads: Counter = Counter()
        for r in sorted(items, key=lambda r: r["publish_job_id"]):
            uploads[r["render_id"]] += 1
            cur = per_render.get(r["render_id"])
            if cur is None or (r.get("views") or 0) > (cur.get("views") or 0):
                per_render[r["render_id"]] = r
        items = sorted(per_render.values(), key=lambda r: (r.get("hook_label") or "", r["publish_job_id"]))
        with_numbers = [r for r in items if snaps.get(r["publish_job_id"])]
        days = min((int((now - r["published_at"]).total_seconds() // 86400) for r in with_numbers),
                   default=None) if len(with_numbers) == len(items) else None
        entries = []
        for r in items:
            js = snaps.get(r["publish_job_id"], [])
            at = views_at_age(js, r["published_at"], days) if days else None
            last = js[-1] if js else None
            entries.append({
                "hook_label": r.get("hook_label") or "", "hook_style": r.get("hook_style") or "",
                "hook_style_key": r.get("hook_style_key") or "", "hook_text": r.get("hook_text") or "",
                "is_base": not r.get("hook_variant_id"), "hook_variant_id": r.get("hook_variant_id"),
                "render_id": r["render_id"], "render_version_label": r.get("render_version_label") or "",
                "publish_job_id": r["publish_job_id"], "title": r["title"], "external_url": r["external_url"],
                "thumbnail_url": r["thumbnail_url"], "published_at": r["published_at"], "age_days": r["age_days"],
                "views": r["views"], "likes": r["likes"],
                "views_at_equal_age": at,
                "views_per_day_equal_age": round(at / days, 2) if at is not None and days else None,
                "likes_per_1k_views": (round(last.likes / last.views * 1000, 1)
                                       if last is not None and last.views else None),
                "cost_usd": r["cost_usd"], "cost_scope": r.get("cost_scope", "project"),
                "cost_is_estimate": r["cost_is_estimate"],
                "uploads": uploads[r["render_id"]],
            })
        verdict, leader, ready = _verdict(entries, days)
        first = items[0]
        out.append({
            "base_render_id": base_id, "project_id": first["project_id"], "project_title": first["project_title"],
            "project_url": first["project_url"],
            "base_version_label": base_labels.get(base_id, ""),
            "equal_age_days": days, "ready": ready, "verdict": verdict, "leader": leader,
            "entries": entries,
        })
    out.sort(key=lambda g: -max(e["publish_job_id"] for e in g["entries"]))
    return out

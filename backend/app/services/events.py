"""Events: what happened to a job, for live progress and notifications.

`emit()` adds a row in the caller's open transaction (no commit), so an
event is committed together with the status change it reports, never
before and never without it. The SSE stream (api/events.py) polls the
table, which works across web processes, RQ workers and sweeper threads,
with or without Redis.

Kinds: render | plan | ambient_loop | ambient_render | studio_job |
training | publish | budget | hook_lab (one alternative opening of a
video, entity "hook_variant"). Statuses: started | progress | completed |
failed | cancelled. Severity: info | warning | error.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from .. import models

log = logging.getLogger("avs.events")

KINDS = ("render", "plan", "ambient_loop", "ambient_render", "studio_job", "training", "publish", "budget",
         "hook_lab")
STATUSES = ("started", "progress", "completed", "failed", "cancelled")
RETENTION_DAYS = 30


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def emit(db: Session, kind: str, entity_type: str, entity_id: int, status: str, title: str, *,
         detail: str = "", url: str = "", project_id: Optional[int] = None,
         severity: Optional[str] = None) -> Optional[models.Event]:
    """Add one event to the caller's transaction (the caller commits).
    Never raises into the caller: a lost notification must not fail a render."""
    try:
        if severity is None:
            severity = "error" if status == "failed" else "info"
        ev = models.Event(
            kind=kind[:40], entity_type=entity_type[:40], entity_id=int(entity_id or 0),
            project_id=project_id, status=status[:20], title=_clip(title, 200),
            detail=str(detail or "")[:2000], url=(url or "")[:300], severity=severity[:10],
            created_at=datetime.utcnow(),
        )
        db.add(ev)
        return ev
    except Exception:  # noqa: BLE001
        log.exception("could not record event %s/%s", kind, status)
        return None


def plain_error(error: str, n: int = 140) -> str:
    """The first line of an error, short enough for a title."""
    first = (str(error or "").strip().splitlines() or [""])[0]
    return _clip(first, n) or "unknown error"


# ---------- typed helpers (titles in plain language) ----------


def _name(project: Optional[models.VideoProject]) -> str:
    if project is None:
        return "your video"
    return _clip(project.title or f"Project {project.id}", 80)


def project_url(project: Optional[models.VideoProject]) -> str:
    if project is None:
        return ""
    if project.mode == "ambient_loop":
        return f"/ambient/{project.id}"
    return f"/projects/{project.id}"


def render_event(db: Session, render: models.Render, project: Optional[models.VideoProject], status: str,
                 *, detail: str = "", error: str = "") -> None:
    ambient = project is not None and project.mode == "ambient_loop"
    kind = "ambient_render" if ambient else "render"
    name = _name(project)
    if status == "completed":
        title = f"{'Scene' if ambient else 'Video'} ready: {name}"
    elif status == "failed":
        title = f"Render failed: {plain_error(error)}"
        detail = detail or f"{name}: {str(error or '')[:600]}"
    elif status == "cancelled":
        title = f"Render cancelled: {name}"
    elif status == "started":
        title = f"{'Rendering scene' if ambient else 'Making video'}: {name}"
    else:
        title = f"{name}: {detail}" if detail else name
    emit(db, kind, "render", render.id, status, title, detail=detail, url=project_url(project),
         project_id=project.id if project is not None else render.project_id)


def hook_lab_url(project: Optional[models.VideoProject]) -> str:
    return f"/projects/{project.id}/hooks" if project is not None else ""


def hook_variant_event(db: Session, variant: models.HookVariant, project: Optional[models.VideoProject],
                       status: str, *, detail: str = "", error: str = "") -> None:
    """One alternative opening (hook lab): started | progress | completed |
    failed | cancelled."""
    name = _name(project)
    what = f"Opening {variant.label}" if variant.label else "Opening"
    if status == "completed":
        title = f"{what} ready: {name}"
    elif status == "failed":
        title = f"{what} failed: {plain_error(error)}"
        detail = detail or f"{name}: {str(error or '')[:600]}"
    elif status == "cancelled":
        title = f"{what} cancelled: {name}"
    elif status == "started":
        title = f"Making {what.lower()}: {name}"
    else:
        title = f"{what}: {detail}" if detail else f"{what}: {name}"
    emit(db, "hook_lab", "hook_variant", variant.id, status, title, detail=detail,
         url=hook_lab_url(project), project_id=variant.project_id)


def plan_event(db: Session, project: models.VideoProject, status: str, *, error: str = "") -> None:
    name = _name(project)
    title = {"started": f"Planning storyboard: {name}", "completed": f"Storyboard ready: {name}",
             "failed": f"Storyboard failed: {plain_error(error)}"}.get(status, name)
    emit(db, "plan", "project", project.id, status, title,
         detail=(f"{name}: {str(error)[:600]}" if error else ""), url=project_url(project),
         project_id=project.id)


def ambient_loop_event(db: Session, project: models.VideoProject, status: str, *, error: str = "") -> None:
    name = _name(project)
    title = {"started": f"Making scene loop: {name}", "completed": f"Scene loop ready: {name}",
             "failed": f"Scene loop failed: {plain_error(error)}"}.get(status, name)
    emit(db, "ambient_loop", "project", project.id, status, title,
         detail=(f"{name}: {str(error)[:600]}" if error else ""), url=project_url(project),
         project_id=project.id)


def studio_event(db: Session, job: models.AssetGenerationJob, status: str, *, error: str = "") -> None:
    what = "picture" if job.output_kind == "image" else "clip"
    if status == "completed":
        title = f"Studio: {what} ready"
    elif status == "failed":
        title = f"Studio: {what} failed"
    elif status == "cancelled":
        title = f"Studio: {what} cancelled"
    else:
        title = f"Studio: making a {what}"
    detail = _clip(job.label or job.prompt or "", 160)
    if error:
        detail = f"{detail} — {plain_error(error, 300)}" if detail else plain_error(error, 300)
    emit(db, "studio_job", "studio_job", job.id, status, title, detail=detail, url="/studio")


def training_url(cm: models.CustomModel) -> str:
    return f"/cast?avatar={cm.owner_id}" if cm.owner_kind == "avatar" else "/cast"


def training_event(db: Session, cm: models.CustomModel, status: str, *, error: str = "") -> None:
    name = _clip(cm.name or f"model {cm.id}", 80)
    title = {"started": f"Training: {name}", "completed": f"Training done: {name}",
             "failed": f"Training failed: {name}", "cancelled": f"Training cancelled: {name}"}.get(status, name)
    emit(db, "training", "custom_model", cm.id, status, title,
         detail=plain_error(error, 400) if error else "", url=training_url(cm))


def publish_event(db: Session, job: models.PublishJob, status: str, *, error: str = "",
                  warning: str = "") -> None:
    name = _clip(job.title or f"video {job.render_id}", 80)
    if status == "failed":
        title, detail = f"YouTube upload failed: {name}", plain_error(error, 400)
    elif job.status == "scheduled":
        title, detail = f"Scheduled on YouTube: {name}", warning
    else:
        title, detail = f"Uploaded to YouTube: {name}", warning
    emit(db, "publish", "publish_job", job.id, status, title, detail=detail, url="/publishing",
         severity="error" if status == "failed" else ("warning" if warning else "info"))


# ---------- retention ----------


def prune(db: Session, *, days: int = RETENTION_DAYS) -> int:
    """Delete events older than `days`; returns how many."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    n = db.query(models.Event).filter(models.Event.created_at < cutoff).delete(synchronize_session=False)
    db.commit()
    return int(n or 0)


def latest_id(db: Session) -> int:
    from sqlalchemy import func

    return int(db.query(func.max(models.Event.id)).scalar() or 0)


def oldest_id(db: Session) -> int:
    from sqlalchemy import func

    return int(db.query(func.min(models.Event.id)).scalar() or 0)


def out(ev: models.Event) -> dict:
    return {
        "id": ev.id, "kind": ev.kind, "entity_type": ev.entity_type, "entity_id": ev.entity_id,
        "project_id": ev.project_id, "status": ev.status, "title": ev.title, "detail": ev.detail or "",
        "url": ev.url or "", "severity": ev.severity or "info",
        "read": ev.read_at is not None,
        "created_at": ev.created_at.isoformat() if ev.created_at else None,
    }


# ---------- what is running right now ----------

_RENDER_STAGE = {
    "pending": "Queued", "planning": "Planning", "generating_audio": "Making the voiceover",
    "generating_shots": "Making clips", "polling": "Making clips", "rendering": "Putting the video together",
}
_STUDIO_STAGE = {"pending": "Queued", "submitted": "Starting", "polling": "Making…"}
_IDLE_PROJECT = ("draft", "planned", "completed", "failed", "cancelled")
_DONE_RENDER = ("completed", "failed", "cancelled", "expired")


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def active(db: Session, *, studio_limit: int = 5) -> dict:
    """In-flight work across the app, from the status tables (cheap
    indexed queries): renders, plans, ambient loops, Studio jobs, training,
    YouTube uploads."""
    items: list[dict] = []
    Render, Project = models.Render, models.VideoProject
    renders = (db.query(Render).filter(Render.status.notin_(_DONE_RENDER))
               .order_by(Render.id.desc()).limit(50).all())
    seen_projects: set[int] = set()
    for r in renders:
        if r.project_id in seen_projects:
            continue  # the newest one per project
        seen_projects.add(r.project_id)
        p = db.get(Project, r.project_id)
        ambient = p is not None and p.mode == "ambient_loop"
        stage = _RENDER_STAGE.get(r.status, r.status)
        if r.status == "generating_shots" and p is not None:
            body = [s for s in p.shots if s.shot_type not in ("end_card", "ambient_segment")]
            done = sum(1 for s in body if s.status == "completed")
            if body:
                stage = f"Making clips ({done} of {len(body)} ready)"
        items.append({
            "kind": "ambient_render" if ambient else "render", "entity_type": "render", "entity_id": r.id,
            "project_id": r.project_id, "title": _name(p), "url": project_url(p), "stage": stage,
            "status": r.status, "started_at": _iso(r.created_at),
        })
    busy = (db.query(Project).filter(Project.status.notin_(_IDLE_PROJECT))
            .order_by(Project.updated_at.desc()).limit(50).all())
    for p in busy:
        if p.id in seen_projects:
            continue
        seen_projects.add(p.id)
        ambient = p.mode == "ambient_loop"
        if p.status == "planning":
            kind, stage = "plan", "Planning the storyboard"
        elif ambient:
            kind, stage = "ambient_loop", "Making the scene loop"
        elif p.status in ("regenerating", "submitting"):
            kind, stage = "render", "Re-making clips"
        else:
            kind, stage = "render", "Starting"
        items.append({
            "kind": kind, "entity_type": "project", "entity_id": p.id, "project_id": p.id,
            "title": _name(p), "url": project_url(p), "stage": stage, "status": p.status,
            "started_at": _iso(p.updated_at),
        })
    Job = models.AssetGenerationJob
    running = Job.status.in_(tuple(_STUDIO_STAGE))
    studio_count = db.query(Job.id).filter(running).count()
    newest = db.query(Job).filter(running).order_by(Job.id.desc()).limit(studio_limit).all()
    studio = {
        "count": studio_count,
        "jobs": [{"id": j.id, "output_kind": j.output_kind, "status": j.status,
                  "stage": _STUDIO_STAGE.get(j.status, j.status), "prompt": _clip(j.prompt or "", 120),
                  "label": j.label or "", "started_at": _iso(j.created_at)} for j in newest],
    }
    if studio_count:
        what = "job" if studio_count == 1 else "jobs"
        items.append({
            "kind": "studio_job", "entity_type": "studio_job", "entity_id": newest[0].id if newest else 0,
            "project_id": None, "title": f"Studio: {studio_count} {what} running", "url": "/studio",
            "stage": _STUDIO_STAGE.get(newest[0].status, "") if newest else "", "status": "running",
            "started_at": _iso(newest[-1].created_at) if newest else None,
        })
    CM = models.CustomModel
    for cm in db.query(CM).filter(CM.status.in_(("pending", "training"))).order_by(CM.id.desc()).limit(20):
        items.append({
            "kind": "training", "entity_type": "custom_model", "entity_id": cm.id, "project_id": None,
            "title": f"Training: {_clip(cm.name or f'model {cm.id}', 80)}", "url": training_url(cm),
            "stage": "Queued" if cm.status == "pending" else f"Training ({round((cm.progress or 0) * 100)}%)",
            "status": cm.status, "started_at": _iso(cm.created_at),
        })
    PJ = models.PublishJob
    for pj in db.query(PJ).filter(PJ.status.in_(("queued", "uploading"))).order_by(PJ.id.desc()).limit(20):
        pct = (f" ({round(pj.bytes_sent / pj.bytes_total * 100)}%)"
               if pj.status == "uploading" and pj.bytes_total else "")
        items.append({
            "kind": "publish", "entity_type": "publish_job", "entity_id": pj.id, "project_id": None,
            "title": f"YouTube: {_clip(pj.title or 'video', 80)}", "url": "/publishing",
            "stage": "Queued" if pj.status == "queued" else f"Uploading{pct}",
            "status": pj.status, "started_at": _iso(pj.created_at),
        })
    counts = {
        "projects": sum(1 for i in items if i["kind"] in ("render", "plan")),
        "ambient": sum(1 for i in items if i["kind"] in ("ambient_render", "ambient_loop")),
        "studio": studio_count,
        "training": sum(1 for i in items if i["kind"] == "training"),
        "publish": sum(1 for i in items if i["kind"] == "publish"),
    }
    return {"items": items, "studio": studio, "counts": counts}

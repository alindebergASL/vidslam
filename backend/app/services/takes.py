"""Takes: every clip made for a storyboard shot is kept, so a re-roll that
turns out worse can be undone without paying again.

A take is recorded whenever a shot gets a new clip (pipeline.on_shot_clip_ready).
A clip made before takes existed is recorded lazily the first time it is
looked at or is about to be replaced. Each shot keeps its newest KEEP takes;
the current one is never removed.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models

log = logging.getLogger("avs.takes")

KEEP = 10  # takes kept per shot (the current one always stays)


def _is_storyboard_shot(shot: models.VideoShot) -> bool:
    project = shot.project
    return (shot.shot_type not in ("end_card", "ambient_segment", "ambient_loop", "ambient_variant")
            and (project is None or project.mode != "ambient_loop"))


def current_take(shot: models.VideoShot) -> Optional[models.ShotTake]:
    if not shot.clip_path:
        return None
    for t in shot.takes:
        if t.clip_path == shot.clip_path:
            return t
    return None


def ensure_current_take(db: Session, shot: models.VideoShot) -> Optional[models.ShotTake]:
    """Record the shot's current clip as a take if it is not one yet (a
    clip from before takes existed). Returns the current take, if any."""
    if not shot.clip_path or shot.status != "completed" or not _is_storyboard_shot(shot):
        return current_take(shot)
    take = current_take(shot)
    if take is not None:
        return take
    take = models.ShotTake(
        shot_id=shot.id, clip_path=shot.clip_path, clip_fingerprint=shot.clip_fingerprint or "",
        aspect_ratio=shot.clip_aspect_ratio or "", provider=shot.provider or "",
        provider_model=shot.provider_model or "", note="",
    )
    shot.takes.append(take)
    db.commit()
    return take


def _ledger_cost(db: Session, job_id: int) -> Optional[float]:
    total = db.query(func.sum(models.SpendEntry.cost_usd)).filter(
        models.SpendEntry.ref == f"video_job:{job_id}").scalar()
    return round(float(total), 6) if total is not None else None


def record(db: Session, shot: models.VideoShot, job: models.VideoJob) -> Optional[models.ShotTake]:
    """The clip `job` just made is now the shot's clip: keep it as a take."""
    if not shot.clip_path or not _is_storyboard_shot(shot):
        return None
    take = current_take(shot)
    if take is None:
        take = models.ShotTake(shot_id=shot.id, clip_path=shot.clip_path)
        shot.takes.append(take)
    take.clip_fingerprint = shot.clip_fingerprint or ""
    take.aspect_ratio = shot.clip_aspect_ratio or ""
    take.provider = shot.provider or ""
    take.provider_model = shot.provider_model or ""
    take.video_job_id = job.id
    take.cost_usd = _ledger_cost(db, job.id) if job.cost_usd is None else round(float(job.cost_usd), 6)
    take.note = str((job.context_json or {}).get("note", "") or "")[:500]
    db.commit()
    prune(db, shot)
    return take


def _referenced_elsewhere(db: Session, path: str, *, take_id: Optional[int] = None,
                          shot_id: Optional[int] = None) -> bool:
    """Another shot's clip or another take still uses this file."""
    q = db.query(models.VideoShot.id).filter(models.VideoShot.clip_path == path)
    if shot_id is not None:
        q = q.filter(models.VideoShot.id != shot_id)
    if q.first() is not None:
        return True
    q = db.query(models.ShotTake.id).filter(models.ShotTake.clip_path == path)
    if take_id is not None:
        q = q.filter(models.ShotTake.id != take_id)
    return q.first() is not None


def remove_file(db: Session, path: str, *, take_id: Optional[int] = None,
                shot_id: Optional[int] = None) -> bool:
    """Delete a clip file unless something else still uses it."""
    if not path or _referenced_elsewhere(db, path, take_id=take_id, shot_id=shot_id):
        return False
    try:
        Path(path).unlink(missing_ok=True)
        return True
    except OSError:
        log.warning("could not delete clip file %s", path)
        return False


def delete(db: Session, shot: models.VideoShot, take: models.ShotTake) -> None:
    """Remove a take that is not the shot's current clip, and its file."""
    path, take_id = take.clip_path, take.id
    shot.takes.remove(take)
    db.flush()
    remove_file(db, path, take_id=take_id)
    db.commit()


def prune(db: Session, shot: models.VideoShot) -> int:
    """Keep the newest KEEP takes (by when they were made); never the
    current one. Returns how many were removed."""
    takes = sorted(shot.takes, key=lambda t: t.id, reverse=True)
    if len(takes) <= KEEP:
        return 0
    removed = 0
    for take in takes[KEEP:]:
        if take.clip_path and take.clip_path == shot.clip_path:
            continue
        delete(db, shot, take)
        removed += 1
    return removed


def use(db: Session, shot: models.VideoShot, take: models.ShotTake) -> None:
    """Make `take` the shot's clip again. Nothing is generated or paid for:
    the next Re-compose (or Generate video, which reuses it) uses it."""
    shot.clip_path = take.clip_path
    shot.clip_fingerprint = take.clip_fingerprint or ""
    shot.clip_aspect_ratio = take.aspect_ratio or ""
    shot.provider = take.provider or ""
    shot.provider_model = take.provider_model or ""
    shot.status = "completed"
    shot.error = ""
    db.commit()


def out(project_id: int, shot: models.VideoShot, take: models.ShotTake) -> dict:
    return {
        "id": take.id,
        "shot_id": shot.id,
        "current": bool(shot.clip_path) and take.clip_path == shot.clip_path,
        "provider": take.provider,
        "provider_model": take.provider_model,
        "cost_usd": take.cost_usd,
        "note": take.note or "",
        "aspect_ratio": take.aspect_ratio or "",
        "file_exists": bool(take.clip_path) and Path(take.clip_path).is_file(),
        "url": f"/api/projects/{project_id}/shots/{shot.id}/takes/{take.id}/clip",
        "created_at": take.created_at.isoformat() if take.created_at else None,
    }

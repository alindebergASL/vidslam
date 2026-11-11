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
    # "pending": a render marked the clip out of date but has not replaced it yet.
    if not shot.clip_path or shot.status not in ("completed", "pending") or not _is_storyboard_shot(shot):
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
    take.inspection_json = None  # a new clip: an earlier verdict doesn't apply to it
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
                shot_id: Optional[int] = None, project_id: Optional[int] = None) -> bool:
    """Delete a clip file unless something else still uses it (another
    shot or take, or, given `project_id`, a render or hook-lab variant of
    the project's hook lab still needs: in_use())."""
    if not path or _referenced_elsewhere(db, path, take_id=take_id, shot_id=shot_id):
        return False
    if project_id is not None and path in in_use(db, project_id)[1]:
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
    remove_file(db, path, take_id=take_id, project_id=shot.project_id)
    db.commit()


def protected(db: Session, project_id: int) -> tuple[set[int], set[str]]:
    """(take ids, clip paths) that a render or a hook-lab variant still
    needs: every take and clip a render's composition_json records (a
    variant re-composes from its base render's clips), and the take a
    variant picked for its shot 1."""
    take_ids: set[int] = set()
    paths: set[str] = set()
    for (comp,) in (db.query(models.Render.composition_json)
                    .filter(models.Render.project_id == project_id,
                            models.Render.composition_json.isnot(None))):
        for sh in (comp or {}).get("shots") or []:
            if sh.get("take_id"):
                take_ids.add(int(sh["take_id"]))
            if sh.get("clip_path"):
                paths.add(str(sh["clip_path"]))
    for (tid,) in (db.query(models.HookVariant.shot1_take_id)
                   .filter(models.HookVariant.project_id == project_id,
                           models.HookVariant.shot1_take_id.isnot(None))):
        take_ids.add(int(tid))
    return take_ids, paths


def in_use(db: Session, project_id: int) -> tuple[set[int], set[str]]:
    """(take ids, clip paths) the hook lab needs right now, so deleting a
    take or a shot must not remove them: the take an opening (not archived)
    picked for its shot 1, and every clip of a version that has openings
    (they are re-composed from its clips) or of an opening being made."""
    take_ids: set[int] = set()
    paths: set[str] = set()
    V = models.HookVariant
    bases: set[int] = set()
    for tid, base_id in (db.query(V.shot1_take_id, V.base_render_id)
                         .filter(V.project_id == project_id, V.status != "archived")):
        if tid:
            take_ids.add(int(tid))
        bases.add(int(base_id))
    R = models.Render
    rows = db.query(R.composition_json).filter(R.project_id == project_id, R.composition_json.isnot(None))
    rows = rows.filter((R.id.in_(bases)) | (R.hook_variant_id.isnot(None) & R.status.notin_(
        ("completed", "failed", "cancelled", "expired")))) if bases else rows.filter(
        R.hook_variant_id.isnot(None), R.status.notin_(("completed", "failed", "cancelled", "expired")))
    for (comp,) in rows:
        for sh in (comp or {}).get("shots") or []:
            if sh.get("take_id"):
                take_ids.add(int(sh["take_id"]))
            if sh.get("clip_path"):
                paths.add(str(sh["clip_path"]))
    return take_ids, paths


def prune(db: Session, shot: models.VideoShot) -> int:
    """Keep the newest KEEP takes (by when they were made); never the
    current one, nor one a render or a hook-lab variant still needs
    (protected()). Returns how many were removed."""
    takes = sorted(shot.takes, key=lambda t: t.id, reverse=True)
    if len(takes) <= KEEP:
        return 0
    keep_ids, keep_paths = protected(db, shot.project_id)
    removed = 0
    for take in takes[KEEP:]:
        if take.clip_path and take.clip_path == shot.clip_path:
            continue
        if take.id in keep_ids or (take.clip_path and take.clip_path in keep_paths):
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
        "inspection": take.inspection_json,
        "created_at": take.created_at.isoformat() if take.created_at else None,
    }


# ---------- a Studio clip as a take ----------

_ASPECTS = {"9:16": 9 / 16, "16:9": 16 / 9, "1:1": 1.0}


def nearest_aspect(width: int, height: int) -> str:
    """"9:16", "16:9" or "1:1", whichever the size's shape is closest to."""
    import math

    r = width / height
    return min(_ASPECTS, key=lambda k: abs(math.log(r / _ASPECTS[k])))


class StudioImportError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def import_studio_clip(db: Session, shot: models.VideoShot,
                       job: models.AssetGenerationJob) -> tuple[models.ShotTake, str, float]:
    """Copy a finished Studio clip into the project as a new take of `shot`
    and make it the shot's clip. Nothing is generated or paid for. A clip
    longer than the shot is trimmed to it; a shorter one or another shape is
    kept, with a plain warning. Returns (take, warning, clip seconds)."""
    import shutil
    import uuid

    from . import fingerprints, storage
    from .renderer import _probe_duration, _probe_size, trim_clip

    project = shot.project
    src = Path(job.result_path or "")
    size = _probe_size(src)
    length = _probe_duration(src)
    if size is None or length <= 0:
        raise StudioImportError(422, "This clip can't be read, so it can't be used in a project.")
    aspect = nearest_aspect(*size)
    planned = float(shot.duration_seconds or 5.0)
    dest = storage.render_subdir(project.id) / f"shot_{shot.shot_order:02d}_{shot.id}_studio{job.id}_{uuid.uuid4().hex[:8]}.mp4"
    warnings: list[str] = []
    if length > planned + 0.05:
        trim_clip(src, dest, duration=planned)  # the video stays in step with captions and voiceover
        length = planned
    else:
        shutil.copyfile(src, dest)  # a copy: Studio delete and take prune each keep their own file
        if length < planned - 0.05:
            warnings.append(f"This clip is {length:.1f} s; the shot is {planned:.1f} s, "
                            f"so the video will be {planned - length:.1f} s shorter.")
    if project.aspect_ratio and aspect != project.aspect_ratio:
        warnings.append(f"This clip is {aspect}; the project is {project.aspect_ratio}, "
                        f"so it will be cropped/letterboxed.")
    vjob = (db.query(models.VideoJob).filter(models.VideoJob.purpose == "studio",
                                             models.VideoJob.ref_id == job.id)
            .order_by(models.VideoJob.id.desc()).first())
    cost = None
    if vjob is not None:
        cost = _ledger_cost(db, vjob.id)
        if cost is None and vjob.cost_usd is not None:
            cost = round(float(vjob.cost_usd), 6)
    ensure_current_take(db, shot)  # keep the clip being swapped out
    # Chosen for this project as it is: another shape is fitted at compose
    # (warned above), not re-made at the next Generate video. The take
    # counts as the project's shape, so using it again later (takes.use)
    # keeps it current too; its real shape is kept in the note.
    other_shape = bool(project.aspect_ratio) and aspect != project.aspect_ratio
    note = f"From Studio #{job.id}" + (f" ({aspect} clip)" if other_shape else "")
    take = models.ShotTake(
        shot_id=shot.id, clip_path=str(dest), clip_fingerprint=fingerprints.video_fingerprint(project, shot),
        aspect_ratio=project.aspect_ratio if other_shape else aspect, provider=job.provider or "",
        provider_model=job.provider_model or "", video_job_id=vjob.id if vjob is not None else None,
        cost_usd=cost, note=note,
    )
    shot.takes.append(take)
    db.commit()
    use(db, shot, take)
    prune(db, shot)
    db.refresh(take)
    return take, " ".join(warnings), round(length, 3)

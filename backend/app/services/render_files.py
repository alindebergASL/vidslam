"""Per-render output files, S3 mirroring and retention.

Every compose writes `final.mp4` / `thumb.jpg` into the project's render
dir. Before this module, each new render overwrote those, so every Render
row in a project's history pointed at the latest video (A/B compare showed
the same file twice). `finalize()` moves the outputs to
`render_<id>.mp4` / `render_<id>.jpg`, mirrors them to S3 when configured,
removes compose scratch files, and applies the retention policy.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from . import s3

log = logging.getLogger("avs.render_files")

# Compose scratch that is regenerated on every render (never referenced by
# a Render row). Kept: shot clips, voice, music, captions.ass (tiny, handy
# for debugging caption timing), ambient loop clip + frames.
_SCRATCH = (
    "norm_*.mp4", "silent.mp4", "endcard.mp4", "concat.txt",
    "ambient_norm.mp4", "ambient_unit.mp4", "ambient_head.mp4", "ambient_tail.mp4",
    "ambient_single.mp4", "ambient_concat.txt", "ambient_music_unit.m4a",
)


def finalize(db: Session, render: models.Render, final_path: Path, thumb_path: Optional[Path]) -> None:
    """Version the outputs for `render`, mirror to S3, clean scratch.
    Sets render.final_video_path / thumbnail_path. Call prune() after the
    render is marked completed so it counts toward the kept set."""
    out_dir = final_path.parent
    video = out_dir / f"render_{render.id}.mp4"
    shutil.move(str(final_path), video)
    render.final_video_path = str(video)
    if thumb_path is not None and thumb_path.exists():
        thumb = out_dir / f"render_{render.id}.jpg"
        shutil.move(str(thumb_path), thumb)
        render.thumbnail_path = str(thumb)
    for pattern in _SCRATCH:
        for p in out_dir.glob(pattern):
            try:
                p.unlink()
            except OSError:
                pass
    if not s3.mirror_to_s3(video, content_type="video/mp4") and get_settings().s3_bucket:
        log.warning("render %s not mirrored to S3; it will be served from local disk", render.id)
    if render.thumbnail_path:
        s3.mirror_to_s3(Path(render.thumbnail_path), content_type="image/jpeg")
    db.commit()


def prune(db: Session, project_id: int) -> int:
    """Keep the newest RENDER_RETENTION_KEEP completed renders per project
    (0 = keep everything). Older ones lose their local files (and S3
    copies) and are marked status="expired". Returns how many expired."""
    keep = get_settings().render_retention_keep
    if keep <= 0:
        return 0
    rows = (
        db.query(models.Render)
        .filter(models.Render.project_id == project_id, models.Render.status == "completed")
        .order_by(models.Render.created_at.desc(), models.Render.id.desc())
        .all()
    )
    busy = {
        rid for (rid,) in db.query(models.PublishJob.render_id)
        .filter(models.PublishJob.status.in_(("queued", "uploading"))).all()
    }
    expired = 0
    for r in rows[keep:]:
        if r.id in busy:
            continue  # an upload is reading this file; prune it next time
        for attr in ("final_video_path", "thumbnail_path"):
            path = getattr(r, attr)
            if not path:
                continue
            p = Path(path)
            # Never delete a file a newer render still points at (legacy
            # rows from before versioning all share final.mp4).
            if any(getattr(k, attr) == path for k in rows[:keep]):
                continue
            s3.delete_from_s3(p)
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        r.status = "expired"
        r.render_log = ("expired by retention policy (RENDER_RETENTION_KEEP=%d)\n" % keep) + (r.render_log or "")
        expired += 1
    if expired:
        db.commit()
        log.info("retention: expired %s render(s) in project %s", expired, project_id)
    return expired


def delivery_url(path: str, content_type: str, download_name: Optional[str] = None) -> Optional[str]:
    """Presigned S3 URL for a render file that is actually in the bucket,
    or None → serve the local file."""
    if not path or not s3.object_exists(Path(path)):
        return None
    return s3.presigned_url_for(Path(path), content_type=content_type, download_name=download_name)

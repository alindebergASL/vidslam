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
# a Render row). Kept: shot clips, voice, music, captions_<id>.ass (tiny,
# handy for debugging caption timing), ambient loop clip + frames. A
# storyboard compose keeps its scratch in its own tmp_<render id>/ dir,
# removed whole.
_SCRATCH = (
    "norm_*.mp4", "silent.mp4", "endcard.mp4", "concat.txt",
    "ambient_norm.mp4", "ambient_unit.mp4", "ambient_head.mp4", "ambient_tail.mp4",
    "ambient_single.mp4", "ambient_concat.txt",
    # Audio loop units: .wav now (gapless looping); .m4a from older renders.
    "ambient_music_unit.*", "ambient_sound_unit.*",
    # Multi-clip ("variety") renders: per-clip normalized/unit clips, and the
    # tiled parts / dissolves (named by offset and length, so they'd pile up).
    "ambient_norm_*.mp4", "ambient_unit_*.mp4", "ambient_part_*.mp4", "ambient_trans_*.mp4",
)


SCRATCH_PREFIX = "tmp_"


def scratch_dir(project_dir: Path, render_id: int) -> Path:
    """A compose's own scratch dir (`tmp_<render id>/` in the project's
    render dir), so two composes of one project never share files."""
    d = project_dir / f"{SCRATCH_PREFIX}{render_id}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def drop_scratch(project_dir: Path, render_id: int) -> None:
    """Remove a compose's scratch dir (after a failed or cancelled compose)."""
    shutil.rmtree(project_dir / f"{SCRATCH_PREFIX}{render_id}", ignore_errors=True)


def finalize(db: Session, render: models.Render, final_path: Path, thumb_path: Optional[Path]) -> None:
    """Version the outputs for `render`, mirror to S3, clean scratch.
    Sets render.final_video_path / thumbnail_path. Call prune() after the
    render is marked completed so it counts toward the kept set."""
    out_dir = final_path.parent
    # A storyboard compose works in its own scratch dir (scratch_dir()):
    # the outputs go to the project's render dir and the scratch dir goes.
    scratch = out_dir if out_dir.name.startswith(SCRATCH_PREFIX) else None
    if scratch is not None:
        out_dir = scratch.parent
    video = out_dir / f"render_{render.id}.mp4"
    shutil.move(str(final_path), video)
    render.final_video_path = str(video)
    if thumb_path is not None and thumb_path.exists():
        thumb = out_dir / f"render_{render.id}.jpg"
        shutil.move(str(thumb_path), thumb)
        render.thumbnail_path = str(thumb)
    if scratch is not None:
        ass = scratch / "captions.ass"
        if ass.exists():  # tiny, handy for checking caption timing
            shutil.move(str(ass), out_dir / f"captions_{render.id}.ass")
        shutil.rmtree(scratch, ignore_errors=True)
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
        # Hook-lab openings neither count toward nor expire under the
        # limit: making openings must never expire the version they are of.
        .filter(models.Render.project_id == project_id, models.Render.status == "completed",
                models.Render.hook_variant_id.is_(None))
        .order_by(models.Render.created_at.desc(), models.Render.id.desc())
        .all()
    )
    busy = {
        rid for (rid,) in db.query(models.PublishJob.render_id)
        .filter(models.PublishJob.status.in_(("queued", "uploading"))).all()
    }
    # A version with live openings is the base they are compared against.
    busy |= {rid for (rid,) in db.query(models.HookVariant.base_render_id)
             .filter(models.HookVariant.project_id == project_id, models.HookVariant.status != "archived")}
    expired = 0
    for r in rows[keep:]:
        if r.id in busy:
            continue  # an upload is reading this file; prune it next time
        for attr in ("final_video_path", "thumbnail_path"):
            path = getattr(r, attr)
            if not path:
                continue
            p = Path(path)
            s3.delete_from_s3(p)
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        if r.final_video_path:
            # Its own copy of the music bed (pipeline.keep_music).
            for p in Path(r.final_video_path).parent.glob(f"render_{r.id}_music.*"):
                p.unlink(missing_ok=True)
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

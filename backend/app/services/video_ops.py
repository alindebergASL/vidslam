"""Studio's free video operations, made here by ffmpeg: save a frame of
a video as a Studio picture ("Save last frame as picture", "Save first
frame", "Save this frame"), and join finished clips into one ("Join
clips", at the end of this module).

The source is either a finished Studio clip (source_job_id) or a video the
owner uploads for this (not kept: only the frame is stored). Each grab is a
new Studio image job with operation "extract_frame", the time asked for in
options_json ({"at": "first" | "last" | seconds, "seconds": the time used})
and a local model id: no provider is called, nothing is billed, and the
monthly budget cap doesn't apply (the rate limit does: it uses this
machine's CPU). ffmpeg is in the API image, so the API grabs the frame
itself and the job is finished before it answers; without ffmpeg there, a
clip's frame is queued for the worker instead (as the free upscale is).

Which checks apply:

- A frame of a Studio clip: no new check. The clip was made from a prompt
  that already passed the minors checks (and, on a clean deployment, with
  the providers' content filters on). The frame is a new Studio picture,
  never a Cast character's own picture: it never skips the content filter
  ("adult content allowed" covers only pictures on the card;
  pipeline.studio_cast_source follows it back to the clip, which isn't one).
- A frame of an uploaded video comes from outside the app, so before it is
  stored the picture model is asked whether anyone in it is, or looks,
  under 18 (minor_check.check_pictures, the same check as Cast pictures).
  A yes is refused; so is no answer (the check can't run, the model won't
  judge it, the frame can't be read): it fails closed. On a clean
  deployment (ADULT_FEATURES=false) the picture model is also asked
  whether the frame shows nudity or sexual content
  (explicit_check.check_picture), failing closed the same way. The stored
  frame belongs to no Cast card, so it never skips the content filter
  either.
"""
from __future__ import annotations

import hashlib
import math
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO, Optional, Union

from sqlalchemy.orm import Session

from .. import models
from .image_ops import STUDIO_DELETED, OpError

OP = "extract_frame"
# The model id a frame job records: made here, by ffmpeg.
LOCAL_MODEL = "local/frame-grab"
PROVIDER = "local"
MODEL_NAME = "Frame grab (free)"

At = Union[str, float]

# Video files an upload may be (by extension or declared type); ffmpeg
# then decides whether it really is one.
VIDEO_EXTENSIONS = (".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".3gp")

MINOR_REFUSED = ("This frame was refused: the safety check thinks it shows someone under 18. Frames from "
                 "uploaded videos can't show anyone under 18.")
CHECK_UNAVAILABLE = ("The safety check couldn't look at this frame, so it wasn't saved. Try again later "
                     "(OpenRouter must be on and have a key in Settings → API keys).")
CHECK_DECLINED = ("This frame was refused: the safety check model declined to judge it, which counts as a no.")
NOT_A_VIDEO = "That file isn't a video this app can read (MP4, MOV or WebM)."
NO_FFMPEG = "This server can't read videos right now (ffmpeg is missing)."


# ---------- the time asked for ----------


def parse_at(value: object) -> At:
    """"first", "last" or a number of seconds (0 or more); OpError 422
    otherwise."""
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("first", "last"):
            return text
        try:
            value = float(text)
        except ValueError:
            raise OpError(422, 'Pick "first", "last" or a time in seconds') from None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OpError(422, 'Pick "first", "last" or a time in seconds')
    t = float(value)
    if not math.isfinite(t) or t < 0:
        raise OpError(422, "The time must be 0 seconds or more")
    return round(t, 3)


def seconds_for(at: At) -> float:
    """The time to ask ffmpeg for ("last" is past the end: it is kept to
    the last frame)."""
    if at == "first":
        return 0.0
    if at == "last":
        return math.inf
    return float(at)


def clock(t: float) -> str:
    """3.2 → "0:03.2"."""
    t = max(float(t), 0.0)
    minutes, secs = divmod(round(t, 1), 60)
    return f"{int(minutes)}:{secs:04.1f}"


def label_for(where: str, t: float) -> str:
    return f"Frame from {where} ({clock(t)})"[:80]


# ---------- grabbing ----------


def ffmpeg_here() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def grab(video: Path, at: At) -> tuple[bytes, float]:
    """(PNG bytes, the time used) of the frame of `video` at `at`. Raises
    RuntimeError when it can't be read."""
    from .renderer import extract_frame_at

    t = seconds_for(at)
    with tempfile.TemporaryDirectory(prefix="frame-") as tmp:
        out = Path(tmp) / "frame.png"
        used = extract_frame_at(video, out, 1e9 if math.isinf(t) else t)
        return out.read_bytes(), used


# ---------- a finished Studio clip ----------


def source_clip(db: Session, job_id: int) -> models.AssetGenerationJob:
    """A finished Studio clip whose file is still there."""
    src = db.get(models.AssetGenerationJob, job_id)
    if src is None or src.status == STUDIO_DELETED:
        raise OpError(404, "That clip was deleted")
    if src.output_kind != "video_clip":
        raise OpError(422, "Only a clip has frames to save")
    if src.status not in ("completed", "saved"):
        raise OpError(409, "That clip isn't finished yet")
    if not src.result_path or not Path(src.result_path).exists():
        raise OpError(409, "That clip's file is missing")
    return src


def new_clip_job(db: Session, src: models.AssetGenerationJob, at: At, *,
                 seconds: Optional[float] = None) -> models.AssetGenerationJob:
    """The unsaved job for a frame of a Studio clip (owned by the clip's
    owner). `seconds`: the time used, when the frame is already grabbed."""
    t = seconds if seconds is not None else (0.0 if at == "first" else
                                             float(src.result_duration_seconds or src.duration_seconds or 0)
                                             if at == "last" else float(at))
    label = label_for(f"clip #{src.id}", t)
    options = {"at": at} if seconds is None else {"at": at, "seconds": seconds}
    return models.AssetGenerationJob(
        owner_kind=src.owner_kind, owner_id=src.owner_id, output_kind="image", prompt=label,
        negative_prompt="", reference_asset_ids_json=[], provider=PROVIDER, provider_model=LOCAL_MODEL,
        label=label, operation=OP, source_job_id=src.id, options_json=options, status="pending",
    )


def at_of(job: models.AssetGenerationJob) -> At:
    try:
        return parse_at((job.options_json or {}).get("at", "last"))
    except OpError:
        return "last"


def grab_for_job(db: Session, job: models.AssetGenerationJob) -> tuple[bytes, float]:
    """The worker's side (a queued frame job): grab the frame again from the
    job's clip. ValueError when the clip was deleted or the frame came from
    an uploaded video (not kept)."""
    if not job.source_job_id:
        raise ValueError("The uploaded video isn't kept; upload it again to save another frame")
    try:
        src = source_clip(db, job.source_job_id)
    except OpError as e:
        raise ValueError(e.message) from None
    return grab(Path(src.result_path), at_of(job))


def finish(db: Session, job: models.AssetGenerationJob, data: bytes, seconds: float) -> models.AssetGenerationJob:
    """Record the time used, then store the picture and complete the job
    (pipeline._finish_studio_image: its real size, a "completed" event)."""
    from .pipeline import _finish_studio_image

    opts = dict(job.options_json or {})
    opts["seconds"] = seconds
    job.options_json = opts
    db.commit()
    return _finish_studio_image(db, job, data, sized=True)


# ---------- an uploaded video ----------


def looks_like_video(content_type: str, filename: str) -> bool:
    ct = (content_type or "").split(";")[0].strip().lower()
    return ct.startswith("video/") or Path(filename or "").suffix.lower() in VIDEO_EXTENSIONS


def sniffed_extension(path: Path) -> str:
    """A fixed extension for a saved upload, from its first bytes (never the
    name the uploader gave it): ".mp4" for the MP4/MOV/3GP family, ".mkv" for
    Matroska/WebM, ".avi" for AVI, "" otherwise (ffmpeg reads the content
    either way; the extension is only a hint)."""
    with open(path, "rb") as f:
        head = f.read(12)
    if head[4:8] == b"ftyp":
        return ".mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return ".mkv"
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return ".avi"
    return ""


def save_upload(file: BinaryIO, dst: Path, limit: int) -> None:
    """Copy an upload to `dst` in pieces, refusing (413) past `limit` bytes."""
    n = 0
    with open(dst, "wb") as out:
        while True:
            chunk = file.read(1024 * 1024)
            if not chunk:
                break
            n += len(chunk)
            if n > limit:
                raise OpError(413, f"That video is too large (the most is {limit // (1024 * 1024)} MB)")
            out.write(chunk)
    if n == 0:
        raise OpError(422, "That file is empty")


def check_uploaded_frame(path: Path, data: bytes, label: str) -> None:
    """The checks a frame from an uploaded video must pass before it is
    stored (see the module notes). OpError (422 refused, 503 the check
    couldn't run) otherwise."""
    from . import explicit_check, minor_check

    digest = hashlib.sha256(data).hexdigest()
    picture = SimpleNamespace(id=0, checksum=digest, file_path=str(path), original_filename=label)
    seen = minor_check.check_pictures("an uploaded video", [picture])
    if seen["verdict"] == "minor":
        raise OpError(422, MINOR_REFUSED)
    if seen["verdict"] != "ok":
        raise OpError(503, CHECK_UNAVAILABLE)
    if seen["declined"]:
        raise OpError(422, CHECK_DECLINED)
    if 0 not in seen["checked"]:
        # Not judged (e.g. the frame couldn't be read): never a yes.
        raise OpError(503, CHECK_UNAVAILABLE)
    try:
        explicit_check.check_picture(str(path), digest)
    except explicit_check.ExplicitContentError as e:
        status = 503 if str(e) == explicit_check.PICTURE_UNAVAILABLE else 422
        raise OpError(status, str(e)) from None


def frame_from_upload(db: Session, *, owner_kind: str, owner_id: int, file: BinaryIO, filename: str,
                      at: At, limit: int) -> models.AssetGenerationJob:
    """Grab a frame from an uploaded video, check it, and store it as a
    finished Studio picture owned by `owner`. The video itself is deleted."""
    if not ffmpeg_here():
        raise OpError(503, NO_FFMPEG)
    name = Path(filename or "video").name[:60] or "video"
    with tempfile.TemporaryDirectory(prefix="upload-") as tmp:
        raw = Path(tmp) / "upload"
        save_upload(file, raw, limit)
        # The client's own suffix is never used for the file on disk.
        video = raw.rename(raw.with_name("upload" + sniffed_extension(raw)))
        try:
            data, used = grab(video, at)
        except RuntimeError:
            raise OpError(422, NOT_A_VIDEO) from None
        label = label_for(name, used)
        frame = Path(tmp) / "frame.png"
        frame.write_bytes(data)
        check_uploaded_frame(frame, data, label)
    job = models.AssetGenerationJob(
        owner_kind=owner_kind, owner_id=owner_id, output_kind="image", prompt=label, negative_prompt="",
        reference_asset_ids_json=[], provider=PROVIDER, provider_model=LOCAL_MODEL, label=label,
        operation=OP, options_json={"at": at, "upload": name}, status="submitted",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return finish(db, job, data, used)


# ---------- join finished Studio clips into one ("Join clips") ----------
#
# 2 to 12 finished Studio clips, in the order picked (one may repeat), are
# played back to back as one new Studio clip (renderer.join_clips_with_audio:
# a cut or a short dissolve, each clip's own sound, silence for a clip
# without any). Free: made here by ffmpeg, no provider, nothing billed, no
# budget cap (the rate limit applies). It can take a while for long clips,
# so it is queued like the other Studio operations (the worker runs it).
#
# Checks: none new. Every source is a Studio clip that was made from a
# prompt that passed the minors checks (and, on a clean deployment, with
# the providers' content filters on); the joined clip only puts them one
# after another. It is a new Studio clip like any other: it carries no
# Cast exemption (pipeline.studio_cast_source gives (None, None) for it, so
# anything made from it keeps the content filter), whatever its sources.

JOIN_OP = "join_clips"
JOIN_MODEL = "local/join"
JOIN_MODEL_NAME = "Join clips (free)"
JOIN_MIN, JOIN_MAX = 2, 12
JOIN_MAX_SECONDS = 600.0  # 10 minutes in all
JOIN_TRANSITIONS = ("cut", "dissolve")
JOIN_DEFAULT_CROSSFADE = 0.5
JOIN_MAX_CROSSFADE = 1.5
# Joins are free but each is a full re-encode on the shared worker queue:
# at most this many may be queued or running at once (deployment-wide).
JOIN_MAX_RUNNING = 2


def join_label(n: int) -> str:
    return f"Joined {n} clips"


def _clip_length(job: models.AssetGenerationJob) -> float:
    return float(job.result_duration_seconds or job.duration_seconds or 0.0)


def join_source(db: Session, job_id: int) -> models.AssetGenerationJob:
    """One clip to join: a finished Studio clip whose file is still there.
    OpError 404 (unknown or deleted), 409 (a picture, not finished, file
    missing)."""
    src = db.get(models.AssetGenerationJob, job_id)
    if src is None or src.status == STUDIO_DELETED:
        raise OpError(404, f"Clip #{job_id} was deleted")
    if src.output_kind != "video_clip":
        raise OpError(409, f"#{job_id} is a picture, not a clip")
    if src.status not in ("completed", "saved"):
        raise OpError(409, f"Clip #{job_id} isn't finished yet")
    if not src.result_path or not Path(src.result_path).exists():
        raise OpError(409, f"Clip #{job_id}'s file is missing")
    return src


def join_sources(db: Session, job_ids: list[int]) -> list[models.AssetGenerationJob]:
    """The clips to join, in order (a clip may be picked more than once).
    OpError 422 for too few, too many or too long in all."""
    ids = [int(i) for i in job_ids]
    if len(ids) < JOIN_MIN:
        raise OpError(422, f"Pick at least {JOIN_MIN} clips to join")
    if len(ids) > JOIN_MAX:
        raise OpError(422, f"Join at most {JOIN_MAX} clips at a time")
    found: dict[int, models.AssetGenerationJob] = {}
    for i in ids:
        if i not in found:
            found[i] = join_source(db, i)
    sources = [found[i] for i in ids]
    total = sum(_clip_length(s) for s in sources)
    if total > JOIN_MAX_SECONDS:
        raise OpError(422, f"Those clips are {clock(total)} long in all; the most is "
                           f"{int(JOIN_MAX_SECONDS // 60)} minutes")
    return sources


def join_busy(db: Session) -> bool:
    """Whether JOIN_MAX_RUNNING joins are already queued or running."""
    from .pipeline import STUDIO_RUNNING

    Job = models.AssetGenerationJob
    running = db.query(Job).filter(Job.operation == JOIN_OP, Job.status.in_(STUDIO_RUNNING)).count()
    return running >= JOIN_MAX_RUNNING


def parse_crossfade(transition: str, crossfade: Optional[float]) -> float:
    """The dissolve asked for (seconds, 0 to 1.5); 0 for a cut."""
    if transition not in JOIN_TRANSITIONS:
        raise OpError(422, 'Pick "cut" or "dissolve"')
    if transition == "cut":
        return 0.0
    cf = JOIN_DEFAULT_CROSSFADE if crossfade is None else float(crossfade)
    if not math.isfinite(cf) or cf < 0 or cf > JOIN_MAX_CROSSFADE:
        raise OpError(422, f"The dissolve must be 0 to {JOIN_MAX_CROSSFADE} seconds")
    return round(cf, 3)


def new_join_job(sources: list[models.AssetGenerationJob], transition: str,
                 crossfade: float) -> models.AssetGenerationJob:
    """The unsaved (pending) job joining `sources`. Owner: the first clip's
    owner (the clip the result looks like: its size, its character); clips
    of other owners may follow it. The clips' ids, in order, are kept in
    options_json["job_ids"] so Retry can join them again."""
    first = sources[0]
    label = join_label(len(sources))
    from .renderer import join_crossfade

    lengths = [_clip_length(s) for s in sources]
    cf = join_crossfade(lengths, transition, crossfade)
    estimate = max(sum(lengths) - (len(lengths) - 1) * cf, 0.0)
    return models.AssetGenerationJob(
        owner_kind=first.owner_kind, owner_id=first.owner_id, output_kind="video_clip",
        duration_seconds=round(estimate, 3), aspect_ratio=first.aspect_ratio or "",
        prompt=label, negative_prompt="", reference_asset_ids_json=[], provider=PROVIDER,
        provider_model=JOIN_MODEL, label=label, operation=JOIN_OP, source_job_id=None,
        options_json={"job_ids": [s.id for s in sources], "transition": transition, "crossfade": crossfade},
        status="pending",
    )


def join_options(job: models.AssetGenerationJob) -> tuple[list[int], str, float]:
    """(clip ids, transition, crossfade) a join job recorded."""
    opts = job.options_json or {}
    ids = [int(i) for i in opts.get("job_ids") or [] if str(i).isdigit()]
    transition = opts.get("transition") if opts.get("transition") in JOIN_TRANSITIONS else "cut"
    try:
        cf = float(opts.get("crossfade") or 0.0)
    except (TypeError, ValueError):
        cf = 0.0
    return ids, transition, min(max(cf, 0.0), JOIN_MAX_CROSSFADE)


def run_join(db: Session, job: models.AssetGenerationJob) -> models.AssetGenerationJob:
    """The worker's side: join the job's clips and complete it with the new
    clip (its real size and length, a "completed" event). ValueError when a
    clip is gone or isn't usable any more; RuntimeError when ffmpeg fails.
    Deleted or cancelled meanwhile: the result is dropped."""
    from sqlalchemy import update as _update

    from . import events, storage
    from .pipeline import STUDIO_RUNNING
    from .renderer import _probe_size, join_clips_with_audio

    ids, transition, crossfade = join_options(job)
    try:
        sources = join_sources(db, ids)
    except OpError as e:
        raise ValueError(e.message) from None
    with tempfile.TemporaryDirectory(prefix="join-") as tmp:
        out = Path(tmp) / "joined.mp4"
        _, seconds, has_audio = join_clips_with_audio(
            [Path(s.result_path) for s in sources], out, transition=transition, crossfade=crossfade)
        size = _probe_size(out)
        path = storage.save_generated_file(out, extension="mp4", subdir_name="clips")
    opts = dict(job.options_json or {})
    opts["has_audio"] = has_audio
    Job = models.AssetGenerationJob
    done = db.execute(_update(Job).where(Job.id == job.id, Job.status.in_(STUDIO_RUNNING)).values(
        result_path=str(path), status="completed", error="", options_json=opts,
        result_width=size[0] if size else None, result_height=size[1] if size else None,
        result_duration_seconds=round(seconds, 3) if seconds > 0 else None,
    )).rowcount
    db.commit()
    if not done:
        Path(path).unlink(missing_ok=True)
        db.refresh(job)
        return job
    db.refresh(job)
    events.studio_event(db, job, "completed")
    db.commit()
    return job

"""Studio's free video operations, made here by ffmpeg: save a frame of
a video as a Studio picture ("Save last frame as picture", "Save first
frame", "Save this frame"), join finished clips into one ("Join clips"),
keep an uploaded video as a Studio clip ("Upload a video"), and the
frame and join steps of "Continue this clip" (the last sections).

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

- A frame of a Studio clip: no new check (except a clip with pictures of
  an uploaded video, checked as below: checked_grab). The clip was made from a prompt
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
- A clip with pictures of an uploaded video used whole in a project (a
  shot's take, "Use in project"): frames across the stretch used, about
  one a second, get the same checks first (check_clip_used_whole). Every
  other place a Studio result goes (a scene's picture, a Cast picture,
  "Animate this", remove background, upscale) takes pictures only, and a
  picture of such a clip was checked as it was saved (checked_grab).
"""
from __future__ import annotations

import hashlib
import math
import os
import resource
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime, timedelta
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
VIDEO_MINOR_REFUSED = ("This video was refused: the safety check thinks it shows someone under 18. Uploaded "
                       "videos can't show anyone under 18.")
VIDEO_CHECK_UNAVAILABLE = ("The safety check couldn't look at this video, so it wasn't saved. Try again later "
                           "(OpenRouter must be on and have a key in Settings → API keys).")
VIDEO_CHECK_DECLINED = ("This video was refused: the safety check model declined to judge it, which counts as "
                        "a no.")
NOT_A_VIDEO = "That file isn't a video this app can read (MP4, MOV, WebM, MKV or AVI)."
# A file that reads as a video but couldn't be prepared (its decoding
# failed or was cut off): not called "not a video".
UPLOAD_NOT_PREPARED = "That video couldn't be prepared here; try exporting it again as an MP4."
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


def grab(video: Path, at: At, *, timeout: Optional[float] = None) -> tuple[bytes, float]:
    """(PNG bytes, the time used) of the frame of `video` at `at`. Raises
    RuntimeError when it can't be read; with `timeout` (each ffmpeg run),
    TimeoutError when it takes longer."""
    from .renderer import extract_frame_at

    t = seconds_for(at)
    with tempfile.TemporaryDirectory(prefix="frame-") as tmp:
        out = Path(tmp) / "frame.png"
        used = extract_frame_at(video, out, 1e9 if math.isinf(t) else t, timeout=timeout)
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


def has_upload(db: Session, job: Optional[models.AssetGenerationJob], *, depth: int = 0) -> bool:
    """Whether any picture of the clip `job` comes from an uploaded video:
    it is one ("upload_video"), or a join with one among its clips (followed
    back). Unknown (a clip that can't be found, too deep): yes."""
    if job is None or depth > 8:
        return True
    if job.operation == UPLOAD_OP:
        return True
    if job.operation == JOIN_OP:
        ids, _, _ = join_options(job)
        return any(has_upload(db, db.get(models.AssetGenerationJob, i), depth=depth + 1)
                   for i in dict.fromkeys(ids))
    return False


def checked_grab(db: Session, src: models.AssetGenerationJob, at: At) -> tuple[bytes, float]:
    """grab() of a Studio clip's frame, with the checks a frame of an
    uploaded video gets when the clip has any picture of one (has_upload):
    only some of its frames were checked when it was uploaded, so the very
    frame saved is checked too. RuntimeError (unreadable), OpError (the
    check refused it or couldn't run)."""
    data, used = grab(Path(src.result_path), at)
    if has_upload(db, src):
        with tempfile.TemporaryDirectory(prefix="frame-check-") as tmp:
            frame = Path(tmp) / "frame.png"
            frame.write_bytes(data)
            check_uploaded_frame(frame, data, label_for(f"clip #{src.id}", used))
    return data, used


# A clip used whole elsewhere (a project shot's take, copied into the
# project and so into renders and uploads): when it has pictures of an
# uploaded video, only a few of which were checked when it was uploaded,
# frames across the stretch used (about one a second, at most
# CLIP_CHECK_MAX_FRAMES, all judged in one call) get the checks a frame of
# an uploaded video gets, before anything of it is used. Frames judged
# before are remembered by the checks, so a clip used again is cheap.
CLIP_CHECK_EVERY = 1.0  # seconds
CLIP_CHECK_MAX_FRAMES = 40  # minor_check.PICTURE_LIMIT: never more than one call judges
CLIP_UNREADABLE = "This clip can't be read, so it can't be used here."


def clip_check_times(seconds: float) -> list[float]:
    """The times of the frames checked over the first `seconds` of a clip:
    the first, the end, and about one every CLIP_CHECK_EVERY seconds
    between (2 to CLIP_CHECK_MAX_FRAMES in all)."""
    seconds = max(float(seconds), 0.0)
    n = min(CLIP_CHECK_MAX_FRAMES, max(2, math.ceil(seconds / CLIP_CHECK_EVERY) + 1))
    return [round(seconds * i / (n - 1), 3) for i in range(n)]


def check_clip_used_whole(db: Session, src: models.AssetGenerationJob, seconds: float) -> None:
    """Before the first `seconds` of the Studio clip `src` are used whole
    (see above): nothing when it has no picture of an uploaded video
    (has_upload; unknown counts as having one), else frames across that
    stretch go through check_uploaded_frames. Fails closed: OpError (422
    refused or unreadable, 503 the check couldn't run)."""
    if not has_upload(db, src):
        return
    path = Path(src.result_path or "")
    with tempfile.TemporaryDirectory(prefix="clip-check-") as tmp:
        frames: list[tuple[Path, bytes]] = []
        for i, t in enumerate(clip_check_times(seconds)):
            try:
                data, _used = grab(path, t, timeout=UPLOAD_STEP_TIMEOUT)
            except (RuntimeError, TimeoutError):
                raise OpError(422, CLIP_UNREADABLE) from None
            frame = Path(tmp) / f"frame{i}.png"
            frame.write_bytes(data)
            frames.append((frame, data))
        check_uploaded_frames(frames, f"clip #{src.id}", video=True)


def grab_for_job(db: Session, job: models.AssetGenerationJob) -> tuple[bytes, float]:
    """The worker's side (a queued frame job): grab the frame again from the
    job's clip (checked as checked_grab does). ValueError when the clip was
    deleted, the frame came from an uploaded video (not kept) or the check
    refused it."""
    if not job.source_job_id:
        raise ValueError("The uploaded video isn't kept; upload it again to save another frame")
    try:
        src = source_clip(db, job.source_job_id)
        return checked_grab(db, src, at_of(job))
    except OpError as e:
        raise ValueError(e.message) from None


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


# The top-level atoms an MP4/MOV/3GP file may start with: "ftyp" in MP4 and
# most MOV files, but older or camera-made QuickTime files often start with
# their movie header, media data, or a filler/preview atom instead.
QUICKTIME_FIRST_ATOMS = (b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip", b"pnot")


def sniffed_extension(path: Path) -> str:
    """A fixed extension for a saved upload, from its first bytes (never the
    name the uploader gave it): ".mp4" for the MP4/MOV/3GP family (any of
    QUICKTIME_FIRST_ATOMS at offset 4), ".mkv" for Matroska/WebM, ".avi" for
    AVI, "" otherwise (ffmpeg reads the content either way; the extension
    is only a hint)."""
    with open(path, "rb") as f:
        head = f.read(12)
    if head[4:8] in QUICKTIME_FIRST_ATOMS:
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
    check_uploaded_frames([(path, data)], label)


def check_uploaded_frames(frames: list[tuple[Path, bytes]], label: str, *, video: bool = False) -> None:
    """The checks every one of `frames` (a frame's file and its bytes) of an
    uploaded video must pass before anything of it is stored: the picture
    minors check on all of them, then, on a clean deployment, the explicit
    picture check on each. Any frame failing refuses the lot; no answer is
    a no. `video`: the messages speak of the video (a whole uploaded clip),
    not of one frame. OpError (422 refused, 503 the check couldn't run)."""
    from . import explicit_check, minor_check

    refused, unavailable, declined = ((VIDEO_MINOR_REFUSED, VIDEO_CHECK_UNAVAILABLE, VIDEO_CHECK_DECLINED)
                                      if video else (MINOR_REFUSED, CHECK_UNAVAILABLE, CHECK_DECLINED))
    if not frames:
        raise OpError(503, unavailable)
    digests = [hashlib.sha256(data).hexdigest() for _path, data in frames]
    pictures = [SimpleNamespace(id=i, checksum=digests[i], file_path=str(path),
                                original_filename=label if len(frames) == 1 else f"{label} (frame {i + 1})")
                for i, (path, _data) in enumerate(frames)]
    seen = minor_check.check_pictures("an uploaded video", pictures)
    if seen["verdict"] == "minor":
        raise OpError(422, refused)
    if seen["verdict"] != "ok":
        raise OpError(503, unavailable)
    if seen["declined"]:
        raise OpError(422, declined)
    if any(i not in seen["checked"] for i in range(len(frames))):
        # Not judged (e.g. a frame couldn't be read): never a yes.
        raise OpError(503, unavailable)
    for (path, _data), digest in zip(frames, digests, strict=True):
        try:
            explicit_check.check_picture(str(path), digest)
        except explicit_check.ExplicitContentError as e:
            status = 503 if str(e) == explicit_check.PICTURE_UNAVAILABLE else 422
            raise OpError(status, str(e)) from None


def frame_from_upload(db: Session, *, owner_kind: str, owner_id: int, file: BinaryIO, filename: str,
                      at: At, limit: int) -> models.AssetGenerationJob:
    """Grab a frame from an uploaded video, check it, and store it as a
    finished Studio picture owned by `owner`. The video itself is deleted.
    The upload is read the one safe way (prepare_upload: refused before
    decoding when it should be, then a capped, limited re-encode), and the
    frame grabbed from that prepared copy, so a browser's live WebM (no
    stated length) works and the same limits apply as for "Upload a
    video" (429 while too many are being prepared). For "first" or a time
    t only the start of the video, up to t and a second, is prepared (and
    only a time past UPLOAD_MAX_SECONDS is refused for length); "last"
    prepares the whole of it, capped, as Upload a video does."""
    if not ffmpeg_here():
        raise OpError(503, NO_FFMPEG)
    if not _upload_slots.acquire(blocking=False):
        raise OpError(429, UPLOAD_BUSY)
    try:
        # No database connection is held while ffmpeg and the checks run.
        db.commit()
        return _frame_from_upload(db, owner_kind=owner_kind, owner_id=owner_id, file=file, filename=filename,
                                  at=at, limit=limit)
    finally:
        _upload_slots.release()


def _frame_from_upload(db: Session, *, owner_kind: str, owner_id: int, file: BinaryIO, filename: str,
                       at: At, limit: int) -> models.AssetGenerationJob:
    name = Path(filename or "video").name[:60] or "video"
    with tempfile.TemporaryDirectory(prefix="upload-") as tmp:
        raw = Path(tmp) / "upload"
        save_upload(file, raw, limit)
        step = _upload_clock()
        prepared = Path(tmp) / "prepared.mp4"
        until = None if at == "last" else seconds_for(at)
        if until is not None and until > UPLOAD_MAX_SECONDS:
            raise OpError(422, f"Pick a time in the first {int(UPLOAD_MAX_SECONDS // 60)} minutes; videos are "
                               f"read up to {int(UPLOAD_MAX_SECONDS // 60)} minutes")
        prepare_upload(raw, prepared, step, until=until)
        try:
            data, used = grab(prepared, at, timeout=step(UPLOAD_STEP_TIMEOUT))
        except TimeoutError:
            raise OpError(422, UPLOAD_TOO_SLOW) from None
        except RuntimeError:
            raise OpError(422, NOT_A_VIDEO) from None
        label = label_for(name, used)
        frame = Path(tmp) / "frame.png"
        frame.write_bytes(data)
        check_uploaded_frame(frame, data, label)
    if not _owner_exists(db, owner_kind, owner_id):
        raise OpError(404, "owner not found")  # deleted while the video was prepared
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


def join_stale_after() -> timedelta:
    """How long a join may run once a worker has claimed it ('submitted'):
    its ffmpeg time limit for the longest join, and a margin. Older, it
    was orphaned (a killed worker): it no longer holds one of the
    JOIN_MAX_RUNNING places, and the sweeper fails it
    (sweepers.sweep_studio). A join still waiting in the queue ('pending')
    may wait longer, behind long renders on the one worker: it keeps its
    place, and only the general pending rule (its queue entry gone) fails
    it."""
    from .renderer import join_timeout

    return timedelta(seconds=join_timeout(JOIN_MAX_SECONDS)) + timedelta(minutes=5)


def joins_running(db: Session, *, now: Optional[datetime] = None) -> int:
    """Joins holding a place: every one still queued ('pending'), and those
    a worker is running that were touched within join_stale_after()."""
    from sqlalchemy import and_, func, or_

    from .pipeline import STUDIO_RUNNING

    Job = models.AssetGenerationJob
    since = (now or datetime.utcnow()) - join_stale_after()
    return (db.query(Job)
            .filter(Job.operation == JOIN_OP, Job.status.in_(STUDIO_RUNNING),
                    or_(Job.status == "pending",
                        and_(Job.status != "pending",
                             func.coalesce(Job.updated_at, Job.created_at) >= since)))
            .count())


def join_busy(db: Session) -> bool:
    """Whether JOIN_MAX_RUNNING joins are already queued or running."""
    return joins_running(db) >= JOIN_MAX_RUNNING


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
    from .renderer import JoinStopped, _probe_size, join_clips_with_audio

    ids, transition, crossfade = join_options(job)
    try:
        sources = join_sources(db, ids)
    except OpError as e:
        raise ValueError(e.message) from None
    def stopped() -> bool:
        # Cancelled or deleted while FFmpeg runs: stop it, so a cancelled
        # join doesn't go on re-encoding after giving up its place.
        from ..db import SessionLocal

        with SessionLocal() as check:
            status = check.query(models.AssetGenerationJob.status).filter(
                models.AssetGenerationJob.id == job.id).scalar()
        return status not in STUDIO_RUNNING

    with tempfile.TemporaryDirectory(prefix="join-") as tmp:
        out = Path(tmp) / "joined.mp4"
        try:
            _, seconds, has_audio = join_clips_with_audio(
                [Path(s.result_path) for s in sources], out, transition=transition, crossfade=crossfade,
                should_stop=stopped)
        except JoinStopped:
            db.refresh(job)
            return job
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


# ---------- an uploaded video kept as a Studio clip ("Upload a video") ----------
#
# A video from the owner's device, kept as a finished Studio clip (operation
# "upload_video", provider "local", free) so it can be continued, joined or
# have its frames saved like any clip. It is stored as an ordinary
# H.264/AAC MP4, always re-encoded (at most 1080p and UPLOAD_MAX_FPS), with
# the prepared copy's real size and length; one larger than 4K, or whose
# picture isn't a known video codec, is refused before it is decoded.
#
# Checks: it comes from outside the app, so before anything of it is stored
# several frames across it (the first, the last and about one every 10 s
# between, at most UPLOAD_MAX_FRAMES) go through the picture minors check
# (minor_check.check_pictures) and, on a clean deployment, the explicit
# picture check (explicit_check.check_picture), as a frame of an uploaded
# video does. Any frame failing, or a check that can't answer, refuses the
# upload. It belongs to no Cast card: pipeline.studio_cast_source gives
# (None, None) for it, so it, its frames and anything made from them keep
# the content filter. The temporary files are removed on every path. Only
# those frames were checked, so any frame saved from it later (or from a
# join it is part of) is checked again as it is saved (checked_grab), and
# the stretch of it used in a project shot is checked about once a second
# first (check_clip_used_whole).

UPLOAD_OP = "upload_video"
UPLOAD_MODEL = "local/upload"
UPLOAD_LABEL = "Uploaded video"
UPLOAD_MIN_FRAMES, UPLOAD_MAX_FRAMES = 3, 8
UPLOAD_FRAME_EVERY = 10.0  # seconds
UPLOAD_MAX_SECONDS = JOIN_MAX_SECONDS


def upload_frame_times(seconds: float) -> list[float]:
    """The times of the frames checked in an uploaded video of `seconds`:
    the first, the last (math.inf: ffmpeg keeps it to the last frame) and
    about one every UPLOAD_FRAME_EVERY seconds between, 3 to 8 in all."""
    n = min(UPLOAD_MAX_FRAMES, max(UPLOAD_MIN_FRAMES, math.ceil(max(seconds, 0.0) / UPLOAD_FRAME_EVERY) + 1))
    return [0.0] + [round(seconds * i / (n - 1), 3) for i in range(1, n - 1)] + [math.inf]


# Only these containers are read, each by its own demuxer (never ffmpeg's
# guess: an upload can't reach playlist, concat or image demuxers), from
# local files only.
UPLOAD_DEMUXERS = {".mp4": "mov", ".mkv": "matroska", ".avi": "avi"}
# The upload is prepared inside the request, so every step is bounded:
# ffprobe on the raw file, and the re-encode, which must end well inside
# the proxy's 600 s read timeout (nginx) with the picture checks after it.
UPLOAD_PROBE_TIMEOUT = 60.0
UPLOAD_ENCODE_TIMEOUT = 360.0
# Once prepared, each probe or frame grab of the copy (each ffmpeg run) has
# this long, and all the ffmpeg work of one upload (raw probe,
# re-encode, the copy's probe and grabs) must end within UPLOAD_DEADLINE of
# its start, leaving the picture checks room inside the proxy's 600 s.
UPLOAD_STEP_TIMEOUT = 20.0
UPLOAD_DEADLINE = 480.0
# Every upload is re-encoded (never copied as it is: its first picture
# size and stated frame rate may not hold for the whole stream), to at
# most 1080p (renderer.JOIN_MAX_LONG x JOIN_MAX_SHORT, either way round)
# and at most this many frames a second.
UPLOAD_MAX_FPS = 60
# Before anything is decoded, an upload whose picture is larger than this
# (either side over UPLOAD_MAX_SIDE, or more than UPLOAD_MAX_PIXELS: 4K) or
# whose picture isn't one of these video codecs is refused; the decoder is
# also told never to make a picture larger than UPLOAD_MAX_PIXELS (a stream
# may change size midway), and each ffmpeg/ffprobe run over the raw upload
# uses at most UPLOAD_THREADS threads, so one upload's memory and CPU stay
# bounded.
UPLOAD_MAX_SIDE = 4096
UPLOAD_MAX_PIXELS = 4096 * 2304
UPLOAD_CODECS = frozenset({"h264", "hevc", "vp8", "vp9", "av1", "mpeg4", "mpeg2video", "prores", "mjpeg",
                           "theora"})
UPLOAD_THREADS = 2
# Every ffmpeg/ffprobe run over the raw upload also runs with at most this
# much address space (RLIMIT_AS, set by prlimit before the command starts,
# or in the child when prlimit isn't installed), with glibc kept to
# MALLOC_ARENA_MAX malloc arenas (each thread's own arena reserves address
# space it never uses), and ffmpeg itself refuses any single allocation
# larger than UPLOAD_MAX_ALLOC (-max_alloc): a small file whose tables
# claim millions of samples can't make it take gigabytes. A run that says
# an allocation failed is refused (UPLOAD_TOO_MUCH_MEMORY).
UPLOAD_MEMORY_LIMIT = 2 * 1024 * 1024 * 1024
MALLOC_ARENA_MAX = 2
UPLOAD_MAX_ALLOC = 512 * 1024 * 1024
UPLOAD_TOO_MUCH_MEMORY = "That video needs too much memory to read"
# What is kept of a raw run's output: its error log's last RAW_LOG_TAIL
# bytes (written to a temporary file, never held whole), and at most
# RAW_OUTPUT_MAX bytes of what it prints (ffprobe's JSON).
RAW_LOG_TAIL = 64 * 1024
RAW_OUTPUT_MAX = 1024 * 1024
# The re-encode's bit rate is capped (a picture of pure noise can't make it
# huge), and its output may be at most UPLOAD_MAX_OUTPUT_BYTES: one that
# reaches it is refused (UPLOAD_TOO_LARGE).
UPLOAD_MAXRATE, UPLOAD_BUFSIZE = "8M", "16M"
UPLOAD_AUDIO_RATE = "160k"
# The most a capped re-encode can come to: 8 Mb/s of picture for the
# longest copy (UPLOAD_MAX_SECONDS and a second) is 601 MB, about 15 MB for
# the sound (160 kb/s for as long is 12 MB, plus container overhead), and
# 10% to spare (the rate cap is kept on average, not frame by frame):
# about 680 MB.
UPLOAD_MAX_OUTPUT_BYTES = int((8_000_000 * (UPLOAD_MAX_SECONDS + 1) / 8 + 15_000_000) * 1.1)
UPLOAD_TOO_LARGE = "That video is too large to prepare here; upload a shorter one"
# Sound in one of these codecs (or any PCM) is kept (re-encoded to AAC);
# any other sound is dropped, not refused: the clip is kept without it.
UPLOAD_AUDIO_CODECS = frozenset({"aac", "mp3", "opus", "vorbis", "flac", "ac3", "eac3", "alac"})
# Uploads being prepared at once in one API process (each is a full
# re-encode on the API's CPU); more are refused (429) rather than queued
# behind them on the request threads.
UPLOAD_MAX_RUNNING = 2
UPLOAD_BUSY = "Other videos are being prepared right now; try again in a minute"
UPLOAD_TOO_SLOW = "That video took too long to prepare here; upload a shorter or smaller one"
_upload_slots = threading.BoundedSemaphore(UPLOAD_MAX_RUNNING)


def _input(path: Path, demuxer: str) -> list[str]:
    """ffmpeg/ffprobe input options that read `path` with `demuxer` only."""
    return ["-protocol_whitelist", "file", "-f", demuxer, "-i", str(path)]


def _raw_input(path: Path, demuxer: str, *, decode: bool = False) -> list[str]:
    """Input options for a run over the raw upload: no single allocation
    past UPLOAD_MAX_ALLOC, `demuxer` only, at most UPLOAD_THREADS threads,
    and when it decodes (ffmpeg), no picture larger than UPLOAD_MAX_PIXELS
    (ffprobe's runs decode nothing, and with that limit ffprobe wouldn't
    even read an oversized picture's headers). Run it with _run_raw."""
    limit = ["-max_pixels", str(UPLOAD_MAX_PIXELS)] if decode else []
    return ["-max_alloc", str(UPLOAD_MAX_ALLOC), "-threads", str(UPLOAD_THREADS), *limit, *_input(path, demuxer)]


# How a run that ran out of memory shows: only a log saying an allocation
# failed (with the address-space cap in force, that is the cap reached). A
# run killed or crashed by a signal is not taken as out of memory: the
# upload is reported as unreadable, like any other failed run.
_MEMORY_WORDS = ("cannot allocate memory", "malloc of size", "resource temporarily unavailable",
                 "failed to map segment")


def _memory_limit() -> int:
    """UPLOAD_MEMORY_LIMIT, kept within this process's own hard limit."""
    _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    return UPLOAD_MEMORY_LIMIT if hard == resource.RLIM_INFINITY else min(UPLOAD_MEMORY_LIMIT, hard)


def _ran_out_of_memory(rc: int, log: str) -> bool:
    text = log.lower()
    return rc != 0 and any(word in text for word in _MEMORY_WORDS)


def _capped(cmd: list[str], limit: int) -> tuple[list[str], object]:
    """`cmd` with its address space capped at `limit`: run through prlimit
    when it is installed (no Python runs in the child between fork and
    exec), else (command, a preexec_fn that sets the limit)."""
    if shutil.which("prlimit"):
        return ["prlimit", f"--as={limit}:{limit}", "--", *cmd], None

    def cap() -> None:  # in the child, before the command starts: nothing that locks
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

    return cmd, cap


def _run_raw(cmd: list[str], timeout: float) -> tuple[int, str, str]:
    """Run one ffmpeg/ffprobe command over a raw upload: (exit code, what it
    printed (at most RAW_OUTPUT_MAX bytes), the last RAW_LOG_TAIL bytes of
    its error log). Its address space is capped at UPLOAD_MEMORY_LIMIT
    (RLIMIT_AS: _capped) and glibc keeps at most MALLOC_ARENA_MAX arenas;
    its output and log go to temporary files, so however much it prints,
    only that much is kept. Past `timeout` it is killed: (-1, "",
    "timed out ..."). OpError 422 (UPLOAD_TOO_MUCH_MEMORY) when its log
    says an allocation failed."""
    run, cap = _capped(cmd, _memory_limit())
    env = {**os.environ, "MALLOC_ARENA_MAX": str(MALLOC_ARENA_MAX)}
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(run, stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env, preexec_fn=cap)
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return -1, "", f"timed out after {timeout:.0f}s"
        out.seek(0)
        printed = out.read(RAW_OUTPUT_MAX)
        size = err.seek(0, 2)
        err.seek(max(0, size - RAW_LOG_TAIL))
        tail = err.read()
    log = tail.decode("utf-8", "replace")
    if _ran_out_of_memory(rc, log):
        raise OpError(422, UPLOAD_TOO_MUCH_MEMORY)
    return rc, printed.decode("utf-8", "replace"), log


def _rate(value: object) -> float:
    """An ffprobe frame rate ("30000/1001", "60/1", "0/0") as a number; 0.0
    when unknown."""
    try:
        num, _, den = str(value or "").partition("/")
        rate = float(num) / float(den or 1)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0
    return rate if math.isfinite(rate) and rate > 0 else 0.0


def _probe_upload(path: Path, demuxer: str) -> dict:
    """What the raw upload claims to be, read with `demuxer` only (bounded
    by UPLOAD_PROBE_TIMEOUT): {"video": codec, "pix_fmt", "audio": codec or
    "", "size": (w, h) or None, "fps": frames a second (0.0 unknown),
    "seconds": its stated length (0.0: it states none, as a browser's live
    WebM recording doesn't), "display": (w, h) as it is shown, its pixels
    made square (its sample aspect ratio applied: a 720x576 PAL 16:9 picture
    shows as 1024x576) or None}; {} when it has no readable picture. Read from
    the container's headers only: nothing is decoded (an upload is refused
    on what is read here, upload_refusal, before any decoding). The stated
    length, size and rate come from the file, which the uploader controls:
    only the prepared copy is trusted. TimeoutError past the limit; OpError
    422 when reading the headers needs too much memory (_run_raw)."""
    import json

    from .renderer import stream_size

    rc, log, err = _run_raw(["ffprobe", "-v", "error", "-nofind_stream_info", "-print_format", "json", "-show_entries",
                    "stream=codec_type,codec_name,pix_fmt,width,height,sample_aspect_ratio,duration,avg_frame_rate,"
                    "r_frame_rate:"
                    "stream_side_data=rotation:stream_tags=rotate:format=duration", *_raw_input(path, demuxer)],
                            timeout=UPLOAD_PROBE_TIMEOUT)
    if rc == -1:
        raise TimeoutError(err)
    if rc != 0:
        return {}
    try:
        info = json.loads(log)
    except ValueError:
        return {}
    out: dict = {"audio": ""}
    seconds = 0.0
    for st in info.get("streams") or []:
        if st.get("codec_type") == "video" and "video" not in out:
            out["video"], out["pix_fmt"] = st.get("codec_name") or "", st.get("pix_fmt") or ""
            out["size"] = stream_size(st)
            out["display"] = display_size(st)
            out["fps"] = _rate(st.get("avg_frame_rate")) or _rate(st.get("r_frame_rate"))
            try:
                seconds = float(st.get("duration") or 0.0)
            except (TypeError, ValueError):
                seconds = 0.0
        elif st.get("codec_type") == "audio" and not out["audio"]:
            out["audio"] = st.get("codec_name") or ""
    if "video" not in out:
        return {}
    if seconds <= 0:
        try:
            seconds = float((info.get("format") or {}).get("duration") or 0.0)
        except (TypeError, ValueError):
            seconds = 0.0
    out["seconds"] = seconds if math.isfinite(seconds) else 0.0
    return out


def _sar(value: object) -> float:
    """An ffprobe sample aspect ratio ("64:45", "1:1", "0:1", "N/A") as a
    number; 1.0 when unknown or nonsense (square pixels)."""
    num, _, den = str(value or "").partition(":")
    try:
        sar = float(num) / float(den)
    except (TypeError, ValueError, ZeroDivisionError):
        return 1.0
    return sar if math.isfinite(sar) and sar > 0 else 1.0


def display_size(st: dict) -> Optional[tuple[int, int]]:
    """(width, height) of one ffprobe video stream entry as it is shown:
    its pixels made square (its sample aspect ratio applied: wider when
    above 1, taller when below, to even numbers, as square_pixels_vf), then
    rotation applied (as renderer.stream_size); None when its size isn't
    stated. Not fitted within any limit: upload_refusal refuses one past
    UPLOAD_MAX_DISPLAY_PIXELS before anything is decoded, and the
    square-pixel step fits the rest within 4K."""
    from .renderer import stream_size

    try:
        w, h = int(st["width"]), int(st["height"])
    except (KeyError, TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    sar = _sar(st.get("sample_aspect_ratio"))
    if sar >= 1:
        w = max(2, int(w * sar / 2) * 2)
    else:
        h = max(2, int(h / sar / 2) * 2)
    return stream_size({**st, "width": w, "height": h})


def square_pixels_vf() -> str:
    """The filter that makes every frame's pixels square (each frame's own
    sample aspect ratio applied: a stream may change it midway), its shape
    kept: wider when the ratio is above 1, taller when below, then fitted
    (only ever made smaller) so neither side passes UPLOAD_MAX_SIDE and
    width x height stays within UPLOAD_MAX_PIXELS. Even sides."""
    sw = "if(gte(sar,1),iw*sar,iw)"
    sh = "if(gte(sar,1),ih,ih/sar)"
    k = (f"min(1,min({UPLOAD_MAX_SIDE}/({sw}),min({UPLOAD_MAX_SIDE}/({sh}),"
         f"sqrt({UPLOAD_MAX_PIXELS}/(({sw})*({sh}))))))")
    return f"scale=w='max(2,trunc(({sw})*{k}/2)*2)':h='max(2,trunc(({sh})*{k}/2)*2)',setsar=1"


# The size an upload's picture may be shown at (its pixels made square) is
# bounded only against absurd sample aspect ratios: at most this many times
# UPLOAD_MAX_PIXELS. The square-pixel step fits whatever it makes within 4K
# (square_pixels_vf), so a stored 4K picture that is shown wider is read.
UPLOAD_MAX_DISPLAY_PIXELS = 4 * UPLOAD_MAX_PIXELS


def upload_refusal(streams: dict) -> Optional[str]:
    """Why an upload (what `_probe_upload` read, nothing decoded yet) is
    refused before it is decoded, or None: a picture stored larger than 4K
    (UPLOAD_MAX_SIDE a side, UPLOAD_MAX_PIXELS in all: what is decoded), one
    shown (its pixels made square: "display") over UPLOAD_MAX_DISPLAY_PIXELS,
    or of a size not stated, or a picture that isn't one of UPLOAD_CODECS."""
    size, display = streams.get("size"), streams.get("display", streams.get("size"))
    for s in (size, display):
        if not s or s[0] <= 0 or s[1] <= 0:
            return NOT_A_VIDEO
    w, h = size
    if w > UPLOAD_MAX_SIDE or h > UPLOAD_MAX_SIDE or w * h > UPLOAD_MAX_PIXELS:
        return f"That video's picture is {w}x{h}; the largest read here is 4K (3840x2160 or 4096x2304)"
    dw, dh = display
    if dw * dh > UPLOAD_MAX_DISPLAY_PIXELS:
        return (f"That video's pixels have an unusual shape (it would be shown at {dw}x{dh}); "
                "export it again with square pixels")
    codec = str(streams.get("video") or "")
    if codec not in UPLOAD_CODECS:
        return (f"That file's picture ({codec or 'unknown format'}) isn't a video format read here; "
                "upload an MP4, WebM, MKV or AVI video")
    return None


def normalize_upload(src: Path, dst: Path, streams: dict, *, timeout: float = UPLOAD_ENCODE_TIMEOUT,
                     until: Optional[float] = None) -> bool:
    """Write `src` (an upload `_probe_upload` read as `streams`) to `dst` as
    an ordinary H.264/AAC MP4 (only its first picture and sound streams,
    fast start), never longer than UPLOAD_MAX_SECONDS and a second, however
    long it claims to be. Always re-encoded, never copied as it is (the
    stream may change size midway, or pack many frames into a moment,
    whatever its first picture and average rate say): every frame's
    pixels are first made square (its own sample aspect ratio applied, so
    an anamorphic 720x576 16:9 picture becomes 1024x576, never stored
    squashed; within 4K: square_pixels_vf), then it is scaled into the
    size its first picture is shown at ("display") fitted to 1080p (even
    sides, shape kept, padded if a later picture's shape differs), at a
    steady rate of at most UPLOAD_MAX_FPS, its bit rate capped
    (UPLOAD_MAXRATE). Sound is kept only in one of UPLOAD_AUDIO_CODECS
    (else dropped). `until`: only the first `until` seconds and one more
    are prepared (a frame near the start needs no more). Returns True (it
    was re-encoded). RuntimeError when ffmpeg can't; TimeoutError when it
    takes longer than `timeout` (UPLOAD_ENCODE_TIMEOUT at most); OpError
    422 when it needs too much memory or its output reaches
    UPLOAD_MAX_OUTPUT_BYTES."""
    from fractions import Fraction

    from .renderer import join_size

    timeout = min(timeout, UPLOAD_ENCODE_TIMEOUT)
    demuxer = UPLOAD_DEMUXERS[sniffed_extension(src)]
    seconds = UPLOAD_MAX_SECONDS if until is None else min(max(float(until), 0.0), UPLOAD_MAX_SECONDS)
    cap = ["-t", f"{seconds + 1:g}"]
    keep_audio = audio_kept(streams.get("audio"))
    maps = ["-map", "0:v:0"] + (["-map", "0:a:0"] if keep_audio else [])
    size = streams.get("display") or streams.get("size")
    if not size:
        raise RuntimeError("the picture's size isn't known")
    w, h = join_size(size)
    stated = float(streams.get("fps") or 0.0)
    rate = Fraction(stated).limit_denominator(1001) if 0 < stated <= UPLOAD_MAX_FPS else Fraction(UPLOAD_MAX_FPS)
    # Square pixels first, within 4K (a stream may change its sample
    # aspect ratio midway), so no frame grows past it.
    vf = ["-vf", f"fps={rate.numerator}/{rate.denominator},{square_pixels_vf()},"
                 f"scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                 f"pad={w}:{h}:-1:-1,setsar=1"]
    audio = ["-c:a", "aac", "-b:a", UPLOAD_AUDIO_RATE] if keep_audio else ["-an"]
    rc, _printed, log = _run_raw(
        ["ffmpeg", "-y", "-v", "error", *_raw_input(src, demuxer, decode=True), *cap, *maps,
         "-filter_threads", str(UPLOAD_THREADS), *vf, "-fpsmax", str(UPLOAD_MAX_FPS),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-maxrate", UPLOAD_MAXRATE,
         "-bufsize", UPLOAD_BUFSIZE, "-threads", str(UPLOAD_THREADS), "-pix_fmt", "yuv420p", *audio,
         "-fs", str(UPLOAD_MAX_OUTPUT_BYTES), "-movflags", "+faststart", str(dst)], timeout=timeout)
    if rc == -1:
        raise TimeoutError("timed out")
    if rc != 0 or not dst.exists() or dst.stat().st_size == 0:
        raise RuntimeError(f"couldn't re-encode the video: {log[-500:]}")
    if dst.stat().st_size >= UPLOAD_MAX_OUTPUT_BYTES:
        # ffmpeg stopped at -fs: what was written is cut short.
        raise OpError(422, UPLOAD_TOO_LARGE)
    return True


def audio_kept(codec: object) -> bool:
    """Whether an upload's sound in `codec` is kept: one of
    UPLOAD_AUDIO_CODECS or any PCM."""
    name = str(codec or "")
    return name in UPLOAD_AUDIO_CODECS or name.startswith("pcm_")


def _probe_prepared(path: Path, timeout: float) -> tuple[float, bool, Optional[tuple[int, int]]]:
    """(length, whether it has sound, size) of the prepared copy (an MP4
    made here), in one ffprobe run bounded by `timeout`; the length is the
    picture's own when known, else the file's (as renderer._probe_clip).
    (0.0, False, None) when it can't be read; TimeoutError past `timeout`."""
    import json

    from .renderer import _run, stream_size

    rc, log = _run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_entries",
                    "stream=codec_type,width,height,duration:stream_side_data=rotation:stream_tags=rotate:"
                    "format=duration", *_input(path, "mov")], timeout=timeout)
    if rc == -1:
        raise TimeoutError(log)
    try:
        info = json.loads(log) if rc == 0 else {}
    except ValueError:
        info = {}
    length, size, has_audio = 0.0, None, False
    for st in info.get("streams") or []:
        if st.get("codec_type") == "audio":
            has_audio = True
        elif st.get("codec_type") == "video" and size is None:
            size = stream_size(st)
            try:
                length = float(st.get("duration") or 0.0)
            except (TypeError, ValueError):
                length = 0.0
    if not (length > 0 and math.isfinite(length)):
        try:
            length = float((info.get("format") or {}).get("duration") or 0.0)
        except (TypeError, ValueError):
            length = 0.0
    return (length if math.isfinite(length) else 0.0), has_audio, size


def aspect_for(size: Optional[tuple[int, int]]) -> str:
    """The Studio aspect ("9:16", "16:9" or "1:1") nearest a picture size;
    "" when it isn't known."""
    if not size or not size[0] or not size[1]:
        return ""
    ratio = size[0] / size[1]
    return min((("9:16", 9 / 16), ("1:1", 1.0), ("16:9", 16 / 9)), key=lambda a: abs(math.log(ratio / a[1])))[0]


def _too_long(seconds: float) -> OpError:
    return OpError(422, f"That video is {clock(seconds)} long; the most is {int(UPLOAD_MAX_SECONDS // 60)} minutes")


def video_from_upload(db: Session, *, owner_kind: str, owner_id: int, file: BinaryIO, filename: str,
                      limit: int) -> models.AssetGenerationJob:
    """Keep an uploaded video as a finished Studio clip owned by `owner`,
    once frames across it passed the picture checks (see above). OpError
    for a file that isn't a readable video, too long, refused, or when too
    many uploads are being prepared (429)."""
    if not ffmpeg_here():
        raise OpError(503, NO_FFMPEG)
    if not _upload_slots.acquire(blocking=False):
        raise OpError(429, UPLOAD_BUSY)
    try:
        # No database connection is held while ffmpeg and the checks run.
        db.commit()
        return _video_from_upload(db, owner_kind=owner_kind, owner_id=owner_id, file=file, filename=filename,
                                  limit=limit)
    finally:
        _upload_slots.release()


def _upload_clock():
    """A step(limit) for one upload, its clock started now (the file is
    in): the time limit for the next ffmpeg run, `limit` or what is left of
    UPLOAD_DEADLINE when less; OpError "took too long" when nothing is."""
    import time

    started = time.monotonic()

    def step(limit: float) -> float:
        left = UPLOAD_DEADLINE - (time.monotonic() - started)
        if left < 1.0:
            raise OpError(422, UPLOAD_TOO_SLOW)
        return min(limit, left)

    return step


def prepare_upload(raw: Path, out: Path, step, *,
                   until: Optional[float] = None) -> tuple[float, bool, tuple[int, int]]:
    """The one safe way an uploaded video (`raw`, saved as it came) is read:
    its container sniffed (only UPLOAD_DEMUXERS are read), its headers
    probed and refused before anything is decoded (upload_refusal, or a
    stated length past the most), then re-encoded to `out`
    (normalize_upload: capped a second past UPLOAD_MAX_SECONDS, time and
    thread limited) and the prepared copy measured, refused when longer
    than the most. A file that states no length (a browser's live WebM
    recording) goes straight to the capped re-encode: the raw file's
    packets are never walked here. `raw` is removed once prepared. Returns
    the copy's (length, has sound, size). OpError for anything refused or
    unreadable, or past the time or memory limits (`step`, _upload_clock,
    _run_raw). `until` (a frame near the start): only the first `until`
    seconds and one more are prepared, and no length is refused (the
    caller refuses a time past the most)."""
    # The client's own suffix is never used for the file on disk; only the
    # containers sniffed here are read at all.
    ext = sniffed_extension(raw)
    if ext not in UPLOAD_DEMUXERS:
        raise OpError(422, NOT_A_VIDEO)
    video = raw.rename(raw.with_name("upload" + ext))
    try:
        streams = _probe_upload(video, UPLOAD_DEMUXERS[ext])
    except TimeoutError:
        raise OpError(422, UPLOAD_TOO_SLOW) from None
    if not streams:
        raise OpError(422, NOT_A_VIDEO)
    # Refused on what its headers say, before anything is decoded.
    refusal = upload_refusal(streams)
    if refusal:
        raise OpError(422, refusal)
    if until is None and streams["seconds"] > UPLOAD_MAX_SECONDS:
        raise _too_long(streams["seconds"])
    try:
        normalize_upload(video, out, streams, timeout=step(UPLOAD_ENCODE_TIMEOUT), until=until)
    except TimeoutError:
        raise OpError(422, UPLOAD_TOO_SLOW) from None
    except RuntimeError:
        raise OpError(422, UPLOAD_NOT_PREPARED) from None
    video.unlink(missing_ok=True)
    # The length that counts is the prepared copy's (the file's own claim,
    # or its lack of one, may be anything; the copy is cut a second past
    # the most, so a longer one is refused here).
    try:
        length, has_audio, size = _probe_prepared(out, step(UPLOAD_STEP_TIMEOUT))
    except TimeoutError:
        raise OpError(422, UPLOAD_TOO_SLOW) from None
    if length <= 0 or not size:
        raise OpError(422, NOT_A_VIDEO)
    if until is None and length > UPLOAD_MAX_SECONDS + 0.5:
        raise OpError(422, f"That video is longer than {int(UPLOAD_MAX_SECONDS // 60)} minutes; "
                           f"the most is {int(UPLOAD_MAX_SECONDS // 60)} minutes")
    return length, has_audio, size


def _video_from_upload(db: Session, *, owner_kind: str, owner_id: int, file: BinaryIO, filename: str,
                       limit: int) -> models.AssetGenerationJob:
    from . import events, storage

    name = Path(filename or "video").name[:60] or "video"
    with tempfile.TemporaryDirectory(prefix="upload-video-") as tmp:
        raw = Path(tmp) / "upload"
        save_upload(file, raw, limit)
        step = _upload_clock()  # the time limits count from here (the file is in)
        out = Path(tmp) / "clip.mp4"
        length, has_audio, size = prepare_upload(raw, out, step)
        frames: list[tuple[Path, bytes]] = []
        for i, t in enumerate(upload_frame_times(length)):
            try:
                data, _used = grab(out, "last" if math.isinf(t) else t, timeout=step(UPLOAD_STEP_TIMEOUT))
            except TimeoutError:
                raise OpError(422, UPLOAD_TOO_SLOW) from None
            except RuntimeError:
                raise OpError(422, NOT_A_VIDEO) from None
            frame = Path(tmp) / f"frame{i}.png"
            frame.write_bytes(data)
            frames.append((frame, data))
        check_uploaded_frames(frames, name, video=True)
        if not _owner_exists(db, owner_kind, owner_id):
            raise OpError(404, "owner not found")  # deleted while the video was prepared
        path = storage.save_generated_file(out, extension="mp4", subdir_name="clips")
    job = models.AssetGenerationJob(
        owner_kind=owner_kind, owner_id=owner_id, output_kind="video_clip", duration_seconds=round(length, 3),
        aspect_ratio=aspect_for(size), prompt="", negative_prompt="", reference_asset_ids_json=[],
        provider=PROVIDER, provider_model=UPLOAD_MODEL, label=UPLOAD_LABEL, operation=UPLOAD_OP,
        options_json={"upload": name, "has_audio": has_audio, "reencoded": True},
        result_path=str(path), result_width=size[0], result_height=size[1],
        result_duration_seconds=round(length, 3), status="completed",
    )
    try:
        db.add(job)
        db.commit()
    except Exception:
        db.rollback()
        Path(path).unlink(missing_ok=True)
        raise
    db.refresh(job)
    events.studio_event(db, job, "completed")
    db.commit()
    return job


# ---------- continue a clip ("Continue this clip") ----------
#
# A finished Studio clip's last frame is saved as a Studio picture (an
# "extract_frame" job, source_job_id the clip) and a new image-to-video
# clip starts from it (first_frame_job_id), from the prompt the owner
# writes: a paid generation like any clip, through every check a clip's
# prompt goes through (api/studio.py). The intent is kept on the new clip:
# options_json {"continues_job_id": the clip, "auto_join": bool}. With
# auto_join, when that clip finishes (pipeline.on_studio_clip_ready, the
# one place a Studio clip completes, from a webhook or a poll alike) one
# join [original, continuation] with a cut is made, once: in the same
# transaction as the conditional "polling → completed" write only one
# completion wins, and the join's id ("join_job_id") or why there is none
# ("join_note", shown in Studio) is stored with it. Auto-joins count
# toward JOIN_MAX_RUNNING like any join, but are queued even when the cap
# is full (one join per paid, rate-limited clip): a manual join waits.
#
# Checks: the frame is a frame of a Studio clip (no new check, as above,
# unless the clip has pictures of an uploaded video: checked_grab);
# the continuation and the join carry no Cast exemption
# (pipeline.studio_cast_source gives (None, None) for each).

CONTINUES = "continues_job_id"
AUTO_JOIN = "auto_join"
JOIN_JOB = "join_job_id"
JOIN_NOTE = "join_note"
AUTO_JOIN_FOR = "auto_join_for"


def continue_options(continues_job_id: int, auto_join: bool) -> dict:
    return {CONTINUES: int(continues_job_id), AUTO_JOIN: bool(auto_join)}


def continuation_of(job: models.AssetGenerationJob) -> Optional[dict]:
    """The continuation intent a clip carries (for Retry), or None."""
    opts = job.options_json or {}
    if not opts.get(CONTINUES):
        return None
    try:
        return continue_options(int(opts[CONTINUES]), bool(opts.get(AUTO_JOIN)))
    except (TypeError, ValueError):
        return None


def continue_origin(db: Session, job: Optional[models.AssetGenerationJob], *,
                    depth: int = 0) -> Optional[models.AssetGenerationJob]:
    """The generated clip a continuation of `job` follows on from: `job`
    itself when it was generated from a prompt; for joined clips, the last
    clip's (followed back); None for an uploaded video or anything else."""
    if job is None or depth > 8 or job.output_kind != "video_clip":
        return None
    if job.operation == JOIN_OP:
        ids, _, _ = join_options(job)
        return continue_origin(db, db.get(models.AssetGenerationJob, ids[-1]) if ids else None, depth=depth + 1)
    return None if job.operation else job


def continue_prompt(db: Session, job: Optional[models.AssetGenerationJob]) -> str:
    """What a continuation of `job` prefills its prompt with: the prompt of
    the clip it follows on from ("" for an uploaded video)."""
    origin = continue_origin(db, job)
    return (origin.prompt or "") if origin is not None else ""


def continue_aspect(job: models.AssetGenerationJob) -> str:
    """The aspect a continuation of `job` is made in: the clip's own, else
    the nearest to its real size, else the owner's default ("")."""
    if job.aspect_ratio in ("9:16", "16:9", "1:1"):
        return job.aspect_ratio
    if job.result_width and job.result_height:
        return aspect_for((job.result_width, job.result_height))
    return ""


def _owner_exists(db: Session, kind: str, owner_id: int) -> bool:
    model = {"avatar": models.Avatar, "ingredient": models.Ingredient}.get(kind)
    return model is not None and db.get(model, owner_id) is not None


def not_joined_note(error: OpError) -> str:
    if error.status == 404:
        return "Not joined: the original was deleted"
    text = error.message.rstrip(".")
    return f"Not joined: {text[:1].lower() + text[1:]}"


def plan_auto_join(db: Session, job: models.AssetGenerationJob) -> Optional[models.AssetGenerationJob]:
    """In the caller's transaction, right after `job` (a clip) was claimed
    completed: when it continues a clip with auto_join on and nothing was
    joined or noted for it yet, add the pending join [original, `job`]
    (a cut) and record its id on `job`, or record why it can't be made
    ("join_note"). Returns the join to queue once committed, else None.
    Never raises for a join that can't be made."""
    opts = dict(job.options_json or {})
    original = opts.get(CONTINUES)
    if not original or not opts.get(AUTO_JOIN) or opts.get(JOIN_JOB) or opts.get(JOIN_NOTE):
        return None
    join = None
    try:
        sources = join_sources(db, [int(original), job.id])
        if not _owner_exists(db, sources[0].owner_kind, sources[0].owner_id):
            opts[JOIN_NOTE] = "Not joined: the original's character or scene was deleted"
        else:
            join = new_join_job(sources, "cut", 0.0)
            join.options_json = {**(join.options_json or {}), AUTO_JOIN_FOR: job.id}
            db.add(join)
            db.flush()
            opts[JOIN_JOB] = join.id
    except OpError as e:
        opts[JOIN_NOTE] = not_joined_note(e)
    except (TypeError, ValueError):
        opts[JOIN_NOTE] = "Not joined: the original clip isn't known"
    job.options_json = opts
    return join


RELINK_TRIES = 3


def relink_auto_join(db: Session, old: models.AssetGenerationJob, new: models.AssetGenerationJob) -> bool:
    """A failed auto-join run again (Retry): the new join is the
    continuation's join from now on (auto_join_for on it, join_job_id on
    the continuation), when the continuation still points at the old one.
    Two Retries at once: the continuation is claimed with a compare-and-set
    on its updated_at (an UPDATE that only matches the row as it was read),
    so only one relinks; the other, reading it again, sees it no longer
    points at the old join and stays a plain join. Returns whether `new`
    was linked. In the caller's transaction."""
    Job = models.AssetGenerationJob
    for_id = (old.options_json or {}).get(AUTO_JOIN_FOR)
    if not for_id or old.status != "failed":
        return False
    try:
        cont_id = int(for_id)
    except (TypeError, ValueError):
        return False
    for _ in range(RELINK_TRIES):
        cont = db.query(Job).filter(Job.id == cont_id).populate_existing().one_or_none()
        if cont is None or cont.status == STUDIO_DELETED:
            return False
        opts = dict(cont.options_json or {})
        if opts.get(JOIN_JOB) != old.id:
            return False
        seen = cont.updated_at
        claimed = db.query(Job).filter(Job.id == cont_id, Job.updated_at == seen).update(
            {Job.options_json: {**opts, JOIN_JOB: new.id}, Job.updated_at: datetime.utcnow()},
            synchronize_session=False)
        if claimed == 1:
            db.expire(cont)
            new.options_json = {**(new.options_json or {}), AUTO_JOIN_FOR: cont_id}
            return True
    return False


def queue_join(db: Session, join: models.AssetGenerationJob) -> None:
    """Queue a committed auto-join for the worker; when the queue refuses
    it, it is marked failed (Retry joins again). Never raises."""
    import logging

    from ..workers.jobs import generate_asset_job
    from ..workers.queue import enqueue

    Job = models.AssetGenerationJob
    try:
        qid = enqueue(generate_asset_job, join.id)
        db.query(Job).filter(Job.id == join.id, Job.status == "pending").update(
            {"queue_job_id": str(qid)[:80]}, synchronize_session=False)
        db.commit()
    except Exception:  # noqa: BLE001 — the clip itself is finished; only the join waits
        db.rollback()
        logging.getLogger(__name__).exception("couldn't queue the auto-join %s", join.id)
        db.query(Job).filter(Job.id == join.id, Job.status == "pending").update(
            {"status": "failed", "error": "Couldn't queue this join; retry it"}, synchronize_session=False)
        db.commit()

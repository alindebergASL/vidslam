"""Save a frame of a video as a Studio picture ("Save last frame as
picture", "Save first frame", "Save this frame").

The source is either a finished Studio clip (source_job_id) or a video the
owner uploads for this (not kept: only the frame is stored). Each grab is a
new Studio image job with operation "extract_frame", the time asked for in
options_json ({"at": "first" | "last" | seconds, "seconds": the time of the
frame saved}) and a local model id: no provider is called and nothing is
billed. A frame of a Studio clip makes no paid call at all, so the monthly
budget cap doesn't apply to it (the rate limit does: it uses this
machine's CPU); a frame of an uploaded video does pay for its safety check
(below), so it is refused once the budget's hard stop is reached. ffmpeg
is in the API image, so the API grabs the frame itself and the job is
finished before it answers; without ffmpeg there, a clip's frame is queued
for the worker instead (as the free upscale is). The frame is the one a
paused player shows at that time (renderer.extract_frame_at), as an
8-bit PNG (HDR tone-mapped), and every ffprobe/ffmpeg run for it shares
one time limit (renderer.FRAME_TIMEOUT): past it the tool is stopped,
nothing is stored and the API answers 504.

Which checks apply:

- A frame of a Studio clip: no new check. The clip was made from a prompt
  that already passed the minors checks (and, on a clean deployment, with
  the providers' content filters on); an animated clip's picture comes
  mostly from the picture it started from, which had that picture's own
  checks. So the frame counts as the picture the clip was made from
  (pipeline.studio_cast_source): a clip animated from a Cast character's
  picture (or from a cutout, upscale or saved frame of one), or made with
  their pictures as references, gives frames that are that character's
  picture, so wherever the frame is used next (Animate, an ambient scene's
  picture, a cutout or upscale of it) an unticked character's provider
  content filter is forced on, exactly as for the Cast picture itself; a
  ticked character's filter is skipped only when every card picture the
  frame comes from is one the tick's picture check covers. A frame of a
  clip made from a prompt alone (or from a new Studio picture) is a new
  Studio picture, like any other.
- A frame of an uploaded video comes from outside the app, so before it is
  stored the picture model is asked whether anyone in it is, or looks,
  under 18 (minor_check.check_pictures, the same check as Cast pictures;
  a vision call costing a fraction of a cent, remembered per picture).
  A yes is refused; so is no answer (the check can't run, the model won't
  judge it, the frame can't be read): it fails closed. On a clean
  deployment (ADULT_FEATURES=false) the picture model is also asked
  whether the frame shows nudity or sexual content
  (explicit_check.check_picture), failing closed the same way. The stored
  frame counts as a picture of its owner when that is a Cast character,
  with no card picture behind it, so no tick covers it: the owner's filter
  is forced on wherever it is used next (saved onto a ticked card, the new
  card picture is covered once the tick's picture check has looked at it,
  like any picture added to the card).
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
from .renderer import FFmpegTimeout, extract_frame_at

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
TOO_SLOW = ("Reading that video took too long, so no frame was saved. Try a shorter or smaller video "
            "(an MP4 reads fastest).")
CLIP_TOO_SLOW = "Reading that clip took too long, so no frame was saved. Try again."


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
    # Kept to the microsecond, rounded down: a paused player shows the frame
    # at or before its time, and rounding up could cross into the next one.
    return math.floor(t * 1_000_000 + 1e-6) / 1_000_000


def seconds_for(at: At) -> float:
    """The time to ask ffmpeg for ("last": math.inf, the last frame)."""
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
    """(PNG bytes, the frame's time) of the frame of `video` shown at `at`.
    Raises RuntimeError when it can't be read (renderer.FFmpegTimeout, a
    RuntimeError, when that takes too long)."""
    with tempfile.TemporaryDirectory(prefix="frame-") as tmp:
        out = Path(tmp) / "frame.png"
        used = extract_frame_at(video, out, seconds_for(at))
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
        except FFmpegTimeout:
            raise OpError(504, TOO_SLOW) from None
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

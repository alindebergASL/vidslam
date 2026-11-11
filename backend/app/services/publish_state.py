"""What a publish job looks like to the creator, and what YouTube says now.

`apply_video` stores one `videos.list` item (part=status,snippet) on a
publish job; `display_state` turns the job into a plain-language state plus
a warning when YouTube's state differs from what was asked.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .. import models

PRIVATE_LOCK = ("YouTube kept this private (your Google project hasn't passed YouTube's audit, "
                "so API uploads stay private)")
SCHEDULE_DROPPED = ("YouTube kept this private and dropped the schedule (your Google project hasn't "
                    "passed YouTube's audit, so API uploads stay private)")
REMOVED = "Removed on YouTube"
PROCESSING = "Processing on YouTube — it can take a few minutes before people can watch it"

# status.failureReason / status.rejectionReason → plain language.
_REASONS = {
    "codec": "YouTube couldn't read the video's codec",
    "conversion": "YouTube couldn't convert the video",
    "emptyFile": "the file YouTube received was empty",
    "invalidFile": "YouTube couldn't read the file",
    "tooSmall": "the file was too small",
    "uploadAborted": "the upload was cut off",
    "claim": "a copyright claim",
    "copyright": "a copyright problem",
    "duplicate": "it's a duplicate of a video already on the channel",
    "inappropriate": "inappropriate content",
    "legal": "a legal problem",
    "length": "it's too long for this channel",
    "termsOfUse": "a terms-of-use problem",
    "trademark": "a trademark problem",
    "uploaderAccountClosed": "the channel was closed",
    "uploaderAccountSuspended": "the channel is suspended",
}

IN_PROGRESS = ("queued", "uploading")
# A render with one of these jobs is already (or about to be) on YouTube.
LIVE_STATUSES = ("queued", "uploading", "processing", "scheduled", "published")


def reason_text(reason: str) -> str:
    return _REASONS.get(reason, reason)


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def mark_removed(job: models.PublishJob, now: Optional[datetime] = None) -> None:
    job.yt_removed = True
    job.yt_checked_at = now or datetime.utcnow()


def can_mark_removed(job: models.PublishJob, channel_id: Optional[str]) -> bool:
    """videos.list leaves out private and scheduled videos the connected
    account doesn't own, so a missing video only means "removed" when it was
    uploaded to the channel connected now."""
    return bool(channel_id and job.channel_id and job.channel_id == channel_id)


def on_youtube(job: models.PublishJob) -> bool:
    """The job already made a YouTube video (even one YouTube rejected):
    uploading it again makes another video, not a resumed one."""
    return bool(job.external_id)


def reset_for_new_upload(job: models.PublishJob) -> None:
    """Forget the earlier video before a fresh upload session starts, so
    nothing YouTube said about it sticks to the new one."""
    job.external_id = ""
    job.external_url = ""
    job.uploaded_at = None
    job.channel_id = ""
    job.yt_privacy_status = ""
    job.yt_upload_status = ""
    job.yt_publish_at = None
    job.yt_failure_reason = ""
    job.yt_title = ""
    job.yt_removed = False
    job.yt_checked_at = None


def apply_video(job: models.PublishJob, item: dict, now: Optional[datetime] = None) -> None:
    """Store what YouTube says about this job's video right now."""
    st = item.get("status") or {}
    sn = item.get("snippet") or {}
    job.yt_checked_at = now or datetime.utcnow()
    job.yt_removed = False
    if st:
        job.yt_privacy_status = (st.get("privacyStatus") or "")[:20]
        job.yt_upload_status = (st.get("uploadStatus") or "")[:20]
        job.yt_publish_at = _parse_time(st.get("publishAt"))
        job.yt_failure_reason = (st.get("rejectionReason") or st.get("failureReason") or "")[:100]
    if sn.get("title"):
        job.yt_title = sn["title"][:200]
    if sn.get("channelId") and not job.channel_id:
        job.channel_id = sn["channelId"][:64]
    if job.yt_upload_status == "deleted":
        job.yt_removed = True
    elif job.yt_upload_status in ("rejected", "failed") and job.status not in IN_PROGRESS:
        what = "rejected" if job.yt_upload_status == "rejected" else "couldn't process"
        why = reason_text(job.yt_failure_reason)
        job.status = "failed"
        job.error = f"YouTube {what} this video" + (f": {why}" if why else "")
    elif job.status == "processing" and job.yt_upload_status == "processed":
        scheduled = (job.yt_publish_at or job.publish_at) and job.yt_privacy_status != "public"
        job.status = "scheduled" if scheduled else "published"
    elif job.status == "scheduled" and job.yt_privacy_status == "public":
        # YouTube made it public at publishAt (it drops publishAt then).
        job.status = "published"


def display_state(job: models.PublishJob) -> tuple[str, str]:
    """(state, warning). state: uploading | published | scheduled | private |
    processing | removed | failed."""
    if job.status in IN_PROGRESS:
        return "uploading", ""
    if job.status == "failed":
        return "failed", ""
    if job.yt_removed:
        return "removed", REMOVED
    checked = job.yt_checked_at is not None
    privacy = job.yt_privacy_status if checked and job.yt_privacy_status else None
    warning = ""
    if privacy == "private":
        if job.publish_at and not job.yt_publish_at:
            warning = SCHEDULE_DROPPED
        elif not job.publish_at and job.privacy in ("public", "unlisted"):
            warning = PRIVATE_LOCK
    if (checked and job.yt_upload_status == "uploaded") or job.status == "processing":
        return "processing", warning or PROCESSING
    publish_at = job.yt_publish_at if checked else job.publish_at
    if privacy in (None, "private") and publish_at and publish_at > datetime.utcnow():
        return "scheduled", warning
    if privacy == "private" or (privacy is None and job.privacy == "private" and not job.publish_at):
        return "private", warning
    return "published", warning

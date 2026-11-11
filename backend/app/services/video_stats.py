"""Daily YouTube statistics for uploaded videos (the results loop).

`snapshot()` reads videos.list?part=status,snippet,statistics in batches of
50 for every video uploaded in the last 90 days to the channel connected now
(plus any extra jobs asked for). It keeps one row per video per UTC day (a
later fetch the same day updates that row) and stores YouTube's state on each
job, so "processing" and "scheduled" move on without a manual check. The
extra parts cost no extra quota.
`start_periodic()` is called by the video-jobs sweeper about once a minute
and, when a fetch is due (about every 6 hours), runs `periodic_tick()` on
its own daemon thread, one at a time, so a slow YouTube never holds up
video-job polling. It never raises and skips silently without YouTube
credentials. The last attempt (which throttles the 6 hours) and the last
successful fetch (what the page shows as "Last updated") are kept apart.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Iterable, Optional

import httpx
from sqlalchemy import true
from sqlalchemy.orm import Session

from .. import models
from ..db import SessionLocal
from . import model_registry as settings_store
from . import publish_state

log = logging.getLogger("avs.video_stats")

WINDOW = timedelta(days=90)
EVERY = timedelta(hours=6)
LAST_RUN_KEY = "youtube.stats_last_run"  # last attempt: {at, ok, error}
LAST_OK_KEY = "youtube.stats_last_ok"  # last successful fetch: {at}
MANUAL_KEY = "youtube.stats_manual_at"
MANUAL_COOLDOWN = timedelta(minutes=2)


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def store(db: Session, job: models.PublishJob, statistics: dict, now: Optional[datetime] = None) -> models.VideoStat:
    """Today's (UTC) snapshot for this job: updated if it exists, else added."""
    from sqlalchemy.exc import IntegrityError

    now = now or datetime.utcnow()
    day = now.date()
    S = models.VideoStat
    row = db.query(S).filter(S.publish_job_id == job.id, S.day == day).first()
    if row is None:
        # Another fetch (the background one, or another process) may add
        # today's row at the same moment: the unique (job, day) key keeps
        # one, and this fetch then updates it.
        try:
            with db.begin_nested():
                row = S(publish_job_id=job.id, day=day, fetched_at=now)
                db.add(row)
                db.flush()
        except IntegrityError:
            row = db.query(S).filter(S.publish_job_id == job.id, S.day == day).one()
    row.fetched_at = now
    row.views = _int(statistics.get("viewCount"))
    row.likes = _int(statistics.get("likeCount"))
    row.comments = _int(statistics.get("commentCount"))
    return row


def _targets(db: Session, extra_job_ids: Iterable[int], now: datetime,
             channel: str) -> list[models.PublishJob]:
    """Recent uploads to the connected channel (or of a channel not known
    yet). Removed ones are asked about again: a miss can be YouTube's."""
    J = models.PublishJob
    # Channel not known (its lookup failed at connect): ask about all of
    # them; nothing is marked removed without a known channel anyway.
    mine = J.channel_id.in_((channel, "")) if channel else true()
    recent = (db.query(J).filter(J.external_id != "", J.status != "failed", J.created_at >= now - WINDOW,
                                 mine).all())
    have = {j.id for j in recent}
    extra = [i for i in extra_job_ids if i not in have]
    if extra:
        recent += db.query(J).filter(J.id.in_(extra), J.external_id != "", mine).all()
    return recent


def snapshot(db: Optional[Session] = None, *, job_ids: Iterable[int] = (),
             client: Optional[httpx.Client] = None, now: Optional[datetime] = None) -> dict:
    """Fetch and store today's numbers. Returns {videos, snapshots, removed,
    skipped}; `skipped` names why nothing was fetched ("not_connected")."""
    from . import youtube as yt

    if not yt.can_read():
        return {"videos": 0, "snapshots": 0, "removed": 0, "skipped": "not_connected"}
    own_db = db is None
    db = db or SessionLocal()
    own = client is None
    client = client or httpx.Client(timeout=30.0)
    now = now or datetime.utcnow()
    try:
        channel = yt.connected_channel_id()
        jobs = _targets(db, job_ids, now, channel)
        out = {"videos": len(jobs), "snapshots": 0, "removed": 0, "skipped": ""}
        if not jobs:
            return out
        token = yt.access_token(client)
        found = yt.list_videos(client, token, [j.external_id for j in jobs], part="status,snippet,statistics")
        for job in jobs:
            item = found.get(job.external_id)
            if item is None:
                if publish_state.can_mark_removed(job, channel) and not job.yt_removed:
                    publish_state.mark_removed(job, now)
                    out["removed"] += 1
                continue
            publish_state.apply_video(job, item, now)
            store(db, job, item.get("statistics") or {}, now)
            out["snapshots"] += 1
        db.commit()
        return out
    finally:
        if own:
            client.close()
        if own_db:
            db.close()


def _parse(value) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def last_run() -> Optional[datetime]:
    """When a fetch was last attempted (successful or not)."""
    return _parse((store_get(LAST_RUN_KEY) or {}).get("at"))


def last_success() -> Optional[datetime]:
    """When numbers were last fetched successfully."""
    return _parse((store_get(LAST_OK_KEY) or {}).get("at"))


def last_attempt() -> dict:
    """{at, failed, error} for the last attempt; `failed` only once it has
    actually failed (not while it runs)."""
    v = store_get(LAST_RUN_KEY) or {}
    return {"at": _parse(v.get("at")), "failed": v.get("ok") is False, "error": v.get("error") or ""}


def store_get(key: str) -> Optional[dict]:
    return settings_store.get_setting(key)


def _iso(now: datetime) -> str:
    return now.isoformat(timespec="seconds")


def record_attempt(now: datetime) -> None:
    settings_store.put_setting(LAST_RUN_KEY, {"at": _iso(now), "ok": None, "error": ""})


def record_outcome(now: datetime, error: str = "") -> None:
    """After a fetch: success (no error) also moves "Last updated"."""
    settings_store.put_setting(LAST_RUN_KEY, {"at": _iso(now), "ok": not error, "error": error[:300]})
    if not error:
        settings_store.put_setting(LAST_OK_KEY, {"at": _iso(now)})


_fetch_lock = threading.Lock()
_periodic_thread: Optional[threading.Thread] = None


def _due(now: datetime) -> bool:
    last = last_run()
    return last is None or now - last >= EVERY


def periodic_tick(now: Optional[datetime] = None) -> bool:
    """Run `snapshot()` if the last attempt was ≥6 h ago. Never raises.
    Returns True when it fetched. At most one runs at a time."""
    if not _fetch_lock.acquire(blocking=False):
        return False
    try:
        from . import youtube as yt

        if not yt.can_read():
            return False
        now = now or datetime.utcnow()
        if not _due(now):
            return False
        # Claim the run first so a failing YouTube isn't retried every minute.
        record_attempt(now)
        try:
            result = snapshot(now=now)
        except Exception as e:  # noqa: BLE001
            record_outcome(now, yt.upstream_message(e))
            raise
        if result.get("skipped"):
            record_outcome(now, "YouTube isn't connected")
            return False
        record_outcome(now)
        log.info("YouTube numbers: %s", result)
        return True
    except Exception:  # noqa: BLE001 — never take the sweeper down
        log.exception("YouTube numbers refresh failed")
        return False
    finally:
        _fetch_lock.release()


def start_periodic(now: Optional[datetime] = None) -> bool:
    """From the sweeper: start `periodic_tick()` on its own daemon thread when
    a fetch is due and none is running. Returns at once; never raises.
    True when a thread was started."""
    global _periodic_thread
    try:
        from . import youtube as yt

        if _fetch_lock.locked() or (_periodic_thread is not None and _periodic_thread.is_alive()):
            return False
        if not yt.can_read() or not _due(now or datetime.utcnow()):
            return False
        _periodic_thread = threading.Thread(target=periodic_tick, args=(now,), name="youtube-numbers",
                                            daemon=True)
        _periodic_thread.start()
        return True
    except Exception:  # noqa: BLE001
        log.exception("YouTube numbers refresh couldn't start")
        return False


def wait_periodic(timeout: float = 30.0) -> bool:
    """Wait for a running periodic fetch (tests, shutdown). True when idle."""
    t = _periodic_thread
    if t is not None:
        t.join(timeout)
        return not t.is_alive()
    return True


def manual_wait_seconds(now: Optional[datetime] = None) -> int:
    """Seconds until "Refresh numbers" may run again (0 = now)."""
    now = now or datetime.utcnow()
    at = _parse((store_get(MANUAL_KEY) or {}).get("at"))
    if at is None or now - at >= MANUAL_COOLDOWN:
        return 0
    return int((MANUAL_COOLDOWN - (now - at)).total_seconds()) + 1


def mark_manual(now: Optional[datetime] = None) -> None:
    now = now or datetime.utcnow()
    settings_store.put_setting(MANUAL_KEY, {"at": _iso(now)})
    record_attempt(now)

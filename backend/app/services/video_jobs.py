"""Asynchronous video jobs: submit, then get out of the way.

    submit()  → provider.submit(callback_url=…/webhooks/openrouter/videos/{token})
                VideoJob row (submitted); the worker is free immediately.
    webhook   → handle_webhook() verifies, dedupes, enqueues complete/fail.
    sweeper   → polls jobs whose next_check_at passed: every
                VIDEO_POLL_SECONDS without webhooks (local dev), every
                VIDEO_SAFETY_POLL_SECONDS as a safety net with them; fails
                jobs past their deadline.
    complete()/fail() claim the job atomically (a webhook and the sweeper can
    race), store the clip, then dispatch() advances whatever was waiting:
    storyboard compose, a shot regenerate's recompose, the next ambient chain
    segment, or a Studio result.

Mock providers finish at submit time, so submit() checks once immediately
and the whole flow runs inline in dev/tests.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import update
from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from ..db import SessionLocal
from ..providers.base import SubmittedJob

log = logging.getLogger("avs.video_jobs")

ACTIVE = ("submitted", "running")
# Providers with no completion callbacks: their jobs are always polled.
NO_WEBHOOK_PROVIDERS = frozenset({"sogni"})
TERMINAL = ("completed", "failed")


def _now() -> datetime:
    return datetime.utcnow()


# ---------- configuration ----------


def webhooks_enabled() -> bool:
    s = get_settings()
    mode = (s.video_webhooks or "auto").lower()
    if mode == "off":
        return False
    https = s.public_base_url.lower().startswith("https://")
    if mode == "on":
        return https  # OpenRouter rejects non-HTTPS callback URLs
    return https and not s.mock_providers


def callback_url(token: str) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/api/webhooks/openrouter/videos/{token}"


def _poll_interval(job: models.VideoJob) -> int:
    s = get_settings()
    return s.video_safety_poll_seconds if job.uses_webhook else s.video_poll_seconds


# ---------- submit ----------


def submit(
    db: Session,
    *,
    purpose: str,
    ref_id: int,
    project_id: Optional[int],
    render_id: Optional[int] = None,
    context: Optional[dict] = None,
    start: Callable[[Optional[str]], SubmittedJob],
    provider_label: str = "",
    webhooks: bool = True,
) -> models.VideoJob:
    """Create the job row, call `start(callback_url)` to submit to the
    provider, and return without waiting. `start` gets the webhook URL (or
    None when webhooks are off) and returns the provider's SubmittedJob.
    `webhooks=False` for providers without callbacks (Sogni): no URL is
    handed out and the job is polled at the VIDEO_POLL_SECONDS cadence."""
    s = get_settings()
    job = models.VideoJob(
        purpose=purpose, ref_id=ref_id, project_id=project_id, render_id=render_id,
        context_json=context or {}, status="submitted",
        deadline_at=_now() + timedelta(minutes=s.video_job_timeout_minutes),
    )
    use_hook = webhooks and webhooks_enabled()
    job.uses_webhook = use_hook
    db.add(job)
    db.flush()  # assigns callback_token default
    try:
        submitted = start(callback_url(job.callback_token) if use_hook else None)
    except Exception as e:
        # Never leave a "submitted" row without a provider job: the sweeper
        # would poll nothing until the deadline and block recomposes.
        job.status = "failed"
        job.error = f"submit failed: {e}"[:1000]
        job.completed_at = _now()
        db.commit()
        raise
    job.provider = provider_label or submitted.provider
    if job.provider in NO_WEBHOOK_PROVIDERS:
        job.uses_webhook = False  # nothing will call back: poll at the normal cadence
    job.model = submitted.model or ""
    sent = (submitted.raw or {}).get("duration") if isinstance(submitted.raw, dict) else None
    if job.provider == "sogni" and sent:
        # The length actually requested (Sogni snaps it): poll() and the spend
        # estimate use it when the result does not report its own duration.
        job.context_json = {**(job.context_json or {}), "duration": sent}
    job.provider_job_id = submitted.job_id
    job.polling_url = submitted.polling_url or ""
    job.next_check_at = _now() + timedelta(seconds=_poll_interval(job))
    db.commit()
    if job.provider == "mock":
        # Mock jobs are done at submit time: complete now so dev/tests flow inline.
        check(db, job.id)
        db.refresh(job)
    return job


def provider_for(job: models.VideoJob):
    """The provider that *submitted* this job — not whatever video model is
    active now (Avatar IV jobs, or jobs submitted before a model switch)."""
    if job.provider == "mock":
        from ..providers.mock_video import get_singleton

        return get_singleton()
    if job.provider == "sogni":
        from ..providers.sogni import SogniVideoProvider

        return SogniVideoProvider(model=job.model or None)
    from ..providers.openrouter_video import OpenRouterVideoProvider

    return OpenRouterVideoProvider(model=job.model or None)


def _submitted(job: models.VideoJob) -> SubmittedJob:
    return SubmittedJob(provider=job.provider, model=job.model, job_id=job.provider_job_id,
                        polling_url=job.polling_url,
                        raw={"duration": (job.context_json or {}).get("duration")})


# ---------- check / complete / fail ----------


def check(db: Session, job_id: int) -> str:
    """Poll the provider once. Returns the job's status afterwards."""
    job = db.get(models.VideoJob, job_id)
    if job is None or job.status not in ACTIVE:
        return job.status if job else "missing"
    job.checks += 1
    try:
        st = provider_for(job).poll(_submitted(job))
    except Exception as e:  # noqa: BLE001 — transient; try again next sweep
        log.warning("video job %s poll error: %s", job_id, e)
        st = None
    if st is not None and st.state == "succeeded":
        db.commit()
        return complete(db, job_id, cost=st.cost, result_url=st.result_url)
    if st is not None and st.state == "failed":
        db.commit()
        return fail(db, job_id, st.error or "provider reported failure")
    if _now() > job.deadline_at:
        db.commit()
        return fail(db, job_id, f"timed out after {get_settings().video_job_timeout_minutes} min")
    if st is not None and st.state == "running":
        job.status = "running"
    job.next_check_at = _now() + timedelta(seconds=_poll_interval(job))
    db.commit()
    return job.status


def _claim(db: Session, job_id: int, to: str) -> bool:
    """Move an active job to `to` exactly once (webhook vs sweeper races)."""
    res = db.execute(
        update(models.VideoJob)
        .where(models.VideoJob.id == job_id, models.VideoJob.status.in_(ACTIVE))
        .values(status=to, updated_at=_now())
    )
    db.commit()
    return res.rowcount == 1


def complete(db: Session, job_id: int, *, cost: Optional[float] = None,
             result_url: Optional[str] = None) -> str:
    from . import storage

    if not _claim(db, job_id, "completing"):
        return "duplicate"
    job = db.get(models.VideoJob, job_id)
    try:
        data = provider_for(job).download(_submitted(job), result_url=result_url)
        if not data:
            raise RuntimeError("provider returned an empty video")
        out_dir = storage.render_subdir(job.project_id) if job.project_id else \
            (get_settings().data_path / "studio" / "clips")
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"vjob_{job.id}.mp4"
        path.write_bytes(data)
    except Exception as e:  # noqa: BLE001
        job.status = "running"  # let fail() claim it
        db.commit()
        return fail(db, job_id, f"download failed: {e}")
    job.result_path = str(path)
    job.cost_usd = cost
    job.status = "completed"
    job.completed_at = _now()
    db.commit()
    _record_spend(db, job)
    try:
        dispatch_completed(db, job)
    except Exception:  # noqa: BLE001
        log.exception("video job %s: completion handler failed", job_id)
    return "completed"


def fail(db: Session, job_id: int, error: str) -> str:
    if not _claim(db, job_id, "failed"):
        return "duplicate"
    job = db.get(models.VideoJob, job_id)
    job.error = (error or "failed")[:800]
    job.completed_at = _now()
    db.commit()
    try:
        dispatch_failed(db, job)
    except Exception:  # noqa: BLE001
        log.exception("video job %s: failure handler failed", job_id)
    return "failed"


def _record_spend(db: Session, job: models.VideoJob) -> None:
    from . import spend

    spend.record_video_job(db, job)


# ---------- dispatch ----------


def dispatch_completed(db: Session, job: models.VideoJob) -> None:
    from . import spend

    # Follow-on paid work (next ambient segment, music, fallback clips) is
    # attributed to the same project / render.
    with spend.scope(project_id=job.project_id, render_id=job.render_id):
        _dispatch_completed(db, job)


def _dispatch_completed(db: Session, job: models.VideoJob) -> None:
    if job.purpose in ("shot", "avatar"):
        from . import pipeline

        pipeline.on_shot_clip_ready(db, job)
    elif job.purpose == "ambient_segment":
        from . import ambient

        ambient.on_segment_ready(db, job)
    elif job.purpose == "studio":
        from . import pipeline

        pipeline.on_studio_clip_ready(db, job)


def dispatch_failed(db: Session, job: models.VideoJob) -> None:
    from . import spend

    with spend.scope(project_id=job.project_id, render_id=job.render_id):
        _dispatch_failed(db, job)


def _dispatch_failed(db: Session, job: models.VideoJob) -> None:
    if job.purpose in ("shot", "avatar"):
        from . import pipeline

        pipeline.on_shot_clip_failed(db, job)
    elif job.purpose == "ambient_segment":
        from . import ambient

        ambient.on_segment_failed(db, job)
    elif job.purpose == "studio":
        from . import pipeline

        pipeline.on_studio_clip_failed(db, job)


def pending_for(db: Session, *, project_id: int, purposes: tuple[str, ...] = ("shot", "avatar")) -> int:
    return (db.query(models.VideoJob)
            .filter(models.VideoJob.project_id == project_id,
                    models.VideoJob.purpose.in_(purposes),
                    models.VideoJob.status.in_(ACTIVE + ("completing",)))
            .count())


# ---------- webhook ----------


class WebhookRejected(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def verify_signature(raw: bytes, header: Optional[str], secret: str, *, now: Optional[float] = None) -> None:
    """OpenRouter: X-OpenRouter-Signature: t=<unix>,v1=<hex HMAC-SHA256 of
    "{t},{raw body}">. Rejects bad/missing signatures and stale timestamps."""
    if not header:
        raise WebhookRejected(401, "missing X-OpenRouter-Signature")
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    t, v1 = parts.get("t"), parts.get("v1")
    if not t or not v1 or not t.isdigit():
        raise WebhookRejected(401, "malformed signature header")
    if abs((now or time.time()) - int(t)) > 300:
        raise WebhookRejected(401, "stale signature timestamp")
    expected = hmac.new(secret.encode(), f"{t},".encode() + raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, v1):
        raise WebhookRejected(401, "signature mismatch")


def handle_webhook(db: Session, token: str, raw: bytes, signature: Optional[str]) -> str:
    import json

    from ..workers.jobs import complete_video_job, fail_video_job
    from ..workers.queue import enqueue

    secret = get_settings().openrouter_webhook_secret
    if secret:
        verify_signature(raw, signature, secret)
    job = db.query(models.VideoJob).filter(models.VideoJob.callback_token == token).first()
    if job is None:
        raise WebhookRejected(404, "unknown job")
    try:
        payload = json.loads(raw or b"{}")
    except ValueError as e:
        raise WebhookRejected(400, "invalid JSON") from e
    data = payload.get("data") or {}
    if data.get("id") and job.provider_job_id and str(data["id"]) != job.provider_job_id:
        raise WebhookRejected(400, "job id mismatch")
    if job.status not in ACTIVE:
        return "already-handled"  # retries / duplicates are no-ops
    kind = (payload.get("type") or "").rsplit(".", 1)[-1] or (data.get("status") or "")
    if kind == "completed":
        urls = data.get("unsigned_urls") or []
        cost = (data.get("usage") or {}).get("cost")
        enqueue(complete_video_job, job.id, float(cost) if cost is not None else None, urls[0] if urls else None)
        return "completing"
    if kind in ("failed", "cancelled", "expired"):
        enqueue(fail_video_job, job.id, str(data.get("error") or f"video job {kind}"))
        return kind
    return "ignored"


# ---------- sweeper ----------


def sweep(limit: int = 50) -> int:
    """Check every due active job once. Each job is claimed by bumping its
    next_check_at, so several sweepers (web workers) never double-poll."""
    from ..workers.jobs import check_video_job
    from ..workers.queue import enqueue

    db = SessionLocal()
    try:
        now = _now()
        due = [j.id for j in db.query(models.VideoJob.id)
               .filter(models.VideoJob.status.in_(ACTIVE), models.VideoJob.next_check_at <= now)
               .order_by(models.VideoJob.next_check_at).limit(limit)]
        claimed = 0
        for jid in due:
            res = db.execute(
                update(models.VideoJob)
                .where(models.VideoJob.id == jid, models.VideoJob.next_check_at <= now)
                .values(next_check_at=now + timedelta(seconds=60))
            )
            db.commit()
            if res.rowcount == 1:
                enqueue(check_video_job, jid)
                claimed += 1
        return claimed
    finally:
        db.close()


_stop = threading.Event()
_thread: Optional[threading.Thread] = None


def start_sweeper(interval: float = 5.0) -> bool:
    global _thread
    if _thread and _thread.is_alive():
        return False
    _stop.clear()

    def loop() -> None:
        ticks = 0
        while not _stop.wait(interval):
            ticks += 1
            try:
                sweep()
            except Exception:  # noqa: BLE001
                log.exception("video job sweep failed")
            if ticks % max(int(60 / interval), 1) == 0:
                # About once a minute: settle TTS costs OpenRouter reports late.
                try:
                    from . import spend

                    spend.reconcile_pending()
                except Exception:  # noqa: BLE001
                    log.exception("spend reconcile failed")

    _thread = threading.Thread(target=loop, name="video-jobs", daemon=True)
    _thread.start()
    return True


def stop_sweeper() -> None:
    _stop.set()


def result_path(job: models.VideoJob) -> Path:
    return Path(job.result_path)

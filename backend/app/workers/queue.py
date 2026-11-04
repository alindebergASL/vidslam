from __future__ import annotations

import logging
from typing import Callable

from ..config import get_settings

log = logging.getLogger("avs.queue")
_settings = get_settings()
_queue_singleton = None
_redis_singleton = None


def _try_redis_queue():
    """Return an RQ Queue if Redis is reachable; otherwise None (fallback to inline execution)."""
    global _queue_singleton, _redis_singleton
    if _queue_singleton is not None:
        return _queue_singleton
    try:
        from redis import Redis
        from rq import Queue

        _redis_singleton = Redis.from_url(get_settings().redis_url, socket_connect_timeout=2)
        _redis_singleton.ping()
        _queue_singleton = Queue("default", connection=_redis_singleton)
        return _queue_singleton
    except Exception as e:  # noqa: BLE001
        log.warning("Redis unavailable (%s); jobs will run inline", e)
        return None


class QueueUnavailable(RuntimeError):
    """QUEUE_MODE=required and Redis isn't reachable."""


def queue_status() -> dict:
    """For /readyz: mode, whether Redis answers, how many RQ workers exist."""
    mode = (get_settings().queue_mode or "auto").lower()
    if mode == "inline":
        return {"mode": mode, "redis": False, "workers": 0, "ok": True}
    q = _try_redis_queue()
    workers = 0
    if q is not None:
        try:
            from rq import Worker

            workers = len(Worker.all(connection=_redis_singleton))
        except Exception:  # noqa: BLE001
            workers = 0
    ok = True if mode == "auto" else (q is not None and workers > 0)
    return {"mode": mode, "redis": q is not None, "workers": workers, "ok": ok}


def enqueue(func: Callable, *args, **kwargs) -> str:
    """Enqueue a function on RQ if Redis is available; otherwise run it inline
    (QUEUE_MODE=auto) or refuse (QUEUE_MODE=required).

    Returns a job identifier (RQ id or "inline:<func_name>").
    """
    s = get_settings()
    mode = (s.queue_mode or "auto").lower()
    q = None if mode == "inline" else _try_redis_queue()
    if q is None and mode == "required":
        # Never fall back to running an hour-long render inside a web worker.
        raise QueueUnavailable("job queue (Redis) unavailable and QUEUE_MODE=required")
    if q is None:
        try:
            func(*args, **kwargs)
        except Exception:
            log.exception("inline job failed: %s", getattr(func, "__name__", str(func)))
        return f"inline:{getattr(func, '__name__', 'job')}"
    job = q.enqueue(func, *args, **kwargs, job_timeout=s.queue_job_timeout_seconds,
                    result_ttl=24 * 3600, failure_ttl=7 * 24 * 3600)
    return job.id

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

        _redis_singleton = Redis.from_url(_settings.redis_url)
        _redis_singleton.ping()
        _queue_singleton = Queue("default", connection=_redis_singleton)
        return _queue_singleton
    except Exception as e:  # noqa: BLE001
        log.warning("Redis unavailable (%s); jobs will run inline", e)
        return None


def enqueue(func: Callable, *args, **kwargs) -> str:
    """Enqueue a function on RQ if Redis is available; otherwise run it inline.

    Returns a job identifier (RQ id or "inline:<func_name>").
    """
    q = _try_redis_queue()
    if q is None:
        try:
            func(*args, **kwargs)
        except Exception:
            log.exception("inline job failed: %s", getattr(func, "__name__", str(func)))
        return f"inline:{getattr(func, '__name__', 'job')}"
    job = q.enqueue(func, *args, **kwargs, job_timeout=60 * 30)
    return job.id

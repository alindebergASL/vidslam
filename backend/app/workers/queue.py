from __future__ import annotations

import logging
import queue as queue_mod
import threading
import time
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


def queue_entry_state(job_id: str) -> str:
    """Whether a job's queue entry can still run it: "alive" (queued,
    started, deferred or scheduled in RQ), "gone" (no RQ id, ran inline,
    finished, failed or expired) or "unknown" (Redis didn't answer)."""
    job_id = (job_id or "").strip()
    if not job_id or ":" in job_id:  # "", "inline:...", "background:..."
        return "gone"
    if (get_settings().queue_mode or "auto").lower() == "inline":
        return "gone"
    if _try_redis_queue() is None:
        return "unknown"
    try:
        from rq.exceptions import NoSuchJobError
        from rq.job import Job

        try:
            job = Job.fetch(job_id, connection=_redis_singleton)
        except NoSuchJobError:
            return "gone"
        status = job.get_status(refresh=False)
    except Exception:  # noqa: BLE001
        return "unknown"
    status = getattr(status, "value", status)
    return "alive" if status in ("queued", "started", "deferred", "scheduled") else "gone"


# ---------- low-priority side jobs (advisory work off the render's path) ----------

LOW_QUEUE = "inspect"  # workers list it after "default", so they drain it last
_low_queues: dict = {}
_bg_lock = threading.Lock()
_bg_tasks: "queue_mod.Queue | None" = None
_bg_thread: "threading.Thread | None" = None


def _try_low_queue(name: str):
    if _try_redis_queue() is None:
        return None
    q = _low_queues.get(name)
    if q is None:
        from rq import Queue

        q = _low_queues[name] = Queue(name, connection=_redis_singleton)
    return q


def _bg_worker() -> None:
    assert _bg_tasks is not None
    while True:
        func, args, kwargs = _bg_tasks.get()
        try:
            func(*args, **kwargs)
        except Exception:  # noqa: BLE001
            log.exception("background job failed: %s", getattr(func, "__name__", str(func)))
        finally:
            _bg_tasks.task_done()


def _run_in_background(func: Callable, *args, **kwargs) -> None:
    """One daemon thread runs side jobs one after another, so the caller
    (the sweeper, a webhook on the event loop) returns at once."""
    global _bg_tasks, _bg_thread
    with _bg_lock:
        if _bg_tasks is None:
            _bg_tasks = queue_mod.Queue()
        if _bg_thread is None or not _bg_thread.is_alive():
            _bg_thread = threading.Thread(target=_bg_worker, name="avs-side-jobs", daemon=True)
            _bg_thread.start()
    _bg_tasks.put((func, args, kwargs))


def wait_background(timeout: float = 30.0) -> bool:
    """Wait until queued side jobs are done (tests, shutdown). True when idle."""
    q = _bg_tasks
    if q is None:
        return True
    deadline = time.monotonic() + timeout
    while q.unfinished_tasks:
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


def enqueue_low(func: Callable, *args, queue_name: str = LOW_QUEUE, **kwargs) -> str:
    """Enqueue advisory work that must never delay or block a render: its own
    RQ queue (drained after "default"), or, without Redis, a background daemon
    thread instead of running inline in the caller. QUEUE_MODE=required with
    Redis down: skipped (logged), like any job that can't be queued."""
    s = get_settings()
    mode = (s.queue_mode or "auto").lower()
    q = None if mode == "inline" else _try_low_queue(queue_name)
    if q is None and mode == "required":
        raise QueueUnavailable("job queue (Redis) unavailable and QUEUE_MODE=required")
    if q is None:
        _run_in_background(func, *args, **kwargs)
        return f"background:{getattr(func, '__name__', 'job')}"
    job = q.enqueue(func, *args, **kwargs, job_timeout=s.queue_job_timeout_seconds,
                    result_ttl=24 * 3600, failure_ttl=7 * 24 * 3600)
    return job.id


# ---------- one waiting job per key (checks that mustn't pile up) ----------

_bg_waiting: set[str] = set()
_bg_waiting_lock = threading.Lock()


def _run_keyed(key: str, func: Callable, args: tuple, kwargs: dict) -> None:
    with _bg_waiting_lock:
        _bg_waiting.discard(key)  # started: a change from now on needs a run of its own
    func(*args, **kwargs)


def _keyed_status(job_id: str) -> "str | None":
    """The RQ status of a job id ("queued", "started", "finished", …), None
    when there's no such job."""
    from rq.exceptions import NoSuchJobError
    from rq.job import Job

    try:
        job = Job.fetch(job_id, connection=_redis_singleton)
    except NoSuchJobError:
        return None
    status = job.get_status(refresh=False)
    return str(getattr(status, "value", status) or "")


def _drop_keyed(job_id: str) -> None:
    """Forget a finished (or failed, stopped, cancelled) job so its id can be used again."""
    from rq.exceptions import NoSuchJobError
    from rq.job import Job

    try:
        Job.fetch(job_id, connection=_redis_singleton).delete()
    except NoSuchJobError:
        pass


def enqueue_once(func: Callable, *args, key: str, **kwargs) -> str:
    """Queue `func` on the default queue unless a job for `key` is already
    waiting there: that one runs later and sees whatever the caller just
    committed. A job for `key` that has started may have missed it, so one
    more can wait behind it (ids `key` and `key.next`). Never runs in the
    caller: without Redis (QUEUE_MODE=auto, or inline) it runs on the
    side-job thread, with the same rule. QUEUE_MODE=required with Redis
    down: not queued, "" returned (logged). Returns the job id."""
    s = get_settings()
    mode = (s.queue_mode or "auto").lower()
    q = None if mode == "inline" else _try_redis_queue()
    if q is None:
        if mode == "required":
            log.warning("job queue (Redis) unavailable: %s not queued", key)
            return ""
        with _bg_waiting_lock:
            if key in _bg_waiting:
                return f"background:{key}"
            _bg_waiting.add(key)
        _run_in_background(_run_keyed, key, func, args, kwargs)
        return f"background:{key}"
    for job_id in (key, f"{key}.next"):
        status = _keyed_status(job_id)
        if status in ("queued", "deferred", "scheduled"):
            return job_id  # waiting: it will see this change
        if status == "started":
            continue  # running: it may have read what it checks before this change
        if status is not None:
            _drop_keyed(job_id)
        job = q.enqueue(func, *args, **kwargs, job_id=job_id, job_timeout=s.queue_job_timeout_seconds,
                        result_ttl=3600, failure_ttl=7 * 24 * 3600)
        return job.id
    log.info("%s: two runs already under way; not queued again", key)
    return key

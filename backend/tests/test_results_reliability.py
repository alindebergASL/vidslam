"""Results loop reliability: the periodic fetch runs off the sweeper (one at
a time), network failures become a plain 502, and "Last updated" is the last
successful fetch, not the last attempt."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

import httpx
import respx

from .test_results import _db, _stats_mock, _uploaded, yt_env  # noqa: F401
from .test_youtube import API, TOKEN, _connect

# ---------- 1: periodic fetch off the sweeper thread ----------


def test_start_periodic_returns_at_once_and_runs_one_fetch_at_a_time(monkeypatch):
    from app.services import video_stats, youtube

    monkeypatch.setattr(youtube, "can_read", lambda: True)
    monkeypatch.setattr(video_stats, "_due", lambda now: True)
    monkeypatch.setattr(video_stats, "record_attempt", lambda now: None)
    monkeypatch.setattr(video_stats, "record_outcome", lambda now, error="": None)
    release = threading.Event()
    started = threading.Event()
    calls = []

    def slow_snapshot(**kw):
        calls.append(threading.current_thread().name)
        started.set()
        release.wait(5)
        return {"videos": 0, "snapshots": 0, "removed": 0, "skipped": ""}

    monkeypatch.setattr(video_stats, "snapshot", slow_snapshot)
    t0 = time.monotonic()
    assert video_stats.start_periodic() is True
    assert time.monotonic() - t0 < 1.0  # the sweeper doesn't wait for YouTube
    assert started.wait(5)
    # Still running: neither another start nor a direct tick fetches again.
    assert video_stats.start_periodic() is False
    assert video_stats.periodic_tick() is False
    release.set()
    assert video_stats.wait_periodic(5)
    assert calls == ["youtube-numbers"]


def test_start_periodic_skips_when_not_due_or_not_connected(monkeypatch):
    from app.services import video_stats, youtube

    monkeypatch.setattr(youtube, "can_read", lambda: False)
    assert video_stats.start_periodic() is False
    monkeypatch.setattr(youtube, "can_read", lambda: True)
    monkeypatch.setattr(video_stats, "last_run", lambda: datetime.utcnow() - timedelta(hours=1))
    assert video_stats.start_periodic() is False


def test_sweeper_uses_the_background_start():
    import inspect

    from app.services import video_jobs

    src = inspect.getsource(video_jobs.start_sweeper)
    assert "video_stats.start_periodic()" in src and "periodic_tick" not in src


# ---------- 2: network failures are a plain 502 ----------


@respx.mock
def test_results_refresh_timeout_is_a_502(auth_client, yt_env):  # noqa: F811
    _connect(auth_client)
    db = _db()
    _uploaded(db, ext="t1")
    db.close()
    respx.get(f"{API}/videos").mock(side_effect=httpx.ReadTimeout("slow"))
    r = auth_client.post("/api/results/refresh")
    assert r.status_code == 502
    assert r.json()["detail"] == "YouTube didn't answer in time — try again in a minute"


@respx.mock
def test_results_refresh_token_refresh_failure_is_a_502(auth_client, yt_env):  # noqa: F811
    _connect(auth_client)
    db = _db()
    _uploaded(db, ext="t2")
    db.close()
    yt_env._access.clear()
    respx.post(TOKEN).mock(side_effect=httpx.ConnectError("refused"))
    r = auth_client.post("/api/results/refresh")
    assert r.status_code == 502 and "Couldn't reach YouTube" in r.json()["detail"]


@respx.mock
def test_token_refresh_with_a_garbled_answer_is_a_502(auth_client, yt_env):  # noqa: F811
    _connect(auth_client)
    db = _db()
    _uploaded(db, ext="t3")
    db.close()
    yt_env._access.clear()
    respx.post(TOKEN).mock(return_value=httpx.Response(200, text="<html>oops</html>"))
    r = auth_client.post("/api/results/refresh")
    assert r.status_code == 502 and "token refresh failed" in r.json()["detail"]


@respx.mock
def test_publish_job_refresh_connection_error_is_a_502(auth_client, yt_env):  # noqa: F811
    _connect(auth_client)
    db = _db()
    _, jid = _uploaded(db, ext="t4")
    db.close()
    respx.get(f"{API}/videos").mock(side_effect=httpx.ConnectError("refused"))
    one = auth_client.post(f"/api/publish-jobs/{jid}/refresh")
    assert one.status_code == 502 and "Couldn't reach YouTube" in one.json()["detail"]
    many = auth_client.post("/api/publish-jobs/refresh", json={"ids": [jid]})
    assert many.status_code == 502 and "Couldn't reach YouTube" in many.json()["detail"]


# ---------- 3: last success vs last attempt ----------


@respx.mock
def test_last_updated_is_the_last_successful_fetch(auth_client, yt_env):  # noqa: F811
    from app.services import video_stats

    _connect(auth_client)
    db = _db()
    _uploaded(db, ext="s1")
    db.close()
    route = respx.get(f"{API}/videos").mock(side_effect=httpx.ReadTimeout("slow"))
    now = datetime.utcnow()
    assert video_stats.periodic_tick(now) is False
    body = auth_client.get("/api/results").json()
    assert body["last_refreshed_at"] is None  # never fetched successfully
    assert body["last_attempt_failed"] is True and "didn't answer" in body["last_attempt_error"]
    assert body["last_attempt_at"] is not None
    # The failed attempt still throttles the 6-hour interval.
    assert video_stats.periodic_tick(now + timedelta(hours=1)) is False
    assert route.call_count == 1

    route.mock(side_effect=None)
    _stats_mock({"s1": (3, 0, 0)})
    ok_at = now + timedelta(hours=6, minutes=1)
    assert video_stats.periodic_tick(ok_at) is True
    body = auth_client.get("/api/results").json()
    assert body["last_refreshed_at"].startswith(ok_at.isoformat(timespec="seconds"))
    assert body["last_attempt_failed"] is False and body["last_attempt_error"] == ""

    # A later failure keeps the last good time and says the last try failed.
    respx.get(f"{API}/videos").mock(return_value=httpx.Response(503, text="down"))
    assert video_stats.periodic_tick(ok_at + timedelta(hours=7)) is False
    body = auth_client.get("/api/results").json()
    assert body["last_refreshed_at"].startswith(ok_at.isoformat(timespec="seconds"))
    assert body["last_attempt_failed"] is True


@respx.mock
def test_failed_manual_refresh_is_recorded(auth_client, yt_env):  # noqa: F811
    _connect(auth_client)
    db = _db()
    _uploaded(db, ext="m1")
    db.close()
    respx.get(f"{API}/videos").mock(side_effect=httpx.ConnectError("refused"))
    assert auth_client.post("/api/results/refresh").status_code == 502
    body = auth_client.get("/api/results").json()
    assert body["last_refreshed_at"] is None and body["last_attempt_failed"] is True


# ---------- 4: stored numbers stay visible while disconnected ----------


def test_results_keep_stored_rows_when_disconnected(auth_client):
    from app.services import video_stats

    db = _db()
    _, jid = _uploaded(db, ext="d1")
    from app import models

    video_stats.store(db, db.get(models.PublishJob, jid), {"viewCount": "9"})
    db.commit()
    db.close()
    body = auth_client.get("/api/results").json()
    assert body["connected"] is False
    assert body["rows"][0]["views"] == 9 and body["groups"]

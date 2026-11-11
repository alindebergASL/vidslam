"""Results loop: daily YouTube numbers per uploaded video, cost per video,
roll-ups, the periodic job and migration 0008."""
from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from .test_youtube import API, _connect


@pytest.fixture
def yt_env(monkeypatch):
    """YouTube credentials configured (same setup as test_youtube)."""
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("FRONTEND_ORIGIN", "http://front.local")
    from app import config
    from app.services import youtube

    config.get_settings.cache_clear()
    youtube._access.clear()
    monkeypatch.setattr(youtube.time, "sleep", lambda s: None)
    return youtube


def _db():
    from app.db import SessionLocal

    return SessionLocal()


def _uploaded(db, *, title="Video", ambient=False, cfg=None, ext="v1", days_ago=0.0, avatar=None,
              aspect=None, model="", channel="UC123") -> tuple[int, int]:
    """A project + completed render + uploaded publish job. → (project id, job id)."""
    from app import models

    p = models.VideoProject(title=title, mode="ambient_loop" if ambient else "reel_montage",
                            aspect_ratio=aspect or ("16:9" if ambient else "9:16"),
                            ambient_config_json=(cfg or {"scene_preset": "rain", "total_seconds": 3600})
                            if ambient else None)
    if avatar:
        a = models.Avatar(name=avatar)
        db.add(a)
        db.flush()
        p.primary_avatar_id = a.id
    db.add(p)
    db.flush()
    if not ambient:
        db.add(models.VideoShot(project_id=p.id, duration_seconds=12, provider_model=model))
    r = models.Render(project_id=p.id, status="completed", final_video_path="/x.mp4")
    db.add(r)
    db.flush()
    when = datetime.utcnow() - timedelta(days=days_ago)
    j = models.PublishJob(render_id=r.id, title=title, status="published", privacy="public", external_id=ext,
                          external_url=f"https://www.youtube.com/watch?v={ext}", uploaded_at=when,
                          created_at=when, channel_id=channel)
    db.add(j)
    db.commit()
    return p.id, j.id


def _stats_mock(numbers: dict):
    calls = []

    def handler(request):
        q = parse_qs(urlparse(str(request.url)).query)
        ids = q["id"][0].split(",")
        calls.append({"ids": ids, "part": q["part"][0]})
        return httpx.Response(200, json={"items": [
            {"id": i, "statistics": {"viewCount": str(numbers[i][0]), "likeCount": str(numbers[i][1]),
                                     "commentCount": str(numbers[i][2])}} for i in ids if i in numbers]})

    route = respx.get(f"{API}/videos").mock(side_effect=handler)
    return calls, route


@respx.mock
def test_snapshot_once_per_utc_day_updates_same_day_row(auth_client, yt_env):
    from app import models
    from app.services import video_stats

    _connect(auth_client)
    db = _db()
    _, jid = _uploaded(db, ext="s1")
    db.close()
    numbers = {"s1": (10, 1, 0)}
    calls, _ = _stats_mock(numbers)
    day1 = datetime.utcnow().replace(hour=9, minute=0, second=0, microsecond=0)
    assert video_stats.snapshot(now=day1) == {"videos": 1, "snapshots": 1, "removed": 0, "skipped": ""}
    numbers["s1"] = (25, 3, 2)
    video_stats.snapshot(now=day1 + timedelta(hours=6))
    db = _db()
    rows = db.query(models.VideoStat).filter_by(publish_job_id=jid).all()
    assert len(rows) == 1 and (rows[0].views, rows[0].likes, rows[0].comments) == (25, 3, 2)
    assert rows[0].fetched_at == day1 + timedelta(hours=6)
    db.close()
    numbers["s1"] = (40, 4, 2)
    video_stats.snapshot(now=day1 + timedelta(days=1))
    db = _db()
    assert [r.views for r in db.query(models.VideoStat).filter_by(publish_job_id=jid)
            .order_by(models.VideoStat.fetched_at)] == [25, 40]
    db.close()
    # State and numbers in one call (same quota as statistics alone).
    assert all(c["part"] == "status,snippet,statistics" for c in calls)


@respx.mock
def test_snapshot_batches_fifty_and_skips_old_and_marks_removed(auth_client, yt_env):
    from app import models
    from app.services import video_stats

    _connect(auth_client)
    db = _db()
    for n in range(110):
        _uploaded(db, title=f"v{n}", ext=f"id{n}")
    _, old = _uploaded(db, title="old", ext="old", days_ago=120)
    _, gone = _uploaded(db, title="gone", ext="gone")
    db.close()
    calls, _ = _stats_mock({f"id{n}": (n, 0, 0) for n in range(110)})
    out = video_stats.snapshot()
    assert out["videos"] == 111 and out["snapshots"] == 110 and out["removed"] == 1
    assert [len(c["ids"]) for c in calls] == [50, 50, 11]
    assert "old" not in {i for c in calls for i in c["ids"]}
    db = _db()
    assert db.get(models.PublishJob, gone).yt_removed is True
    assert db.query(models.VideoStat).filter_by(publish_job_id=old).count() == 0
    db.close()
    # Asked for explicitly (on-demand refresh), an older video is included.
    calls.clear()
    video_stats.snapshot(job_ids=[old])
    assert "old" in {i for c in calls for i in c["ids"]}


def test_periodic_job_skips_without_credentials(auth_client):
    from app.services import video_stats

    with respx.mock(assert_all_called=False) as mock:
        assert video_stats.periodic_tick() is False
        assert video_stats.snapshot()["skipped"] == "not_connected"
        assert len(mock.calls) == 0
    assert video_stats.last_run() is None


@respx.mock
def test_periodic_job_never_raises_and_runs_every_six_hours(auth_client, yt_env):
    from app.services import video_stats

    _connect(auth_client)
    db = _db()
    _uploaded(db, ext="p1")
    db.close()
    route = respx.get(f"{API}/videos").mock(return_value=httpx.Response(503, text="down"))
    now = datetime.utcnow()
    assert video_stats.periodic_tick(now) is False  # YouTube down: logged, not raised
    assert route.call_count == 1
    assert video_stats.periodic_tick(now + timedelta(hours=1)) is False  # not due yet
    assert route.call_count == 1
    route.mock(return_value=httpx.Response(200, json={"items": [{"id": "p1", "statistics": {"viewCount": "5"}}]}))
    assert video_stats.periodic_tick(now + timedelta(hours=6, minutes=1)) is True
    assert route.call_count == 2


def test_periodic_job_swallows_unexpected_errors(monkeypatch):
    from app.services import video_stats, youtube

    def boom(*a, **k):
        raise RuntimeError("bad")

    monkeypatch.setattr(youtube, "can_read", lambda: True)
    monkeypatch.setattr(video_stats, "snapshot", boom)
    assert video_stats.periodic_tick() is False


def test_cost_per_thousand_views_and_zero_views(auth_client):
    from app import models
    from app.services import publishing, video_stats

    db = _db()
    pid, jid = _uploaded(db, ext="c1", days_ago=10)
    pid2, jid2 = _uploaded(db, title="No views", ext="c2", days_ago=2)
    db.add_all([
        models.SpendEntry(provider="openrouter", kind="video", project_id=pid, render_id=None, cost_usd=1.5),
        models.SpendEntry(provider="elevenlabs", kind="tts", project_id=pid, cost_usd=0.5, is_estimate=True),
        models.SpendEntry(provider="openrouter", kind="video", project_id=pid2, cost_usd=2.0),
    ])
    db.commit()
    now = datetime.utcnow()
    video_stats.store(db, db.get(models.PublishJob, jid), {"viewCount": "100"}, now - timedelta(days=8))
    video_stats.store(db, db.get(models.PublishJob, jid), {"viewCount": "4000", "likeCount": "40",
                                                           "commentCount": "4"}, now)
    video_stats.store(db, db.get(models.PublishJob, jid2), {"viewCount": "0"}, now)
    db.commit()
    db.close()
    assert publishing.per_1k(2.0, 0) is None and publishing.per_1k(2.0, None) is None
    body = auth_client.get("/api/results").json()
    rows = {r["publish_job_id"]: r for r in body["rows"]}
    a, b = rows[jid], rows[jid2]
    assert a["cost_usd"] == 2.0 and a["cost_is_estimate"] is True
    assert a["cost_per_1k_views"] == 0.5 and a["views"] == 4000 and a["likes"] == 40 and a["comments"] == 4
    assert a["age_days"] == 10 and a["views_per_day"] == 400.0
    assert a["views_before"] == 100 and a["views_change"] == 3900 and a["trend_days"] == 8
    assert a["sparkline"] == [100, 4000]
    assert b["views"] == 0 and b["cost_per_1k_views"] is None and b["cost_is_estimate"] is False
    assert b["views_before"] is None  # one snapshot: no trend yet
    assert body["has_numbers"] is True and body["connected"] is False


def test_rollups_group_and_mark_too_few(auth_client):
    from app import models
    from app.services import video_stats

    db = _db()
    jobs = []
    for n, (_views, preset, tod) in enumerate([(100, "rain", "night"), (300, "rain", "night"), (200, "forest", "dawn")]):
        jobs.append(_uploaded(db, title=f"amb{n}", ambient=True, ext=f"a{n}",
                              cfg={"scene_preset": preset, "time_of_day": tod, "weather": "rain",
                                   "total_seconds": 3 * 3600 if n else 1800})[1])
    jobs.append(_uploaded(db, title="story", ext="s0", avatar="Mia", model="wan-x")[1])
    db.add(models.VideoJob(purpose="shot", ref_id=1, project_id=None, model="ignored"))
    db.commit()
    for jid, views in zip(jobs, (100, 300, 200, 50), strict=True):
        video_stats.store(db, db.get(models.PublishJob, jid), {"viewCount": str(views)})
    db.commit()
    db.close()

    def groups(by):
        body = auth_client.get(f"/api/results?group_by={by}").json()
        return body, {g["key"]: g for g in body["groups"]}

    body, g = groups("type")
    assert body["group_label"] == "Video type"
    assert g["Ambient"]["videos"] == 3 and g["Ambient"]["too_few"] is False
    assert g["Ambient"]["total_views"] == 600 and g["Ambient"]["median_views"] == 200
    assert g["Storyboard"]["videos"] == 1 and g["Storyboard"]["too_few"] is True
    assert g["Ambient"]["cost_per_1k_views"] == 0.0  # mock mode: nothing was spent
    _, g = groups("preset")
    assert g["rain"]["videos"] == 2 and g["rain"]["too_few"] is True and "Storyboard (no preset)" in g
    _, g = groups("time_weather")
    assert g["night · rain"]["total_views"] == 400
    _, g = groups("length")
    assert set(g) == {"10–60 min", "Over 1 hour", "Under 1 min"}
    assert g["Over 1 hour"]["videos"] == 2
    _, g = groups("character")
    assert g["Mia"]["videos"] == 1 and g["No character (ambient)"]["videos"] == 3
    _, g = groups("model")
    assert "wan-x" in g
    _, g = groups("aspect")
    assert g["16:9"]["videos"] == 3 and g["9:16"]["videos"] == 1
    assert auth_client.get("/api/results?group_by=nope").status_code == 422


def test_rollup_counts_project_cost_once(auth_client):
    from app import models
    from app.services import publishing

    db = _db()
    pid, jid = _uploaded(db, ext="d1")
    job = db.get(models.PublishJob, jid)
    db.add(models.PublishJob(render_id=job.render_id, title="again", status="published", external_id="d2"))
    db.add(models.SpendEntry(provider="openrouter", kind="video", project_id=pid, cost_usd=3.0))
    db.commit()
    rows = publishing.result_rows(db)
    db.close()
    assert len(rows) == 2 and all(r["cost_usd"] == 3.0 for r in rows)
    (g,) = publishing.group_rows(rows, "type")
    assert g["total_cost_usd"] == 3.0 and g["videos"] == 2


def test_results_empty_states(auth_client):
    body = auth_client.get("/api/results").json()
    assert body["rows"] == [] and body["groups"] == [] and body["has_numbers"] is False
    assert body["configured"] is False and body["connected"] is False and body["last_refreshed_at"] is None
    r = auth_client.post("/api/results/refresh")
    assert r.status_code == 409 and "Connect YouTube in Settings" in r.json()["detail"]


@respx.mock
def test_refresh_numbers_runs_now_and_is_rate_limited(auth_client, yt_env):
    _connect(auth_client)
    db = _db()
    _uploaded(db, ext="r1")
    db.close()
    _stats_mock({"r1": (7, 1, 0)})
    r = auth_client.post("/api/results/refresh")
    assert r.status_code == 200, r.text
    assert r.json()["snapshots"] == 1 and r.json()["skipped"] == ""
    body = auth_client.get("/api/results").json()
    assert body["rows"][0]["views"] == 7 and body["last_refreshed_at"] is not None and body["connected"]
    again = auth_client.post("/api/results/refresh")
    assert again.status_code == 429 and int(again.headers["Retry-After"]) > 0


def test_migration_0008(tmp_path, monkeypatch):
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import inspect, text

    from app import config
    from app import db as dbmod

    url = f"sqlite:///{tmp_path / 'm.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config.get_settings.cache_clear()
    dbmod.reset_engine(url)
    command.upgrade(dbmod._alembic_config(), "0007")
    with dbmod.engine.begin() as conn:
        conn.execute(text("INSERT INTO video_projects (title, original_script, mode, aspect_ratio, "
                          "target_duration_seconds, cta_text, caption_style, include_disclosure, disclosure_text, "
                          "creative_direction, voiceover_source, voiceover_upload_path, music_upload_path, "
                          "music_volume, status, created_at, updated_at) VALUES ('p','', 'reel_montage', '9:16', "
                          "25, '', 'clean_white', 1, '', '', 'tts', '', '', 0.25, 'draft', '2026-01-01', "
                          "'2026-01-01')"))
    assert "video_stats" not in inspect(dbmod.engine).get_table_names()
    command.upgrade(dbmod._alembic_config(), "head")
    insp = inspect(dbmod.engine)
    cols = {c["name"]: c for c in insp.get_columns("publish_jobs")}
    assert {"uploaded_at", "yt_privacy_status", "yt_upload_status", "yt_publish_at", "yt_failure_reason",
            "yt_title", "yt_removed", "yt_checked_at"} <= set(cols)
    assert {c["name"] for c in insp.get_columns("video_stats")} == {
        "id", "publish_job_id", "fetched_at", "day", "views", "likes", "comments"}
    assert any(i["column_names"] == ["publish_job_id", "fetched_at"] for i in insp.get_indexes("video_stats"))
    assert any(u["column_names"] == ["publish_job_id", "day"] for u in insp.get_unique_constraints("video_stats"))
    fk = insp.get_foreign_keys("video_stats")[0]
    assert fk["referred_table"] == "publish_jobs" and fk["options"].get("ondelete") == "CASCADE"
    with dbmod.engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0009"
        diff = compare_metadata(MigrationContext.configure(conn), dbmod.Base.metadata)
    assert diff == [], diff  # same as `alembic check`
    command.downgrade(dbmod._alembic_config(), "0007")
    insp = inspect(dbmod.engine)
    assert "video_stats" not in insp.get_table_names()
    assert "yt_removed" not in {c["name"] for c in insp.get_columns("publish_jobs")}
    command.upgrade(dbmod._alembic_config(), "head")


def test_deleting_a_project_drops_its_numbers(auth_client):
    from app import models
    from app.services import video_stats

    db = _db()
    pid, jid = _uploaded(db, ext="del")
    video_stats.store(db, db.get(models.PublishJob, jid), {"viewCount": "1"})
    db.commit()
    db.close()
    assert auth_client.delete(f"/api/projects/{pid}").status_code == 204
    db = _db()
    assert db.query(models.VideoStat).count() == 0
    db.close()


def test_one_row_per_video_per_day_even_when_two_fetches_race(client):
    """The (job, day) key holds: a second insert for the same day updates
    the row the first one added instead of adding another."""
    import threading

    from app import models
    from app.services import video_stats

    db = _db()
    try:
        _pid, jid = _uploaded(db)
    finally:
        db.close()
    now = datetime.utcnow()
    barrier = threading.Barrier(2)

    def fetch(views):
        s = _db()
        try:
            job = s.get(models.PublishJob, jid)
            barrier.wait()
            video_stats.store(s, job, {"viewCount": str(views)}, now)
            s.commit()
        finally:
            s.close()

    threads = [threading.Thread(target=fetch, args=(v,)) for v in (10, 20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    db = _db()
    try:
        rows = db.query(models.VideoStat).filter_by(publish_job_id=jid).all()
        assert len(rows) == 1 and rows[0].views in (10, 20) and rows[0].day == now.date()
    finally:
        db.close()


def test_a_new_upload_on_the_same_job_starts_with_no_numbers(client):
    from app import models
    from app.services import publish_state, video_stats

    db = _db()
    try:
        _pid, jid = _uploaded(db)
        job = db.get(models.PublishJob, jid)
        video_stats.store(db, job, {"viewCount": "500"})
        db.commit()
        publish_state.reset_for_new_upload(job)
        db.commit()
        assert db.query(models.VideoStat).filter_by(publish_job_id=jid).count() == 0
        assert job.external_id == ""
    finally:
        db.close()

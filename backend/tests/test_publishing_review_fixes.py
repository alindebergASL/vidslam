"""Publishing review fixes: rejected-video retries, automatic YouTube state,
channel-aware "removed", the per-render publish lock, stable upload dates,
roll-ups without numbers, the results page's query count and render links."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from sqlalchemy import event

from .test_youtube import API, SESSION, UP, _connect, _render


@pytest.fixture
def yt_env(monkeypatch):
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


def _videos_mock(items_by_id: dict):
    calls = []

    def handler(request):
        q = parse_qs(urlparse(str(request.url)).query)
        ids = q["id"][0].split(",")
        calls.append({"ids": ids, "part": q["part"][0]})
        return httpx.Response(200, json={"items": [items_by_id[i] for i in ids if i in items_by_id]})

    route = respx.get(f"{API}/videos").mock(side_effect=handler)
    return calls, route


def _upload_mocks(video_id: str):
    init = respx.post(f"{UP}/videos").mock(return_value=httpx.Response(200, headers={"Location": SESSION}))
    respx.put(SESSION).mock(return_value=httpx.Response(201, json={"id": video_id}))
    respx.post(f"{UP}/thumbnails/set").mock(return_value=httpx.Response(200, json={}))
    return init


def _job(db, rid: int, **kw):
    from app import models

    kw.setdefault("title", "Video")
    kw.setdefault("status", "published")
    j = models.PublishJob(render_id=rid, platform="youtube", **kw)
    db.add(j)
    db.commit()
    return j


def _project_render(db, *, ambient=False):
    from app import models

    p = models.VideoProject(title="P", mode="ambient_loop" if ambient else "reel_montage",
                            aspect_ratio="9:16", ambient_config_json={"scene_preset": "rain"} if ambient else None)
    db.add(p)
    db.flush()
    r = models.Render(project_id=p.id, status="completed", final_video_path="/x.mp4")
    db.add(r)
    db.commit()
    return p, r


# ---------- 1 + 9: retries that would make a second video ----------


@respx.mock
def test_retry_of_a_youtube_rejected_job_needs_confirmation_and_starts_clean(auth_client, yt_env):
    from app import models

    _connect(auth_client)
    rid = _render(auth_client, size=1024)
    _upload_mocks("vrej")
    _, route = _videos_mock({"vrej": {"id": "vrej", "snippet": {"channelId": "UC123"},
                                      "status": {"privacyStatus": "private", "uploadStatus": "rejected",
                                                 "rejectionReason": "duplicate"}}})
    assert auth_client.post(f"/api/renders/{rid}/publish/youtube", json={"title": "x"}).status_code == 202
    job = auth_client.get(f"/api/renders/{rid}/publish-jobs").json()[0]
    assert job["status"] == "failed" and job["external_id"] == "vrej" and "duplicate" in job["error"]

    r = auth_client.post(f"/api/publish-jobs/{job['id']}/retry")
    assert r.status_code == 409 and r.headers["X-Needs-Confirm"] == "new-upload"
    db = _db()
    assert db.get(models.PublishJob, job["id"]).status == "failed"
    db.close()

    # Confirmed: a fresh upload whose state check fails keeps nothing of the
    # rejected video.
    respx.put(SESSION).mock(return_value=httpx.Response(201, json={"id": "vnew"}))
    route.mock(return_value=httpx.Response(500, text="down"))
    r = auth_client.post(f"/api/publish-jobs/{job['id']}/retry", json={"confirm_new_upload": True})
    assert r.status_code == 202, r.text
    db = _db()
    j = db.get(models.PublishJob, job["id"])
    assert j.external_id == "vnew" and j.status == "published"
    assert j.yt_upload_status == "" and j.yt_failure_reason == "" and j.yt_checked_at is None
    assert j.channel_id == "UC123"  # the connected channel
    db.close()


@respx.mock
def test_retry_of_a_failed_job_is_refused_while_the_render_is_live(auth_client, yt_env):
    from app import models

    _connect(auth_client)
    rid = _render(auth_client, size=1024)
    db = _db()
    failed = _job(db, rid, status="failed", error="quotaExceeded").id
    live = _job(db, rid, status="published", external_id="vlive",
                external_url="https://www.youtube.com/watch?v=vlive").id
    resumable = _job(db, rid, status="failed", error="cut off", upload_url=SESSION).id
    db.close()
    r = auth_client.post(f"/api/publish-jobs/{failed}/retry")
    assert r.status_code == 409 and r.headers["X-Duplicate-Of"] == str(live)
    db = _db()
    assert db.get(models.PublishJob, failed).status == "failed"
    db.close()

    # Resuming a saved session isn't a new video: allowed.
    respx.put(SESSION).mock(side_effect=[httpx.Response(308, headers={"Range": "bytes=0-99"}),
                                         httpx.Response(201, json={"id": "vres"})])
    respx.post(f"{UP}/thumbnails/set").mock(return_value=httpx.Response(200, json={}))
    _videos_mock({})
    assert auth_client.post(f"/api/publish-jobs/{resumable}/retry").status_code == 202
    # Confirmed on purpose: allowed.
    _upload_mocks("vagain")
    r = auth_client.post(f"/api/publish-jobs/{failed}/retry", json={"confirm_new_upload": True})
    assert r.status_code == 202 and r.json()["external_id"] == "vagain"


# ---------- 2: automatic state ----------


@respx.mock
def test_snapshot_moves_processing_and_past_schedules_on(auth_client, yt_env):
    from app import models
    from app.services import video_stats

    _connect(auth_client)
    db = _db()
    _, r = _project_render(db)
    past = datetime.utcnow() - timedelta(hours=2)
    proc = _job(db, r.id, external_id="vproc", privacy="public", channel_id="UC123",
                yt_upload_status="uploaded", yt_privacy_status="public", yt_checked_at=past).id
    sched = _job(db, r.id, external_id="vsched", status="scheduled", privacy="private", publish_at=past,
                 channel_id="UC123", yt_upload_status="processed", yt_privacy_status="private",
                 yt_publish_at=past, yt_checked_at=past - timedelta(days=1)).id
    db.close()
    calls, _ = _videos_mock({
        "vproc": {"id": "vproc", "status": {"privacyStatus": "public", "uploadStatus": "processed"},
                  "statistics": {"viewCount": "4"}},
        "vsched": {"id": "vsched", "status": {"privacyStatus": "public", "uploadStatus": "processed"},
                   "statistics": {"viewCount": "9"}},
    })
    out = video_stats.snapshot()
    assert out["snapshots"] == 2 and calls[0]["part"] == "status,snippet,statistics"
    db = _db()
    p, s = db.get(models.PublishJob, proc), db.get(models.PublishJob, sched)
    assert p.state == "published" and p.yt_upload_status == "processed"
    assert s.status == "published" and s.state == "published"
    db.close()
    page = auth_client.get("/api/publishing/jobs").json()
    assert page["counts"]["scheduled"] == 0 and page["counts"]["published"] == 2


def test_scheduled_job_goes_published_once_youtube_says_public():
    from app import models
    from app.services import publish_state

    j = models.PublishJob(status="scheduled", publish_at=datetime.utcnow() + timedelta(days=1), privacy="private")
    publish_state.apply_video(j, {"status": {"privacyStatus": "private", "uploadStatus": "processed",
                                             "publishAt": "2099-01-01T00:00:00Z"}})
    assert j.status == "scheduled"
    publish_state.apply_video(j, {"status": {"privacyStatus": "public", "uploadStatus": "processed"}})
    assert j.status == "published"
    k = models.PublishJob(status="processing", publish_at=datetime.utcnow() - timedelta(days=1))
    publish_state.apply_video(k, {"status": {"privacyStatus": "public", "uploadStatus": "processed"}})
    assert k.status == "published"


# ---------- 3: only the connected channel's videos can be "removed" ----------


@respx.mock
def test_other_channels_videos_are_never_marked_removed(auth_client, yt_env):
    from app import models
    from app.services import video_stats

    _connect(auth_client)  # channel UC123
    db = _db()
    _, r = _project_render(db)
    other = _job(db, r.id, external_id="vother", channel_id="UCold").id
    legacy_missing = _job(db, r.id, external_id="vlegacy").id
    legacy_found = _job(db, r.id, external_id="vfound").id
    mine_gone = _job(db, r.id, external_id="vgone", channel_id="UC123").id
    db.close()
    calls, _ = _videos_mock({"vfound": {"id": "vfound", "snippet": {"channelId": "UC123"},
                                        "status": {"privacyStatus": "public", "uploadStatus": "processed"}}})
    out = video_stats.snapshot()
    assert out["removed"] == 1
    asked = {i for c in calls for i in c["ids"]}
    assert "vother" not in asked and {"vlegacy", "vfound", "vgone"} <= asked
    db = _db()
    assert db.get(models.PublishJob, other).yt_removed is False
    assert db.get(models.PublishJob, legacy_missing).yt_removed is False
    assert db.get(models.PublishJob, legacy_found).channel_id == "UC123"  # learned
    assert db.get(models.PublishJob, mine_gone).yt_removed is True
    db.close()

    # On-demand refresh: the other channel's video is skipped, not removed.
    res = auth_client.post("/api/publish-jobs/refresh", json={"ids": [other, legacy_missing]}).json()
    assert res["removed"] == 0 and res["skipped"] == 1
    db = _db()
    assert db.get(models.PublishJob, other).yt_removed is False
    assert db.get(models.PublishJob, legacy_missing).yt_removed is False
    db.close()


@respx.mock
def test_snapshot_rechecks_removed_videos(auth_client, yt_env):
    from app import models
    from app.services import video_stats

    _connect(auth_client)
    db = _db()
    _, r = _project_render(db)
    jid = _job(db, r.id, external_id="vback", channel_id="UC123", yt_removed=True).id
    db.close()
    _videos_mock({"vback": {"id": "vback", "status": {"privacyStatus": "public", "uploadStatus": "processed"}}})
    video_stats.snapshot()
    db = _db()
    assert db.get(models.PublishJob, jid).yt_removed is False
    db.close()


# ---------- 4: two publish requests at once ----------


@respx.mock
def test_concurrent_publish_requests_make_one_job(auth_client, yt_env, monkeypatch):
    from fastapi import HTTPException

    from app import models
    from app.api import youtube as yt_api
    from app.services import publishing

    _connect(auth_client)
    rid = _render(auth_client, size=1024)
    monkeypatch.setattr(yt_api, "enqueue", lambda *a, **k: "noop")
    barrier = threading.Barrier(2)
    real = publishing.duplicate_of

    def slow_duplicate_of(db, render_id, exclude_id=None):
        # Without a lock both requests meet here before either inserts.
        try:
            barrier.wait(timeout=1.5)
        except threading.BrokenBarrierError:
            pass
        return real(db, render_id, exclude_id=exclude_id)

    monkeypatch.setattr(yt_api, "duplicate_of", slow_duplicate_of)
    results: list = []

    def call():
        db = _db()
        try:
            yt_api.publish(rid, yt_api.PublishRequest(title="t"), db=db)
            results.append(202)
        except HTTPException as e:
            results.append(e.status_code)
        finally:
            db.close()

    threads = [threading.Thread(target=call) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert sorted(results) == [202, 409]
    db = _db()
    assert db.query(models.PublishJob).filter_by(render_id=rid).count() == 1
    db.close()


# ---------- 5: stable upload date ----------


def test_old_upload_date_does_not_move_with_checks(auth_client):
    from app.services import publish_state, publishing

    db = _db()
    _, r = _project_render(db)
    march = datetime.utcnow() - timedelta(days=200)
    j = _job(db, r.id, external_id="vold", privacy="public", created_at=march, updated_at=march)
    publish_state.apply_video(j, {"status": {"privacyStatus": "public", "uploadStatus": "processed"}})
    db.commit()
    assert j.updated_at > march + timedelta(days=100)
    assert publishing.uploaded_at(j) == march
    (row,) = publishing.result_rows(db)
    assert row["published_at"] == march and row["age_days"] == 200
    db.close()


def _insert(conn, table: str, values: dict) -> None:
    """Insert a row, filling every other NOT NULL column without a default."""
    from sqlalchemy import inspect, text

    row = dict(values)
    for c in inspect(conn).get_columns(table):
        if c["name"] in row or c["nullable"] or c.get("default") is not None or c.get("autoincrement") is True \
                or c["name"] == "id":
            continue
        t = str(c["type"]).upper()
        row[c["name"]] = 0 if any(k in t for k in ("INT", "BOOL", "FLOAT", "NUMERIC", "REAL")) else \
            ("2026-01-01" if "DATE" in t else ("{}" if "JSON" in t else ""))
    cols = ", ".join(row)
    conn.execute(text(f"INSERT INTO {table} ({cols}) VALUES ({', '.join(':' + k for k in row)})"), row)


def test_migration_0008_backfills_upload_time(tmp_path, monkeypatch):
    from alembic import command
    from sqlalchemy import text

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
        _insert(conn, "renders", {"project_id": 1, "status": "completed", "final_video_path": "/x.mp4"})
        for ext in ("vup", ""):
            _insert(conn, "publish_jobs", {"render_id": 1, "status": "published", "external_id": ext,
                                           "created_at": "2026-03-01 10:00:00",
                                           "updated_at": "2026-03-02 10:00:00"})
    command.upgrade(dbmod._alembic_config(), "head")
    with dbmod.engine.connect() as conn:
        got = dict(conn.execute(text("SELECT external_id, uploaded_at FROM publish_jobs")).all())
    assert str(got["vup"]).startswith("2026-03-02 10:00:00") and got[""] is None


# ---------- 6: roll-ups leave out videos with no numbers ----------


def test_rollups_ignore_videos_without_numbers():
    from app.services import publishing

    def row(pid, views, cost=0.0):
        return {"project_id": pid, "views": views, "cost_usd": cost, "cost_is_estimate": False,
                "project_kind": "ambient"}

    rows = [row(1, 1000, 1.0), row(2, 2000, 2.0), row(3, None, 30.0)]
    (g,) = publishing.group_rows(rows, "type")
    assert g["median_views"] == 1500 and g["total_views"] == 3000
    assert g["videos"] == 3 and g["videos_with_numbers"] == 2 and g["too_few"] is True
    assert g["cost_per_1k_views"] == 1.0  # the unfetched video's project cost is left out
    assert g["total_cost_usd"] == 33.0
    (g,) = publishing.group_rows([row(4, None), row(5, None)], "type")
    assert g["median_views"] is None and g["total_views"] == 0 and g["cost_per_1k_views"] is None


# ---------- 7: results page query count ----------


def _seed_results(db, n: int):
    from app import models
    from app.services import video_stats

    now = datetime.utcnow()
    for k in range(n):
        a = models.Avatar(name=f"A{k}")
        db.add(a)
        db.flush()
        p = models.VideoProject(title=f"S{k}", mode="reel_montage", aspect_ratio="9:16", primary_avatar_id=a.id)
        db.add(p)
        db.flush()
        db.add(models.ProjectCastMember(project_id=p.id, avatar_id=a.id))
        for s in range(4):
            shot = models.VideoShot(project_id=p.id, duration_seconds=5, shot_order=s)
            db.add(shot)
            db.flush()
            db.add(models.ShotTake(shot_id=shot.id, provider_model="m-take"))
        r = models.Render(project_id=p.id, status="completed", final_video_path="/x.mp4")
        db.add(r)
        db.flush()
        j = models.PublishJob(render_id=r.id, title="t", status="published", external_id=f"q{k}",
                              uploaded_at=now - timedelta(days=100))
        db.add(j)
        db.flush()
        for d in (100, 60, 20, 8, 1, 0):
            video_stats.store(db, j, {"viewCount": str(1000 - d)}, now - timedelta(days=d))
    db.commit()


def _count_queries(db, fn):
    n = [0]

    def before(*a, **k):
        n[0] += 1

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", before)
    try:
        out = fn()
    finally:
        event.remove(engine, "before_cursor_execute", before)
    return n[0], out


def test_results_queries_do_not_grow_with_projects(auth_client):
    from app.services import publishing

    db = _db()
    _seed_results(db, 2)
    db.close()
    db = _db()
    small, rows = _count_queries(db, lambda: publishing.result_rows(db))
    db.close()
    assert rows[0]["model"] == "m-take" and rows[0]["character"].startswith("A")
    db = _db()
    _seed_results(db, 6)
    db.close()
    db = _db()
    big, rows = _count_queries(db, lambda: publishing.result_rows(db))
    db.close()
    assert len(rows) == 8 and big == small
    # Only the last 31 days of snapshots (plus the latest) are loaded.
    assert rows[0]["sparkline"] == [980, 992, 999, 1000]
    assert rows[0]["views_before"] == 992 and rows[0]["trend_days"] == 8


# ---------- 12: links to the exact render ----------


def test_overview_items_link_to_their_render(auth_client):
    db = _db()
    p, r = _project_render(db)
    ap, ar = _project_render(db, ambient=True)
    _job(db, r.id, status="failed")
    _job(db, ar.id, status="failed")
    ids = (p.id, r.id, ap.id, ar.id)
    db.close()
    items = auth_client.get("/api/publishing/jobs").json()["items"]
    urls = {i["render_id"]: i["render_url"] for i in items}
    assert urls[ids[1]] == f"/projects/{ids[0]}/render?id={ids[1]}"
    assert urls[ids[3]] == f"/ambient/{ids[2]}?render={ids[3]}"


def test_overview_can_poll_only_some_ids(auth_client):
    db = _db()
    _, r = _project_render(db)
    a = _job(db, r.id, status="uploading").id
    _job(db, r.id)
    c = _job(db, r.id, status="queued").id
    db.close()
    page = auth_client.get(f"/api/publishing/jobs?ids={a}&ids={c}&limit=100").json()
    assert [i["id"] for i in page["items"]] == [c, a] and page["counts"]["all"] == 3

"""Publishing overview: every publish job in one list, the renders not
posted yet, refreshing YouTube's real state, and the duplicate guard."""
from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from .test_youtube import API, SESSION, UP, _connect, _render


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


def _project(db, title: str, *, ambient: bool = False, cfg: dict | None = None):
    from app import models

    p = models.VideoProject(title=title, mode="ambient_loop" if ambient else "reel_montage",
                            aspect_ratio="16:9" if ambient else "9:16",
                            ambient_config_json=(cfg or {"scene_preset": "rain"}) if ambient else None)
    db.add(p)
    db.commit()
    return p


def _render_row(db, pid: int, status: str = "completed", path: str = "/nope.mp4"):
    from app import models

    r = models.Render(project_id=pid, status=status, final_video_path=path)
    db.add(r)
    db.commit()
    return r


def _job(db, rid: int, **kw):
    from app import models

    kw.setdefault("title", "Video")
    kw.setdefault("status", "published")
    j = models.PublishJob(render_id=rid, platform="youtube", **kw)
    db.add(j)
    db.commit()
    return j


def _seed(db) -> dict:
    sb = _project(db, "Story one")
    amb = _project(db, "Rainy night", ambient=True)
    r1 = _render_row(db, sb.id)
    r2 = _render_row(db, sb.id)
    r3 = _render_row(db, amb.id)
    ids = {
        "pub": _job(db, r1.id, title="First cut", external_id="a1",
                    external_url="https://www.youtube.com/watch?v=a1").id,
        "fail": _job(db, r2.id, title="Second cut", status="failed", error="quotaExceeded").id,
        "sched": _job(db, r3.id, title="Rain 1 hour", status="scheduled", external_id="b2",
                      publish_at=datetime.utcnow() + timedelta(days=2)).id,
        "up": _job(db, r3.id, title="Rain again", status="uploading").id,
    }
    return {"sb": sb.id, "amb": amb.id, "r1": r1.id, "r2": r2.id, "r3": r3.id, **ids}


def test_list_filters_search_pagination_and_routing(auth_client):
    db = _db()
    s = _seed(db)
    db.close()
    page = auth_client.get("/api/publishing/jobs").json()
    assert [i["id"] for i in page["items"]] == [s["up"], s["sched"], s["fail"], s["pub"]]  # newest first
    assert page["counts"] == {"all": 4, "scheduled": 1, "published": 1, "failed": 1, "uploading": 1}
    by_id = {i["id"]: i for i in page["items"]}
    assert by_id[s["pub"]]["project_url"] == f"/projects/{s['sb']}/render"
    assert by_id[s["pub"]]["project_kind"] == "storyboard" and by_id[s["pub"]]["render_version"] == 1
    assert by_id[s["fail"]]["render_version"] == 2 and by_id[s["fail"]]["state"] == "failed"
    assert by_id[s["sched"]]["project_url"] == f"/ambient/{s['amb']}"
    assert by_id[s["sched"]]["state"] == "scheduled" and by_id[s["up"]]["state"] == "uploading"
    assert by_id[s["pub"]]["uploaded_at"] is not None and by_id[s["up"]]["uploaded_at"] is None

    for f, want in [("scheduled", s["sched"]), ("published", s["pub"]), ("failed", s["fail"]),
                    ("uploading", s["up"])]:
        got = auth_client.get(f"/api/publishing/jobs?status={f}").json()["items"]
        assert [i["id"] for i in got] == [want], f
    assert [i["id"] for i in auth_client.get("/api/publishing/jobs?q=rain").json()["items"]] == [s["up"], s["sched"]]
    assert [i["id"] for i in auth_client.get("/api/publishing/jobs?q=story").json()["items"]] == [s["fail"], s["pub"]]

    p1 = auth_client.get("/api/publishing/jobs?limit=3").json()
    assert len(p1["items"]) == 3 and p1["next_before_id"] == s["fail"]
    p2 = auth_client.get(f"/api/publishing/jobs?limit=3&before_id={p1['next_before_id']}").json()
    assert [i["id"] for i in p2["items"]] == [s["pub"]] and p2["next_before_id"] is None
    assert auth_client.get("/api/publishing/jobs?status=bogus").status_code == 422


def test_not_posted_lists_latest_render_per_project_without_upload(auth_client):
    db = _db()
    posted = _project(db, "Posted")
    _job(db, _render_row(db, posted.id).id, external_id="x")
    fresh = _project(db, "Never posted")
    _render_row(db, fresh.id)
    latest = _render_row(db, fresh.id)
    _render_row(db, fresh.id, status="failed")
    amb = _project(db, "Waves", ambient=True)
    ar = _render_row(db, amb.id)
    _job(db, ar.id, status="failed", error="boom")  # a failed attempt isn't "posted"
    _project(db, "Nothing rendered")
    ar, latest, amb, fresh = ar.id, latest.id, amb.id, fresh.id
    db.close()
    items = auth_client.get("/api/publishing/not-posted").json()["items"]
    assert [(i["render_id"], i["project_title"]) for i in items] == [(ar, "Waves"), (latest, "Never posted")]
    # The publish form of that exact render, not the project's newest render.
    assert items[0]["publish_url"] == f"/ambient/{amb}?render={ar}" and items[0]["failed_attempts"] == 1
    assert items[1]["publish_url"] == f"/projects/{fresh}/render?id={latest}" and items[1]["render_version"] == 2
    assert items[1]["file_available"] is False
    p1 = auth_client.get("/api/publishing/not-posted?limit=1").json()
    assert p1["next_before_id"] == ar
    assert [i["render_id"] for i in auth_client.get(
        f"/api/publishing/not-posted?before_id={ar}").json()["items"]] == [latest]


def _videos_mock(items_by_id: dict):
    calls = []

    def handler(request):
        q = parse_qs(urlparse(str(request.url)).query)
        ids = q["id"][0].split(",")
        calls.append({"ids": ids, "part": q["part"][0]})
        return httpx.Response(200, json={"items": [items_by_id[i] for i in ids if i in items_by_id]})

    respx.get(f"{API}/videos").mock(side_effect=handler)
    return calls


@respx.mock
def test_refresh_maps_every_youtube_state(auth_client, yt_env):
    from app.services import publish_state

    _connect(auth_client)
    db = _db()
    p = _project(db, "States")
    r = _render_row(db, p.id)
    later = datetime.utcnow() + timedelta(days=3)
    j = {
        "lock": _job(db, r.id, external_id="v-lock", privacy="public").id,
        "gone": _job(db, r.id, external_id="v-gone", privacy="unlisted", channel_id="UC123").id,
        "proc": _job(db, r.id, external_id="v-proc", privacy="public").id,
        "sched": _job(db, r.id, external_id="v-sched", status="scheduled", privacy="private",
                      publish_at=later.replace(microsecond=0)).id,
        "drop": _job(db, r.id, external_id="v-drop", status="scheduled", privacy="private",
                     publish_at=later.replace(microsecond=0)).id,
        "rej": _job(db, r.id, external_id="v-rej", privacy="public").id,
        "ok": _job(db, r.id, external_id="v-ok", privacy="public").id,
        "none": _job(db, r.id, status="uploading").id,
    }
    db.close()
    stamp = later.replace(microsecond=0).isoformat() + "Z"
    calls = _videos_mock({
        "v-lock": {"id": "v-lock", "status": {"privacyStatus": "private", "uploadStatus": "processed"},
                   "snippet": {"title": "Locked"}, "statistics": {"viewCount": "3"}},
        "v-proc": {"id": "v-proc", "status": {"privacyStatus": "public", "uploadStatus": "uploaded"}},
        "v-sched": {"id": "v-sched", "status": {"privacyStatus": "private", "uploadStatus": "processed",
                                                 "publishAt": stamp}},
        "v-drop": {"id": "v-drop", "status": {"privacyStatus": "private", "uploadStatus": "processed"}},
        "v-rej": {"id": "v-rej", "status": {"privacyStatus": "public", "uploadStatus": "rejected",
                                            "rejectionReason": "duplicate"}},
        "v-ok": {"id": "v-ok", "status": {"privacyStatus": "public", "uploadStatus": "processed"},
                 "snippet": {"title": "Renamed on YouTube"}},
    })
    r = auth_client.post("/api/publish-jobs/refresh", json={"ids": list(j.values())})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["checked"] == 7 and out["removed"] == 1 and out["skipped"] == 1
    assert len(calls) == 1 and "status" in calls[0]["part"] and "snippet" in calls[0]["part"]
    got = {i["id"]: i for i in out["items"]}
    assert got[j["lock"]]["state"] == "private" and got[j["lock"]]["warning"] == publish_state.PRIVATE_LOCK
    assert got[j["lock"]]["youtube_title"] == "Locked" and got[j["lock"]]["status"] == "published"
    assert got[j["gone"]]["state"] == "removed" and got[j["gone"]]["warning"] == "Removed on YouTube"
    assert got[j["proc"]]["state"] == "processing" and "Processing" in got[j["proc"]]["warning"]
    assert got[j["sched"]]["state"] == "scheduled" and got[j["sched"]]["warning"] == ""
    assert got[j["sched"]]["youtube_publish_at"].startswith(later.replace(microsecond=0).isoformat())
    assert got[j["drop"]]["state"] == "private" and "dropped the schedule" in got[j["drop"]]["warning"]
    assert got[j["rej"]]["state"] == "failed" and "duplicate" in got[j["rej"]]["error"]
    assert got[j["ok"]]["state"] == "published" and got[j["ok"]]["warning"] == ""
    assert got[j["none"]]["state"] == "uploading" and got[j["none"]]["checked_at"] is None
    # The on-demand refresh also kept today's numbers.
    from app import models

    db = _db()
    assert db.query(models.VideoStat).filter_by(publish_job_id=j["lock"]).one().views == 3
    db.close()
    # Refresh one.
    one = auth_client.post(f"/api/publish-jobs/{j['ok']}/refresh")
    assert one.status_code == 200 and one.json()["id"] == j["ok"]
    assert auth_client.post("/api/publish-jobs/99999/refresh").status_code == 404


@respx.mock
def test_refresh_batches_ids_by_fifty(auth_client, yt_env):
    _connect(auth_client)
    db = _db()
    p = _project(db, "Many")
    r = _render_row(db, p.id)
    ids = [_job(db, r.id, external_id=f"v{n}").id for n in range(120)]
    db.close()
    calls = _videos_mock({f"v{n}": {"id": f"v{n}", "status": {"privacyStatus": "public",
                                                               "uploadStatus": "processed"}} for n in range(120)})
    out = auth_client.post("/api/publish-jobs/refresh", json={"ids": ids}).json()
    assert out["checked"] == 120 and out["removed"] == 0
    assert [len(c["ids"]) for c in calls] == [50, 50, 20]
    assert len({i for c in calls for i in c["ids"]}) == 120


def test_refresh_needs_youtube(auth_client):
    db = _db()
    s = _seed(db)
    db.close()
    r = auth_client.post(f"/api/publish-jobs/{s['pub']}/refresh")
    assert r.status_code == 409 and "Connect YouTube" in r.json()["detail"]


def _upload_mocks(video_id: str):
    respx.post(f"{UP}/videos").mock(return_value=httpx.Response(200, headers={"Location": SESSION}))
    respx.put(SESSION).mock(return_value=httpx.Response(201, json={"id": video_id}))
    respx.post(f"{UP}/thumbnails/set").mock(return_value=httpx.Response(200, json={}))


@respx.mock
def test_upload_completion_records_youtubes_real_state(auth_client, yt_env):
    from app.services import publish_state

    _connect(auth_client)
    rid = _render(auth_client, size=1024)
    _upload_mocks("vlock")
    calls = _videos_mock({"vlock": {"id": "vlock", "status": {"privacyStatus": "private",
                                                                "uploadStatus": "uploaded"},
                                    "snippet": {"title": "Asked public"}}})
    r = auth_client.post(f"/api/renders/{rid}/publish/youtube", json={"title": "Asked public", "privacy": "public"})
    assert r.status_code == 202, r.text
    job = auth_client.get(f"/api/renders/{rid}/publish-jobs").json()[0]
    assert len(calls) == 1 and calls[0]["ids"] == ["vlock"]
    assert job["status"] == "published" and job["yt_privacy_status"] == "private"
    assert job["state"] == "processing" and job["warning"] == publish_state.PRIVATE_LOCK
    assert job["uploaded_at"] is not None and job["yt_checked_at"] is not None


@respx.mock
def test_upload_completion_survives_a_failed_state_check(auth_client, yt_env):
    _connect(auth_client)
    rid = _render(auth_client, size=1024)
    _upload_mocks("vok")
    respx.get(f"{API}/videos").mock(return_value=httpx.Response(500, text="backend error"))
    auth_client.post(f"/api/renders/{rid}/publish/youtube", json={"title": "x", "privacy": "public"})
    job = auth_client.get(f"/api/renders/{rid}/publish-jobs").json()[0]
    assert job["status"] == "published" and job["yt_checked_at"] is None and job["state"] == "published"


@respx.mock
def test_duplicate_guard_needs_confirmation(auth_client, yt_env):
    _connect(auth_client)
    rid = _render(auth_client, size=1024)
    _upload_mocks("vdup")
    _videos_mock({"vdup": {"id": "vdup", "status": {"privacyStatus": "unlisted", "uploadStatus": "processed"}}})
    assert auth_client.post(f"/api/renders/{rid}/publish/youtube", json={"title": "One"}).status_code == 202
    again = auth_client.post(f"/api/renders/{rid}/publish/youtube", json={"title": "Two"})
    assert again.status_code == 409
    assert again.json()["detail"] == ("This version is already on YouTube (https://www.youtube.com/watch?v=vdup). "
                                      "Upload it again as a separate video?")
    assert again.headers["X-Duplicate-Of"].isdigit()
    assert len(auth_client.get(f"/api/renders/{rid}/publish-jobs").json()) == 1
    ok = auth_client.post(f"/api/renders/{rid}/publish/youtube", json={"title": "Two", "confirm_duplicate": True})
    assert ok.status_code == 202
    assert len(auth_client.get(f"/api/renders/{rid}/publish-jobs").json()) == 2


@respx.mock
def test_duplicate_guard_covers_in_progress_but_not_failed_or_removed(auth_client, yt_env):
    from app import models

    _connect(auth_client)
    rid = _render(auth_client, size=1024)
    db = _db()
    jid = _job(db, rid, status="uploading").id
    db.close()
    r = auth_client.post(f"/api/renders/{rid}/publish/youtube", json={"title": "x"})
    assert r.status_code == 409 and "already uploading to YouTube" in r.json()["detail"]
    db = _db()
    job = db.get(models.PublishJob, jid)
    job.status, job.external_id, job.yt_removed = "published", "gone", True
    db.add(models.PublishJob(render_id=rid, status="failed", title="f"))
    db.commit()
    db.close()
    _upload_mocks("vnew")
    _videos_mock({})
    assert auth_client.post(f"/api/renders/{rid}/publish/youtube", json={"title": "x"}).status_code == 202

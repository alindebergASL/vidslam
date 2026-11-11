"""Live progress events: emitted at every transition (spot-checked per
kind), the SSE stream (snapshot, ids, resume, reset, auth, bounded), the
recent / read / active endpoints, and retention."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from .conftest import make_png_bytes

SHORT_AMBIENT = {"scene_preset": "waterfall", "loop_seconds": 5, "total_seconds": 20,
                 "crossfade_seconds": 1.0, "fade_seconds": 1.0, "music_mood": "spa"}


def _events(**filters) -> list:
    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        q = db.query(models.Event)
        for k, v in filters.items():
            q = q.filter(getattr(models.Event, k) == v)
        rows = q.order_by(models.Event.id).all()
        for r in rows:
            db.expunge(r)
        return rows


def _emit(n: int = 1, **kw) -> list[int]:
    from app.db import SessionLocal
    from app.services import events

    ids = []
    with SessionLocal() as db:
        for i in range(n):
            ev = events.emit(db, kw.get("kind", "studio_job"), "studio_job", i + 1, kw.get("status", "completed"),
                             kw.get("title", f"Studio: picture ready {i}"), url="/studio")
            db.flush()
            ids.append(ev.id)
        db.commit()
    return ids


def _project(client, title: str = "Beach day") -> int:
    aid = client.post("/api/avatars", json={"name": "Mia"}).json()["id"]
    client.post(f"/api/avatars/{aid}/assets", files={"file": ("h.png", make_png_bytes(), "image/png")},
                data={"asset_type": "hero", "rights_confirmed": "true"})
    return client.post("/api/projects", json={
        "title": title, "original_script": "one two three four five", "cta_text": "Go",
        "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
    }).json()["id"]


def _parse(body: str) -> list[dict]:
    """SSE frames → [{event, id, data}] (comments and retry lines dropped)."""
    out = []
    for frame in body.split("\n\n"):
        item: dict = {}
        for line in frame.splitlines():
            if line.startswith(":") or not line:
                continue
            key, _, value = line.partition(": ")
            if key == "data":
                item["data"] = json.loads(value)
            elif key in ("event", "id", "retry"):
                item[key] = value
        if "data" in item:
            out.append(item)
    return out


@pytest.fixture
def fast_stream(monkeypatch):
    from app.api import events as ev_api

    monkeypatch.setattr(ev_api, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(ev_api, "ALLOW_MAX_SECONDS", True)


# ---------- emitted at transitions ----------


def test_plan_and_render_emit_events(auth_client):
    pid = _project(auth_client, "Beach day")
    auth_client.post(f"/api/projects/{pid}/generate-plan")
    assert [e.status for e in _events(kind="plan")] == ["started", "completed"]
    assert _events(kind="plan")[-1].title == "Storyboard ready: Beach day"
    r = auth_client.post(f"/api/projects/{pid}/generate-video")
    assert r.status_code == 202, r.text
    evs = _events(kind="render")
    statuses = [e.status for e in evs]
    assert statuses[0] == "started" and statuses[-1] == "completed"
    assert "progress" in statuses
    details = [e.detail for e in evs if e.status == "progress"]
    assert "Making clips" in details and "Putting the video together" in details
    assert any(d.startswith("Clips: ") and " of " in d for d in details)
    done = evs[-1]
    assert done.title == "Video ready: Beach day"
    assert (done.url, done.project_id, done.entity_type) == (f"/projects/{pid}", pid, "render")
    assert all(e.project_id == pid for e in evs)


def test_render_failure_and_cancel_emit_events(auth_client, monkeypatch):
    from app.services import pipeline

    pid = _project(auth_client)
    auth_client.post(f"/api/projects/{pid}/generate-plan")

    def broken(*a, **k):
        raise RuntimeError("ffmpeg exploded\nlong trace")

    monkeypatch.setattr(pipeline, "compose", broken)
    auth_client.post(f"/api/projects/{pid}/generate-video")
    failed = _events(kind="render", status="failed")
    assert len(failed) == 1
    assert failed[0].title == "Render failed: ffmpeg exploded"
    assert failed[0].severity == "error"

    # Cancel: the transition is the cancel request itself.
    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        db.add(models.Render(project_id=pid, status="generating_shots"))
        db.get(models.VideoProject, pid).status = "generating"
        db.commit()
    assert auth_client.post(f"/api/projects/{pid}/cancel").json()["action"] == "signalled"
    assert [e.status for e in _events(kind="render", status="cancelled")] == ["cancelled"]


def test_studio_events(auth_client):
    aid = auth_client.post("/api/avatars", json={"name": "Mia"}).json()["id"]
    r = auth_client.post("/api/studio/generate-image", json={
        "owner_kind": "avatar", "owner_id": aid, "prompt": "a portrait", "variations": 2})
    assert r.status_code == 202
    evs = _events(kind="studio_job")
    assert [(e.status, e.title, e.url) for e in evs] == [("completed", "Studio: picture ready", "/studio")] * 2
    assert {e.entity_id for e in evs} == set(r.json()["batch_ids"])


def test_studio_failure_emits_and_alerts(auth_client, monkeypatch):
    from app.providers import mock_image
    from app.services import alerts

    def boom(self, **kw):
        raise RuntimeError("provider said no")

    monkeypatch.setattr(mock_image.MockImageProvider, "generate", boom)
    aid = auth_client.post("/api/avatars", json={"name": "Mia"}).json()["id"]
    auth_client.post("/api/studio/generate-image", json={"owner_kind": "avatar", "owner_id": aid, "prompt": "x"})
    evs = _events(kind="studio_job")
    assert [e.status for e in evs] == ["failed"]
    assert "provider said no" in evs[0].detail
    assert any(a["key"] == "studio-failed" for a in alerts.recent())


def test_ambient_events(auth_client):
    r = auth_client.post("/api/ambient", json={"config": SHORT_AMBIENT, "generate": True})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert [e.status for e in _events(kind="ambient_loop")] == ["started", "completed"]
    assert _events(kind="ambient_loop")[-1].url == f"/ambient/{sid}"
    assert auth_client.post(f"/api/ambient/{sid}/render").status_code == 202
    evs = _events(kind="ambient_render")
    assert [e.status for e in evs] == ["started", "completed"]
    assert evs[-1].title.startswith("Scene ready: ")


def test_training_events(auth_client):
    aid = auth_client.post("/api/avatars", json={"name": "Naina"}).json()["id"]
    ids = []
    for i in range(3):
        ids.append(auth_client.post(f"/api/avatars/{aid}/assets",
                                    files={"file": (f"i{i}.png", make_png_bytes(), "image/png")},
                                    data={"asset_type": "hero", "rights_confirmed": "true"}).json()["id"])
    r = auth_client.post(f"/api/cast/avatar/{aid}/train", json={
        "name": "Naina LoRA", "kind": "character_lora", "training_asset_ids": ids, "config": {}})
    assert r.status_code == 202, r.text
    evs = _events(kind="training")
    assert [e.status for e in evs] == ["started", "completed"]
    assert evs[-1].title == "Training done: Naina LoRA"
    assert evs[-1].url == f"/cast?avatar={aid}"


def test_publish_failure_emits(auth_client, monkeypatch):
    from app import models
    from app.db import SessionLocal
    from app.services import youtube
    from app.workers.jobs import publish_youtube_job

    pid = _project(auth_client)
    with SessionLocal() as db:
        render = models.Render(project_id=pid, status="completed")
        db.add(render)
        db.flush()
        job = models.PublishJob(render_id=render.id, title="My video", status="queued")
        db.add(job)
        db.commit()
        jid = job.id

    def boom(db, job):
        raise RuntimeError("quota exceeded")

    monkeypatch.setattr(youtube, "upload", boom)
    publish_youtube_job(jid)
    evs = _events(kind="publish")
    assert [(e.status, e.title, e.url) for e in evs] == [("failed", "YouTube upload failed: My video", "/publishing")]


def test_publish_success_with_youtube_warning(auth_client):
    from app import models
    from app.db import SessionLocal
    from app.services import events

    pid = _project(auth_client)
    with SessionLocal() as db:
        render = models.Render(project_id=pid, status="completed")
        db.add(render)
        db.flush()
        job = models.PublishJob(render_id=render.id, title="My video", status="published")
        db.add(job)
        db.flush()
        events.publish_event(db, job, "completed", warning="YouTube kept it private")
        db.commit()
    ev = _events(kind="publish")[0]
    assert (ev.title, ev.severity) == ("Uploaded to YouTube: My video", "warning")


def test_budget_reached_emits(auth_client):
    from app.db import SessionLocal
    from app.services import spend

    with SessionLocal() as db:
        spend.set_budget(db, monthly_usd=1.0, hard_stop=True, alert_pcts=[100])
    spend.record(provider="openrouter", kind="video", model="m", cost=1.5)
    evs = _events(kind="budget")
    assert len(evs) == 1
    assert evs[0].status == "failed" and evs[0].title.startswith("Monthly budget reached")


def test_emit_never_raises():
    from app.services import events

    class Broken:
        def add(self, _):
            raise RuntimeError("db gone")

    assert events.emit(Broken(), "render", "render", 1, "completed", "x") is None


# ---------- stream ----------


def test_stream_requires_login(client):
    assert client.get("/api/events/stream").status_code == 401
    assert client.get("/api/events/recent").status_code == 401
    assert client.get("/api/activity/active").status_code == 401


def test_stream_fresh_connect_sends_snapshot_then_new_events(auth_client, fast_stream):
    old = _emit(2)
    r = auth_client.get("/api/events/stream", params={"max_seconds": 0.3})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache"
    assert r.headers["x-accel-buffering"] == "no"
    assert "retry: 5000" in r.text
    frames = _parse(r.text)
    assert frames[0]["event"] == "snapshot"
    assert frames[0]["data"]["latest_id"] == old[-1]
    assert "items" in frames[0]["data"]["active"]
    assert len(frames) == 1  # events from before the connect are not replayed


def test_stream_resumes_after_last_event_id(auth_client, fast_stream):
    ids = _emit(3)
    r = auth_client.get("/api/events/stream", params={"max_seconds": 0.3},
                        headers={"Last-Event-ID": str(ids[0])})
    frames = _parse(r.text)
    assert [f.get("event") for f in frames] == [None, None]
    assert [int(f["id"]) for f in frames] == ids[1:]
    assert frames[0]["data"]["title"] == "Studio: picture ready 1"
    assert frames[0]["data"]["url"] == "/studio"
    # ?after= works the same.
    r = auth_client.get("/api/events/stream", params={"max_seconds": 0.3, "after": ids[1]})
    assert [int(f["id"]) for f in _parse(r.text)] == ids[2:]


def test_stream_resets_when_the_resume_point_was_pruned(auth_client, fast_stream):
    from app import models
    from app.db import SessionLocal

    ids = _emit(5)
    with SessionLocal() as db:
        db.query(models.Event).filter(models.Event.id < ids[3]).delete()
        db.commit()
    r = auth_client.get("/api/events/stream", params={"max_seconds": 0.3},
                        headers={"Last-Event-ID": str(ids[0])})
    frames = _parse(r.text)
    assert [f.get("event") for f in frames][:2] == ["reset", "snapshot"]
    assert frames[0]["data"]["latest_id"] == ids[-1]
    # A resume id beyond the newest (a reset database) resets too.
    r = auth_client.get("/api/events/stream", params={"max_seconds": 0.3}, headers={"Last-Event-ID": "99999"})
    assert _parse(r.text)[0]["event"] == "reset"


def test_max_seconds_is_ignored_outside_tests(auth_client, monkeypatch):
    from app.api import events as ev_api

    monkeypatch.setattr(ev_api, "ALLOW_MAX_SECONDS", False)
    monkeypatch.setattr(ev_api, "POLL_SECONDS", 0.01)

    # Without the bound the generator would run forever; stop it after the
    # first poll by making the disconnect check say the client left.
    calls = {"n": 0}

    async def gone(self):
        calls["n"] += 1
        return calls["n"] > 3

    from starlette.requests import Request

    monkeypatch.setattr(Request, "is_disconnected", gone)
    r = auth_client.get("/api/events/stream", params={"max_seconds": 0.01})
    assert r.status_code == 200 and calls["n"] > 3


# ---------- recent / read / active ----------


def test_recent_read_and_unread_count(auth_client):
    ids = _emit(3)
    _emit(1, status="progress", title="half way")
    r = auth_client.get("/api/events/recent", params={"limit": 2}).json()
    assert [e["id"] for e in r["events"]] == [ids[2] + 1, ids[2]]
    assert r["has_more"] is True
    assert r["unread"] == 3  # progress events don't count toward the badge
    more = auth_client.get("/api/events/recent", params={"before_id": ids[2]}).json()
    assert [e["id"] for e in more["events"]] == [ids[1], ids[0]]
    only = auth_client.get("/api/events/recent", params={"status": "progress"}).json()
    assert [e["title"] for e in only["events"]] == ["half way"]
    r = auth_client.post("/api/events/read", json={"ids": [ids[0]]}).json()
    assert r == {"updated": 1, "unread": 2}
    assert auth_client.post("/api/events/read", json={"all": True}).json()["unread"] == 0
    assert all(e["read"] for e in auth_client.get("/api/events/recent").json()["events"])


def test_activity_active_lists_running_work(auth_client):
    from app import models
    from app.db import SessionLocal

    pid = _project(auth_client, "Beach day")
    with SessionLocal() as db:
        db.add(models.Render(project_id=pid, status="generating_audio"))
        for _ in range(3):
            db.add(models.AssetGenerationJob(owner_kind="avatar", owner_id=1, prompt="p", status="polling",
                                             output_kind="video_clip"))
        db.add(models.CustomModel(name="Lora", owner_kind="avatar", owner_id=4, status="training", progress=0.5))
        db.commit()
    out = auth_client.get("/api/activity/active").json()
    kinds = {i["kind"]: i for i in out["items"]}
    assert kinds["render"]["title"] == "Beach day"
    assert kinds["render"]["url"] == f"/projects/{pid}"
    assert kinds["render"]["stage"] == "Making the voiceover"
    assert kinds["studio_job"]["title"] == "Studio: 3 jobs running"
    assert out["studio"]["count"] == 3 and len(out["studio"]["jobs"]) == 3
    assert kinds["training"]["stage"] == "Training (50%)"
    assert kinds["training"]["url"] == "/cast?avatar=4"
    assert out["counts"] == {"projects": 1, "ambient": 0, "studio": 3, "training": 1, "publish": 0}


# ---------- retention ----------


def test_retention_deletes_events_older_than_30_days(auth_client):
    from app import models
    from app.db import SessionLocal
    from app.services import events

    ids = _emit(2)
    with SessionLocal() as db:
        db.get(models.Event, ids[0]).created_at = datetime.utcnow() - timedelta(days=31)
        db.commit()
        assert events.prune(db) == 1
    assert [e.id for e in _events()] == [ids[1]]


def test_sweeper_prunes_once_a_day(auth_client, monkeypatch):
    from app import models
    from app.db import SessionLocal
    from app.services import sweepers

    ids = _emit(1)
    with SessionLocal() as db:
        db.get(models.Event, ids[0]).created_at = datetime.utcnow() - timedelta(days=40)
        db.commit()
    monkeypatch.setattr(sweepers, "_last_prune", 0.0)
    assert sweepers.run_once()["pruned_events"] == 1
    assert "pruned_events" not in sweepers.run_once()


def test_cursor_waits_for_an_id_committed_late():
    from app.api.events import GAP_WAIT_SECONDS, Cursor

    c = Cursor(40)
    assert c.take({"id": 42}, 0.0)  # 41 is still in an open transaction
    assert c.last == 40
    assert c.take({"id": 41}, 1.0)  # committed late: still sent
    assert c.last == 42
    assert not c.take({"id": 41}, 1.0) and not c.take({"id": 42}, 1.0)  # never twice
    # A gap that never fills (a rolled-back insert) is passed after a while.
    assert c.take({"id": 44}, 2.0)
    assert c.last == 42
    c.advance(2.0 + GAP_WAIT_SECONDS)
    assert c.last == 44


def test_stream_sends_an_event_committed_out_of_id_order(auth_client, fast_stream, monkeypatch):
    from app.api import events as ev_api

    ids = _emit(3)
    real = ev_api._fetch_after
    calls = {"n": 0}

    def fetch(last, high=None):  # the middle row's transaction commits after the first poll
        calls["n"] += 1
        return [r for r in real(last, high) if not (calls["n"] == 1 and r["id"] == ids[1])]

    monkeypatch.setattr(ev_api, "_fetch_after", fetch)
    r = auth_client.get("/api/events/stream", params={"max_seconds": 0.3},
                        headers={"Last-Event-ID": str(ids[0] - 1)})
    frames = _parse(r.text)
    assert sorted(f["data"]["id"] for f in frames) == ids
    # The resume id (SSE id) never moves past the late event before it is sent.
    assert [int(f["id"]) for f in frames] == [ids[0], ids[0], ids[2]]

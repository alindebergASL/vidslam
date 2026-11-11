"""Double-paid and stuck renders: generate-video / retry / generate-plan and
ambient render claim the project before queueing (409 for a second
request), give the claim back when the queue refuses, and the sweeper
fails renders a dead worker left running."""
from __future__ import annotations

from datetime import datetime, timedelta

from .conftest import make_png_bytes

SHORT_AMBIENT = {"scene_preset": "waterfall", "loop_seconds": 5, "total_seconds": 20,
                 "crossfade_seconds": 1.0, "fade_seconds": 1.0, "music_mood": "spa"}


def _project(client) -> int:
    aid = client.post("/api/avatars", json={"name": "Mia"}).json()["id"]
    client.post(f"/api/avatars/{aid}/assets", files={"file": ("h.png", make_png_bytes(), "image/png")},
                data={"asset_type": "hero", "rights_confirmed": "true"})
    pid = client.post("/api/projects", json={
        "title": "Beach day", "original_script": "one two three four five", "cta_text": "Go",
        "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
    }).json()["id"]
    assert client.post(f"/api/projects/{pid}/generate-plan").status_code == 202
    return pid


def _status(pid: int) -> str:
    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        return db.get(models.VideoProject, pid).status


def _renders(pid: int) -> list:
    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        rows = db.query(models.Render).filter(models.Render.project_id == pid).order_by(models.Render.id).all()
        for r in rows:
            db.expunge(r)
        return rows


def _hold(monkeypatch) -> list:
    """From now on the queue accepts jobs but no worker runs them yet."""
    from app.api import ambient, generation

    queued: list = []

    def hold(fn, *a, **k):
        queued.append((fn, a, k))
        return "rq:held"

    monkeypatch.setattr(generation, "enqueue", hold)
    monkeypatch.setattr(ambient, "enqueue", hold)
    return queued


def _queue_down(monkeypatch) -> None:
    from app.api import ambient, generation
    from app.workers.queue import QueueUnavailable

    def down(*a, **k):
        raise QueueUnavailable("job queue (Redis) unavailable and QUEUE_MODE=required")

    monkeypatch.setattr(generation, "enqueue", down)
    monkeypatch.setattr(ambient, "enqueue", down)


def test_double_generate_video_queues_one_render(auth_client, monkeypatch):
    pid = _project(auth_client)
    held = _hold(monkeypatch)
    first = auth_client.post(f"/api/projects/{pid}/generate-video")
    assert first.status_code == 202, first.text
    assert _status(pid) == "generating"  # the editor keeps polling while the job waits
    second = auth_client.post(f"/api/projects/{pid}/generate-video")
    assert second.status_code == 409
    assert second.json()["detail"] == "Already making this video"
    assert auth_client.post(f"/api/projects/{pid}/retry").status_code == 409
    assert auth_client.post(f"/api/projects/{pid}/generate-plan").status_code == 409
    assert len(held) == 1
    fn, args, kw = held[0]
    fn(*args, **kw)  # the worker picks it up
    assert len(_renders(pid)) == 1
    assert _renders(pid)[0].status == "completed"
    assert _status(pid) == "completed"


def test_double_generate_plan_is_refused(auth_client, monkeypatch):
    pid = _project(auth_client)
    held = _hold(monkeypatch)
    assert auth_client.post(f"/api/projects/{pid}/generate-plan").status_code == 202
    assert _status(pid) == "planning"
    r = auth_client.post(f"/api/projects/{pid}/generate-plan")
    assert r.status_code == 409 and r.json()["detail"] == "Already planning this storyboard"
    assert len(held) == 1


def test_retry_claims_too(auth_client, monkeypatch):
    from app import models
    from app.db import SessionLocal

    pid = _project(auth_client)
    held = _hold(monkeypatch)
    with SessionLocal() as db:
        db.get(models.VideoProject, pid).status = "failed"
        db.add(models.Render(project_id=pid, status="failed", error="boom"))
        db.commit()
    assert auth_client.post(f"/api/projects/{pid}/retry").status_code == 202
    assert _status(pid) == "generating"
    assert auth_client.post(f"/api/projects/{pid}/retry").status_code == 409
    assert len(held) == 1


def test_enqueue_failure_gives_the_claim_back(auth_client, monkeypatch):
    pid = _project(auth_client)
    _queue_down(monkeypatch)
    assert _status(pid) == "planned"
    assert auth_client.post(f"/api/projects/{pid}/generate-video").status_code == 503
    assert _status(pid) == "planned"
    assert auth_client.post(f"/api/projects/{pid}/generate-plan").status_code == 503
    assert _status(pid) == "planned"


def test_ambient_double_render_is_refused(auth_client, monkeypatch):
    from app.api import ambient

    sid = auth_client.post("/api/ambient", json={"config": SHORT_AMBIENT, "generate": True}).json()["id"]
    queued: list = []
    monkeypatch.setattr(ambient, "enqueue", lambda fn, *a, **k: queued.append((fn, a)) or "rq:held")
    assert auth_client.post(f"/api/ambient/{sid}/render").status_code == 202
    r = auth_client.post(f"/api/ambient/{sid}/render")
    assert r.status_code == 409
    assert len(queued) == 1
    fn, args = queued[0]
    fn(*args)
    assert auth_client.get(f"/api/ambient/{sid}").json()["status"] == "completed"


def test_ambient_render_enqueue_failure_gives_the_claim_back(auth_client):
    sid = auth_client.post("/api/ambient", json={"config": SHORT_AMBIENT, "generate": True}).json()["id"]
    from app.api import ambient
    from app.workers.queue import QueueUnavailable

    def down(*a, **k):
        raise QueueUnavailable("down")

    orig = ambient.enqueue
    ambient.enqueue = down
    try:
        assert auth_client.post(f"/api/ambient/{sid}/render").status_code == 503
    finally:
        ambient.enqueue = orig
    assert _status(sid) == "planned"


# ---------- sweeper ----------


def _age_render(rid: int, by: timedelta) -> None:
    from sqlalchemy import update

    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        db.execute(update(models.Render).where(models.Render.id == rid)
                   .values(updated_at=datetime.utcnow() - by))
        db.commit()


def test_sweeper_fails_a_stuck_render_and_retry_works(auth_client):
    from app import models
    from app.config import get_settings
    from app.db import SessionLocal
    from app.services import sweepers

    pid = _project(auth_client)
    with SessionLocal() as db:
        r = models.Render(project_id=pid, status="rendering")
        db.add(r)
        db.get(models.VideoProject, pid).status = "generating"
        fresh = models.Render(project_id=pid, status="generating_audio")
        db.add(fresh)
        db.commit()
        rid, fid = r.id, fresh.id
    _age_render(rid, timedelta(seconds=get_settings().queue_job_timeout_seconds + 11 * 60))
    with SessionLocal() as db:
        out = sweepers.sweep_renders(db)
    assert out["failed"] == 1
    rows = {r.id: r for r in _renders(pid)}
    assert rows[rid].status == "failed"
    assert rows[rid].error == "Stopped unexpectedly; Retry continues from where it stopped"
    assert rows[fid].status == "generating_audio"
    assert _status(pid) == "failed"
    with SessionLocal() as db:
        ev = db.query(models.Event).filter(models.Event.kind == "render", models.Event.status == "failed").one()
        assert ev.entity_id == rid and ev.project_id == pid
        db.get(models.Render, fid).status = "failed"
        db.commit()
    assert auth_client.post(f"/api/projects/{pid}/retry").status_code == 202


def test_sweeper_composes_when_every_clip_is_back(auth_client):
    from app import models
    from app.db import SessionLocal
    from app.services import sweepers

    pid = _project(auth_client)
    auth_client.post(f"/api/projects/{pid}/generate-video")
    with SessionLocal() as db:
        r = models.Render(project_id=pid, status="generating_shots",
                          audio_path=_renders(pid)[0].audio_path)
        db.add(r)
        db.get(models.VideoProject, pid).status = "generating"
        db.commit()
        rid = r.id
    _age_render(rid, timedelta(minutes=3))
    with SessionLocal() as db:
        assert sweepers.sweep_renders(db)["composed"] == 1
    assert {r.id: r for r in _renders(pid)}[rid].status == "completed"


def test_sweeper_releases_a_claim_whose_job_never_ran(auth_client):
    from sqlalchemy import update

    from app import models
    from app.config import get_settings
    from app.db import SessionLocal
    from app.services import sweepers

    pid = _project(auth_client)
    with SessionLocal() as db:
        db.execute(update(models.VideoProject).where(models.VideoProject.id == pid).values(
            status="generating",
            updated_at=datetime.utcnow() - timedelta(seconds=get_settings().queue_job_timeout_seconds + 11 * 60)))
        db.commit()
        assert sweepers.sweep_renders(db)["projects"] == 1
    assert _status(pid) == "failed"


# ---------- a claim waiting in the queue ----------


def test_cancel_releases_a_claim_whose_job_is_still_queued(auth_client, monkeypatch):
    pid = _project(auth_client)
    held = _hold(monkeypatch)
    assert auth_client.post(f"/api/projects/{pid}/generate-video").status_code == 202
    r = auth_client.post(f"/api/projects/{pid}/cancel")
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "released"
    assert _status(pid) == "cancelled"
    fn, args, kw = held[0]
    fn(*args, **kw)  # the worker reaches it later: nothing is made or paid for
    assert _renders(pid) == []
    assert _status(pid) == "cancelled"
    assert auth_client.post(f"/api/projects/{pid}/generate-video").status_code == 202


def test_a_job_swept_as_failed_does_nothing_when_it_runs(auth_client, monkeypatch):
    from app import models
    from app.db import SessionLocal

    pid = _project(auth_client)
    held = _hold(monkeypatch)
    assert auth_client.post(f"/api/projects/{pid}/generate-video").status_code == 202
    with SessionLocal() as db:
        assert db.get(models.VideoProject, pid).queue_job_id == "rq:held"
        db.get(models.VideoProject, pid).status = "failed"  # the sweeper gave up on it
        db.commit()
    fn, args, kw = held[0]
    fn(*args, **kw)
    assert _renders(pid) == [] and _status(pid) == "failed"


def test_render_that_fails_before_it_starts_gives_the_claim_back(auth_client, monkeypatch):
    import pytest

    from app import models
    from app.db import SessionLocal

    pid = _project(auth_client)
    held = _hold(monkeypatch)
    assert auth_client.post(f"/api/projects/{pid}/generate-video").status_code == 202
    with SessionLocal() as db:
        db.get(models.VideoProject, pid).generated_plan_json = {"shots": "not a list"}
        db.commit()
    fn, args, kw = held[0]
    with pytest.raises(ValueError):
        fn(*args, **kw)
    assert _renders(pid) == [] and _status(pid) == "failed"


def test_sweeper_leaves_a_claim_alone_while_its_job_is_queued(auth_client, monkeypatch):
    from sqlalchemy import update

    from app import models
    from app.config import get_settings
    from app.db import SessionLocal
    from app.services import sweepers
    from app.workers import queue

    state = {"v": "alive"}
    monkeypatch.setattr(queue, "queue_entry_state", lambda qid: state["v"])
    pid = _project(auth_client)
    with SessionLocal() as db:
        db.execute(update(models.VideoProject).where(models.VideoProject.id == pid).values(
            status="generating", queue_job_id="rq-1",
            updated_at=datetime.utcnow() - timedelta(seconds=get_settings().queue_job_timeout_seconds + 11 * 60)))
        db.commit()
        assert sweepers.sweep_renders(db)["projects"] == 0
        state["v"] = "unknown"
        assert sweepers.sweep_renders(db)["projects"] == 0
        state["v"] = "gone"
        assert sweepers.sweep_renders(db)["projects"] == 1
    assert _status(pid) == "failed"


def test_queue_entry_state_without_an_rq_id():
    from app.workers.queue import queue_entry_state

    assert queue_entry_state("") == "gone"
    assert queue_entry_state("inline:generate_video_job") == "gone"


# ---------- each claim has its own token ----------


def test_a_cancelled_entry_never_runs_under_a_newer_claim(auth_client, monkeypatch):
    pid = _project(auth_client)
    held = _hold(monkeypatch)
    assert auth_client.post(f"/api/projects/{pid}/generate-video").status_code == 202
    assert auth_client.post(f"/api/projects/{pid}/cancel").json()["action"] == "released"
    assert auth_client.post(f"/api/projects/{pid}/generate-video").status_code == 202
    assert _status(pid) == "generating"
    old_fn, old_args, old_kw = held[0]
    new_fn, new_args, new_kw = held[1]
    assert old_kw["claim_token"] and old_kw["claim_token"] != new_kw["claim_token"]
    old_fn(*old_args, **old_kw)  # same status ("generating"), old token: does nothing
    assert _renders(pid) == []
    new_fn(*new_args, **new_kw)
    assert len(_renders(pid)) == 1
    new_fn(*new_args, **new_kw)  # the same entry delivered twice: the token is used up
    assert len(_renders(pid)) == 1


def test_a_plan_cancelled_while_the_planner_runs_is_not_written(auth_client, monkeypatch):
    from app import models
    from app.db import SessionLocal
    from app.providers.mock_chat import MockChatProvider

    pid = _project(auth_client)
    with SessionLocal() as db:
        before = db.get(models.VideoProject, pid).generated_plan_json
        shots_before = len(db.get(models.VideoProject, pid).shots)
    held = _hold(monkeypatch)
    assert auth_client.post(f"/api/projects/{pid}/generate-plan").status_code == 202
    orig = MockChatProvider.generate_storyboard

    def slow_planner(self, *a, **k):
        # The user presses Cancel while the planner is still answering.
        assert auth_client.post(f"/api/projects/{pid}/cancel").json()["action"] == "released"
        plan = orig(self, *a, **k)
        plan.cleaned_voice_script = "A plan nobody wants any more."
        return plan

    monkeypatch.setattr(MockChatProvider, "generate_storyboard", slow_planner)
    fn, args, kw = held[0]
    assert fn(*args, **kw) is None
    assert _status(pid) == "cancelled"
    with SessionLocal() as db:
        p = db.get(models.VideoProject, pid)
        assert p.generated_plan_json == before and len(p.shots) == shots_before
        assert p.claim_token == ""


def test_a_failed_planner_after_cancel_keeps_the_cancel(auth_client, monkeypatch):
    from app.providers.mock_chat import MockChatProvider

    pid = _project(auth_client)
    held = _hold(monkeypatch)
    assert auth_client.post(f"/api/projects/{pid}/generate-plan").status_code == 202

    def broken(self, *a, **k):
        auth_client.post(f"/api/projects/{pid}/cancel")
        raise RuntimeError("planner timed out")

    monkeypatch.setattr(MockChatProvider, "generate_storyboard", broken)
    fn, args, kw = held[0]
    import pytest

    with pytest.raises(RuntimeError):
        fn(*args, **kw)
    assert _status(pid) == "cancelled"


def test_cancel_after_the_worker_took_the_claim_goes_through_the_render(auth_client, monkeypatch):
    from app import models
    from app.db import SessionLocal
    from app.services import pipeline

    pid = _project(auth_client)
    held = _hold(monkeypatch)
    assert auth_client.post(f"/api/projects/{pid}/generate-plan").status_code == 202
    fn, args, kw = held[0]
    with SessionLocal() as db:  # a worker has consumed the claim but not committed its result yet
        assert pipeline.consume_claim(db, pid, "planning", kw["claim_token"])
        db.commit()
    r = auth_client.post(f"/api/projects/{pid}/cancel")
    assert r.status_code == 409  # nothing to release: the job owns it now
    with SessionLocal() as db:
        assert db.get(models.VideoProject, pid).status == "planning"


def test_ambient_render_cancelled_while_queued_does_nothing(auth_client, monkeypatch):
    sid = auth_client.post("/api/ambient", json={"config": SHORT_AMBIENT, "generate": True}).json()["id"]
    renders_before = len(_renders(sid))
    held = _hold(monkeypatch)
    assert auth_client.post(f"/api/ambient/{sid}/render").status_code == 202
    r = auth_client.post(f"/api/projects/{sid}/cancel")
    assert r.status_code == 200 and r.json()["action"] == "released", r.text
    fn, args, kw = held[0]
    assert kw.get("claim_token")
    fn(*args, **kw)
    assert len(_renders(sid)) == renders_before
    assert _status(sid) == "cancelled"
    # A new render after the cancel runs normally.
    assert auth_client.post(f"/api/ambient/{sid}/render").status_code == 202
    fn, args, kw = held[1]
    fn(*args, **kw)
    assert _status(sid) == "completed"


def test_ambient_generate_cancelled_while_queued_does_nothing(auth_client, monkeypatch):
    from app import models
    from app.db import SessionLocal

    sid = auth_client.post("/api/ambient", json={"config": SHORT_AMBIENT}).json()["id"]
    with SessionLocal() as db:
        clips_before = [(s.id, s.clip_path) for s in db.get(models.VideoProject, sid).shots]
        jobs_before = db.query(models.VideoJob).count()
    held = _hold(monkeypatch)
    assert auth_client.post(f"/api/ambient/{sid}/generate-loop").status_code == 202
    assert auth_client.post(f"/api/projects/{sid}/cancel").json()["action"] == "released"
    fn, args, kw = held[0]
    fn(*args, **kw)
    with SessionLocal() as db:
        p = db.get(models.VideoProject, sid)
        assert p.status == "cancelled"
        assert [(s.id, s.clip_path) for s in p.shots] == clips_before
        assert db.query(models.VideoJob).count() == jobs_before


def test_an_expired_newest_render_does_not_lock_the_project(auth_client):
    """Retention marks old renders "expired". The newest one expiring must
    not leave the project looking busy: not listed as active, edits and
    Generate allowed, and the sweeper still sees nothing running."""
    from app import models
    from app.db import SessionLocal
    from app.services import pipeline, sweepers

    client = auth_client
    pid = _project(client)
    assert client.post(f"/api/projects/{pid}/generate-video").status_code == 202
    assert _renders(pid)[-1].status == "completed"
    with SessionLocal() as db:
        r = db.get(models.Render, _renders(pid)[-1].id)
        r.status = "expired"
        db.commit()

    active = client.get("/api/activity/active").json()
    assert not [i for i in active["items"] if i.get("project_id") == pid]
    with SessionLocal() as db:
        assert pipeline.busy_reason(db, db.get(models.VideoProject, pid)) is None
    assert client.get(f"/api/projects/{pid}/status").json()["project_status"] == "completed"

    # A claim whose job never ran is still swept, even with an expired render.
    with SessionLocal() as db:
        db.query(models.VideoProject).filter(models.VideoProject.id == pid).update(
            {"status": "generating", "updated_at": datetime.utcnow() - timedelta(days=1)})
        db.commit()
    out = sweepers.sweep_renders(SessionLocal(), now=datetime.utcnow())
    assert out["projects"] == 1 and _status(pid) == "failed"

    # And Generate works again.
    assert client.post(f"/api/projects/{pid}/generate-video").status_code == 202
    assert _renders(pid)[-1].status == "completed"

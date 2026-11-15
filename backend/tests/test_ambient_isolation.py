"""Ambient scenes stay away from the storyboard tools, and a scene stuck in
"generating" can be given back.

An Ambient scene's loop clip is a shot row like a storyboard shot, so the
storyboard editor's re-roll, recompose and shot edits used to reach it: the
re-roll sent no scene picture (an unrelated clip replaced the loop) and the
recompose raised for want of a storyboard plan, leaving the scene stuck in
"generating" with its claim consumed, so Cancel did nothing.
"""
from __future__ import annotations

import pytest
from sqlalchemy import update

from .conftest import make_png_bytes

SHORT = {
    "scene_preset": "waterfall",
    "loop_seconds": 5,
    "total_seconds": 20,
    "crossfade_seconds": 1.0,
    "fade_seconds": 1.0,
    "music_mood": "none",
}


def _scene(auth_client) -> dict:
    r = auth_client.post("/api/ambient", json={"config": SHORT, "generate": True})
    assert r.status_code == 201, r.text
    scene = r.json()
    assert scene["status"] == "planned" and scene["loop_status"] == "completed", scene
    return scene


def _state(pid: int) -> tuple[str, int]:
    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        p = db.get(models.VideoProject, pid)
        jobs = db.query(models.VideoJob).filter(models.VideoJob.project_id == pid).count()
        return p.status, jobs


def _set(pid: int, **values) -> None:
    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        db.execute(update(models.VideoProject).where(models.VideoProject.id == pid).values(**values))
        db.commit()


def _storyboard(auth_client, *, plan: bool = True) -> int:
    aid = auth_client.post("/api/avatars", json={"name": "X"}).json()["id"]
    auth_client.post(
        f"/api/avatars/{aid}/assets",
        files={"file": ("h.png", make_png_bytes(), "image/png")},
        data={"asset_type": "hero", "rights_confirmed": "true"},
    )
    pid = auth_client.post(
        "/api/projects",
        json={"title": "S", "original_script": "one two three four five", "cta_text": "Go",
              "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}]},
    ).json()["id"]
    if plan:
        assert auth_client.post(f"/api/projects/{pid}/generate-plan").status_code == 202
    return pid


# ---------- 1. the storyboard routes refuse an Ambient scene ----------


def test_storyboard_routes_refuse_an_ambient_scene(auth_client):
    scene = _scene(auth_client)
    sid, shot = scene["id"], scene["loop_shot"]["id"]
    before = _state(sid)
    calls = [
        ("post", f"/api/projects/{sid}/shots/{shot}/regenerate", None),
        ("post", f"/api/projects/{sid}/shots/{shot}/regenerate?recompose=false", None),  # "clip only"
        ("post", f"/api/projects/{sid}/shots/regenerate-bulk", {"shot_ids": [shot]}),
        ("post", f"/api/projects/{sid}/generate-plan", None),
        ("post", f"/api/projects/{sid}/generate-video?force=true", None),
        ("post", f"/api/projects/{sid}/recompose", None),
        ("post", f"/api/projects/{sid}/retry", None),
        ("patch", f"/api/projects/{sid}/shots/{shot}", {"model_override": "mock/other"}),
        ("patch", f"/api/projects/{sid}/plan", {"end_card_text": "x"}),
        ("post", f"/api/projects/{sid}/shots/reorder", {"shot_ids": [shot]}),
        ("post", f"/api/projects/{sid}/shots", {"shot_type": "b_roll", "prompt": "a river"}),
        ("post", f"/api/projects/{sid}/shots/{shot}/duplicate", None),
        ("delete", f"/api/projects/{sid}/shots/{shot}", None),
        ("get", f"/api/projects/{sid}/shots/{shot}/takes", None),
        ("post", f"/api/projects/{sid}/music/generate", {"prompt": "calm", "duration_seconds": 3600}),
    ]
    for method, url, body in calls:
        kw = {"json": body} if body is not None else {}
        r = getattr(auth_client, method)(url, **kw)
        assert r.status_code == 409, (url, r.status_code, r.text)
        assert "Ambient" in r.json()["detail"], (url, r.text)
        assert _state(sid) == before, url
    assert f"/api/ambient/{sid}/music" in auth_client.post(
        f"/api/projects/{sid}/music/generate", json={"prompt": "calm"}).json()["detail"]
    # The loop clip is the same one, still the scene's.
    after = auth_client.get(f"/api/ambient/{sid}").json()
    assert after["loop_shot"]["clip_path"] == scene["loop_shot"]["clip_path"]
    assert after["loop_shot"].get("model_override") in (None, "", scene["loop_shot"].get("model_override"))


def test_what_the_ambient_pages_use_stays_open(auth_client):
    scene = _scene(auth_client)
    sid, shot = scene["id"], scene["loop_shot"]["id"]
    assert auth_client.get(f"/api/projects/{sid}").status_code == 200
    assert auth_client.get(f"/api/projects/{sid}/shots/{shot}/clip").status_code == 200
    assert auth_client.patch(f"/api/projects/{sid}", json={"title": "Renamed"}).status_code == 200
    assert auth_client.get(f"/api/projects/{sid}/publish-meta").status_code == 200
    assert auth_client.post(f"/api/ambient/{sid}/render").status_code == 202
    assert auth_client.get(f"/api/projects/{sid}/renders").status_code == 200


# ---------- 2. pipeline backstops ----------


@pytest.mark.parametrize("bulk", [False, True])
def test_pipeline_regenerate_refuses_an_ambient_scene(auth_client, bulk):
    from app.db import SessionLocal
    from app.services import pipeline

    scene = _scene(auth_client)
    sid, shot = scene["id"], scene["loop_shot"]["id"]
    before = _state(sid)
    with SessionLocal() as db, pytest.raises(ValueError):
        if bulk:
            pipeline.regenerate_shots_bulk(db, sid, [shot])
        else:
            pipeline.regenerate_shot(db, sid, shot, recompose=True)
    assert _state(sid) == before


# ---------- 3. recompose with no plan does not leave "generating" ----------


def test_recompose_without_a_plan_gives_an_ambient_scene_back(auth_client):
    from app.db import SessionLocal
    from app.services import pipeline

    sid = _scene(auth_client)["id"]
    _set(sid, status="generating", claim_token="")
    with SessionLocal() as db, pytest.raises(ValueError, match="storyboard plan"):
        pipeline.recompose_project(db, sid)
    assert _state(sid)[0] == "planned"


def test_recompose_without_a_plan_fails_a_storyboard(auth_client):
    from app.db import SessionLocal
    from app.services import pipeline

    pid = _storyboard(auth_client, plan=False)
    _set(pid, status="generating", claim_token="")
    with SessionLocal() as db, pytest.raises(ValueError, match="storyboard plan"):
        pipeline.recompose_project(db, pid)
    assert _state(pid)[0] == "failed"


def test_recompose_without_a_plan_or_loop_fails_an_ambient_scene(auth_client):
    from app.db import SessionLocal
    from app.services import pipeline

    r = auth_client.post("/api/ambient", json={"config": SHORT, "generate": False})
    sid = r.json()["id"]
    _set(sid, status="generating", claim_token="")
    with SessionLocal() as db, pytest.raises(ValueError):
        pipeline.recompose_project(db, sid)
    assert _state(sid)[0] == "failed"


# ---------- 4. maybe_recompose never queues a storyboard recompose ----------


def test_maybe_recompose_restores_an_ambient_scene(auth_client, monkeypatch):
    from app.db import SessionLocal
    from app.services import pipeline
    from app.workers import queue

    queued: list = []
    monkeypatch.setattr(queue, "enqueue", lambda *a, **k: queued.append(a) or "x")
    sid = _scene(auth_client)["id"]
    _set(sid, status="regenerating")
    with SessionLocal() as db:
        pipeline.maybe_recompose(db, sid)
    assert queued == []
    assert _state(sid)[0] == "planned"


# ---------- 5. cancel releases a stuck scene whose queue entry is gone ----------


@pytest.mark.parametrize("entry,action,status", [("gone", "released", "cancelled"),
                                                  ("alive", "noop", "generating"),
                                                  ("unknown", "noop", "generating")])
def test_cancel_releases_a_stuck_ambient_scene(auth_client, monkeypatch, entry, action, status):
    from app.workers import queue

    sid = _scene(auth_client)["id"]
    assert auth_client.post(f"/api/ambient/{sid}/render").status_code == 202  # a finished render
    # Stuck like the report: the claim was consumed, then the job raised.
    _set(sid, status="generating", claim_token="", queue_job_id="rq-123")
    monkeypatch.setattr(queue, "queue_entry_state", lambda qid: entry)
    r = auth_client.post(f"/api/projects/{sid}/cancel")
    assert r.status_code == 200, r.text
    assert r.json()["action"] == action
    assert _state(sid)[0] == status


def test_cancel_leaves_a_stuck_scene_with_clips_in_flight(auth_client, monkeypatch):
    from app.services import video_jobs
    from app.workers import queue

    sid = _scene(auth_client)["id"]
    assert auth_client.post(f"/api/ambient/{sid}/render").status_code == 202
    _set(sid, status="generating", claim_token="", queue_job_id="rq-123")
    monkeypatch.setattr(queue, "queue_entry_state", lambda qid: "gone")
    monkeypatch.setattr(video_jobs, "pending_for", lambda db, **kw: 1)
    assert auth_client.post(f"/api/projects/{sid}/cancel").json()["action"] == "noop"
    assert _state(sid)[0] == "generating"


def test_cancel_still_refuses_a_hook_lab_claim(auth_client, monkeypatch):
    from app.services.pipeline import hook_claim_token
    from app.workers import queue

    pid = _storyboard(auth_client)
    assert auth_client.post(f"/api/projects/{pid}/generate-video").status_code == 202
    _set(pid, status="generating", claim_token=hook_claim_token("completed"), queue_job_id="rq-1")
    monkeypatch.setattr(queue, "queue_entry_state", lambda qid: "gone")
    assert auth_client.post(f"/api/projects/{pid}/cancel").status_code == 409
    assert _state(pid)[0] == "generating"


# ---------- 6. the storyboard paths still work ----------


def test_storyboard_regenerate_still_queues(auth_client):
    pid = _storyboard(auth_client)
    assert auth_client.post(f"/api/projects/{pid}/generate-video").status_code == 202
    shots = auth_client.get(f"/api/projects/{pid}").json()["shots"]
    sid = next(s["id"] for s in shots if s["shot_type"] != "end_card")
    r = auth_client.post(f"/api/projects/{pid}/shots/{sid}/regenerate")
    assert r.status_code == 202, r.text
    assert auth_client.patch(f"/api/projects/{pid}/shots/{sid}", json={"caption_text": "hi"}).status_code == 200


def test_recent_renders_and_spend_rows_carry_the_mode(auth_client):
    sid = _scene(auth_client)["id"]
    assert auth_client.post(f"/api/ambient/{sid}/render").status_code == 202
    recent = auth_client.get("/api/renders/recent").json()
    mine = [r for r in recent if r["project_id"] == sid]
    assert mine and mine[0]["project_mode"] == "ambient_loop"

    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        db.add(models.SpendEntry(provider="mock", kind="music", model="m", project_id=sid, cost_usd=0.5,
                                 ref="t", source="estimate", note=""))
        db.commit()
    rows = auth_client.get("/api/spend/entries", params={"project_id": sid}).json()
    assert rows and all(r["project_mode"] == "ambient_loop" for r in rows)
    top = auth_client.get("/api/spend/summary").json()["top_projects"]
    assert any(p["project_id"] == sid and p["project_mode"] == "ambient_loop" for p in top)


@pytest.mark.parametrize("qid", ["", "inline:recompose_project_job", "background:x"])
def test_cancel_leaves_a_consumed_claim_without_an_rq_entry(auth_client, monkeypatch, qid):
    """Without an RQ entry there is no telling a running job from a dead
    one (both read as gone): the job keeps the claim."""
    sid = _scene(auth_client)["id"]
    assert auth_client.post(f"/api/ambient/{sid}/render").status_code == 202
    _set(sid, status="generating", claim_token="", queue_job_id=qid)
    assert auth_client.post(f"/api/projects/{sid}/cancel").json()["action"] == "noop"
    assert _state(sid)[0] == "generating"


# ---------- 5b. Cancel never takes a scene from a job that is still at work ----------


def _qid(pid: int) -> str:
    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        return db.get(models.VideoProject, pid).queue_job_id


def test_storyboard_recompose_after_a_reroll_records_its_queue_entry(auth_client, monkeypatch):
    """maybe_recompose's recompose is not a claimed job: before, the project
    kept an older, finished queue entry, so a Cancel while the recompose was
    still queued read "gone" and gave the project back from under it."""
    from app.db import SessionLocal
    from app.services import pipeline
    from app.workers import queue

    pid = _storyboard(auth_client)
    assert auth_client.post(f"/api/projects/{pid}/generate-video").status_code == 202
    _set(pid, status="regenerating", claim_token="", queue_job_id="rq-old")
    monkeypatch.setattr(queue, "enqueue", lambda *a, **k: "rq-recompose")
    with SessionLocal() as db:
        pipeline.maybe_recompose(db, pid)
    assert _state(pid)[0] == "generating"
    assert _qid(pid) == "rq-recompose"
    monkeypatch.setattr(queue, "queue_entry_state", lambda qid: "alive" if qid == "rq-recompose" else "gone")
    assert auth_client.post(f"/api/projects/{pid}/cancel").json()["action"] == "noop"
    assert _state(pid)[0] == "generating"
    # Once that entry is gone too (the recompose raised), Cancel frees it.
    monkeypatch.setattr(queue, "queue_entry_state", lambda qid: "gone")
    assert auth_client.post(f"/api/projects/{pid}/cancel").json()["action"] == "released"
    assert _state(pid)[0] == "cancelled"


@pytest.mark.parametrize("route", ["render", "generate"])
def test_ambient_claims_record_their_queue_entry(auth_client, monkeypatch, route):
    """The Ambient routes' jobs are recorded like the storyboard's, so a
    stuck scene can be freed by Cancel and a queued one is left alone."""
    from app.api import ambient as ambient_api
    from app.workers import queue

    sid = _scene(auth_client)["id"]
    monkeypatch.setattr(ambient_api, "enqueue", lambda *a, **k: "rq-amb")
    r = auth_client.post(f"/api/ambient/{sid}/render" if route == "render" else f"/api/ambient/{sid}/generate-loop")
    assert r.status_code == 202, r.text
    assert _state(sid)[0] == "generating" and _qid(sid) == "rq-amb"
    _set(sid, claim_token="")  # the job took the claim
    monkeypatch.setattr(queue, "queue_entry_state", lambda qid: "alive")
    r = auth_client.post(f"/api/projects/{sid}/cancel")  # no render yet: 409 "no render in flight"
    assert r.status_code == 409 or r.json()["action"] == "noop", r.text
    assert _state(sid)[0] == "generating"
    monkeypatch.setattr(queue, "queue_entry_state", lambda qid: "gone")
    assert auth_client.post(f"/api/projects/{sid}/cancel").json()["action"] == "released"
    assert _state(sid)[0] == "cancelled"


def test_cancel_leaves_an_ambient_loop_whose_clips_are_being_made(auth_client, monkeypatch):
    from app import models
    from app.db import SessionLocal
    from app.workers import queue

    scene = _scene(auth_client)
    sid = scene["id"]
    _set(sid, status="generating", claim_token="", queue_job_id="rq-123")
    with SessionLocal() as db:
        db.add(models.VideoJob(purpose="ambient_segment", ref_id=scene["loop_shot"]["id"], project_id=sid,
                               status="submitted"))
        db.commit()
    monkeypatch.setattr(queue, "queue_entry_state", lambda qid: "gone")
    r = auth_client.post(f"/api/projects/{sid}/cancel")
    assert r.status_code in (200, 409), r.text
    assert _state(sid)[0] == "generating"


@pytest.mark.parametrize("failed,status", [(False, "generating"), (True, "cancelled")])
def test_cancel_leaves_an_ambient_loop_chain_in_flight(auth_client, monkeypatch, failed, status):
    """Between one clip landing and the next being sent, and while the
    finished loop's music is made, only the chain says the scene is busy."""
    from app import models
    from app.db import SessionLocal
    from app.workers import queue

    sid = _scene(auth_client)["id"]
    with SessionLocal() as db:
        p = db.get(models.VideoProject, sid)
        p.ambient_config_json = {**(p.ambient_config_json or {}),
                                 "_chain": {"segment_ids": [], "failed": failed}}
        p.status, p.claim_token, p.queue_job_id = "generating", "", "rq-123"
        db.commit()
    monkeypatch.setattr(queue, "queue_entry_state", lambda qid: "gone")
    auth_client.post(f"/api/projects/{sid}/cancel")
    assert _state(sid)[0] == status

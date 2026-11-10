"""Review fixes around the scene-description rewrite (a planner call of up
to a minute) in saves and generation: combined saves keep every field, a
generation that starts during the call wins, the worker never writes back a
stale copy, the rewrite stays inside the budget and rate limit, and the
still model only resets clips that used it."""
from __future__ import annotations

import pytest

from .test_ambient import SHORT

HYPE = "beautiful tropical beach with crystal clear turquoise water and palm trees, 8k ultra realistic"


@pytest.fixture
def rewrites(monkeypatch):
    """Count calls to the (mock) planner's rewrite; `during` runs inside it,
    standing in for whatever happens elsewhere while the call is out."""
    from app.providers.mock_chat import MockChatProvider

    calls: list[str] = []
    during: list = []
    real = MockChatProvider.rewrite_scene

    def counted(self, text):
        calls.append(text)
        for fn in during:
            fn()
        return real(self, text)

    monkeypatch.setattr(MockChatProvider, "rewrite_scene", counted)
    return calls, during


def _other_session(fn):
    """Run fn(project) in a separate session and commit (another request / the worker)."""
    from app import models
    from app.db import SessionLocal

    def run(sid):
        db = SessionLocal()
        try:
            fn(db.get(models.VideoProject, sid))
            db.commit()
        finally:
            db.close()
    return run


def _custom(**kw):
    return {**SHORT, "scene_preset": "custom", "scene_prompt": HYPE, **kw}


# ---------- #1 / #5 / #11: a combined save keeps title and aspect ----------


@pytest.mark.parametrize("cfg", [_custom(), SHORT], ids=["rewritten", "preset"])
def test_a_combined_save_keeps_title_and_aspect(auth_client, rewrites, cfg):
    sid = auth_client.post("/api/ambient", json={"config": cfg, "generate": True}).json()["id"]
    scene = auth_client.get(f"/api/ambient/{sid}").json()
    assert scene["aspect_ratio"] == "16:9" and scene["loop_status"] == "completed"
    new_cfg = {**scene["config"], "scene_prompt": "a quiet misty lake at the edge of a pine forest"}
    r = auth_client.patch(f"/api/ambient/{sid}?discard_loop=true",
                          json={"title": "Renamed", "aspect_ratio": "9:16", "config": new_cfg})
    assert r.status_code == 200, r.text
    for out in (r.json(), auth_client.get(f"/api/ambient/{sid}").json()):
        assert out["title"] == "Renamed" and out["aspect_ratio"] == "9:16"
        assert out["config"]["scene_prompt"].startswith("a quiet misty lake")
        assert out["loop_status"] == "pending"
    from app import models
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        data = db.get(models.VideoProject, sid).ambient_config_json
    finally:
        db.close()
    assert "_generated" not in data  # the old clip's record isn't resurrected


# ---------- #2 / #13: a generation started during the rewrite wins ----------


def test_a_generation_started_during_the_rewrite_is_not_overwritten(auth_client, rewrites):
    calls, during = rewrites
    sid = auth_client.post("/api/ambient", json={"config": _custom(), "generate": False}).json()["id"]
    before = auth_client.get(f"/api/ambient/{sid}").json()
    chain = {"segment_ids": [1], "variant": 0}

    def start(p):
        p.status = "generating"
        p.ambient_config_json = {**p.ambient_config_json, "_chain": chain}

    during.append(lambda: _other_session(start)(sid))
    r = auth_client.patch(f"/api/ambient/{sid}", json={
        "title": "Changed", "config": {**before["config"], "scene_prompt": "a quiet misty lake"}})
    assert r.status_code == 409, r.text
    during.clear()

    from app import models
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        p = db.get(models.VideoProject, sid)
        assert p.status == "generating" and p.title == before["title"]
        assert p.ambient_config_json["_chain"] == chain
        assert p.ambient_config_json["scene_prompt"] == HYPE
    finally:
        db.close()


def test_a_save_keeps_internal_state(auth_client, rewrites):
    sid = auth_client.post("/api/ambient", json={"config": _custom(), "generate": False}).json()["id"]
    _other_session(lambda p: setattr(p, "ambient_config_json",
                                     {**p.ambient_config_json, "_chain": {"variant": 0},
                                      "_loop_started": "2026-01-01T00:00:00"}))(sid)
    cfg = auth_client.get(f"/api/ambient/{sid}").json()["config"]
    r = auth_client.patch(f"/api/ambient/{sid}", json={"config": {**cfg, "music_volume": 0.3}})
    assert r.status_code == 200, r.text
    from app import models
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        assert db.get(models.VideoProject, sid).ambient_config_json["_chain"] == {"variant": 0}
    finally:
        db.close()
    # A change that needs a new clip drops the chain (it was for the old
    # prompt) and keeps the rest.
    r = auth_client.patch(f"/api/ambient/{sid}", json={"config": {**cfg, "scene_prompt": "a quiet lake"}})
    assert r.status_code == 200, r.text
    db = SessionLocal()
    try:
        state = db.get(models.VideoProject, sid).ambient_config_json
        assert "_chain" not in state and state["_loop_started"] == "2026-01-01T00:00:00"
    finally:
        db.close()


# ---------- #3: generate_loop marks the scene generating first, then re-reads ----------


def test_generate_marks_generating_before_the_rewrite_and_keeps_concurrent_saves(auth_client, monkeypatch,
                                                                                  rewrites):
    from app.providers.mock_chat import MockChatProvider

    calls, during = rewrites
    counted = MockChatProvider.rewrite_scene
    monkeypatch.setattr(MockChatProvider, "rewrite_scene", lambda self, text: (_ for _ in ()).throw(
        RuntimeError("planner down")))
    sid = auth_client.post("/api/ambient", json={"config": _custom(), "generate": False}).json()["id"]
    assert auth_client.get(f"/api/ambient/{sid}").json()["config"]["photo"] is None
    monkeypatch.setattr(MockChatProvider, "rewrite_scene", counted)  # the planner works again
    seen: list[str] = []

    def concurrent(p):
        seen.append(p.status)
        p.title = "Saved meanwhile"
        p.ambient_config_json = {**p.ambient_config_json, "music_volume": 0.123}

    during.append(lambda: _other_session(concurrent)(sid))
    assert auth_client.post(f"/api/ambient/{sid}/generate-loop").status_code == 202
    assert seen == ["generating"]  # a second Generate gets the API's 409 meanwhile
    scene = auth_client.get(f"/api/ambient/{sid}").json()
    assert scene["config"]["music_volume"] == pytest.approx(0.123)  # not overwritten by a stale copy
    assert scene["title"] == "Saved meanwhile"
    assert scene["config"]["photo"]["scene"] and scene["loop_status"] == "completed"


# ---------- #4 / #12: the rewrite stays inside the budget and rate limit ----------


def test_no_rewrite_over_budget_but_the_scene_still_saves(auth_client, monkeypatch, rewrites):
    from app.services import spend

    calls, _ = rewrites
    monkeypatch.setattr(spend, "over_budget", lambda db: "Monthly budget reached")
    r = auth_client.post("/api/ambient", json={"config": _custom(), "generate": False})
    assert r.status_code == 201, r.text
    assert r.json()["config"]["photo"] is None and r.json()["config"]["scene_prompt"] == HYPE
    sid = r.json()["id"]
    r = auth_client.patch(f"/api/ambient/{sid}", json={"config": {**r.json()["config"],
                                                                   "scene_prompt": "a quiet misty lake"}})
    assert r.status_code == 200, r.text
    assert r.json()["config"]["scene_prompt"] == "a quiet misty lake" and r.json()["config"]["photo"] is None
    assert calls == []


def test_a_save_rewrite_is_rate_limited_but_the_save_goes_through(auth_client, monkeypatch, rewrites):
    from app import config as _c
    from app.services import ratelimit

    calls, _ = rewrites
    sid = auth_client.post("/api/ambient", json={"config": SHORT, "generate": False}).json()["id"]
    cfg = auth_client.get(f"/api/ambient/{sid}").json()["config"]
    monkeypatch.setenv("RATE_LIMIT_GENERATION_BURST", "1")
    monkeypatch.setenv("RATE_LIMIT_GENERATION_PER_MINUTE", "0")
    _c.get_settings.cache_clear()
    ratelimit._limiter = None
    # A save that makes no rewrite takes no token.
    assert auth_client.patch(f"/api/ambient/{sid}", json={"config": {**cfg, "music_volume": 0.2}}).status_code == 200
    r = auth_client.patch(f"/api/ambient/{sid}", json={"config": _custom()})
    assert r.status_code == 200 and r.json()["config"]["photo"] and len(calls) == 1
    r = auth_client.patch(f"/api/ambient/{sid}", json={"config": _custom(scene_prompt="a quiet misty lake")})
    assert r.status_code == 200, r.text
    assert r.json()["config"]["scene_prompt"] == "a quiet misty lake" and r.json()["config"]["photo"] is None
    assert len(calls) == 1


# ---------- #6: the still model only resets clips that used a still ----------


def _caps(monkeypatch, first, last):
    from app.providers.base import VideoCapabilities
    from app.services import ambient

    caps = VideoCapabilities(durations=[5, 10], first_frame=first, last_frame=last)
    monkeypatch.setattr(ambient, "video_capabilities", lambda model: caps)


def _still_change_resets(auth_client, **cfg):
    sid = auth_client.post("/api/ambient", json={"config": {**SHORT, **cfg}, "generate": True}).json()["id"]
    scene = auth_client.get(f"/api/ambient/{sid}").json()
    assert scene["loop_status"] == "completed"
    r = auth_client.patch(f"/api/ambient/{sid}?discard_loop=true", json={
        "config": {**scene["config"], "still_model": "black-forest-labs/flux-3-image"}})
    assert r.status_code == 200, r.text
    return r.json()["loop_status"] != "completed"


def test_still_model_change_keeps_a_video_clip_made_without_a_still(auth_client, monkeypatch):
    _caps(monkeypatch, first=False, last=False)
    assert not _still_change_resets(auth_client)
    # A first-frame-only model with one loop makes no still either.
    _caps(monkeypatch, first=True, last=False)
    assert not _still_change_resets(auth_client)


def test_still_model_change_resets_clips_that_used_a_still(auth_client, monkeypatch):
    _caps(monkeypatch, first=True, last=True)  # anchored: the anchor still
    assert _still_change_resets(auth_client)
    _caps(monkeypatch, first=True, last=False)  # one start still per time of day
    assert _still_change_resets(auth_client, variety="day_night", variations=2)
    assert _still_change_resets(auth_client, source="image_drift")


def test_uses_still_model_follows_the_plan(monkeypatch):
    from app.schemas.ambient import AmbientConfig
    from app.services import ambient

    def cfg(**kw):
        return AmbientConfig(**{**SHORT, **kw})

    _caps(monkeypatch, first=True, last=True)
    assert ambient.uses_still_model(cfg())
    # The scene's own picture is the anchor: no still is generated.
    assert not ambient.uses_still_model(cfg(picture_origin="upload"))
    assert ambient.uses_still_model(cfg(picture_origin="upload", variety="day_night", variations=2))
    # Takes share the first anchor (the picture).
    assert not ambient.uses_still_model(cfg(picture_origin="upload", variety="takes", variations=2))
    _caps(monkeypatch, first=False, last=False)
    assert not ambient.uses_still_model(cfg())
    assert not ambient.uses_still_model(cfg(variety="day_night", variations=2))
    assert ambient.uses_still_model(cfg(source="image_drift"))
    assert not ambient.uses_still_model(cfg(source="image_drift", picture_origin="upload"))


# ---------- #3 on a real queue: the API claims the scene before enqueueing ----------


@pytest.fixture
def queued(monkeypatch):
    """Stand in for RQ: enqueue records the job and returns, so it waits in
    the queue instead of running inline."""
    import app.api.ambient as api

    jobs: list[tuple] = []

    def fake_enqueue(func, *args, **kwargs):
        jobs.append((func.__name__, args))
        return f"rq:{len(jobs)}"

    monkeypatch.setattr(api, "enqueue", fake_enqueue)
    return jobs


def test_a_queued_generation_blocks_a_second_generate(auth_client, queued):
    sid = auth_client.post("/api/ambient", json={"config": SHORT, "generate": False}).json()["id"]
    assert auth_client.post(f"/api/ambient/{sid}/generate-loop").status_code == 202
    assert auth_client.get(f"/api/ambient/{sid}").json()["status"] == "generating"
    assert auth_client.post(f"/api/ambient/{sid}/generate-loop").status_code == 409
    assert [name for name, _ in queued] == ["generate_ambient_loop_job"]


def test_create_and_generate_claims_the_scene_before_queueing(auth_client, queued):
    r = auth_client.post("/api/ambient", json={"config": SHORT, "generate": True})
    assert r.status_code == 201 and r.json()["status"] == "generating"
    assert auth_client.post(f"/api/ambient/{r.json()['id']}/generate-loop").status_code == 409
    assert len(queued) == 1


def test_queue_down_puts_the_previous_status_back(auth_client, monkeypatch):
    import app.api.ambient as api
    from app.workers.queue import QueueUnavailable

    sid = auth_client.post("/api/ambient", json={"config": SHORT, "generate": False}).json()["id"]
    before = auth_client.get(f"/api/ambient/{sid}").json()["status"]

    def down(*a, **k):
        raise QueueUnavailable("job queue (Redis) unavailable and QUEUE_MODE=required")

    monkeypatch.setattr(api, "enqueue", down)
    assert auth_client.post(f"/api/ambient/{sid}/generate-loop").status_code == 503
    assert auth_client.get(f"/api/ambient/{sid}").json()["status"] == before


def test_a_redis_error_while_enqueueing_puts_the_previous_status_back(auth_client, monkeypatch):
    """Review: Redis can drop between the health check and the enqueue, with an
    error that isn't QueueUnavailable. No job was accepted, so the scene must
    not stay 'generating' (it would refuse every save and Generate)."""
    import app.api.ambient as api

    sid = auth_client.post("/api/ambient", json={"config": SHORT, "generate": False}).json()["id"]
    before = auth_client.get(f"/api/ambient/{sid}").json()["status"]

    def dropped(*a, **k):
        raise ConnectionError("Error 111 connecting to redis:6379. Connection refused.")

    monkeypatch.setattr(api, "enqueue", dropped)
    assert auth_client.post(f"/api/ambient/{sid}/generate-loop").status_code == 500
    assert auth_client.get(f"/api/ambient/{sid}").json()["status"] == before

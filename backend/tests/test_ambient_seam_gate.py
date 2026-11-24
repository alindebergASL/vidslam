"""Seam truth: the render's blend at the loop's join comes from the
measured join (services.ambient.seam_treatment), never from the model's
capabilities, and a loop that visibly jumps is not rendered without asking.
Mock providers and synthetic ffmpeg clips."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from .test_ambient import SHORT
from .test_loop_health import _replace, hard_cut, seamless


def _scene(client, **cfg) -> dict:
    r = client.post("/api/ambient", json={"config": {**SHORT, **cfg}, "generate": True})
    assert r.status_code == 201, r.text
    scene = r.json()
    assert scene["loop_status"] == "completed", scene
    return scene


def _treatment(sid: int) -> dict:
    from app import models
    from app.db import SessionLocal
    from app.services import ambient

    db = SessionLocal()
    try:
        return ambient.seam_treatment(db.get(models.VideoProject, sid))
    finally:
        db.close()


def _patch(client, sid: int, **cfg) -> dict:
    cur = client.get(f"/api/ambient/{sid}").json()["config"]
    r = client.patch(f"/api/ambient/{sid}", json={"config": {**cur, **cfg}})
    assert r.status_code == 200, r.text
    return r.json()


def test_a_jumping_closed_loop_is_measured_blended_longer_and_gated(auth_client):
    scene = _scene(auth_client)
    sid = scene["id"]
    # The mock takes a first and a last frame: the plan calls it closed,
    # which must not shorten the blend by itself.
    assert scene["plan"]["closed"] is True
    _replace(Path(scene["loop_shot"]["clip_path"]), hard_cut)
    # The old reading is stale: until measured, the blend is the setting.
    t = _treatment(sid)
    assert t["basis"] == "plan" and t["blend_seconds"] == 1.0 and t["verdict"] is None

    # POST /render measures inline, finds a visible jump, and asks.
    r = auth_client.post(f"/api/ambient/{sid}/render")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "seam_jump" and detail["ratio"] > 6 and detail["blend_seconds"] == 2.0
    out = auth_client.get(f"/api/ambient/{sid}").json()
    assert out["status"] != "generating"  # nothing was claimed
    st = out["seam_treatment"]
    assert st == {"blend_seconds": 2.0, "verdict": "visible_jump", "ratio": detail["ratio"],
                  "basis": "measured", "covers": False}
    # A longer setting is kept: max(setting, 2.0).
    assert _patch(auth_client, sid, crossfade_seconds=3.0)["seam_treatment"]["blend_seconds"] == 3.0
    assert auth_client.post(f"/api/ambient/{sid}/render").status_code == 409

    r = auth_client.post(f"/api/ambient/{sid}/render?accept_seam=true")
    assert r.status_code == 202, r.text
    log = auth_client.get(f"/api/ambient/{sid}").json()["latest_render"]["render_log"]
    assert "seam=visible_jump blend=3s" in log


def test_a_slight_jump_blends_at_least_one_and_a_half_seconds(auth_client, monkeypatch):
    from app.services import loop_health

    sid = _scene(auth_client)["id"]
    entry = {"seam": {"verdict": "slight_jump", "ratio": 4.0}}
    monkeypatch.setattr(loop_health, "current_entry", lambda p, s: entry if s is not None else None)
    t = _treatment(sid)
    assert t == {"blend_seconds": 1.5, "verdict": "slight_jump", "ratio": 4.0, "basis": "measured",
                 "covers": True}
    monkeypatch.undo()
    _patch(auth_client, sid, crossfade_seconds=2.5)
    monkeypatch.setattr(loop_health, "current_entry", lambda p, s: entry if s is not None else None)
    assert _treatment(sid)["blend_seconds"] == 2.5  # never below the setting


def test_a_seamless_join_gets_a_short_hedge(auth_client):
    scene = _scene(auth_client, crossfade_seconds=2.0)
    sid = scene["id"]
    _replace(Path(scene["loop_shot"]["clip_path"]), seamless)
    assert auth_client.post(f"/api/ambient/{sid}/loop-health").status_code == 200
    out = auth_client.get(f"/api/ambient/{sid}").json()
    assert out["seam_treatment"]["verdict"] == "seamless"
    assert out["seam_treatment"]["blend_seconds"] == 0.5
    assert out["loop_health"]["seam"]["blend_seconds"] == 0.5
    # The plan the page shows uses the blend the render will use.
    assert out["plan"]["seam_crossfade"] == 0.5
    assert out["plan"]["unit_seconds"] == pytest.approx(out["plan"]["loop_seconds"] - 0.5, abs=0.01)
    assert auth_client.post(f"/api/ambient/{sid}/render").status_code == 202


def test_blend_mode_uses_the_setting_and_does_not_ask(auth_client):
    scene = _scene(auth_client)
    sid = scene["id"]
    _replace(Path(scene["loop_shot"]["clip_path"]), hard_cut)
    assert auth_client.post(f"/api/ambient/{sid}/loop-health").status_code == 200
    out = _patch(auth_client, sid, seam_mode="blend", crossfade_seconds=1.0)
    assert out["loop_status"] == "completed"  # a render-time setting keeps the clip
    assert out["seam_treatment"]["blend_seconds"] == 1.0
    assert out["seam_treatment"]["verdict"] == "visible_jump"
    r = auth_client.post(f"/api/ambient/{sid}/render")
    assert r.status_code == 202, r.text
    assert "blend=1s" in auth_client.get(f"/api/ambient/{sid}").json()["latest_render"]["render_log"]


def test_still_drift_needs_no_blend(auth_client):
    scene = _scene(auth_client, source="image_drift")
    st = scene["seam_treatment"]
    assert st["basis"] == "drift" and st["blend_seconds"] == 0.0 and st["covers"] is True


def test_a_batch_scene_with_a_jumping_loop_is_not_rendered(auth_client, monkeypatch, tmp_path):
    from app import models
    from app.db import SessionLocal
    from app.services import loop_health
    from app.workers import jobs, queue

    jump = hard_cut(tmp_path / "jump.mp4")
    real_measure = loop_health.measure
    monkeypatch.setattr(loop_health, "measure", lambda clip, still=False, **kw: real_measure(jump, still=still, **kw))
    renders = []
    real_enqueue = queue.enqueue

    def spy(fn, *a, **kw):
        if fn is jobs.render_ambient_job:
            renders.append(a)
        return real_enqueue(fn, *a, **kw)

    monkeypatch.setattr(queue, "enqueue", spy)
    r = auth_client.post("/api/ambient/batch", json={
        "presets": ["fireplace"], "config": {**SHORT, "music_mood": "none"}, "render": True})
    assert r.status_code == 201, r.text
    sid = r.json()["scene_ids"][0]
    s = auth_client.get(f"/api/ambient/{sid}").json()
    assert s["loop_status"] == "completed" and s["status"] == "planned"
    assert renders == [] and s["latest_render"] is None
    db = SessionLocal()
    try:
        gen = db.get(models.VideoProject, sid).ambient_config_json["_generated"]
        assert gen["seam_verdict"] == "visible_jump" and gen["seam_ratio"] > 6
        ev = (db.query(models.Event).filter(models.Event.project_id == sid,
                                            models.Event.status == "needs_look").one())
        assert ev.severity == "warning" and "needs a look" in ev.title
    finally:
        db.close()


def test_a_batch_scene_with_a_clean_loop_is_rendered(auth_client):
    r = auth_client.post("/api/ambient/batch", json={
        "presets": ["fireplace"], "config": {**SHORT, "music_mood": "none"}, "render": True})
    sid = r.json()["scene_ids"][0]
    s = auth_client.get(f"/api/ambient/{sid}").json()
    assert s["seam_treatment"]["verdict"] == "seamless"
    assert s["latest_render"] and s["latest_render"]["status"] == "completed"


def test_loop_health_keeps_bad_when_the_blend_does_not_cover(auth_client):
    scene = _scene(auth_client)
    sid = scene["id"]
    _replace(Path(scene["loop_shot"]["clip_path"]), hard_cut)
    seam = auth_client.post(f"/api/ambient/{sid}/loop-health").json()["loop_health"]["seam"]
    assert seam["verdict"] == "visible_jump"
    assert seam["level"] == "bad" and seam["raw_level"] == "bad"
    assert seam["treatment"]["covers"] is False and seam["blend_seconds"] == 2.0
    assert "won't hide it" in seam["text"]


def test_the_music_bed_loops_on_its_own_long_crossfade(auth_client, monkeypatch, tmp_path):
    from app.services import ambient, renderer

    seen = []
    real_compose = ambient.compose_ambient

    def spy_compose(inputs):
        seen.append(inputs)
        return real_compose(inputs)

    monkeypatch.setattr(ambient, "compose_ambient", spy_compose)
    scene = _scene(auth_client)
    assert auth_client.post(f"/api/ambient/{scene['id']}/render").status_code == 202
    assert seen and seen[0].music_crossfade == renderer.MUSIC_JOIN_CROSSFADE == 6.0
    assert seen[0].crossfade_seconds == 0.5  # the video seam is separate

    # compose_ambient hands the music crossfade (not the video's) to the bed's loop.
    xfades = []
    real_unit = renderer._make_seamless_audio_unit

    def spy_unit(src, dst, *, crossfade, loops=True):
        xfades.append(crossfade)
        return real_unit(src, dst, crossfade=crossfade, loops=loops)

    monkeypatch.setattr(renderer, "_make_seamless_audio_unit", spy_unit)
    clip = seamless(tmp_path / "clip.mp4")
    music = tmp_path / "bed.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=330:duration=30",
                    str(music)], check=True)
    renderer.compose_ambient(renderer.AmbientRenderInputs(
        project_id=0, out_dir=tmp_path / "out", clip_path=clip, total_seconds=10,
        crossfade_seconds=1.0, fade_seconds=0.0, music_path=music))
    assert xfades and xfades[0] == 6.0


def test_a_variation_with_no_reading_is_left_to_the_worker_which_stops_on_a_jump(auth_client):
    """The loop clip's reading is current but a variation's clip changed
    since: the request measures only the loop clip (its basis decides the
    gate) and queues; the worker measures the variation, finds a visible
    jump nobody accepted, and stops. Its reading then gates the next
    request (the scene's treatment is the worst join)."""
    from app.services import ambient

    scene = _scene(auth_client, variety="takes", variations=2)
    sid = scene["id"]
    assert len(scene["variant_shots"]) == 1
    assert scene["seam_treatment"]["verdict"] == "seamless"
    _replace(Path(scene["variant_shots"][0]["clip_path"]), hard_cut)
    r = auth_client.post(f"/api/ambient/{sid}/render")
    assert r.status_code == 202, r.text
    out = auth_client.get(f"/api/ambient/{sid}").json()
    assert out["status"] == "failed"
    assert out["latest_render"]["status"] == "failed"
    # Names the clip that jumped: the loop clip measured seamless.
    err = out["latest_render"]["error"]
    assert err != ambient.SEAM_STOP_MESSAGE and "clip 2 (a variation) jumps at the join" in err
    assert "the loop clip" not in err
    r = auth_client.post(f"/api/ambient/{sid}/render")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "seam_jump"
    assert detail["clip"] == 1 and detail["message"].startswith("Clip 2 (a variation) jumps at the join")
    out = auth_client.get(f"/api/ambient/{sid}").json()
    # The jump comes round once per seamless unit of the blend in use.
    assert detail["repeat_every_seconds"] == out["plan"]["unit_seconds"]
    assert out["plan"]["seam_crossfade"] == 2.0
    # The loop clip's own (seamless) reading isn't alarmed by the variation.
    assert out["loop_health"]["seam"]["level"] == "good"
    vseam = out["variant_loop_health"][0]["seam"]
    assert vseam["verdict"] == "visible_jump" and vseam["level"] == "bad"


def test_the_worker_render_measures_an_unmeasured_variation(auth_client):
    """A render queued while a variation had no reading still sizes the
    blend from it (the render measures before composing), once a jump was
    accepted."""
    from app.services import ambient

    scene = _scene(auth_client, variety="takes", variations=2)
    sid = scene["id"]
    _replace(Path(scene["variant_shots"][0]["clip_path"]), hard_cut)
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        ambient.render_scene(db, sid, accept_seam=True)
    finally:
        db.close()
    log = auth_client.get(f"/api/ambient/{sid}").json()["latest_render"]["render_log"]
    assert "seam=visible_jump blend=2s" in log


# ---------- no reading at request time ----------


def _boom(*a, **kw):
    raise RuntimeError("ffmpeg took too long")


def test_a_loop_that_cannot_be_measured_asks_before_rendering(auth_client, monkeypatch):
    """The inline measurement gets no reading (it failed or timed out):
    the request can't tell whether the loop jumps, so it asks."""
    from app.services import loop_health

    scene = _scene(auth_client)
    sid = scene["id"]
    _replace(Path(scene["loop_shot"]["clip_path"]), hard_cut)
    monkeypatch.setattr(loop_health, "measure", _boom)
    r = auth_client.post(f"/api/ambient/{sid}/render")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "seam_unmeasured" and detail["message"]
    assert detail["blend_seconds"] == 1.0
    out = auth_client.get(f"/api/ambient/{sid}").json()
    assert out["status"] != "generating" and out["latest_render"] is None
    assert out["seam_treatment"]["basis"] == "plan"


def test_blend_mode_renders_an_unmeasured_loop_without_asking(auth_client, monkeypatch):
    from app.services import loop_health

    scene = _scene(auth_client)
    sid = scene["id"]
    _patch(auth_client, sid, seam_mode="blend")
    _replace(Path(scene["loop_shot"]["clip_path"]), hard_cut)
    monkeypatch.setattr(loop_health, "measure", _boom)
    assert auth_client.post(f"/api/ambient/{sid}/render").status_code == 202


def test_the_inline_measurement_reads_only_the_loop_clip_within_a_deadline(auth_client, monkeypatch):
    from app.api import ambient as ambient_api
    from app.services import loop_health

    scene = _scene(auth_client, variety="takes", variations=2)
    sid = scene["id"]
    loop_clip = Path(scene["loop_shot"]["clip_path"])
    var_clip = Path(scene["variant_shots"][0]["clip_path"])
    _replace(loop_clip, seamless)
    _replace(var_clip, seamless)
    seen = []
    real_measure = loop_health.measure

    def spy(clip, still=False, **kw):
        seen.append((Path(clip), kw.get("timeout"), kw.get("deadline") is not None))
        return real_measure(clip, still=still, **kw)

    monkeypatch.setattr(loop_health, "measure", spy)
    assert auth_client.post(f"/api/ambient/{sid}/render").status_code == 202
    # The request reads the loop clip under one overall deadline; the
    # worker reads the variation with its own (longer, per-read) timeout.
    assert ambient_api.INLINE_MEASURE_SECONDS == 45
    assert seen == [(loop_clip, ambient_api.INLINE_MEASURE_SECONDS, True),
                    (var_clip, loop_health.MEASURE_TIMEOUT_SECONDS, False)]
    assert loop_health.MEASURE_TIMEOUT_SECONDS == 120  # the worker's is unchanged
    assert auth_client.get(f"/api/ambient/{sid}").json()["latest_render"]["status"] == "completed"


def test_the_inline_deadline_bounds_the_whole_measurement(auth_client, monkeypatch):
    """Each FFmpeg read gets only what is left of the overall deadline; once
    it's gone the measurement stops and the request asks."""
    import time

    from app.api import ambient as ambient_api
    from app.services import loop_health

    scene = _scene(auth_client)
    sid = scene["id"]
    _replace(Path(scene["loop_shot"]["clip_path"]), hard_cut)
    monkeypatch.setattr(ambient_api, "INLINE_MEASURE_SECONDS", 0.6)
    timeouts = []
    real_read = loop_health._read_frames

    def slow_read(cmd_in, vf_pre, size, *, timeout):
        timeouts.append(timeout)
        time.sleep(0.35)
        return real_read(cmd_in, vf_pre, size, timeout=60)

    monkeypatch.setattr(loop_health, "_read_frames", slow_read)
    r = auth_client.post(f"/api/ambient/{sid}/render")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "seam_unmeasured"
    assert len(timeouts) < 3  # the third read never started
    assert all(t <= 0.6 for t in timeouts)
    assert all(b < a for a, b in zip(timeouts, timeouts[1:], strict=False))


def test_a_spent_deadline_raises_before_reading(tmp_path, monkeypatch):
    import time

    from app.services import loop_health

    clip = seamless(tmp_path / "s.mp4")
    monkeypatch.setattr(loop_health, "_read_frames", _boom)
    with pytest.raises(TimeoutError):
        loop_health.measure(clip, deadline=time.monotonic() - 1)


def test_accept_seam_reaches_the_worker(auth_client, monkeypatch):
    """Render anyway after an unmeasured join: the worker measures, finds a
    visible jump, and renders it because the person said so."""
    from app.services import ambient, loop_health
    from app.workers import jobs, queue

    scene = _scene(auth_client)
    sid = scene["id"]
    _replace(Path(scene["loop_shot"]["clip_path"]), hard_cut)
    real_measure = loop_health.measure
    calls = {"n": 0}

    def first_fails(clip, still=False, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("ffmpeg took too long")
        return real_measure(clip, still=still, **kw)

    monkeypatch.setattr(loop_health, "measure", first_fails)
    kwargs = []
    real_enqueue = queue.enqueue

    def spy(fn, *a, **kw):
        if fn is jobs.render_ambient_job:
            kwargs.append(kw)
        return real_enqueue(fn, *a, **kw)

    monkeypatch.setattr("app.api.ambient.enqueue", spy)
    assert auth_client.post(f"/api/ambient/{sid}/render").json()["detail"]["code"] == "seam_unmeasured"
    r = auth_client.post(f"/api/ambient/{sid}/render?accept_seam=true")
    assert r.status_code == 202, r.text
    assert "accept_seam" not in kwargs[-1]
    assert kwargs[-1]["claim_token"].startswith(ambient.SEAM_ACCEPTED_CLAIM)
    out = auth_client.get(f"/api/ambient/{sid}").json()
    assert out["latest_render"]["status"] == "completed"
    assert "seam=visible_jump blend=2s" in out["latest_render"]["render_log"]


def test_the_worker_stops_an_unasked_jumping_render(auth_client):
    """A render queued without accept_seam whose own reading finds a visible
    jump stops before composing: the render fails with a clear message, the
    scene is released, and the person is told."""
    from app import models
    from app.db import SessionLocal
    from app.services import ambient
    from app.workers import jobs

    scene = _scene(auth_client)
    sid = scene["id"]
    _replace(Path(scene["loop_shot"]["clip_path"]), hard_cut)
    db = SessionLocal()
    try:
        # As POST /render claims the scene before queueing.
        db.query(models.VideoProject).filter(models.VideoProject.id == sid).update(
            {"status": "generating", "claim_token": "tok"}, synchronize_session=False)
        db.commit()
    finally:
        db.close()
    jobs.render_ambient_job(sid, claim_token="tok", accept_seam=False)
    out = auth_client.get(f"/api/ambient/{sid}").json()
    assert out["status"] == "failed"  # not left "generating"
    latest = out["latest_render"]
    assert latest["status"] == "failed" and latest["error"] == ambient.SEAM_STOP_MESSAGE
    assert out["seam_treatment"]["verdict"] == "visible_jump"
    db = SessionLocal()
    try:
        ev = (db.query(models.Event).filter(models.Event.project_id == sid,
                                            models.Event.kind == "ambient_render",
                                            models.Event.status == "failed").one())
        assert "jumps visibly" in ev.detail
        assert not db.query(models.Render).filter(models.Render.project_id == sid,
                                                  models.Render.final_video_path.isnot(None),
                                                  models.Render.final_video_path != "").count()
    finally:
        db.close()
    # Render anyway from the page now goes ahead.
    r = auth_client.post(f"/api/ambient/{sid}/render?accept_seam=true")
    assert r.status_code == 202, r.text
    assert auth_client.get(f"/api/ambient/{sid}").json()["latest_render"]["status"] == "completed"


def _claim(sid: int, token: str = "tok") -> None:
    from app import models
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        # As POST /render claims the scene before queueing.
        db.query(models.VideoProject).filter(models.VideoProject.id == sid).update(
            {"status": "generating", "claim_token": token}, synchronize_session=False)
        db.commit()
    finally:
        db.close()


def test_a_render_queued_before_accept_seam_travelled_is_honoured(auth_client):
    """A job POST /render queued before the job carried accept_seam (an
    earlier "Render anyway") has a claim token and no accept_seam: its
    request already passed the gate, so the worker's jump doesn't stop it."""
    from app.workers import jobs

    scene = _scene(auth_client)
    sid = scene["id"]
    _replace(Path(scene["loop_shot"]["clip_path"]), hard_cut)
    _claim(sid)
    jobs.render_ambient_job(sid, claim_token="tok")
    out = auth_client.get(f"/api/ambient/{sid}").json()
    assert out["status"] == "completed"
    assert out["latest_render"]["status"] == "completed"
    assert "seam=visible_jump blend=2s" in out["latest_render"]["render_log"]


def test_the_api_carries_accept_seam_in_the_claim_token(auth_client, monkeypatch):
    """No new job argument (a worker deployed before the web would raise
    TypeError on one): the claim token says whether the jump was accepted."""
    import inspect

    from app.services import ambient
    from app.workers import jobs, queue

    scene = _scene(auth_client)
    sid = scene["id"]
    kwargs = []
    real_enqueue = queue.enqueue

    def spy(fn, *a, **kw):
        if fn is jobs.render_ambient_job:
            kwargs.append(kw)
        return real_enqueue(fn, *a, **kw)

    monkeypatch.setattr("app.api.ambient.enqueue", spy)
    assert auth_client.post(f"/api/ambient/{sid}/render").status_code == 202
    assert auth_client.post(f"/api/ambient/{sid}/render?accept_seam=true").status_code == 202
    # Exactly the arguments the worker before the seam gate took.
    assert [set(kw) for kw in kwargs] == [{"claim_token"}, {"claim_token"}]
    asked, accepted = (kw["claim_token"] for kw in kwargs)
    assert asked.startswith(ambient.SEAM_ASK_CLAIM) and not ambient.claim_accepts_seam(asked)
    assert accepted.startswith(ambient.SEAM_ACCEPTED_CLAIM) and ambient.claim_accepts_seam(accepted)
    assert ambient.claim_accepts_seam("0123456789abcdef0123456789abcdef")  # queued before the gate
    from app import models

    width = models.VideoProject.__table__.c.claim_token.type.length
    assert len(asked) <= width and len(accepted) <= width
    assert list(inspect.signature(jobs.render_ambient_job).parameters)[:2] == ["project_id", "claim_token"]


def test_an_asking_claim_token_stops_a_jumping_render_in_the_worker(auth_client):
    from app.services import ambient
    from app.workers import jobs

    scene = _scene(auth_client)
    sid = scene["id"]
    _replace(Path(scene["loop_shot"]["clip_path"]), hard_cut)
    token = ambient.render_claim_token(False)
    _claim(sid, token)
    jobs.render_ambient_job(sid, claim_token=token)
    out = auth_client.get(f"/api/ambient/{sid}").json()
    assert out["status"] == "failed"
    assert out["latest_render"]["status"] == "failed"
    assert out["latest_render"]["error"] == ambient.SEAM_STOP_MESSAGE

    token = ambient.render_claim_token(True)
    _claim(sid, token)
    jobs.render_ambient_job(sid, claim_token=token)
    assert auth_client.get(f"/api/ambient/{sid}").json()["latest_render"]["status"] == "completed"


def _no_alerts(monkeypatch) -> list:
    from app.services import alerts

    sent = []
    monkeypatch.setattr(alerts, "render_failed", lambda *a, **kw: sent.append(a))
    return sent


def _assert_batch_asked(auth_client, sid: int) -> None:
    from app import models
    from app.db import SessionLocal
    from app.services import ambient

    out = auth_client.get(f"/api/ambient/{sid}").json()
    assert out["status"] == "planned"
    latest = out["latest_render"]
    assert latest["status"] == "cancelled" and latest["error"] == ambient.SEAM_STOP_MESSAGE
    db = SessionLocal()
    try:
        ev = (db.query(models.Event).filter(models.Event.project_id == sid,
                                            models.Event.status == "needs_look").one())
        assert ev.kind == "ambient_loop" and ev.severity == "warning" and "needs a look" in ev.title
        assert not db.query(models.Event).filter(models.Event.project_id == sid,
                                                 models.Event.status == "failed").count()
    finally:
        db.close()


def test_a_batch_render_that_finds_a_jump_asks_instead_of_failing(auth_client, monkeypatch, tmp_path):
    """A batch queued the render without a reading (the generate-time
    measurement failed); the worker's own reading finds a visible jump. As
    when the batch finds it at generate time: the scene goes back to
    planned with a needs_look event; no failed event, no alert."""
    from app.services import loop_health
    from app.workers import jobs, queue

    sent = _no_alerts(monkeypatch)
    jump = hard_cut(tmp_path / "jump.mp4")
    real_measure = loop_health.measure
    calls = {"n": 0}

    def first_fails(clip, still=False, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("ffmpeg took too long")
        return real_measure(jump, still=still, **kw)

    monkeypatch.setattr(loop_health, "measure", first_fails)
    queued = []
    real_enqueue = queue.enqueue

    def spy(fn, *a, **kw):
        if fn is jobs.render_ambient_job:
            queued.append(kw)
        return real_enqueue(fn, *a, **kw)

    monkeypatch.setattr(queue, "enqueue", spy)
    r = auth_client.post("/api/ambient/batch", json={
        "presets": ["fireplace"], "config": {**SHORT, "music_mood": "none"}, "render": True})
    assert r.status_code == 201, r.text
    sid = r.json()["scene_ids"][0]
    assert calls["n"] == 2  # generate time (failed), then the worker
    assert queued == [{}]  # the batch's job: no claim token, no accept_seam
    _assert_batch_asked(auth_client, sid)
    assert sent == []


def test_a_batch_queued_render_stops_and_asks(auth_client, monkeypatch):
    """The batch's job (no claim token, no accept_seam) on a scene whose
    clip now jumps: no failure, the scene asks."""
    from app.workers import jobs

    sent = _no_alerts(monkeypatch)
    scene = _scene(auth_client)
    sid = scene["id"]
    _replace(Path(scene["loop_shot"]["clip_path"]), hard_cut)
    jobs.render_ambient_job(sid)
    _assert_batch_asked(auth_client, sid)
    assert sent == []


def test_a_cancel_during_the_measurement_wins_over_the_seam_stop(auth_client, monkeypatch):
    from app import models
    from app.db import SessionLocal
    from app.services import ambient, loop_health

    scene = _scene(auth_client)
    sid = scene["id"]
    _replace(Path(scene["loop_shot"]["clip_path"]), hard_cut)
    real_measure = loop_health.measure

    def cancel_meanwhile(clip, still=False, **kw):
        db = SessionLocal()
        try:
            db.query(models.Render).filter(models.Render.project_id == sid,
                                           models.Render.status == "rendering").update(
                {"status": "cancelled"}, synchronize_session=False)
            db.commit()
        finally:
            db.close()
        return real_measure(clip, still=still, **kw)

    monkeypatch.setattr(loop_health, "measure", cancel_meanwhile)
    db = SessionLocal()
    try:
        render = ambient.render_scene(db, sid, accept_seam=False)
        assert render.status == "cancelled" and render.error == ""
    finally:
        db.close()
    out = auth_client.get(f"/api/ambient/{sid}").json()
    assert out["status"] == "cancelled"
    db = SessionLocal()
    try:
        assert not db.query(models.Event).filter(models.Event.project_id == sid,
                                                 models.Event.status.in_(("failed", "needs_look"))).count()
    finally:
        db.close()


# ---------- a seamless join and an explicit short setting ----------


@pytest.mark.parametrize("setting, blend", [(0.0, 0.0), (0.3, 0.3), (2.0, 0.5), (6.0, 0.5)])
def test_a_seamless_join_hedges_but_an_explicit_shorter_setting_wins(auth_client, monkeypatch, setting, blend):
    from app.services import loop_health

    sid = _scene(auth_client, crossfade_seconds=setting)["id"]
    entry = {"seam": {"verdict": "seamless", "ratio": 1.0}}
    monkeypatch.setattr(loop_health, "current_entry", lambda p, s: entry if s is not None else None)
    t = _treatment(sid)
    assert t["verdict"] == "seamless" and t["basis"] == "measured" and t["blend_seconds"] == blend


# ---------- the "needs a look" event counts toward the badge ----------


def test_a_needs_look_event_counts_as_unread(auth_client):
    from app.api import events as ev_api
    from app.db import SessionLocal
    from app.services import events

    assert "needs_look" in events.STATUSES and "needs_look" in ev_api._BADGE_STATUSES
    before = auth_client.get("/api/events/recent").json()["unread"]
    db = SessionLocal()
    try:
        events.emit(db, "ambient_loop", "project", 1, "needs_look", "Loop needs a look: x", severity="warning")
        events.emit(db, "ambient_loop", "project", 1, "progress", "half way")
        db.commit()
    finally:
        db.close()
    assert auth_client.get("/api/events/recent").json()["unread"] == before + 1


# ---------- the inline measurement only runs where it can change the answer ----------


@pytest.mark.parametrize("how", ["accept_seam", "blend"])
def test_no_inline_measurement_when_it_cannot_change_the_outcome(auth_client, monkeypatch, how):
    from app.services import loop_health

    scene = _scene(auth_client)
    sid = scene["id"]
    if how == "blend":
        _patch(auth_client, sid, seam_mode="blend")
    _replace(Path(scene["loop_shot"]["clip_path"]), hard_cut)
    inline = []
    monkeypatch.setattr(loop_health, "measure_loop", lambda *a, **kw: inline.append(kw))
    url = f"/api/ambient/{sid}/render" + ("?accept_seam=true" if how == "accept_seam" else "")
    r = auth_client.post(url)
    assert r.status_code == 202, r.text
    assert inline == []
    # The worker still measured before rendering.
    out = auth_client.get(f"/api/ambient/{sid}").json()
    assert out["latest_render"]["status"] == "completed"
    assert out["seam_treatment"]["verdict"] == "visible_jump"


def test_the_gated_request_still_measures_inline(auth_client, monkeypatch):
    from app.services import loop_health

    scene = _scene(auth_client)
    sid = scene["id"]
    _replace(Path(scene["loop_shot"]["clip_path"]), hard_cut)
    inline = []
    real = loop_health.measure_loop
    monkeypatch.setattr(loop_health, "measure_loop",
                        lambda db, p, **kw: inline.append(kw) or real(db, p, **kw))
    assert auth_client.post(f"/api/ambient/{sid}/render").status_code == 409
    assert len(inline) == 1


# ---------- ffprobe under the inline deadline ----------


def test_the_deadline_bounds_the_probes_too(tmp_path, monkeypatch):
    import time

    from app.services import loop_health, renderer

    clip = seamless(tmp_path / "s.mp4")
    seen = []
    real_run = renderer._run

    def spy(cmd, timeout=None, should_stop=None):
        if cmd[0] == "ffprobe":
            seen.append(timeout)
        return real_run(cmd, timeout=timeout, should_stop=should_stop)

    monkeypatch.setattr(renderer, "_run", spy)
    loop_health.measure(clip, deadline=time.monotonic() + 30)
    assert len(seen) == 2 and all(t is not None and 0 < t <= 30 for t in seen)
    seen.clear()
    loop_health.measure(clip)  # no deadline: probes as before
    assert seen == [None, None]


def test_a_probe_that_outlasts_the_deadline_is_a_timeout(tmp_path, monkeypatch):
    import time

    from app.services import loop_health, renderer

    clip = seamless(tmp_path / "s.mp4")

    def slow_probe(cmd, timeout=None, should_stop=None):
        time.sleep(0.3)
        return -1, "timed out"

    monkeypatch.setattr(renderer, "_run", slow_probe)
    monkeypatch.setattr(loop_health, "_read_frames", _boom)
    with pytest.raises(TimeoutError):
        loop_health.measure(clip, deadline=time.monotonic() + 0.2)


# ---------- a batch render asks when the join can't be measured ----------


def test_a_batch_render_of_an_unmeasured_loop_asks(auth_client, monkeypatch):
    from app import models
    from app.db import SessionLocal
    from app.services import ambient, loop_health
    from app.workers import jobs

    sent = _no_alerts(monkeypatch)
    scene = _scene(auth_client)
    sid = scene["id"]
    _replace(Path(scene["loop_shot"]["clip_path"]), seamless)  # stale reading
    monkeypatch.setattr(loop_health, "measure", _boom)
    jobs.render_ambient_job(sid)
    out = auth_client.get(f"/api/ambient/{sid}").json()
    assert out["status"] == "planned"
    latest = out["latest_render"]
    assert latest["status"] == "cancelled" and latest["error"] == ambient.SEAM_UNMEASURED_MESSAGE
    assert not latest["final_video_path"]
    db = SessionLocal()
    try:
        ev = (db.query(models.Event).filter(models.Event.project_id == sid,
                                            models.Event.status == "needs_look").one())
        assert "couldn't be measured" in ev.detail
    finally:
        db.close()
    assert sent == []


def test_an_unmeasured_loop_in_blend_mode_or_accepted_renders_from_a_batch(auth_client, monkeypatch):
    from app.services import loop_health
    from app.workers import jobs

    scene = _scene(auth_client)
    sid = scene["id"]
    _patch(auth_client, sid, seam_mode="blend")
    _replace(Path(scene["loop_shot"]["clip_path"]), seamless)
    monkeypatch.setattr(loop_health, "measure", _boom)
    jobs.render_ambient_job(sid)
    assert auth_client.get(f"/api/ambient/{sid}").json()["latest_render"]["status"] == "completed"


def test_a_page_render_of_an_unmeasured_loop_is_not_stopped_by_the_worker(auth_client, monkeypatch):
    """Asked from the page, the request already asked (seam_unmeasured) or
    the person accepted: the worker doesn't ask a second time."""
    from app.services import ambient, loop_health
    from app.workers import jobs

    scene = _scene(auth_client)
    sid = scene["id"]
    _replace(Path(scene["loop_shot"]["clip_path"]), seamless)
    monkeypatch.setattr(loop_health, "measure", _boom)
    token = ambient.render_claim_token(True)
    _claim(sid, token)
    jobs.render_ambient_job(sid, claim_token=token)
    assert auth_client.get(f"/api/ambient/{sid}").json()["latest_render"]["status"] == "completed"


# ---------- races at the seam stop ----------


def _at_the_stop(monkeypatch, fn) -> None:
    """Run `fn` (in its own session) right after the last cancel check,
    just before the seam stop is written."""
    from app.db import SessionLocal
    from app.services import ambient

    real = ambient.seam_needs_a_look

    def hooked(project, treatment=None):
        got = real(project, treatment)
        if got:
            db = SessionLocal()
            try:
                fn(db)
                db.commit()
            finally:
                db.close()
        return got

    monkeypatch.setattr(ambient, "seam_needs_a_look", hooked)


@pytest.mark.parametrize("batch", [True, False])
def test_a_cancel_just_before_the_seam_stop_is_kept(auth_client, monkeypatch, batch):
    from app import models
    from app.db import SessionLocal
    from app.services import ambient

    scene = _scene(auth_client)
    sid = scene["id"]
    _replace(Path(scene["loop_shot"]["clip_path"]), hard_cut)
    assert auth_client.post(f"/api/ambient/{sid}/loop-health").status_code == 200

    def cancel(db):
        # What POST /projects/{id}/cancel writes.
        db.query(models.Render).filter(models.Render.project_id == sid,
                                       models.Render.status == "rendering").update(
            {"status": "cancelled"}, synchronize_session=False)
        db.query(models.VideoProject).filter(models.VideoProject.id == sid).update(
            {"status": "cancelled"}, synchronize_session=False)

    _at_the_stop(monkeypatch, cancel)
    db = SessionLocal()
    try:
        render = ambient.render_scene(db, sid, batch=batch)
        assert render.status == "cancelled" and render.error == ""
    finally:
        db.close()
    out = auth_client.get(f"/api/ambient/{sid}").json()
    assert out["status"] == "cancelled"
    assert out["latest_render"]["status"] == "cancelled" and out["latest_render"]["error"] == ""
    db = SessionLocal()
    try:
        assert not db.query(models.Event).filter(models.Event.project_id == sid,
                                                 models.Event.status.in_(("failed", "needs_look"))).count()
    finally:
        db.close()


def test_a_batch_seam_stop_leaves_a_newer_claim_alone(auth_client, monkeypatch):
    from app import models
    from app.db import SessionLocal
    from app.services import ambient
    from app.workers import jobs

    scene = _scene(auth_client)
    sid = scene["id"]
    _replace(Path(scene["loop_shot"]["clip_path"]), hard_cut)

    def newer_claim(db):
        # A newer Generate claimed the scene (a conditional UPDATE with a token).
        db.query(models.VideoProject).filter(models.VideoProject.id == sid).update(
            {"status": "generating", "claim_token": "newer"}, synchronize_session=False)

    _at_the_stop(monkeypatch, newer_claim)
    jobs.render_ambient_job(sid)
    db = SessionLocal()
    try:
        p = db.get(models.VideoProject, sid)
        assert p.status == "generating" and p.claim_token == "newer"
        r = db.query(models.Render).filter(models.Render.project_id == sid).order_by(models.Render.id.desc()).first()
        assert r.status == "cancelled" and r.error == ambient.SEAM_STOP_MESSAGE
        assert db.query(models.Event).filter(models.Event.project_id == sid,
                                             models.Event.status == "needs_look").count() == 1
    finally:
        db.close()


def test_a_loop_that_snaps_to_its_last_frame_asks_before_rendering(auth_client):
    """The host's snap: the last frame lands on the target, so the join
    alone reads seamless, but the take jumps to it in its last two frames.
    It is treated like a visible jump: no blend covers it (a blend over
    that tail double-exposes), so a render asks first."""
    from .test_loop_health import _scroll

    scene = _scene(auth_client)
    sid = scene["id"]
    _replace(Path(scene["loop_shot"]["clip_path"]), lambda p: _scroll(p, "if(gte(n,94),0,n)"))
    assert auth_client.post(f"/api/ambient/{sid}/loop-health").status_code == 200
    out = auth_client.get(f"/api/ambient/{sid}").json()
    assert out["loop_health"]["seam"]["verdict"] == "snap"
    assert out["loop_health"]["seam"]["level"] == "bad"
    st = out["seam_treatment"]
    assert st["verdict"] == "snap" and st["covers"] is False and st["blend_seconds"] >= 2.0
    r = auth_client.post(f"/api/ambient/{sid}/render")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "seam_jump"
    # Its join step ratio is under 1, so the words say how it jumps instead.
    msg = r.json()["detail"]["message"]
    assert "jumps to its last frame at the very end" in msg and "normal frame step" not in msg
    assert auth_client.post(f"/api/ambient/{sid}/render?accept_seam=true").status_code == 202


def _rewrite_reading(sid: int, shot_id: int, **changes) -> None:
    """Edit a stored reading as an older build would have left it."""
    from sqlalchemy.orm.attributes import flag_modified

    from app import models
    from app.db import SessionLocal
    from app.services import loop_health

    db = SessionLocal()
    try:
        p = db.get(models.VideoProject, sid)
        data = dict(p.ambient_config_json or {})
        health = dict(data[loop_health.STATE_KEY])
        entry = dict(health[str(shot_id)])
        for k, v in changes.items():
            if v is None:
                entry.pop(k, None)
            else:
                entry[k] = v
        health[str(shot_id)] = entry
        data[loop_health.STATE_KEY] = health
        p.ambient_config_json = data
        flag_modified(p, "ambient_config_json")
        db.commit()
    finally:
        db.close()


def test_a_reading_from_before_the_snap_check_is_measured_again(auth_client):
    """A snapping clip measured by an older build was kept as "seamless":
    that reading has no version, so it is not current, and the render
    measures again, finds the snap and asks."""
    from .test_loop_health import _scroll

    scene = _scene(auth_client)
    sid, shot_id = scene["id"], scene["loop_shot"]["id"]
    _replace(Path(scene["loop_shot"]["clip_path"]), lambda p: _scroll(p, "if(gte(n,94),0,n)"))
    assert auth_client.post(f"/api/ambient/{sid}/loop-health").status_code == 200
    old_seam = {"verdict": "seamless", "level": "good", "text": "The join is seamless",
                "difference": 0.0, "typical": 0.01, "ratio": 0.0}
    _rewrite_reading(sid, shot_id, version=None, seam=old_seam)
    out = auth_client.get(f"/api/ambient/{sid}").json()
    assert out["loop_health"] is None  # not current: read before the snap check
    assert out["loop_health_outdated"] == [True]  # the page asks to measure again
    r = auth_client.post(f"/api/ambient/{sid}/render")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "seam_jump"
    assert auth_client.get(f"/api/ambient/{sid}").json()["loop_health"]["seam"]["verdict"] == "snap"


@pytest.mark.parametrize("batch", [True, False])
def test_a_variation_that_cannot_be_measured_stops_an_unaccepted_render(auth_client, monkeypatch, batch):
    """The loop clip reads seamless but a variation the render repeats can't
    be measured: the scene's treatment rests on the loop clip alone, so an
    unaccepted render stops and asks (page or batch) instead of repeating
    the unmeasured clip with a short hedge."""
    from app.services import ambient, loop_health
    from app.workers import jobs

    _no_alerts(monkeypatch)
    scene = _scene(auth_client, variety="takes", variations=2)
    sid = scene["id"]
    assert scene["seam_treatment"]["verdict"] == "seamless"
    variant = scene["variant_shots"][0]
    _replace(Path(variant["clip_path"]), seamless)  # its reading is stale
    real = loop_health.measure

    def fail_on_the_variation(clip, *a, **kw):
        if str(clip) == variant["clip_path"]:
            raise RuntimeError("ffmpeg took too long")
        return real(clip, *a, **kw)

    monkeypatch.setattr(loop_health, "measure", fail_on_the_variation)
    if batch:
        jobs.render_ambient_job(sid)
    else:
        token = ambient.render_claim_token(False)
        _claim(sid, token)
        jobs.render_ambient_job(sid, claim_token=token)
    latest = auth_client.get(f"/api/ambient/{sid}").json()["latest_render"]
    assert latest["error"] == ambient.SEAM_UNMEASURED_MESSAGE
    assert not latest["final_video_path"]
    # Accepted, it renders.
    token = ambient.render_claim_token(True)
    _claim(sid, token)
    jobs.render_ambient_job(sid, claim_token=token)
    assert auth_client.get(f"/api/ambient/{sid}").json()["latest_render"]["status"] == "completed"


def test_a_batch_snap_needs_look_says_how_it_jumps(auth_client, monkeypatch):
    """A snap's join step ratio is under 1: "(0.49x a normal frame step)"
    read like no jump. The needs_look event says it jumps at the very end."""
    from app import models
    from app.db import SessionLocal
    from app.services import ambient, events

    from .test_loop_health import _scroll

    scene = _scene(auth_client)
    sid = scene["id"]
    _replace(Path(scene["loop_shot"]["clip_path"]), lambda p: _scroll(p, "if(gte(n,94),0,n)"))
    assert auth_client.post(f"/api/ambient/{sid}/loop-health").status_code == 200
    db = SessionLocal()
    try:
        p = db.get(models.VideoProject, sid)
        events.ambient_seam_event(db, p, ambient.seam_treatment(p))
        db.commit()
        ev = db.query(models.Event).filter(models.Event.project_id == sid,
                                           models.Event.status == "needs_look").one()
        assert "the loop clip jumps to its last frame at the very end" in ev.detail
        assert "normal frame step" not in ev.detail
    finally:
        db.close()

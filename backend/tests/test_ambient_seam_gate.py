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


def test_a_jumping_variation_with_no_reading_is_measured_and_gated(auth_client):
    """The loop clip's reading is current but a variation's clip changed
    since: POST /render measures the variation too, and its visible jump
    asks first (the scene's treatment is the worst join)."""
    scene = _scene(auth_client, variety="takes", variations=2)
    sid = scene["id"]
    assert len(scene["variant_shots"]) == 1
    assert scene["seam_treatment"]["verdict"] == "seamless"
    _replace(Path(scene["variant_shots"][0]["clip_path"]), hard_cut)
    r = auth_client.post(f"/api/ambient/{sid}/render")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "seam_jump"
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


def test_the_inline_measurement_uses_a_short_timeout(auth_client, monkeypatch):
    from app.api import ambient as ambient_api
    from app.services import loop_health

    scene = _scene(auth_client)
    sid = scene["id"]
    _replace(Path(scene["loop_shot"]["clip_path"]), seamless)
    seen = []
    real_measure = loop_health.measure

    def spy(clip, still=False, **kw):
        seen.append(kw.get("timeout"))
        return real_measure(clip, still=still, **kw)

    monkeypatch.setattr(loop_health, "measure", spy)
    assert auth_client.post(f"/api/ambient/{sid}/render").status_code == 202
    # The request's own reading is short; the worker had a current one.
    assert seen == [ambient_api.INLINE_MEASURE_TIMEOUT] and ambient_api.INLINE_MEASURE_TIMEOUT <= 30
    assert loop_health.MEASURE_TIMEOUT_SECONDS == 120  # the worker's is unchanged


def test_accept_seam_reaches_the_worker(auth_client, monkeypatch):
    """Render anyway after an unmeasured join: the worker measures, finds a
    visible jump, and renders it because the person said so."""
    from app.services import loop_health
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
    assert kwargs[-1]["accept_seam"] is True
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
    jobs.render_ambient_job(sid, claim_token="tok")
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

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
    monkeypatch.setattr(loop_health, "measure", lambda clip, still=False: real_measure(jump, still=still))
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

    def spy_unit(src, dst, *, crossfade):
        xfades.append(crossfade)
        return real_unit(src, dst, crossfade=crossfade)

    monkeypatch.setattr(renderer, "_make_seamless_audio_unit", spy_unit)
    clip = seamless(tmp_path / "clip.mp4")
    music = tmp_path / "bed.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=330:duration=30",
                    str(music)], check=True)
    renderer.compose_ambient(renderer.AmbientRenderInputs(
        project_id=0, out_dir=tmp_path / "out", clip_path=clip, total_seconds=10,
        crossfade_seconds=1.0, fade_seconds=0.0, music_path=music))
    assert xfades and xfades[0] == 6.0

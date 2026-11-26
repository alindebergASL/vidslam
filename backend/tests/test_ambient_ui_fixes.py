"""Ambient Stage 1, quick UI fixes: settings that don't reach a clip made
from the scene's own picture keep the loop, the loop clip has a version for
cache-busting, a scene left "generating" with nothing making it is flagged
stuck, and the scene carries the prices of its paid buttons."""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from .conftest import make_png_bytes
from .test_ambient import SHORT


def _scene(client, **cfg) -> int:
    r = client.post("/api/ambient", json={"aspect_ratio": "16:9", "config": {**SHORT, **cfg}, "generate": False})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _own_picture_loop(client, **cfg) -> dict:
    """A video scene with an uploaded picture and its loop made."""
    sid = _scene(client, **cfg)
    r = client.post(f"/api/ambient/{sid}/picture",
                    files={"file": ("pic.png", make_png_bytes((1920, 1080), (30, 160, 60)), "image/png")})
    assert r.status_code == 200, r.text
    client.post(f"/api/ambient/{sid}/generate-loop")
    scene = client.get(f"/api/ambient/{sid}").json()
    assert scene["loop_status"] == "completed", scene
    return scene


def _patch(client, scene: dict, **changes):
    return client.patch(f"/api/ambient/{scene['id']}", json={"config": {**scene["config"], **changes}})


def test_look_and_preset_keep_a_loop_made_from_your_picture(auth_client):
    scene = _own_picture_loop(auth_client)
    r = _patch(auth_client, scene, look="watercolor", look_strength=0.9, scene_preset="ocean")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["loop_status"] == "completed" and out["config"]["look"] == "watercolor"

    # Time of day and weather reach the prompt (light_phrase): a new clip.
    r = _patch(auth_client, out, time_of_day="night")
    assert r.status_code == 409, r.text
    assert auth_client.get(f"/api/ambient/{scene['id']}").json()["loop_status"] == "completed"


def test_look_still_counts_without_your_own_picture(auth_client):
    sid = _scene(auth_client)
    auth_client.post(f"/api/ambient/{sid}/generate-loop")
    scene = auth_client.get(f"/api/ambient/{sid}").json()
    assert scene["loop_status"] == "completed"
    assert _patch(auth_client, scene, look="watercolor").status_code == 409
    assert _patch(auth_client, scene, scene_preset="ocean").status_code == 409


def test_clip_changed_rules():
    from app.schemas.ambient import AmbientConfig
    from app.services import ambient

    own = AmbientConfig(**SHORT, picture_origin="upload")
    assert not ambient._clip_changed(own, own.model_copy(update={"look": "anime", "look_strength": 0.1}))
    assert not ambient._clip_changed(own, own.model_copy(update={"scene_preset": "ocean"}))
    assert ambient._clip_changed(own, own.model_copy(update={"weather": "rain"}))
    # Indoors, the time of day is "outside": switching to an indoor preset changes the prompt.
    lit = own.model_copy(update={"time_of_day": "night"})
    assert ambient._clip_changed(lit, lit.model_copy(update={"scene_preset": "fireplace"}))
    # A generated picture, Day to night and Still + drift still use them.
    gen = own.model_copy(update={"picture_origin": "generated"})
    assert ambient._clip_changed(gen, gen.model_copy(update={"look": "anime"}))
    dn = own.model_copy(update={"variety": "day_night"})
    assert ambient._clip_changed(dn, dn.model_copy(update={"look": "anime"}))
    drift = own.model_copy(update={"source": "image_drift"})
    assert ambient._clip_changed(drift, drift.model_copy(update={"scene_preset": "ocean"}))


def test_clip_version_changes_after_a_second_generate(auth_client):
    sid = _scene(auth_client, music_mood="none")
    assert auth_client.get(f"/api/ambient/{sid}").json()["clip_version"] == ""
    auth_client.post(f"/api/ambient/{sid}/generate-loop")
    v1 = auth_client.get(f"/api/ambient/{sid}").json()["clip_version"]
    time.sleep(0.02)
    auth_client.post(f"/api/ambient/{sid}/generate-loop")
    v2 = auth_client.get(f"/api/ambient/{sid}").json()["clip_version"]
    assert v1 and v2 and v1 != v2


def _set(sid: int, **values) -> None:
    from app import models
    from app.db import SessionLocal

    db = SessionLocal()
    p = db.get(models.VideoProject, sid)
    for k, v in values.items():
        setattr(p, k, v)
    db.commit()
    # updated_at has onupdate: put it back where the test wants it.
    if "updated_at" in values:
        db.query(models.VideoProject).filter(models.VideoProject.id == sid).update(
            {"updated_at": values["updated_at"]}, synchronize_session=False)
        db.commit()
    db.close()


def test_stuck_flag(auth_client, monkeypatch):
    from app import models
    from app.db import SessionLocal
    from app.workers import queue

    state = {"v": "gone"}
    monkeypatch.setattr(queue, "queue_entry_state", lambda job_id: state["v"])
    sid = _scene(auth_client)
    get = lambda: auth_client.get(f"/api/ambient/{sid}").json()["stuck"]  # noqa: E731
    long_ago = datetime.utcnow() - timedelta(hours=1)

    assert get() is False  # a draft is never stuck
    _set(sid, status="generating", queue_job_id="rq-123", updated_at=long_ago)
    assert get() is True
    # In the list too.
    row = next(s for s in auth_client.get("/api/ambient").json() if s["id"] == sid)
    assert row["stuck"] is True

    state["v"] = "alive"  # still waiting in a busy queue
    assert get() is False
    state["v"] = "unknown"  # Redis didn't answer: don't guess
    assert get() is False
    state["v"] = "gone"

    _set(sid, updated_at=datetime.utcnow())  # just claimed: give it a moment
    assert get() is False
    _set(sid, updated_at=long_ago)

    db = SessionLocal()
    job = models.VideoJob(purpose="ambient_segment", ref_id=0, project_id=sid, provider="mock", model="mock",
                          provider_job_id="x", status="submitted")
    db.add(job)
    db.commit()
    assert get() is False  # a clip is still being made
    job.status = "completed"
    db.commit()
    db.query(models.VideoJob).filter(models.VideoJob.id == job.id).update(
        {"updated_at": long_ago}, synchronize_session=False)
    db.commit()
    assert get() is True

    render = models.Render(project_id=sid, status="rendering")
    db.add(render)
    db.commit()
    assert get() is False  # a live render
    render.status = "failed"
    db.commit()
    db.close()
    assert get() is True


def test_generation_records_its_queue_entry(auth_client, monkeypatch):
    from app import models
    from app.api import ambient as api_ambient
    from app.db import SessionLocal

    monkeypatch.setattr(api_ambient, "enqueue", lambda *a, **k: "rq-job-42")
    sid = _scene(auth_client)
    r = auth_client.post(f"/api/ambient/{sid}/generate-loop")
    assert r.status_code == 202, r.text
    db = SessionLocal()
    assert db.get(models.VideoProject, sid).queue_job_id == "rq-job-42"
    db.close()


def test_cost_fields(auth_client):
    from app.config import get_settings

    s = get_settings()
    sid = _scene(auth_client, sound="rain")
    out = auth_client.get(f"/api/ambient/{sid}").json()
    for k in ("music_cost", "sound_cost", "picture_cost"):
        assert k in out and out[k] >= 0
    assert out["music_cost"] >= s.cost_music_per_generation
    assert out["sound_cost"] == round(s.cost_sound_per_generation, 4)

    quiet = auth_client.get(f"/api/ambient/{_scene(auth_client, music_mood='none')}").json()
    assert quiet["music_cost"] == 0 and quiet["sound_cost"] == 0


def test_stuck_follows_what_cancel_can_free(auth_client, monkeypatch):
    """Stuck is only shown where the page's Reset (the project cancel) can
    free the scene, and never while a loop chain is still working."""
    from app.services.pipeline import HOOK_CLAIM
    from app.workers import queue

    monkeypatch.setattr(queue, "queue_entry_state", lambda job_id: "gone")
    sid = _scene(auth_client)
    get = lambda: auth_client.get(f"/api/ambient/{sid}").json()["stuck"]  # noqa: E731
    long_ago = datetime.utcnow() - timedelta(hours=1)

    # A claim no job took (its entry lost): stuck, and Reset frees it.
    _set(sid, status="generating", claim_token="tok", queue_job_id="rq-lost", updated_at=long_ago)
    assert get() is True
    r = auth_client.post(f"/api/projects/{sid}/cancel")
    assert r.status_code == 200 and r.json()["action"] == "released", r.text
    assert auth_client.get(f"/api/ambient/{sid}").json()["stuck"] is False

    # The job took the claim but ran inline / in the background: its entry
    # always reads as gone, so it can't tell; left to the sweeper.
    _set(sid, status="generating", claim_token="", queue_job_id="inline:generate_ambient_loop_job",
         updated_at=long_ago)
    assert get() is False
    _set(sid, queue_job_id="", updated_at=long_ago)
    assert get() is False

    # A loop chain under way (between clips, or making the music bed): working.
    from app import models
    from app.db import SessionLocal

    def chain(value):
        db = SessionLocal()
        p = db.get(models.VideoProject, sid)
        p.ambient_config_json = {**(p.ambient_config_json or {}), "_chain": value}
        db.commit()
        db.close()
        _set(sid, updated_at=long_ago)

    _set(sid, queue_job_id="rq-1")
    chain({"segment_ids": [], "variant": 0})
    assert get() is False
    chain({"segment_ids": [], "variant": 0, "failed": True})
    assert get() is True

    # The hook lab's claim is stopped there, never here.
    _set(sid, claim_token=f"{HOOK_CLAIM}planned", updated_at=long_ago)
    assert get() is False


def test_batch_records_its_queue_entry(auth_client, monkeypatch):
    from app import models
    from app.api import ambient as api_ambient
    from app.db import SessionLocal

    monkeypatch.setattr(api_ambient, "enqueue", lambda *a, **k: "rq-batch-7")
    r = auth_client.post("/api/ambient/batch", json={
        "presets": ["ocean", "waterfall"], "render": False, "aspect_ratio": "16:9", "config": SHORT})
    assert r.status_code == 201, r.text
    db = SessionLocal()
    assert {db.get(models.VideoProject, i).queue_job_id for i in r.json()["scene_ids"]} == {"rq-batch-7"}
    db.close()


def test_queue_entry_not_written_over_a_finished_scene(auth_client, monkeypatch):
    """A job that already finished (inline, or a fast worker) leaves the
    scene alone: no entry is recorded after it."""
    from app import models
    from app.api import ambient as api_ambient
    from app.db import SessionLocal

    def finish(*a, **k):
        _set(sid, status="planned", claim_token="")
        return "rq-done"

    monkeypatch.setattr(api_ambient, "enqueue", finish)
    sid = _scene(auth_client)
    assert auth_client.post(f"/api/ambient/{sid}/generate-loop").status_code == 202
    db = SessionLocal()
    assert db.get(models.VideoProject, sid).queue_job_id == ""
    db.close()


def test_sound_resolved_says_whether_generate_sound_has_a_sound_to_make(auth_client):
    """"Match the scene" on a custom scene with no weather matches nothing:
    the page then offers no free Generate sound that the server refuses."""
    out = auth_client.get(f"/api/ambient/{_scene(auth_client, sound='rain')}").json()
    assert out["sound_resolved"] is True
    sid = _scene(auth_client, sound="auto", scene_preset="custom", weather="any", scene_prompt="a quiet room")
    out = auth_client.get(f"/api/ambient/{sid}").json()
    assert out["sound_resolved"] is False and out["sound_cost"] == 0
    assert auth_client.post(f"/api/ambient/{sid}/sound").status_code == 409


def _killed_mid_drift(auth_client, monkeypatch) -> int:
    """A Still + drift scene whose worker was killed after the shot went
    "submitted": generating, its queue entry gone, nothing making the clip."""
    from app import models
    from app.db import SessionLocal
    from app.workers import queue

    monkeypatch.setattr(queue, "queue_entry_state", lambda job_id: "gone")
    sid = _scene(auth_client, source="image_drift")
    auth_client.post(f"/api/ambient/{sid}/generate-loop")
    db = SessionLocal()
    shots = db.query(models.VideoShot).filter(models.VideoShot.project_id == sid).all()
    assert shots
    for s in shots:
        s.status = "submitted"
    db.commit()
    db.close()
    _set(sid, status="generating", claim_token="", queue_job_id="rq-lost",
         updated_at=datetime.utcnow() - timedelta(hours=1))
    scene = auth_client.get(f"/api/ambient/{sid}").json()
    assert scene["stuck"] is True and scene["loop_status"] == "submitted", scene
    return sid


def test_reset_on_a_stuck_drift_scene_frees_its_clip_too(auth_client, monkeypatch):
    """Reset leaves no clip marked in flight: the page is usable again."""
    sid = _killed_mid_drift(auth_client, monkeypatch)
    r = auth_client.post(f"/api/projects/{sid}/cancel")
    assert r.status_code == 200 and r.json()["action"] == "released", r.text
    scene = auth_client.get(f"/api/ambient/{sid}").json()
    assert scene["status"] == "cancelled" and scene["loop_status"] == "failed", scene
    assert scene["stuck"] is False


def test_the_sweeper_frees_a_stuck_drift_scenes_clip_too(auth_client, monkeypatch):
    from app.db import SessionLocal
    from app.services import sweepers

    sid = _killed_mid_drift(auth_client, monkeypatch)
    db = SessionLocal()
    try:
        sweepers.sweep_renders(db, now=datetime.utcnow() + timedelta(hours=6))
    finally:
        db.close()
    scene = auth_client.get(f"/api/ambient/{sid}").json()
    assert scene["status"] == "failed" and scene["loop_status"] == "failed", scene


def test_a_removed_pictures_placeholder_describes_nothing(auth_client):
    """/new stores a placeholder description for a picture scene with none:
    it pays for no generated picture, it is cleared when the picture is
    removed, and an older stored one buys no loop of nothing."""
    sid = _scene(auth_client, scene_preset="custom", scene_prompt="the scene in the given image")
    r = auth_client.post(f"/api/ambient/{sid}/picture",
                         files={"file": ("pic.png", make_png_bytes((1920, 1080), (30, 160, 60)), "image/png")})
    assert r.status_code == 200, r.text
    assert auth_client.post(f"/api/ambient/{sid}/picture/generate?discard_loop=true").status_code == 422
    assert auth_client.request("DELETE", f"/api/ambient/{sid}/picture").status_code in (200, 204)
    assert auth_client.get(f"/api/ambient/{sid}").json()["config"]["scene_prompt"] == ""

    old = _scene(auth_client, scene_preset="custom", scene_prompt="the scene in the given image")
    assert auth_client.post(f"/api/ambient/{old}/generate-loop").status_code == 422
    assert auth_client.post(f"/api/ambient/{old}/picture/generate").status_code == 422


def test_presets_say_what_match_the_scene_picks(auth_client):
    out = auth_client.get("/api/ambient/presets").json()
    assert out["auto_sound_by_weather"].get("light_rain") == "rain"
    assert "custom" not in out["auto_sound_by_scene"] and out["auto_sound_by_scene"]

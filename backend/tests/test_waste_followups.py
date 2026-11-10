"""Follow-ups to the "don't waste paid work" changes:

- a video job whose download was interrupted (stuck in "completing") stops
  counting as being made, and the sweeper fails it;
- Retry on a project rendered before voiceover fingerprints existed reuses
  the voiceover instead of buying it again;
- an empty spoken script renders without a voice instead of failing;
- a lip-synced shot is not re-rolled against an out-of-date voiceover;
- re-plan with "keep my edits" never makes a finished clip out of date;
- the ambient picture is made in the first variation's light;
- resuming an ambient loop keeps the model the kept clips were made with.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from .test_ambient import SHORT
from .test_ambient_waste import CHAIN, _scene, _segments, _spy_submits
from .test_regen_lipsync import _jobs, _lipsync_on
from .test_review_fixes import _talking_project
from .test_storyboard_waste import (
    _body,
    _count_tts,
    _planned,
    _put_shot_in_flight,
    _rendered,
    _video_jobs,
)


def _db():
    from app.db import SessionLocal

    return SessionLocal()


# ---------- a job stuck in "completing" ----------


def _stick(job_id: int, *, minutes_ago: float) -> None:
    """The worker that claimed the job for download died `minutes_ago`."""
    from app import models

    db = _db()
    db.query(models.VideoJob).filter_by(id=job_id).update(
        {"status": "completing", "updated_at": datetime.utcnow() - timedelta(minutes=minutes_ago)})
    db.commit()
    db.close()


def test_a_job_being_downloaded_right_now_still_counts_as_in_flight(auth_client):
    from app.services import video_jobs

    pid = _rendered(auth_client)
    _shot_id, job_id = _put_shot_in_flight(pid)
    _stick(job_id, minutes_ago=0)
    assert auth_client.get(f"/api/projects/{pid}/render-plan").json()["in_flight"] == 1
    db = _db()
    assert video_jobs.pending_for(db, project_id=pid) == 1
    assert video_jobs.fail_stuck_completing(db) == 0
    db.close()


def test_a_job_stuck_in_completing_does_not_block_later_renders(auth_client):
    from app import models
    from app.services import video_jobs

    pid = _rendered(auth_client)
    shot_id, job_id = _put_shot_in_flight(pid)
    _stick(job_id, minutes_ago=30)
    plan = auth_client.get(f"/api/projects/{pid}/render-plan").json()
    assert plan["in_flight"] == 0 and plan["generate"] == 1, "a dead job is not 'already being made'"
    db = _db()
    assert video_jobs.pending_for(db, project_id=pid) == 0
    db.close()

    before = _video_jobs(pid)
    r = auth_client.post(f"/api/projects/{pid}/retry")
    assert r.json()["action"] == "resume" and r.json()["clips_in_flight"] == 0
    assert _video_jobs(pid) == before + 1, "the shot is submitted again"
    st = auth_client.get(f"/api/projects/{pid}/status").json()
    assert st["project_status"] == "completed" and st["latest_render"]["status"] == "completed"

    # The sweeper fails the dead job; the clip made since is not touched.
    db = _db()
    assert video_jobs.fail_stuck_completing(db) == 1
    assert db.get(models.VideoJob, job_id).status == "failed"
    assert video_jobs.fail_stuck_completing(db) == 0
    db.close()
    st = auth_client.get(f"/api/projects/{pid}/status").json()
    shot = next(s for s in st["shots"] if s["id"] == shot_id)
    assert shot["status"] == "completed" and Path(shot["clip_path"]).exists()
    assert st["project_status"] == "completed"


def test_the_sweeper_fails_a_stuck_job_so_the_shot_can_be_retried(auth_client):
    from app import models
    from app.services import video_jobs

    pid = _rendered(auth_client)
    shot_id, job_id = _put_shot_in_flight(pid)
    _stick(job_id, minutes_ago=30)
    video_jobs.sweep()
    db = _db()
    job = db.get(models.VideoJob, job_id)
    assert job.status == "failed" and "interrupted" in job.error
    assert db.get(models.VideoShot, shot_id).status == "failed"
    db.close()
    assert auth_client.post(f"/api/projects/{pid}/retry").json()["action"] == "resume"
    assert auth_client.get(f"/api/projects/{pid}/status").json()["project_status"] == "completed"


# ---------- a voiceover made before fingerprints existed ----------


def _forget_voice_fingerprints(pid: int, *, fail_render: bool = True) -> None:
    from app import models

    db = _db()
    db.query(models.Render).filter_by(project_id=pid).update({"voice_fingerprint": ""})
    if fail_render:
        latest = db.query(models.Render).filter_by(project_id=pid).order_by(models.Render.id.desc()).first()
        latest.status = "failed"
        db.get(models.VideoProject, pid).status = "failed"
    db.commit()
    db.close()


def test_retry_on_a_project_from_before_voice_fingerprints_is_a_free_recompose(auth_client, monkeypatch):
    from app import models

    calls = _count_tts(monkeypatch)
    pid = _rendered(auth_client)
    _forget_voice_fingerprints(pid)
    jobs = _video_jobs(pid)
    r = auth_client.post(f"/api/projects/{pid}/retry").json()
    assert r["action"] == "recompose" and r["voiceover"] == "reuse" and r["voiceover_cost"] == 0
    assert len(calls) == 1, "the voiceover on disk is used, not bought again"
    assert _video_jobs(pid) == jobs
    assert auth_client.get(f"/api/projects/{pid}/status").json()["project_status"] == "completed"
    db = _db()
    latest = db.query(models.Render).filter_by(project_id=pid).order_by(models.Render.id.desc()).first()
    assert latest.voice_fingerprint, "the reused voiceover is known from now on"
    db.close()
    assert auth_client.get(f"/api/projects/{pid}/render-plan").json()["voiceover"] == "reuse"


def test_retry_that_remakes_a_clip_on_an_old_project_keeps_the_voiceover(auth_client, monkeypatch):
    from app import models

    calls = _count_tts(monkeypatch)
    pid = _rendered(auth_client)
    _forget_voice_fingerprints(pid)
    db = _db()
    shot = db.query(models.VideoShot).filter_by(project_id=pid).order_by(models.VideoShot.shot_order).first()
    Path(shot.clip_path).unlink()
    shot.clip_path, shot.status, shot.error = "", "failed", "simulated"
    db.commit()
    db.close()
    r = auth_client.post(f"/api/projects/{pid}/retry").json()
    assert r["action"] == "resume" and r["voiceover"] == "reuse"
    assert len(calls) == 1
    assert auth_client.get(f"/api/projects/{pid}/status").json()["project_status"] == "completed"


def test_retry_says_when_it_buys_a_new_voiceover(auth_client, monkeypatch):
    calls = _count_tts(monkeypatch)
    pid = _rendered(auth_client)
    auth_client.patch(f"/api/projects/{pid}/plan", json={"cleaned_voice_script": "A different line to speak."})
    r = auth_client.post(f"/api/projects/{pid}/retry").json()
    assert r["action"] == "resume" and r["voiceover"] == "new"
    assert len(calls) == 2


# ---------- nothing to say ----------


def test_an_empty_spoken_script_renders_without_a_voice(auth_client, monkeypatch):
    from app.providers import mock_tts

    pid = _planned(auth_client)
    assert auth_client.patch(f"/api/projects/{pid}/plan", json={"cleaned_voice_script": "  "}).status_code == 200

    def reject_empty(self, *, text, voice_id, settings=None):
        raise RuntimeError("400 input text is empty")

    monkeypatch.setattr(mock_tts.MockTTSProvider, "synthesize", reject_empty)
    plan = auth_client.get(f"/api/projects/{pid}/render-plan").json()
    assert plan["voiceover"] == "silent" and plan["voiceover_cost"] == 0
    voice = next(c for c in auth_client.get(f"/api/projects/{pid}/preflight").json()["checks"]
                 if c["id"] == "voice_selected")
    assert voice["status"] == "warn" and "without a voice" in voice["message"]

    assert auth_client.post(f"/api/projects/{pid}/generate-video?force=true").status_code == 202
    st = auth_client.get(f"/api/projects/{pid}/status").json()
    assert st["project_status"] == "completed", st["latest_render"]["error"]
    assert st["latest_render"]["audio_path"] == ""


# ---------- re-rolling a lip-synced shot against an old voiceover ----------


def test_lip_sync_shot_is_not_rerolled_while_the_voiceover_is_out_of_date(auth_client):
    _lipsync_on(auth_client)
    pid = _talking_project(auth_client)
    auth_client.post(f"/api/projects/{pid}/generate-video?force=true")
    shots = _body(auth_client, pid)
    talking = next(s for s in shots if s["shot_type"] == "talking_head")
    rows = auth_client.get(f"/api/projects/{pid}/render-plan").json()["shots"]
    assert not any(r["needs_new_voiceover"] for r in rows)

    auth_client.patch(f"/api/projects/{pid}/plan", json={"cleaned_voice_script": "Completely new words to say."})
    rows = {r["id"]: r for r in auth_client.get(f"/api/projects/{pid}/render-plan").json()["shots"]}
    assert rows[talking["id"]]["needs_new_voiceover"] is True
    assert not any(r["needs_new_voiceover"] for i, r in rows.items() if i != talking["id"])

    before = _video_jobs(pid)
    r = auth_client.post(f"/api/projects/{pid}/shots/{talking['id']}/regenerate?recompose=false")
    assert r.status_code == 409 and "Generate video" in r.json()["detail"], r.text
    r = auth_client.post(f"/api/projects/{pid}/shots/regenerate-bulk", json={"shot_ids": [s["id"] for s in shots]})
    assert r.status_code == 409 and f"Shot {talking['shot_order']} is" in r.json()["detail"], r.text
    assert _video_jobs(pid) == before, "nothing was paid for"

    # Generate video makes the voiceover and the clip together; then a re-roll is fine again.
    auth_client.post(f"/api/projects/{pid}/generate-video?force=true")
    assert _jobs(pid, "avatar") == 2
    r = auth_client.post(f"/api/projects/{pid}/shots/{talking['id']}/regenerate?recompose=false")
    assert r.status_code == 202, r.text
    assert not any(s["clip_stale"] for s in _body(auth_client, pid))


def test_other_shots_can_still_be_rerolled_while_the_voiceover_is_out_of_date(auth_client):
    pid = _rendered(auth_client)
    auth_client.patch(f"/api/projects/{pid}/plan", json={"cleaned_voice_script": "A different line to speak."})
    shot = _body(auth_client, pid)[0]
    r = auth_client.post(f"/api/projects/{pid}/shots/{shot['id']}/regenerate?recompose=false")
    assert r.status_code == 202, r.text


# ---------- re-plan, keep my edits ----------


def _planner_changes_its_mind(monkeypatch) -> None:
    """Every new plan words the camera direction differently and flips the
    reference strategy, as a real planner does."""
    from app.services import pipeline

    real = pipeline.get_chat

    def patched():
        chat = real()
        inner = chat.generate_storyboard

        def generate_storyboard(**kw):
            plan = inner(**kw)
            for s in plan.shots:
                s.camera_direction = f"a new move nobody asked for ({s.order})"
                s.reference_strategy = ("frame_images" if s.reference_strategy == "input_references"
                                        else "input_references")
            return plan

        chat.generate_storyboard = generate_storyboard
        return chat

    monkeypatch.setattr(pipeline, "get_chat", patched)


def test_replan_keeping_edits_keeps_every_finished_clip_current(auth_client, monkeypatch):
    _lipsync_on(auth_client)
    pid = _talking_project(auth_client)
    auth_client.post(f"/api/projects/{pid}/generate-video?force=true")
    before = {s["id"]: s for s in _body(auth_client, pid)}
    assert any(s["provider"] == "lipsync" for s in before.values())

    _planner_changes_its_mind(monkeypatch)
    assert auth_client.post(f"/api/projects/{pid}/generate-plan?preserve_edits=true").status_code == 202
    after = {s["id"]: s for s in _body(auth_client, pid)}
    assert set(after) == set(before)
    assert not any(s["clip_stale"] for s in after.values())
    for sid, s in after.items():
        assert s["clip_path"] == before[sid]["clip_path"]
        assert s["camera_direction"] == before[sid]["camera_direction"]
        assert s["reference_strategy"] == before[sid]["reference_strategy"]
    plan = auth_client.get(f"/api/projects/{pid}/render-plan").json()
    assert plan["generate"] == 0 and plan["clip_cost"] == 0, "the next render buys no clip again"


def test_replan_keeping_edits_still_refreshes_shots_that_have_no_clip(auth_client, monkeypatch):
    pid = _planned(auth_client)
    _planner_changes_its_mind(monkeypatch)
    auth_client.post(f"/api/projects/{pid}/generate-plan?preserve_edits=true")
    assert all(s["camera_direction"].startswith("a new move") for s in _body(auth_client, pid))


# ---------- ambient: the picture is the first variation's first frame ----------


def test_picture_is_made_in_the_light_of_the_first_variation(auth_client, monkeypatch):
    from app.schemas.ambient import AmbientConfig
    from app.services import ambient as svc

    cfg = AmbientConfig(**{**SHORT, "variety": "day_night", "variations": 3})
    first = svc.still_prompt(svc.variant_config(cfg, 0))
    assert svc.picture_prompt(cfg) == first != svc.still_prompt(cfg)
    plain = AmbientConfig(**SHORT)
    assert svc.picture_prompt(plain) == svc.still_prompt(plain)

    prompts: list[str] = []
    real = svc.generate_still

    def spy(db, c, *, prompt, **kw):
        prompts.append(prompt)
        return real(db, c, prompt=prompt, **kw)

    monkeypatch.setattr(svc, "generate_still", spy)
    sid = _scene(auth_client, generate=False, variety="day_night", variations=3)["id"]
    assert auth_client.post(f"/api/ambient/{sid}/picture/generate").status_code == 200
    db = _db()
    from app import models

    saved = svc._config_of(db.get(models.VideoProject, sid))
    db.close()
    assert prompts == [svc.still_prompt(svc.variant_config(saved.model_copy(update={"picture_origin": ""}), 0))]


# ---------- ambient: resume keeps the model the kept clips were made with ----------


def test_resume_keeps_the_model_the_loop_was_planned_for(auth_client, monkeypatch):
    from app.services import ambient as svc

    calls = _spy_submits(monkeypatch, fail_on={3})
    scene = _scene(auth_client, **CHAIN)
    sid = scene["id"]
    model = scene["resume"]["model"]
    assert model and scene["resume"]["model_changed"] is False
    assert [s["model_override"] for s in _segments(auth_client, sid)] == [model] * 3

    # The default video model is changed in Settings before resuming.
    real = svc._active_model
    monkeypatch.setattr(svc, "_active_model", lambda kind: "other/video-model" if kind == "video" else real(kind))
    scene = auth_client.get(f"/api/ambient/{sid}").json()
    assert scene["resume"]["model"] == model and scene["resume"]["model_changed"] is True

    # A chain kept from before clips were pinned to their model.
    from app import models

    db = _db()
    db.query(models.VideoShot).filter_by(project_id=sid, shot_type="ambient_segment").update({"model_override": ""})
    db.commit()
    db.close()
    assert auth_client.post(f"/api/ambient/{sid}/resume-loop").status_code == 202
    assert len(calls) == 4
    assert _segments(auth_client, sid)[2]["model_override"] == model
    assert auth_client.get(f"/api/ambient/{sid}").json()["loop_status"] == "completed"

"""Storyboard renders must not waste money or silently lose work:

- a failed voiceover stops the render before any clip is submitted;
- an edited shot gets a new clip (and says so before the render);
- an unchanged voiceover is reused, a changed one is made new;
- a clip still being made is never submitted a second time;
- re-plan with "keep my edits" keeps the edited script, end card, captions
  and each shot's avoid text.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from .conftest import make_png_bytes
from .test_regen_lipsync import _jobs, _lipsync_on
from .test_review_fixes import _talking_project


def _planned(client) -> int:
    aid = client.post("/api/avatars", json={"name": "X"}).json()["id"]
    client.post(f"/api/avatars/{aid}/assets", files={"file": ("h.png", make_png_bytes(), "image/png")},
                data={"asset_type": "hero", "rights_confirmed": "true"})
    pid = client.post("/api/projects", json={
        "title": "W", "original_script": "one two three four five six seven eight", "cta_text": "Go",
        "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}]}).json()["id"]
    client.post(f"/api/projects/{pid}/generate-plan")
    return pid


def _rendered(client) -> int:
    pid = _planned(client)
    assert client.post(f"/api/projects/{pid}/generate-video").status_code == 202
    assert client.get(f"/api/projects/{pid}/status").json()["project_status"] == "completed"
    return pid


def _body(client, pid) -> list[dict]:
    return [s for s in client.get(f"/api/projects/{pid}").json()["shots"] if s["shot_type"] != "end_card"]


def _video_jobs(pid) -> int:
    from app import models
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        return db.query(models.VideoJob).filter_by(project_id=pid).count()
    finally:
        db.close()


def _count_tts(monkeypatch) -> list[str]:
    from app.providers import mock_tts

    calls: list[str] = []
    real = mock_tts.MockTTSProvider.synthesize

    def counting(self, *, text, voice_id, settings=None):
        calls.append(text)
        return real(self, text=text, voice_id=voice_id, settings=settings)

    monkeypatch.setattr(mock_tts.MockTTSProvider, "synthesize", counting)
    return calls


# ---------- a failed voiceover stops the render before clips ----------


def test_tts_failure_stops_before_any_clip_and_names_the_error(auth_client, monkeypatch):
    from app.providers import mock_tts

    pid = _planned(auth_client)

    def boom(self, *, text, voice_id, settings=None):
        raise RuntimeError("quota exceeded for this voice")

    with monkeypatch.context() as m:
        m.setattr(mock_tts.MockTTSProvider, "synthesize", boom)
        assert auth_client.post(f"/api/projects/{pid}/generate-video").status_code == 202
        st = auth_client.get(f"/api/projects/{pid}/status").json()
        assert st["project_status"] == "failed"
        assert st["latest_render"]["status"] == "failed"
        assert "Voiceover failed" in st["latest_render"]["error"]
        assert "quota exceeded for this voice" in st["latest_render"]["error"]
        assert _video_jobs(pid) == 0, "no clip may be submitted (and billed) without a voiceover"
        assert all(s["status"] == "pending" for s in _body(auth_client, pid))

    # Retry once the provider works again: the whole render goes through.
    r = auth_client.post(f"/api/projects/{pid}/retry")
    assert r.status_code == 202 and r.json()["action"] == "resume"
    assert auth_client.get(f"/api/projects/{pid}/status").json()["project_status"] == "completed"


def test_silent_project_still_renders_without_a_voiceover(auth_client):
    pid = _planned(auth_client)
    auth_client.patch(f"/api/projects/{pid}", json={"voiceover_source": "silent"})
    auth_client.post(f"/api/projects/{pid}/generate-video")
    st = auth_client.get(f"/api/projects/{pid}/status").json()
    assert st["project_status"] == "completed" and st["latest_render"]["audio_path"] == ""


# ---------- edited shots get a new clip; the editor says so first ----------


def test_editing_a_shot_marks_its_clip_stale_and_the_next_render_remakes_only_it(auth_client):
    pid = _rendered(auth_client)
    shots = _body(auth_client, pid)
    assert len(shots) >= 2 and not any(s["clip_stale"] for s in shots)
    edited, kept = shots[0], shots[1]

    auth_client.patch(f"/api/projects/{pid}/shots/{edited['id']}", json={"prompt": "a completely new prompt"})
    now = {s["id"]: s for s in _body(auth_client, pid)}
    assert now[edited["id"]]["clip_stale"] is True
    assert now[kept["id"]]["clip_stale"] is False

    plan = auth_client.get(f"/api/projects/{pid}/render-plan").json()
    assert plan["generate"] == 1 and plan["reuse"] == len(shots) - 1
    row = next(s for s in plan["shots"] if s["id"] == edited["id"])
    assert row["action"] == "generate" and "edited" in row["reason"] and row["cost"] > 0
    assert plan["voiceover"] == "reuse" and plan["voiceover_cost"] == 0

    before = _video_jobs(pid)
    auth_client.post(f"/api/projects/{pid}/generate-video")
    assert _video_jobs(pid) == before + 1, "only the edited shot is submitted"
    after = {s["id"]: s for s in _body(auth_client, pid)}
    assert after[edited["id"]]["clip_path"] != edited["clip_path"]
    assert after[kept["id"]]["clip_path"] == kept["clip_path"]
    assert not any(s["clip_stale"] for s in after.values())


def test_two_edited_shots_are_both_remade_before_the_video_is_put_together(auth_client, monkeypatch):
    """A new clip landing must not compose the video while another edited
    shot still shows its old clip."""
    from app.services import pipeline

    pid = _rendered(auth_client)
    shots = _body(auth_client, pid)[:2]
    for s in shots:
        auth_client.patch(f"/api/projects/{pid}/shots/{s['id']}", json={"prompt": s["prompt"] + " at night"})
    composed: list[list[str]] = []
    real = pipeline.compose

    def recording(inputs):
        composed.append([str(p) for p in inputs.clip_paths])
        return real(inputs)

    monkeypatch.setattr(pipeline, "compose", recording)
    before = _video_jobs(pid)
    auth_client.post(f"/api/projects/{pid}/generate-video")
    assert _video_jobs(pid) == before + 2
    after = {s["id"]: s for s in _body(auth_client, pid)}
    assert all(after[s["id"]]["clip_path"] != s["clip_path"] for s in shots)
    assert auth_client.get(f"/api/projects/{pid}/status").json()["project_status"] == "completed"
    assert len(composed) == 1, "the video is put together once"
    assert all(after[s["id"]]["clip_path"] in composed[0] for s in shots), "and from the new clips"


def test_each_clip_input_counts_as_an_edit(auth_client):
    pid = _rendered(auth_client)
    sid = _body(auth_client, pid)[0]["id"]

    def stale_after(change: dict, undo: dict) -> bool:
        auth_client.patch(f"/api/projects/{pid}/shots/{sid}", json=change)
        flag = next(s for s in _body(auth_client, pid) if s["id"] == sid)["clip_stale"]
        auth_client.patch(f"/api/projects/{pid}/shots/{sid}", json=undo)
        assert next(s for s in _body(auth_client, pid) if s["id"] == sid)["clip_stale"] is False
        return flag

    shot = _body(auth_client, pid)[0]
    assert stale_after({"negative_prompt": "crowds"}, {"negative_prompt": shot["negative_prompt"]})
    assert stale_after({"duration_seconds": shot["duration_seconds"] + 1},
                       {"duration_seconds": shot["duration_seconds"]})
    assert stale_after({"model_override": "mock/video-other"}, {"model_override": ""})
    # The caption is drawn at compose time; it is not part of the clip.
    assert not stale_after({"caption_text": "new caption"}, {"caption_text": shot["caption_text"]})


def test_retry_does_not_just_recompose_when_a_shot_was_edited(auth_client):
    pid = _rendered(auth_client)
    shot = _body(auth_client, pid)[0]
    auth_client.patch(f"/api/projects/{pid}/shots/{shot['id']}", json={"prompt": "changed"})
    r = auth_client.post(f"/api/projects/{pid}/retry")
    assert r.json()["action"] == "resume"
    assert next(s for s in _body(auth_client, pid) if s["id"] == shot["id"])["clip_path"] != shot["clip_path"]


def test_clips_made_before_fingerprints_count_as_current(auth_client):
    from app import models
    from app.db import SessionLocal

    pid = _rendered(auth_client)
    shot = _body(auth_client, pid)[0]
    db = SessionLocal()
    db.query(models.VideoShot).filter_by(project_id=pid).update({"clip_fingerprint": ""})
    db.commit()
    db.close()
    auth_client.patch(f"/api/projects/{pid}/shots/{shot['id']}", json={"prompt": "edited an old clip"})
    assert not any(s["clip_stale"] for s in _body(auth_client, pid))
    before = _video_jobs(pid)
    auth_client.post(f"/api/projects/{pid}/generate-video")
    assert _video_jobs(pid) == before, "a clip with no fingerprint is never remade on a guess"


# ---------- the voiceover is reused until the script / voice changes ----------


def test_unchanged_voiceover_is_reused_on_rerender_and_retry(auth_client, monkeypatch):
    calls = _count_tts(monkeypatch)
    pid = _rendered(auth_client)
    assert len(calls) == 1
    auth_client.post(f"/api/projects/{pid}/generate-video")
    assert len(calls) == 1, "same script, voice and model: no second synthesis"

    # One failed shot, then Retry: the clip is remade, the voiceover is not.
    from app import models
    from app.db import SessionLocal

    db = SessionLocal()
    shot = db.query(models.VideoShot).filter_by(project_id=pid).order_by(models.VideoShot.shot_order).first()
    Path(shot.clip_path).unlink()
    shot.clip_path, shot.status, shot.error = "", "failed", "simulated"
    db.commit()
    db.close()
    assert auth_client.post(f"/api/projects/{pid}/retry").json()["action"] == "resume"
    assert auth_client.get(f"/api/projects/{pid}/status").json()["project_status"] == "completed"
    assert len(calls) == 1


def test_edited_script_after_a_finished_render_gets_a_new_voiceover_and_keeps_the_clips(auth_client, monkeypatch):
    calls = _count_tts(monkeypatch)
    pid = _rendered(auth_client)
    clips = {s["id"]: s["clip_path"] for s in _body(auth_client, pid)}
    assert auth_client.get(f"/api/projects/{pid}/render-plan").json()["voiceover_changed"] is False

    auth_client.patch(f"/api/projects/{pid}/plan",
                      json={"cleaned_voice_script": "A different line to speak.", "resync_captions": True})
    plan = auth_client.get(f"/api/projects/{pid}/render-plan").json()
    assert plan["voiceover"] == "new" and plan["voiceover_changed"] is True
    assert plan["generate"] == 0 and plan["reuse"] == len(clips), "plain video clips don't depend on the voice"

    # Retry / Re-compose alone would keep the old voice: retry must resume.
    before = _video_jobs(pid)
    assert auth_client.post(f"/api/projects/{pid}/generate-video").status_code == 202
    assert calls == [calls[0], "A different line to speak."]
    assert _video_jobs(pid) == before, "no clip is paid for again"
    assert {s["id"]: s["clip_path"] for s in _body(auth_client, pid)} == clips
    st = auth_client.get(f"/api/projects/{pid}/status").json()
    assert st["project_status"] == "completed"
    after = auth_client.get(f"/api/projects/{pid}/render-plan").json()
    assert after["voiceover"] == "reuse" and after["voiceover_changed"] is False


def test_recompose_keeps_knowing_which_voiceover_it_used(auth_client, monkeypatch):
    calls = _count_tts(monkeypatch)
    pid = _rendered(auth_client)
    auth_client.post(f"/api/projects/{pid}/recompose")
    auth_client.post(f"/api/projects/{pid}/generate-video")
    assert len(calls) == 1


def test_new_voiceover_remakes_lip_sync_clips_and_keeps_the_rest(auth_client):
    _lipsync_on(auth_client)
    pid = _talking_project(auth_client)
    auth_client.post(f"/api/projects/{pid}/generate-video?force=true")
    shots = _body(auth_client, pid)
    talking = next(s for s in shots if s["shot_type"] == "talking_head")
    assert talking["provider"] == "lipsync" and not any(s["clip_stale"] for s in shots)

    auth_client.patch(f"/api/projects/{pid}/plan", json={"cleaned_voice_script": "Completely new words to say."})
    now = {s["id"]: s for s in _body(auth_client, pid)}
    assert now[talking["id"]]["clip_stale"] is True
    assert not any(s["clip_stale"] for s in now.values() if s["id"] != talking["id"])
    plan = auth_client.get(f"/api/projects/{pid}/render-plan").json()
    row = next(s for s in plan["shots"] if s["id"] == talking["id"])
    assert row["action"] == "generate" and "voiceover" in row["reason"] and row["lipsync"] and row["cost"] > 0
    assert plan["generate"] == 1

    lipsync_jobs, all_jobs = _jobs(pid, "avatar"), _video_jobs(pid)
    auth_client.post(f"/api/projects/{pid}/generate-video?force=true")
    after = {s["id"]: s for s in _body(auth_client, pid)}
    # (A lip-sync clip keeps its file name, so count the jobs instead.)
    assert _jobs(pid, "avatar") == lipsync_jobs + 1, "the talking head is lip-synced to the new voiceover"
    assert _video_jobs(pid) == all_jobs + 1, "and nothing else is submitted"
    assert after[talking["id"]]["provider"] == "lipsync"
    for s in shots:
        if s["id"] != talking["id"]:
            assert after[s["id"]]["clip_path"] == s["clip_path"]
    assert not any(s["clip_stale"] for s in after.values())


def test_rerolled_lip_sync_clip_is_current(auth_client):
    _lipsync_on(auth_client)
    pid = _talking_project(auth_client)
    auth_client.post(f"/api/projects/{pid}/generate-video?force=true")
    talking = next(s for s in _body(auth_client, pid) if s["shot_type"] == "talking_head")
    auth_client.post(f"/api/projects/{pid}/shots/{talking['id']}/regenerate?recompose=false")
    now = next(s for s in _body(auth_client, pid) if s["id"] == talking["id"])
    assert now["provider"] == "lipsync" and now["clip_stale"] is False


# ---------- cancel, then resume: never pay twice for a clip in flight ----------


def _put_shot_in_flight(pid) -> tuple[int, int]:
    """Make the first shot look like its clip is still being made at the
    provider, under a render that was then cancelled."""
    from app import models
    from app.db import SessionLocal
    from app.providers import mock_video

    db = SessionLocal()
    try:
        shot = db.query(models.VideoShot).filter_by(project_id=pid).order_by(models.VideoShot.shot_order).first()
        old = db.query(models.Render).filter_by(project_id=pid).order_by(models.Render.id.desc()).first()
        data = Path(shot.clip_path).read_bytes()
        shot.clip_path, shot.status = "", "submitted"
        submitted = mock_video.get_singleton().put(data)
        job = models.VideoJob(purpose="shot", ref_id=shot.id, project_id=pid, render_id=old.id,
                              provider="mock", model=submitted.model, provider_job_id=submitted.job_id,
                              status="running", context_json={"after": "compose"},
                              deadline_at=datetime.utcnow() + timedelta(hours=1))
        db.add(job)
        old.status = "cancelled"
        db.get(models.VideoProject, pid).status = "cancelled"
        db.commit()
        return shot.id, job.id
    finally:
        db.close()


def test_resume_waits_for_a_clip_in_flight_instead_of_submitting_it_again(auth_client):
    from app import models
    from app.db import SessionLocal
    from app.services import video_jobs

    pid = _rendered(auth_client)
    shot_id, job_id = _put_shot_in_flight(pid)
    plan = auth_client.get(f"/api/projects/{pid}/render-plan").json()
    assert plan["in_flight"] == 1 and plan["generate"] == 0 and plan["total"] == 0
    before = _video_jobs(pid)

    r = auth_client.post(f"/api/projects/{pid}/retry")
    assert r.status_code == 202 and r.json()["action"] == "resume" and r.json()["clips_in_flight"] == 1
    assert _video_jobs(pid) == before, "the shot already has a job at the provider: no second one"
    st = auth_client.get(f"/api/projects/{pid}/status").json()
    assert st["latest_render"]["status"] == "generating_shots"  # waiting for that one clip

    db = SessionLocal()
    try:
        job = db.get(models.VideoJob, job_id)
        assert job.render_id == st["latest_render"]["id"], "the job now belongs to the resumed render"
        assert video_jobs.check(db, job_id) == "completed"
    finally:
        db.close()
    st = auth_client.get(f"/api/projects/{pid}/status").json()
    assert st["project_status"] == "completed" and st["latest_render"]["status"] == "completed"
    assert next(s for s in st["shots"] if s["id"] == shot_id)["status"] == "completed"


def test_a_landing_clip_composes_a_render_that_is_waiting_for_it(auth_client):
    """Even if the job still points at the old render (it was already being
    downloaded when the run was resumed), the waiting render composes."""
    from app import models
    from app.db import SessionLocal
    from app.services import video_jobs

    pid = _rendered(auth_client)
    _shot_id, job_id = _put_shot_in_flight(pid)
    auth_client.post(f"/api/projects/{pid}/retry")
    db = SessionLocal()
    try:
        job = db.get(models.VideoJob, job_id)
        job.render_id = None
        job.context_json = {"after": "none"}
        db.commit()
        video_jobs.check(db, job_id)
    finally:
        db.close()
    assert auth_client.get(f"/api/projects/{pid}/status").json()["project_status"] == "completed"


def test_cancel_reports_clips_still_in_flight(auth_client):
    from app import models
    from app.db import SessionLocal

    pid = _rendered(auth_client)
    _put_shot_in_flight(pid)
    db = SessionLocal()
    db.query(models.Render).filter_by(project_id=pid).update({"status": "generating_shots"})
    db.commit()
    db.close()
    r = auth_client.post(f"/api/projects/{pid}/cancel").json()
    assert r["action"] == "signalled" and r["clips_in_flight"] == 1


# ---------- re-plan with "keep my edits" ----------


def test_replan_keeping_edits_keeps_script_end_card_captions_and_avoid_text(auth_client):
    pid = _planned(auth_client)
    sid = _body(auth_client, pid)[0]["id"]
    auth_client.patch(f"/api/projects/{pid}/plan", json={
        "cleaned_voice_script": "My own spoken words, edited by hand.", "end_card_text": "Shop today",
        "resync_captions": True})
    auth_client.patch(f"/api/projects/{pid}/shots/{sid}", json={"negative_prompt": "crowds, text on screen"})
    edited = auth_client.get(f"/api/projects/{pid}").json()["generated_plan_json"]

    assert auth_client.post(f"/api/projects/{pid}/generate-plan?preserve_edits=true").status_code == 202
    project = auth_client.get(f"/api/projects/{pid}").json()
    plan = project["generated_plan_json"]
    assert plan["cleaned_voice_script"] == "My own spoken words, edited by hand."
    assert plan["end_card_text"] == "Shop today"
    assert plan["caption_chunks"] == edited["caption_chunks"]
    assert next(s for s in project["shots"] if s["id"] == sid)["negative_prompt"] == "crowds, text on screen"

    # "Start fresh" replaces all of it.
    auth_client.post(f"/api/projects/{pid}/generate-plan")
    project = auth_client.get(f"/api/projects/{pid}").json()
    assert project["generated_plan_json"]["cleaned_voice_script"] != "My own spoken words, edited by hand."
    assert all(s["negative_prompt"] != "crowds, text on screen" for s in project["shots"])


# ---------- migration ----------


def test_migration_0005_adds_fingerprint_columns_empty_for_existing_rows(tmp_path, monkeypatch):
    from alembic import command
    from sqlalchemy import inspect

    from app import config
    from app import db as dbmod

    url = f"sqlite:///{tmp_path / 'm.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config.get_settings.cache_clear()
    dbmod.reset_engine(url)
    command.upgrade(dbmod._alembic_config(), "0004")
    assert "clip_fingerprint" not in {c["name"] for c in inspect(dbmod.engine).get_columns("video_shots")}
    command.upgrade(dbmod._alembic_config(), "0005")
    # Existing rows get "" (made before fingerprints existed: counts as current).
    shot = {c["name"]: c for c in inspect(dbmod.engine).get_columns("video_shots")}["clip_fingerprint"]
    render = {c["name"]: c for c in inspect(dbmod.engine).get_columns("renders")}["voice_fingerprint"]
    for col in (shot, render):
        assert col["nullable"] is False and col["default"] in ("''", '""')
    command.downgrade(dbmod._alembic_config(), "0004")
    assert "clip_fingerprint" not in {c["name"] for c in inspect(dbmod.engine).get_columns("video_shots")}
    assert "voice_fingerprint" not in {c["name"] for c in inspect(dbmod.engine).get_columns("renders")}

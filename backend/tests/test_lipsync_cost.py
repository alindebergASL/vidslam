"""Live-deploy finding: a lip-synced talking head is billed as lip-sync on
the speech it gets (HeyGen: ~$0.23 for a 4.6 s clip), not as a video clip
at the planned length (Wan: ~$1.05 for 7 s)."""
from __future__ import annotations

import pytest

from .test_regen_lipsync import _lipsync_on
from .test_review_fixes import _talking_project


def _plan(*shots):
    from app.schemas.storyboard import StoryboardPlan

    return StoryboardPlan.model_validate({
        "title": "t", "cleaned_voice_script": "x" * 240, "estimated_duration_seconds": 20,
        "shots": [{"order": i + 1, "shot_type": t, "duration_seconds": d, "visual_prompt": "p"}
                  for i, (t, d) in enumerate(shots)],
    })


def test_speech_seconds_follow_the_voiceover():
    from app.services.cost import speech_seconds

    plan = _plan(("hero", 5), ("talking_head", 7), ("talking_head", 7), ("end_card", 3))
    # 16 s of voice: the first talking head (5–12 s) gets all 7 s, the
    # second (12–19 s) only the 4 s that are left.
    assert speech_seconds(plan, 16.0) == {2: 7.0, 3: 4.0}
    assert speech_seconds(plan, None) == {2: 7.0, 3: 7.0}  # unknown: as planned
    assert speech_seconds(plan, 3.0) == {2: 1.0, 3: 1.0}  # floor of 1 s


def test_estimate_prices_talking_heads_as_lip_sync():
    from app.config import get_settings
    from app.services.cost import estimate_plan_cost

    s = get_settings()
    plan = _plan(("hero", 5), ("talking_head", 7), ("end_card", 3))
    plain = estimate_plan_cost({"voiceover_source": "tts"}, plan)
    lip = estimate_plan_cost({"voiceover_source": "tts"}, plan, lipsync=True, voice_seconds=9.6)
    line = next(ln for ln in lip["lines"] if ln["item"].startswith("Lip-sync clips"))
    assert line["qty"] == pytest.approx(4.6) and line["unit_cost"] == s.cost_avatar_per_second
    video = next(ln for ln in lip["lines"] if ln["item"].startswith("Video clips"))
    assert video["qty"] == 5  # the hero shot only
    assert lip["total"] < plain["total"]
    assert not any(ln["item"].startswith("Lip-sync") for ln in plain["lines"])


def test_cost_estimate_endpoint_knows_about_lip_sync(auth_client):
    pid = _talking_project(auth_client)
    off = auth_client.get(f"/api/projects/{pid}/cost-estimate").json()
    assert not any("Lip-sync" in ln["item"] for ln in off["lines"])
    _lipsync_on(auth_client)
    before = auth_client.get(f"/api/projects/{pid}/cost-estimate").json()
    assert any("Lip-sync clips (1 talking head" in ln["item"] for ln in before["lines"])
    assert before["total"] < off["total"]
    # After a render the real voiceover length is used.
    auth_client.post(f"/api/projects/{pid}/generate-video?force=true")
    after = auth_client.get(f"/api/projects/{pid}/cost-estimate").json()
    assert any("Lip-sync clips" in ln["item"] for ln in after["lines"])


def test_avatar_job_records_its_speech_length(auth_client):
    from app import models
    from app.db import SessionLocal

    _lipsync_on(auth_client)
    pid = _talking_project(auth_client)
    auth_client.post(f"/api/projects/{pid}/generate-video?force=true")
    db = SessionLocal()
    try:
        job = db.query(models.VideoJob).filter_by(project_id=pid, purpose="avatar").one()
        ctx = job.context_json
        assert 0 < ctx["speech_seconds"] <= ctx["duration"]
    finally:
        db.close()


def test_rate_card_fallback_bills_lip_sync_on_its_speech(client):
    """Provider reported no cost: lip-sync uses its own rate and the speech
    length (4.6 s), not the video rate and the planned 7 s."""
    from app import models
    from app.db import SessionLocal
    from app.services import spend

    from .test_spend import _entries

    db = SessionLocal()
    try:
        job = models.VideoJob(purpose="avatar", ref_id=1, provider="openrouter", model="heygen/avatar-iv",
                              provider_job_id="g1", status="completed",
                              context_json={"duration": 7.0, "speech_seconds": 4.6})
        db.add(job)
        db.commit()
        spend.record_video_job(db, job)
        db.commit()
    finally:
        db.close()
    (row,) = _entries(kind="avatar")
    assert row["cost_usd"] == pytest.approx(0.23) and row["source"] == "rate_card"


def test_estimate_follows_the_current_shot_order(auth_client, tmp_path):
    """Review: with a 6 s voiceover, a 5 s hero then a 7 s talking head
    gives the talking head 1 s; moved first, it gets 6 s. The estimate must
    use the shots as they are now, not the original storyboard."""
    import subprocess

    from app import models
    from app.db import SessionLocal

    _lipsync_on(auth_client)
    pid = _talking_project(auth_client)
    voice = tmp_path / "voice.m4a"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=220:d=6",
                    str(voice)], check=True)

    def lip_secs():
        est = auth_client.get(f"/api/projects/{pid}/cost-estimate").json()
        return next(ln["qty"] for ln in est["lines"] if ln["item"].startswith("Lip-sync"))

    db = SessionLocal()
    try:
        p = db.get(models.VideoProject, pid)
        p.voiceover_source, p.voiceover_upload_path = "upload", str(voice)
        body = sorted((s for s in p.shots if s.shot_type != "end_card"), key=lambda s: s.shot_order)
        th = next(s for s in body if s.shot_type == "talking_head")
        others = [s for s in body if s is not th]
        if not others:  # a talking-head-only plan: add a hero shot to play before it
            hero = models.VideoShot(project_id=pid, shot_order=0, shot_type="hero", prompt="hero")
            db.add(hero)
            others = [hero]
        th.duration_seconds = 7.0
        for s in others:
            s.duration_seconds = 5.0 / len(others)
        # Talking head last: it starts at 5 s and gets the last 1 s.
        for i, s in enumerate([*others, th], start=1):
            s.shot_order = i
        db.commit()
        assert lip_secs() == pytest.approx(1.0, abs=0.1)
        # Moved first: it gets all 6 s.
        for i, s in enumerate([th, *others], start=1):
            s.shot_order = i
        db.commit()
        assert lip_secs() == pytest.approx(6.0, abs=0.1)
    finally:
        db.close()

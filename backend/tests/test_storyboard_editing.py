"""Storyboard editing basics:

- hear the voiceover before paying for video (and the next render reuses
  it at no charge), with the project's own voice;
- add, duplicate and delete shots by hand;
- keep earlier takes of a shot and pick one again without paying;
- change project settings after creation (aspect ratio remakes clips,
  caption style only needs a re-compose);
- migration 0006.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from .test_regen_lipsync import _jobs, _lipsync_on
from .test_review_fixes import _talking_project
from .test_storyboard_waste import _body, _count_tts, _planned, _rendered, _video_jobs


def _project(client, pid) -> dict:
    return client.get(f"/api/projects/{pid}").json()


def _shots(client, pid) -> list[dict]:
    return _project(client, pid)["shots"]


def _db():
    from app.db import SessionLocal

    return SessionLocal()


# ---------- voiceover preview ----------


def test_preview_is_reused_by_the_next_render_at_no_charge(auth_client, monkeypatch):
    calls = _count_tts(monkeypatch)
    pid = _planned(auth_client)
    assert auth_client.get(f"/api/projects/{pid}/voiceover/preview").status_code == 404
    before = auth_client.get(f"/api/projects/{pid}/render-plan").json()
    assert before["voiceover"] == "new" and before["voiceover_cost"] > 0

    r = auth_client.post(f"/api/projects/{pid}/voiceover/preview")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reused"] is False and body["source"] == "preview" and body["current"] is True
    assert body["cost_estimate"] == before["voiceover_cost"]
    assert body["duration_seconds"] > 0 and body["video_seconds"] > 0
    assert body["fit"] in ("fits", "too_long", "too_short") and body["fit_text"]
    assert len(calls) == 1
    audio = auth_client.get(body["url"])
    assert audio.status_code == 200 and len(audio.content) > 0

    # Counted as reuse in the render plan, and Generate video pays nothing for it.
    plan = auth_client.get(f"/api/projects/{pid}/render-plan").json()
    assert plan["voiceover"] == "reuse" and plan["voiceover_cost"] == 0
    assert auth_client.post(f"/api/projects/{pid}/generate-video").status_code == 202
    st = auth_client.get(f"/api/projects/{pid}/status").json()
    assert st["project_status"] == "completed"
    assert len(calls) == 1, "the render used the preview, it did not synthesize again"
    assert "voice_preview_" in st["latest_render"]["audio_path"]

    # Asking again with nothing changed is free.
    again = auth_client.post(f"/api/projects/{pid}/voiceover/preview").json()
    assert again["reused"] is True and again["cost_estimate"] == 0 and len(calls) == 1


def test_preview_after_a_script_edit_makes_a_new_one_and_keeps_clips(auth_client, monkeypatch):
    calls = _count_tts(monkeypatch)
    pid = _rendered(auth_client)
    clips = {s["id"]: s["clip_path"] for s in _body(auth_client, pid)}
    auth_client.patch(f"/api/projects/{pid}/plan", json={"cleaned_voice_script": "Some brand new words."})
    got = auth_client.get(f"/api/projects/{pid}/voiceover/preview").json()
    assert got["source"] == "render" and got["current"] is False  # "Remake voiceover"

    made = auth_client.post(f"/api/projects/{pid}/voiceover/preview").json()
    assert made["reused"] is False and made["current"] is True and len(calls) == 2
    before = _video_jobs(pid)
    auth_client.post(f"/api/projects/{pid}/generate-video")
    assert len(calls) == 2 and _video_jobs(pid) == before
    assert {s["id"]: s["clip_path"] for s in _body(auth_client, pid)} == clips


def test_preview_is_guarded_like_any_paid_call(auth_client, monkeypatch):
    from app.services import spend

    from .test_spend import _add

    calls = _count_tts(monkeypatch)
    monkeypatch.setattr(spend, "_all_mocked", lambda: False)  # mock mode never hits the cap
    pid = _planned(auth_client)
    auth_client.put("/api/spend/budget", json={"monthly_usd": 1, "hard_stop": True, "alert_pcts": []})
    _add(1.5)
    r = auth_client.post(f"/api/projects/{pid}/voiceover/preview")
    assert r.status_code == 402 and calls == []


def test_preview_refused_without_plan_or_without_a_spoken_voiceover(auth_client):
    pid = auth_client.post("/api/projects", json={"title": "x", "original_script": "hello"}).json()["id"]
    assert auth_client.post(f"/api/projects/{pid}/voiceover/preview").status_code == 409
    pid = _planned(auth_client)
    auth_client.patch(f"/api/projects/{pid}", json={"voiceover_source": "silent"})
    r = auth_client.post(f"/api/projects/{pid}/voiceover/preview")
    assert r.status_code == 409 and "silent" in r.json()["detail"]
    assert auth_client.post("/api/projects/99999/voiceover/preview").status_code == 404


def test_preview_says_when_the_voice_is_longer_than_the_video(auth_client, monkeypatch):
    from app.providers import mock_tts

    pid = _planned(auth_client)
    shots = sum(s["duration_seconds"] for s in _body(auth_client, pid))
    real = mock_tts.MockTTSProvider.synthesize

    def long(self, *, text, voice_id, settings=None):
        return real(self, text=text, voice_id=voice_id, settings={"duration": shots + 6})

    monkeypatch.setattr(mock_tts.MockTTSProvider, "synthesize", long)
    body = auth_client.post(f"/api/projects/{pid}/voiceover/preview").json()
    assert body["fit"] == "too_long" and body["difference_seconds"] > 5
    assert "longer than the video: the end will be cut" in body["fit_text"]


def test_fit_verdicts():
    from types import SimpleNamespace

    from app.services.pipeline import voiceover_fit

    p = SimpleNamespace(cta_text="Go", shots=[SimpleNamespace(shot_type="b_roll", duration_seconds=5.0),
                                              SimpleNamespace(shot_type="b_roll", duration_seconds=5.0),
                                              SimpleNamespace(shot_type="end_card", duration_seconds=2.0)])
    assert voiceover_fit(p, 10.2)["fit"] == "fits"
    over = voiceover_fit(p, 11.5)
    assert over["fit"] == "too_long" and "runs over the end card" in over["fit_text"]
    assert "the end will be cut" in voiceover_fit(p, 14)["fit_text"]
    short = voiceover_fit(p, 7)
    assert short["fit"] == "too_short" and short["fit_text"].startswith("about 3 s shorter")


# ---------- the project's own voice ----------


def test_project_voice_changes_the_fingerprint_and_is_spoken(auth_client, monkeypatch):
    from app import models
    from app.providers import mock_tts
    from app.services import fingerprints

    pid = _rendered(auth_client)
    assert _project(auth_client, pid)["voice_id"] is None
    voices: list[str] = []
    real = mock_tts.MockTTSProvider.synthesize

    def spy(self, *, text, voice_id, settings=None):
        voices.append(voice_id)
        return real(self, text=text, voice_id=voice_id, settings=settings)

    monkeypatch.setattr(mock_tts.MockTTSProvider, "synthesize", spy)
    db = _db()
    before = fingerprints.voice_fingerprint(db, db.get(models.VideoProject, pid))
    db.close()

    assert auth_client.patch(f"/api/projects/{pid}", json={"voice_id": "voice-b"}).json()["voice_id"] == "voice-b"
    plan = auth_client.get(f"/api/projects/{pid}/render-plan").json()
    assert plan["voiceover"] == "new" and plan["voiceover_changed"] is True
    db = _db()
    assert fingerprints.voice_fingerprint(db, db.get(models.VideoProject, pid)) != before
    db.close()
    auth_client.post(f"/api/projects/{pid}/voiceover/preview")
    assert voices == ["voice-b"]

    # Back to the character's voice: the old voiceover matches again.
    assert auth_client.patch(f"/api/projects/{pid}", json={"voice_id": None}).json()["voice_id"] is None
    db = _db()
    assert fingerprints.voice_fingerprint(db, db.get(models.VideoProject, pid)) == before
    db.close()
    assert auth_client.get(f"/api/projects/{pid}/render-plan").json()["voiceover"] == "reuse"


# ---------- re-rolling a lip-synced shot after "Make voiceover" ----------


def _latest_render(pid):
    from app import models

    db = _db()
    try:
        r = (db.query(models.Render).filter_by(project_id=pid)
             .order_by(models.Render.id.desc()).first())
        return {"audio_path": r.audio_path, "voice_fingerprint": r.voice_fingerprint, "status": r.status}
    finally:
        db.close()


def _talking_with_new_voice_made(client, *, extra_shot=False) -> tuple[int, dict, dict]:
    """Rendered with the old voice, then a new voice picked and "Make
    voiceover" pressed. Returns the project id, the old render and the
    preview."""
    _lipsync_on(client)
    pid = _talking_project(client, extra_shot=extra_shot)
    client.post(f"/api/projects/{pid}/generate-video?force=true")
    old = _latest_render(pid)
    assert old["status"] == "completed" and old["audio_path"] and old["voice_fingerprint"]
    assert client.patch(f"/api/projects/{pid}", json={"voice_id": "voice-b"}).status_code == 200
    preview = client.post(f"/api/projects/{pid}/voiceover/preview").json()
    assert preview["current"] is True
    from app import models

    db = _db()
    try:
        made = dict(db.get(models.VideoProject, pid).voiceover_preview_json or {})
    finally:
        db.close()
    assert made.get("path") and made.get("fingerprint")
    return pid, old, made


def test_reroll_after_make_voiceover_recomposes_with_the_new_voiceover(auth_client):
    pid, old, preview = _talking_with_new_voice_made(auth_client)
    assert preview["fingerprint"] != old["voice_fingerprint"]
    talking = next(s for s in _body(auth_client, pid) if s["shot_type"] == "talking_head")

    r = auth_client.post(f"/api/projects/{pid}/shots/{talking['id']}/regenerate")
    assert r.status_code == 202, r.text
    assert auth_client.get(f"/api/projects/{pid}/status").json()["project_status"] == "completed"
    new = _latest_render(pid)
    assert new["status"] == "completed"
    # The lips follow the new voiceover, and so does the soundtrack.
    assert new["audio_path"] == preview["path"] and "voice_preview_" in new["audio_path"]
    assert new["voice_fingerprint"] == preview["fingerprint"]
    assert not any(s["clip_stale"] for s in _body(auth_client, pid))
    plan = auth_client.get(f"/api/projects/{pid}/render-plan").json()
    assert plan["voiceover_changed"] is False and plan["voiceover"] == "reuse"


def test_recompose_keeps_the_old_voiceover_while_the_clips_follow_it(auth_client):
    pid, old, _ = _talking_with_new_voice_made(auth_client)
    assert auth_client.post(f"/api/projects/{pid}/recompose").status_code == 202
    new = _latest_render(pid)
    assert new["status"] == "completed"
    assert new["audio_path"] == old["audio_path"]
    assert new["voice_fingerprint"] == old["voice_fingerprint"]


def test_reroll_of_one_lip_synced_shot_is_refused_while_another_follows_the_old_voice(auth_client):
    pid, old, preview = _talking_with_new_voice_made(auth_client, extra_shot=True)
    talking = [s for s in _body(auth_client, pid) if s["shot_type"] == "talking_head"]
    assert len(talking) >= 2
    before = _video_jobs(pid) + _jobs(pid, "avatar")

    r = auth_client.post(f"/api/projects/{pid}/shots/{talking[0]['id']}/regenerate")
    assert r.status_code == 409, r.text
    assert "new voiceover is made" in r.json()["detail"]
    assert _video_jobs(pid) + _jobs(pid, "avatar") == before, "nothing was paid for"
    assert _latest_render(pid)["audio_path"] == old["audio_path"]

    # Without a recompose nothing is composed out of step: allowed.
    r = auth_client.post(f"/api/projects/{pid}/shots/{talking[0]['id']}/regenerate?recompose=false")
    assert r.status_code == 202, r.text

    # All lip-synced shots together: allowed, and the video uses the new voiceover.
    r = auth_client.post(f"/api/projects/{pid}/shots/regenerate-bulk",
                         json={"shot_ids": [s["id"] for s in talking]})
    assert r.status_code == 202, r.text
    new = _latest_render(pid)
    assert new["status"] == "completed"
    assert new["audio_path"] == preview["path"] and new["voice_fingerprint"] == preview["fingerprint"]
    assert not any(s["clip_stale"] for s in _body(auth_client, pid))


def test_old_projects_keep_their_voice_fingerprint(auth_client):
    """A project with no voice of its own hashes exactly what #26 hashed."""
    from app import models
    from app.config import get_settings
    from app.services import fingerprints
    from app.services import model_registry as reg

    pid = _planned(auth_client)
    db = _db()
    try:
        p = db.get(models.VideoProject, pid)
        avatar = db.get(models.Avatar, p.primary_avatar_id) if p.primary_avatar_id else None
        voice_id = (avatar.elevenlabs_voice_id if avatar else "") or ""
        legacy = fingerprints._digest({
            "source": "tts", "script": p.generated_plan_json["cleaned_voice_script"], "voice_id": voice_id,
            "default_voice": "" if voice_id else get_settings().elevenlabs_default_voice_id,
            "model": reg.active_model("tts") or "", "options": reg.active_options("tts") or {},
        })
        assert p.voice_id is None and fingerprints.voice_fingerprint(db, p) == legacy
    finally:
        db.close()


def test_voices_list_passes_preview_url_through(auth_client):
    voices = auth_client.get("/api/providers/elevenlabs/voices").json()
    assert all("preview_url" in v for v in voices)


def test_elevenlabs_voices_keep_preview_url(monkeypatch):
    import httpx
    import respx

    from app.config import get_settings
    from app.providers.elevenlabs_tts import ElevenLabsTTSProvider

    with respx.mock:
        respx.get(f"{get_settings().elevenlabs_base_url.rstrip('/')}/voices").mock(
            return_value=httpx.Response(200, json={"voices": [
                {"voice_id": "v1", "name": "Ann", "preview_url": "https://cdn.example/ann.mp3"}]}))
        out = ElevenLabsTTSProvider(api_key="k").list_voices()
    assert out == [{"voice_id": "v1", "name": "Ann", "preview_url": "https://cdn.example/ann.mp3"}]


# ---------- add / duplicate / delete shots ----------


def test_add_shot_after_a_shot_and_at_the_end(auth_client):
    pid = _planned(auth_client)
    shots = _shots(auth_client, pid)
    first = shots[0]
    total = sum(s["duration_seconds"] for s in shots if s["shot_type"] != "end_card")

    r = auth_client.post(f"/api/projects/{pid}/shots",
                         json={"after_shot_id": first["id"], "prompt": "a close-up of hands", "duration_seconds": 4})
    assert r.status_code == 201, r.text
    out = r.json()
    new = out["shot"]
    assert new["shot_order"] == 2 and new["status"] == "pending" and new["clip_path"] == ""
    assert new["shot_type"] == "b_roll" and new["prompt"] == "a close-up of hands"
    orders = [s["shot_order"] for s in out["project"]["shots"]]
    assert orders == list(range(1, len(shots) + 2))
    # Captions spread over the new total shot time.
    assert out["captions_resynced"] is True
    chunks = out["project"]["generated_plan_json"]["caption_chunks"]
    from app.services.captions import chunk_script

    script = out["project"]["generated_plan_json"]["cleaned_voice_script"]
    assert chunks == [c.model_dump() for c in chunk_script(script, total + 4)]

    end = auth_client.post(f"/api/projects/{pid}/shots", json={"shot_type": "hero"}).json()["shot"]
    now = _shots(auth_client, pid)
    assert now[-1]["shot_type"] == "end_card", "the end card stays last"
    assert now[-2]["id"] == end["id"] and end["duration_seconds"] == 5.0

    plan = auth_client.get(f"/api/projects/{pid}/render-plan").json()
    assert next(s for s in plan["shots"] if s["id"] == new["id"])["reason"] == "no clip yet"


def test_add_shot_needs_a_plan_and_a_known_shot(auth_client):
    pid = auth_client.post("/api/projects", json={"title": "x"}).json()["id"]
    assert auth_client.post(f"/api/projects/{pid}/shots", json={}).status_code == 409
    pid = _planned(auth_client)
    assert auth_client.post(f"/api/projects/{pid}/shots", json={"after_shot_id": 999999}).status_code == 404
    assert auth_client.post(f"/api/projects/{pid}/shots", json={"shot_type": "end_card"}).status_code == 422


def test_duplicate_copies_settings_not_the_clip(auth_client):
    pid = _rendered(auth_client)
    src = _body(auth_client, pid)[0]
    auth_client.patch(f"/api/projects/{pid}/shots/{src['id']}", json={
        "negative_prompt": "crowds", "camera_direction": "slow push in", "model_override": "mock/video-other",
        "reference_asset_ids_json": [1]})
    src = next(s for s in _shots(auth_client, pid) if s["id"] == src["id"])
    r = auth_client.post(f"/api/projects/{pid}/shots/{src['id']}/duplicate")
    assert r.status_code == 201, r.text
    dup = r.json()["shot"]
    for k in ("prompt", "negative_prompt", "duration_seconds", "reference_asset_ids_json", "camera_direction",
              "model_override", "shot_type", "reference_strategy"):
        assert dup[k] == src[k], k
    assert dup["clip_path"] == "" and dup["status"] == "pending" and dup["takes_count"] == 0
    assert dup["shot_order"] == src["shot_order"] + 1


def test_delete_shot_renumbers_resyncs_and_drops_its_clip(auth_client):
    pid = _rendered(auth_client)
    body = _body(auth_client, pid)
    victim = body[0]
    r = auth_client.delete(f"/api/projects/{pid}/shots/{victim['id']}")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["shot"] is None and out["captions_resynced"] is True and out["clips_discarded"] == 1
    assert not Path(victim["clip_path"]).exists()
    shots = out["project"]["shots"]
    assert victim["id"] not in {s["id"] for s in shots}
    assert [s["shot_order"] for s in shots] == list(range(1, len(shots) + 1))


def test_cannot_delete_the_last_body_shot(auth_client):
    pid = _planned(auth_client)
    body = _body(auth_client, pid)
    for s in body[:-1]:
        assert auth_client.delete(f"/api/projects/{pid}/shots/{s['id']}").status_code == 200
    r = auth_client.delete(f"/api/projects/{pid}/shots/{body[-1]['id']}")
    assert r.status_code == 409 and "at least one shot" in r.json()["detail"]


def test_cannot_delete_a_shot_whose_clip_is_being_made(auth_client):
    from app import models

    pid = _rendered(auth_client)
    shot = _body(auth_client, pid)[0]
    db = _db()
    db.add(models.VideoJob(purpose="shot", ref_id=shot["id"], project_id=pid, provider="mock",
                           provider_job_id="x", status="running", context_json={"after": "none"},
                           deadline_at=datetime.utcnow() + timedelta(hours=1)))
    db.commit()
    db.close()
    r = auth_client.delete(f"/api/projects/{pid}/shots/{shot['id']}")
    assert r.status_code == 409 and "still being made" in r.json()["detail"]


def test_end_card_is_deleted_only_without_end_card_text(auth_client):
    pid = _planned(auth_client)
    end = next(s for s in _shots(auth_client, pid) if s["shot_type"] == "end_card")
    r = auth_client.delete(f"/api/projects/{pid}/shots/{end['id']}")
    assert r.status_code == 409 and "end-card text" in r.json()["detail"]
    assert auth_client.post(f"/api/projects/{pid}/shots/{end['id']}/duplicate").status_code == 409
    auth_client.patch(f"/api/projects/{pid}", json={"cta_text": ""})
    assert auth_client.delete(f"/api/projects/{pid}/shots/{end['id']}").status_code == 200


def test_new_shot_order_survives_drag_reorder(auth_client):
    pid = _planned(auth_client)
    new = auth_client.post(f"/api/projects/{pid}/shots", json={"prompt": "extra"}).json()["shot"]
    ids = [s["id"] for s in _shots(auth_client, pid)]
    ids.remove(new["id"])
    auth_client.post(f"/api/projects/{pid}/shots/reorder", json={"shot_ids": [new["id"], *ids]})
    assert _shots(auth_client, pid)[0]["id"] == new["id"]


# ---------- takes ----------


def _takes(client, pid, sid) -> dict:
    r = client.get(f"/api/projects/{pid}/shots/{sid}/takes")
    assert r.status_code == 200, r.text
    return r.json()


def test_every_new_clip_is_a_take_and_a_reroll_keeps_the_old_one(auth_client):
    pid = _rendered(auth_client)
    shot = _body(auth_client, pid)[0]
    assert shot["takes_count"] == 1
    first = _takes(auth_client, pid, shot["id"])
    assert first["keep"] == 10 and len(first["takes"]) == 1 and first["takes"][0]["current"] is True
    assert auth_client.get(first["takes"][0]["url"]).status_code == 200

    auth_client.post(f"/api/projects/{pid}/shots/{shot['id']}/regenerate?recompose=false")
    now = next(s for s in _shots(auth_client, pid) if s["id"] == shot["id"])
    assert now["clip_path"] != shot["clip_path"] and now["takes_count"] == 2
    assert Path(shot["clip_path"]).exists(), "the earlier clip stays on disk"
    takes = _takes(auth_client, pid, shot["id"])["takes"]
    assert [t["current"] for t in takes] == [True, False], "newest first"
    assert takes[0]["provider"] and takes[0]["created_at"]


def test_use_take_switches_clip_and_fingerprint_without_paying(auth_client, monkeypatch):
    from app import models

    calls = _count_tts(monkeypatch)
    pid = _rendered(auth_client)
    shot = _body(auth_client, pid)[0]
    db = _db()
    old_fp = db.get(models.VideoShot, shot["id"]).clip_fingerprint
    db.close()
    auth_client.patch(f"/api/projects/{pid}/shots/{shot['id']}", json={"prompt": "something else"})
    auth_client.post(f"/api/projects/{pid}/shots/{shot['id']}/regenerate?recompose=false")
    old = next(t for t in _takes(auth_client, pid, shot["id"])["takes"] if not t["current"])

    jobs, tts = _video_jobs(pid), len(calls)
    r = auth_client.post(f"/api/projects/{pid}/shots/{shot['id']}/takes/{old['id']}/use")
    assert r.status_code == 200, r.text
    assert r.json()["clip_path"] == shot["clip_path"] and r.json()["status"] == "completed"
    db = _db()
    assert db.get(models.VideoShot, shot["id"]).clip_fingerprint == old_fp
    db.close()
    assert _video_jobs(pid) == jobs and len(calls) == tts, "no paid call"
    assert next(t for t in _takes(auth_client, pid, shot["id"])["takes"] if t["id"] == old["id"])["current"]
    # The old take was made from the old prompt: the render plan says so.
    row = next(s for s in auth_client.get(f"/api/projects/{pid}/render-plan").json()["shots"]
               if s["id"] == shot["id"])
    assert row["action"] == "generate" and "edited" in row["reason"]
    # Re-compose (free) builds the video with it.
    auth_client.post(f"/api/projects/{pid}/recompose")
    assert auth_client.get(f"/api/projects/{pid}/status").json()["project_status"] == "completed"
    assert _video_jobs(pid) == jobs


def test_use_take_with_unchanged_shot_is_reused_by_render_plan(auth_client):
    pid = _rendered(auth_client)
    shot = _body(auth_client, pid)[0]
    auth_client.post(f"/api/projects/{pid}/shots/{shot['id']}/regenerate?recompose=false")
    old = next(t for t in _takes(auth_client, pid, shot["id"])["takes"] if not t["current"])
    auth_client.post(f"/api/projects/{pid}/shots/{shot['id']}/takes/{old['id']}/use")
    plan = auth_client.get(f"/api/projects/{pid}/render-plan").json()
    assert next(s for s in plan["shots"] if s["id"] == shot["id"])["action"] == "reuse"


def test_delete_take_only_when_not_current(auth_client):
    pid = _rendered(auth_client)
    shot = _body(auth_client, pid)[0]
    auth_client.post(f"/api/projects/{pid}/shots/{shot['id']}/regenerate?recompose=false")
    takes = _takes(auth_client, pid, shot["id"])["takes"]
    cur = next(t for t in takes if t["current"])
    old = next(t for t in takes if not t["current"])
    r = auth_client.delete(f"/api/projects/{pid}/shots/{shot['id']}/takes/{cur['id']}")
    assert r.status_code == 409
    assert auth_client.delete(f"/api/projects/{pid}/shots/{shot['id']}/takes/{old['id']}").status_code == 204
    assert not Path(shot["clip_path"]).exists()
    assert [t["id"] for t in _takes(auth_client, pid, shot["id"])["takes"]] == [cur["id"]]
    assert auth_client.delete(f"/api/projects/{pid}/shots/{shot['id']}/takes/{old['id']}").status_code == 404


def test_takes_are_capped_and_the_current_one_is_kept(auth_client, monkeypatch):
    from app.services import takes

    monkeypatch.setattr(takes, "KEEP", 2)
    pid = _rendered(auth_client)
    shot = _body(auth_client, pid)[0]
    first_clip = shot["clip_path"]
    for _ in range(3):
        auth_client.post(f"/api/projects/{pid}/shots/{shot['id']}/regenerate?recompose=false")
    got = _takes(auth_client, pid, shot["id"])
    assert got["keep"] == 2 and len(got["takes"]) == 2 and got["takes"][0]["current"]
    assert not Path(first_clip).exists(), "the oldest take's file is deleted"


def test_old_clip_shows_up_as_take_one(auth_client):
    from app import models

    pid = _rendered(auth_client)
    shot = _body(auth_client, pid)[0]
    db = _db()
    db.query(models.ShotTake).delete()
    db.commit()
    db.close()
    takes = _takes(auth_client, pid, shot["id"])["takes"]
    assert len(takes) == 1 and takes[0]["current"] and takes[0]["file_exists"]


def test_lip_sync_takes_keep_their_own_files(auth_client):
    _lipsync_on(auth_client)
    pid = _talking_project(auth_client)
    auth_client.post(f"/api/projects/{pid}/generate-video?force=true")
    talking = next(s for s in _body(auth_client, pid) if s["shot_type"] == "talking_head")
    auth_client.post(f"/api/projects/{pid}/shots/{talking['id']}/regenerate?recompose=false")
    assert _jobs(pid, "avatar") == 2
    takes = _takes(auth_client, pid, talking["id"])["takes"]
    assert len(takes) == 2 and all(t["file_exists"] for t in takes)


# ---------- settings after creation ----------


def test_aspect_change_marks_every_clip_stale_and_prices_them(auth_client):
    pid = _rendered(auth_client)
    assert all(s["clip_aspect_ratio"] == "9:16" for s in _body(auth_client, pid))
    auth_client.patch(f"/api/projects/{pid}", json={"aspect_ratio": "16:9"})
    body = _body(auth_client, pid)
    assert all(s["clip_stale"] for s in body)
    plan = auth_client.get(f"/api/projects/{pid}/render-plan").json()
    assert plan["generate"] == len(body) and plan["clip_cost"] > 0
    assert all("aspect ratio" in s["reason"] for s in plan["shots"])
    assert plan["voiceover"] == "reuse"

    auth_client.patch(f"/api/projects/{pid}", json={"aspect_ratio": "9:16"})
    assert not any(s["clip_stale"] for s in _body(auth_client, pid))

    auth_client.patch(f"/api/projects/{pid}", json={"aspect_ratio": "16:9"})
    auth_client.post(f"/api/projects/{pid}/generate-video")
    after = _body(auth_client, pid)
    assert all(s["clip_aspect_ratio"] == "16:9" and not s["clip_stale"] for s in after)


def test_clip_with_unknown_aspect_counts_as_current(auth_client):
    from app import models

    pid = _rendered(auth_client)
    db = _db()
    db.query(models.VideoShot).filter_by(project_id=pid).update({"clip_aspect_ratio": ""})
    db.commit()
    db.close()
    auth_client.patch(f"/api/projects/{pid}", json={"aspect_ratio": "1:1"})
    assert not any(s["clip_stale"] for s in _body(auth_client, pid))


def test_caption_style_and_title_changes_only_need_a_recompose(auth_client):
    pid = _rendered(auth_client)
    auth_client.patch(f"/api/projects/{pid}", json={
        "caption_style": "influencer_bold", "title": "New name", "include_disclosure": False,
        "cta_text": "Shop now"})
    plan = auth_client.get(f"/api/projects/{pid}/render-plan").json()
    assert plan["generate"] == 0 and plan["voiceover"] == "reuse" and plan["total"] == 0


# ---------- migration ----------


def test_migration_0006(tmp_path, monkeypatch):
    from alembic import command
    from sqlalchemy import inspect, text

    from app import config
    from app import db as dbmod

    url = f"sqlite:///{tmp_path / 'm.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config.get_settings.cache_clear()
    dbmod.reset_engine(url)
    command.upgrade(dbmod._alembic_config(), "0005")
    now = datetime.utcnow()
    with dbmod.engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO video_projects (id, title, original_script, mode, aspect_ratio, target_duration_seconds, "
            "cta_text, caption_style, include_disclosure, disclosure_text, creative_direction, voiceover_source, "
            "voiceover_upload_path, music_upload_path, music_volume, status, created_at, updated_at) VALUES "
            "(1, 't', '', 'reel_montage', '1:1', 20, '', 'clean_white', 1, '', '', 'tts', '', '', 0.25, "
            "'completed', :n, :n)"), {"n": now})
        conn.execute(text(
            "INSERT INTO video_shots (id, project_id, shot_order, shot_type, prompt, negative_prompt, "
            "duration_seconds, reference_strategy, caption_text, camera_direction, model_override, provider, "
            "provider_model, provider_job_id, polling_url, status, clip_path, clip_fingerprint, error, "
            "created_at, updated_at) VALUES "
            "(1, 1, 1, 'b_roll', 'p', '', 5, 'input_references', '', '', '', 'mock', 'mock/v', '', '', "
            "'completed', '/x/clip.mp4', 'abc', '', :n, :n),"
            "(2, 1, 2, 'end_card', '', '', 2, 'input_references', '', '', '', '', '', '', '', "
            "'completed', '', '', '', :n, :n)"), {"n": now})
    command.upgrade(dbmod._alembic_config(), "0006")
    insp = inspect(dbmod.engine)
    proj = {c["name"]: c for c in insp.get_columns("video_projects")}
    assert proj["voice_id"]["nullable"] is True and "VARCHAR" in str(proj["voice_id"]["type"]).upper()
    assert proj["voiceover_preview_json"]["nullable"] is True
    shot = {c["name"]: c for c in insp.get_columns("video_shots")}["clip_aspect_ratio"]
    assert shot["nullable"] is False and shot["default"] in ("''", '""')
    takes = {c["name"]: c for c in insp.get_columns("shot_takes")}
    assert takes["cost_usd"]["nullable"] is True and "FLOAT" in str(takes["cost_usd"]["type"]).upper()
    fks = insp.get_foreign_keys("shot_takes")
    assert fks[0]["referred_table"] == "video_shots" and fks[0]["options"].get("ondelete") == "CASCADE"
    with dbmod.engine.connect() as conn:
        assert conn.execute(text("SELECT voice_id FROM video_projects")).scalar() is None
        assert conn.execute(text("SELECT clip_aspect_ratio FROM video_shots WHERE id = 1")).scalar() == "1:1"
        assert conn.execute(text("SELECT clip_aspect_ratio FROM video_shots WHERE id = 2")).scalar() == ""
        rows = conn.execute(text("SELECT shot_id, clip_path, clip_fingerprint, aspect_ratio, provider "
                                 "FROM shot_takes")).fetchall()
    assert [tuple(r) for r in rows] == [(1, "/x/clip.mp4", "abc", "1:1", "mock")]
    command.downgrade(dbmod._alembic_config(), "0005")
    insp = inspect(dbmod.engine)
    assert "shot_takes" not in insp.get_table_names()
    assert "voice_id" not in {c["name"] for c in insp.get_columns("video_projects")}
    assert "clip_aspect_ratio" not in {c["name"] for c in insp.get_columns("video_shots")}

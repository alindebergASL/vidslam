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
        return real(self, text=text, voice_id=voice_id, settings={"duration": shots + 9})

    monkeypatch.setattr(mock_tts.MockTTSProvider, "synthesize", long)
    body = auth_client.post(f"/api/projects/{pid}/voiceover/preview").json()
    assert body["fit"] == "too_long" and body["difference_seconds"] > 8
    # 9 s past the shots is past the hold's cap with or without an end card.
    assert "the last shot is held for 4 s more at most, then the last" in body["fit_text"]
    assert "of the voice are cut" in body["fit_text"]


def test_fit_verdicts():
    from types import SimpleNamespace

    from app.services.pipeline import voiceover_fit

    p = SimpleNamespace(cta_text="Go", shots=[SimpleNamespace(shot_type="b_roll", duration_seconds=5.0),
                                              SimpleNamespace(shot_type="b_roll", duration_seconds=5.0),
                                              SimpleNamespace(shot_type="end_card", duration_seconds=2.0)])
    assert voiceover_fit(p, 10.2)["fit"] == "fits"
    over = voiceover_fit(p, 11.5)
    assert over["fit"] == "too_long" and "runs over the end card" in over["fit_text"]
    # Past the end card, the last shot is held on so the voice can finish...
    held = voiceover_fit(p, 14)
    assert held["fit"] == "too_long"
    assert held["fit_text"] == "about 4 s longer than the shots: the last shot is held for about 1.5 s more so the voice can finish"
    # ...for VOICE_HOLD_MAX_SECONDS at most: a longer voice is cut, and it says so.
    cut = voiceover_fit(p, 30)
    assert cut["fit"] == "too_long" and "cut" in cut["fit_text"]
    assert cut["fit_text"] == ("about 20 s longer than the video: the last shot is held for 4 s more at most, "
                               "then the last 13.5 s of the voice are cut")
    assert cut["cut_seconds"] == 13.5
    assert held["cut_seconds"] == 0.0 and over["cut_seconds"] == 0.0
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
    # The newest 2 are kept, plus the first take: the finished video was
    # composed from it (its render's composition_json), so prune keeps it
    # (the hook lab re-composes other openings from those clips).
    assert got["keep"] == 2 and len(got["takes"]) == 3 and got["takes"][0]["current"]
    assert Path(first_clip).exists() and got["takes"][-1]["file_exists"]
    from app import models

    db = _db()
    try:
        pruned = (db.query(models.VideoJob).filter_by(project_id=pid, purpose="shot", ref_id=shot["id"])
                  .order_by(models.VideoJob.id).all())[1]  # the first re-roll: not kept, not in a video
        assert not Path(pruned.result_path).exists(), "an old take nothing uses is deleted"
    finally:
        db.close()


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


# ---------- review follow-ups ----------


def _set(pid, **fields) -> None:
    from app import models

    db = _db()
    try:
        p = db.get(models.VideoProject, pid)
        for k, v in fields.items():
            setattr(p, k, v)
        db.commit()
    finally:
        db.close()


def _running_job(pid, shot_id) -> None:
    from app import models

    db = _db()
    db.add(models.VideoJob(purpose="shot", ref_id=shot_id, project_id=pid, provider="mock",
                           provider_job_id="x", status="running", context_json={"after": "none"},
                           deadline_at=datetime.utcnow() + timedelta(hours=1)))
    db.commit()
    db.close()


def test_add_duplicate_delete_are_refused_while_the_video_is_being_made(auth_client):
    pid = _rendered(auth_client)
    body = _body(auth_client, pid)
    before = [s["id"] for s in _shots(auth_client, pid)]
    for status in ("generating", "regenerating", "submitting"):
        _set(pid, status=status)
        r = auth_client.post(f"/api/projects/{pid}/shots", json={"after_shot_id": body[0]["id"]})
        assert r.status_code == 409 and "being made" in r.json()["detail"], r.text
        assert auth_client.post(f"/api/projects/{pid}/shots/{body[0]['id']}/duplicate").status_code == 409
        assert auth_client.delete(f"/api/projects/{pid}/shots/{body[1]['id']}").status_code == 409
    assert [s["id"] for s in _shots(auth_client, pid)] == before, "nothing changed"

    # A clip-only re-roll leaves the project "completed": still refused.
    _set(pid, status="completed")
    _running_job(pid, body[0]["id"])
    r = auth_client.post(f"/api/projects/{pid}/shots", json={})
    assert r.status_code == 409 and "still being made" in r.json()["detail"]
    assert auth_client.delete(f"/api/projects/{pid}/shots/{body[1]['id']}").status_code == 409


def test_add_is_refused_while_a_render_row_is_still_running(auth_client):
    from app import models

    pid = _rendered(auth_client)
    db = _db()
    db.add(models.Render(project_id=pid, status="generating_shots"))
    db.commit()
    db.close()
    assert auth_client.post(f"/api/projects/{pid}/shots", json={}).status_code == 409


def test_clip_settings_are_refused_mid_render_but_harmless_ones_save(auth_client):
    pid = _rendered(auth_client)
    p = _project(auth_client, pid)
    _set(pid, status="generating")
    for change in ({"aspect_ratio": "16:9"}, {"mode": "static_motion"}, {"voice_id": "voice-b"},
                   {"voiceover_source": "silent"}):
        r = auth_client.patch(f"/api/projects/{pid}", json=change)
        assert r.status_code == 409 and "being made" in r.json()["detail"], (change, r.text)
    r = auth_client.patch(f"/api/projects/{pid}/cast", json=[])
    assert r.status_code == 409, r.text
    r = auth_client.post(f"/api/projects/{pid}/voiceover", files={"file": ("v.mp3", b"ID3xx", "audio/mpeg")})
    assert r.status_code == 409, r.text
    r = auth_client.patch(f"/api/projects/{pid}/plan", json={"cleaned_voice_script": "Other words now."})
    assert r.status_code == 409, r.text
    after = _project(auth_client, pid)
    assert (after["aspect_ratio"], after["mode"], after["voice_id"], after["voiceover_source"]) == (
        p["aspect_ratio"], p["mode"], p["voice_id"], p["voiceover_source"])
    assert len(after["cast_members"]) == len(p["cast_members"])

    # Harmless fields save, and so does a clip field sent unchanged with them.
    r = auth_client.patch(f"/api/projects/{pid}", json={
        "title": "Renamed", "caption_style": "influencer_bold", "include_disclosure": False,
        "cta_text": "Shop now", "aspect_ratio": p["aspect_ratio"], "voice_id": None})
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Renamed"
    cast = [{k: c[k] for k in ("member_kind", "avatar_id", "ingredient_id", "role")} for c in p["cast_members"]]
    assert auth_client.patch(f"/api/projects/{pid}/cast", json=cast).status_code == 200


def _lipsync_rendered(client) -> int:
    _lipsync_on(client)
    pid = _talking_project(client, extra_shot=True)
    client.post(f"/api/projects/{pid}/generate-video?force=true")
    assert client.get(f"/api/projects/{pid}/status").json()["project_status"] == "completed"
    return pid


def _stale_ids(client, pid) -> set[int]:
    return {s["id"] for s in _body(client, pid) if s["clip_stale"]}


def test_edit_impact_names_the_lip_synced_clips_an_add_puts_out_of_date(auth_client):
    pid = _lipsync_rendered(auth_client)
    talking = [s for s in _body(auth_client, pid) if s["shot_type"] == "talking_head"]
    assert len(talking) >= 2 and all(s["provider"] == "lipsync" for s in talking)
    assert not _stale_ids(auth_client, pid)
    jobs = _video_jobs(pid)

    q = {"op": "add", "after_shot_id": talking[0]["id"], "duration_seconds": 2}
    impact = auth_client.get(f"/api/projects/{pid}/shots/edit-impact", params=q).json()
    later = {s["id"] for s in _body(auth_client, pid)
             if s["provider"] == "lipsync" and s["shot_order"] > talking[0]["shot_order"]}
    assert later and {x["id"] for x in impact["stale_shots"]} == later
    assert impact["stale_count"] == len(later) and impact["stale_cost"] > 0
    assert impact["new_clip_cost"] > 0
    assert not _stale_ids(auth_client, pid) and _video_jobs(pid) == jobs, "a dry run changes nothing"

    auth_client.post(f"/api/projects/{pid}/shots", json={"after_shot_id": talking[0]["id"], "duration_seconds": 2})
    assert _stale_ids(auth_client, pid) == later, "the dialog named exactly the clips that went stale"
    plan = auth_client.get(f"/api/projects/{pid}/render-plan").json()
    priced = sum(s["cost"] for s in plan["shots"] if s["id"] in later)
    assert round(priced, 4) == impact["stale_cost"]


def test_edit_impact_for_delete_and_duplicate(auth_client):
    pid = _lipsync_rendered(auth_client)
    talking = [s for s in _body(auth_client, pid) if s["shot_type"] == "talking_head"]
    first, last = talking[0], talking[-1]

    dup = auth_client.get(f"/api/projects/{pid}/shots/edit-impact",
                          params={"op": "duplicate", "shot_id": first["id"]}).json()
    assert last["id"] in {x["id"] for x in dup["stale_shots"]}
    # Duplicating / adding after the last shot moves nothing.
    tail = auth_client.get(f"/api/projects/{pid}/shots/edit-impact",
                           params={"op": "duplicate", "shot_id": last["id"]}).json()
    assert tail["stale_count"] == 0 and tail["stale_cost"] == 0

    gone = auth_client.get(f"/api/projects/{pid}/shots/edit-impact",
                           params={"op": "delete", "shot_id": first["id"]}).json()
    assert {x["id"] for x in gone["stale_shots"]} >= {last["id"]} and gone["new_clip_cost"] == 0
    auth_client.delete(f"/api/projects/{pid}/shots/{first['id']}")
    assert _stale_ids(auth_client, pid) == {x["id"] for x in gone["stale_shots"]}

    assert auth_client.get(f"/api/projects/{pid}/shots/edit-impact",
                           params={"op": "delete"}).status_code == 422


def test_video_clips_never_go_stale_from_an_add(auth_client):
    pid = _rendered(auth_client)
    body = _body(auth_client, pid)
    impact = auth_client.get(f"/api/projects/{pid}/shots/edit-impact",
                             params={"op": "add", "after_shot_id": body[0]["id"]}).json()
    assert impact["stale_count"] == 0
    auth_client.post(f"/api/projects/{pid}/shots", json={"after_shot_id": body[0]["id"]})
    assert not _stale_ids(auth_client, pid)


def test_cast_change_puts_clips_without_own_pictures_out_of_date(auth_client):
    """What "Project settings" reports after a cast change comes from the
    render plan: clips made from the cast's pictures are remade, priced."""
    pid = _rendered(auth_client)
    body = _body(auth_client, pid)
    p = _project(auth_client, pid)
    own = body[0]
    asset_ids = [a["id"] for a in auth_client.get(f"/api/projects/{pid}/cast-assets").json()][:1]
    db_shot_refs = {"reference_asset_ids_json": asset_ids}
    # Give one shot its own pictures (and remake its clip to match).
    auth_client.patch(f"/api/projects/{pid}/shots/{own['id']}", json=db_shot_refs)
    auth_client.post(f"/api/projects/{pid}/generate-video")
    assert not _stale_ids(auth_client, pid)

    aid = auth_client.post("/api/avatars", json={"name": "Other"}).json()["id"]
    from .conftest import make_png_bytes
    auth_client.post(f"/api/avatars/{aid}/assets", files={"file": ("o.png", make_png_bytes(), "image/png")},
                     data={"asset_type": "hero", "rights_confirmed": "true"})
    cast = [{k: c[k] for k in ("member_kind", "avatar_id", "ingredient_id", "role")} for c in p["cast_members"]]
    assert auth_client.patch(f"/api/projects/{pid}/cast",
                             json=cast + [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}]).status_code == 200
    stale = _stale_ids(auth_client, pid)
    assert own["id"] not in stale and stale == {s["id"] for s in body if s["id"] != own["id"]}
    plan = auth_client.get(f"/api/projects/{pid}/render-plan").json()
    rows = [s for s in plan["shots"] if s["id"] in stale]
    assert all(s["action"] == "generate" and s["cost"] > 0 for s in rows)


def _forget_voice_fingerprint(pid) -> None:
    from app import models

    db = _db()
    db.query(models.Render).filter_by(project_id=pid).update({"voice_fingerprint": ""})
    db.commit()
    db.close()


def test_voiceover_from_before_fingerprints_is_current_until_the_script_changes(auth_client, monkeypatch):
    pid = _rendered(auth_client)
    _forget_voice_fingerprint(pid)
    calls = _count_tts(monkeypatch)

    got = auth_client.get(f"/api/projects/{pid}/voiceover/preview").json()
    assert got["source"] == "render" and got["current"] is True, "not 'Out of date' for a missing fingerprint"
    plan = auth_client.get(f"/api/projects/{pid}/render-plan").json()
    assert plan["voiceover"] == "reuse" and plan["voiceover_cost"] == 0 and not plan["voiceover_changed"]
    made = auth_client.post(f"/api/projects/{pid}/voiceover/preview").json()
    assert made["reused"] is True and made["cost_estimate"] == 0 and calls == []

    auth_client.patch(f"/api/projects/{pid}/plan", json={"cleaned_voice_script": "Different words entirely."})
    assert auth_client.get(f"/api/projects/{pid}/voiceover/preview").json()["current"] is False
    assert auth_client.get(f"/api/projects/{pid}/render-plan").json()["voiceover"] == "new"


def test_old_voiceover_counts_as_what_the_script_is_changed_from(auth_client):
    """Edited before anything looked at it: the change is still seen."""
    pid = _rendered(auth_client)
    _forget_voice_fingerprint(pid)
    auth_client.patch(f"/api/projects/{pid}", json={"voice_id": "voice-b"})
    assert auth_client.get(f"/api/projects/{pid}/voiceover/preview").json()["current"] is False
    assert auth_client.get(f"/api/projects/{pid}/render-plan").json()["voiceover_changed"] is True


def test_resync_captions_uses_the_shots_as_they_are_now(auth_client):
    pid = _rendered(auth_client)
    body = _body(auth_client, pid)
    r = auth_client.delete(f"/api/projects/{pid}/shots/{body[0]['id']}").json()
    after_delete = r["project"]["generated_plan_json"]["caption_chunks"]
    total = sum(s["duration_seconds"] for s in _body(auth_client, pid))
    assert len(after_delete) == 2 and after_delete[1]["start_hint"] == round(total / 2, 2)

    out = auth_client.patch(f"/api/projects/{pid}/plan", json={"resync_captions": True}).json()
    assert out["generated_plan_json"]["caption_chunks"] == after_delete, "not the planner's old shot list"


def test_a_stale_clip_without_a_take_row_is_kept_as_a_take_when_generate_remakes_it(auth_client):
    from app import models

    pid = _rendered(auth_client)
    shot = _body(auth_client, pid)[0]
    db = _db()
    db.query(models.ShotTake).delete()  # a clip from before takes existed
    db.commit()
    db.close()
    auth_client.patch(f"/api/projects/{pid}/shots/{shot['id']}", json={"prompt": "something else entirely"})
    assert auth_client.post(f"/api/projects/{pid}/generate-video").status_code == 202
    takes = _takes(auth_client, pid, shot["id"])["takes"]
    paths = {t.get("url") for t in takes}
    assert len(takes) == 2 and len(paths) == 2
    assert Path(shot["clip_path"]).exists()
    db = _db()
    try:
        kept = {t.clip_path for t in db.query(models.ShotTake).filter_by(shot_id=shot["id"])}
    finally:
        db.close()
    assert shot["clip_path"] in kept, "the old clip stays selectable"


def test_reorder_and_shot_length_are_refused_mid_render(auth_client):
    """Moving shots or changing a length mid-render would shift voiceover
    slices under the compose step; other shot edits still save."""
    from app import models
    from app.db import SessionLocal

    from .test_storyboard_waste import _body, _rendered

    pid = _rendered(auth_client)
    shots = _body(auth_client, pid)
    db = SessionLocal()
    try:
        db.get(models.VideoProject, pid).status = "generating"
        db.commit()
    finally:
        db.close()
    ids = [s["id"] for s in reversed(shots)]
    assert auth_client.post(f"/api/projects/{pid}/shots/reorder", json={"shot_ids": ids}).status_code == 409
    sid = shots[0]["id"]
    assert auth_client.patch(f"/api/projects/{pid}/shots/{sid}",
                             json={"duration_seconds": shots[0]["duration_seconds"] + 1}).status_code == 409
    assert auth_client.patch(f"/api/projects/{pid}/shots/{sid}", json={"prompt": "a quiet street"}).status_code == 200


def test_removing_every_character_clears_the_main_character(auth_client):
    """A character taken out of the cast no longer lends its face or voice."""
    from .test_storyboard_waste import _planned

    pid = _planned(auth_client)
    assert auth_client.get(f"/api/projects/{pid}").json()["primary_avatar_id"] is not None
    r = auth_client.patch(f"/api/projects/{pid}/cast", json=[])
    assert r.status_code == 200 and r.json()["primary_avatar_id"] is None


def test_voiceover_preview_is_refused_while_a_render_runs(auth_client):
    """The render makes its own voiceover: making one here too would pay twice."""
    from app import models
    from app.db import SessionLocal

    from .test_storyboard_waste import _planned

    pid = _planned(auth_client)
    db = SessionLocal()
    try:
        db.get(models.VideoProject, pid).status = "generating"
        db.commit()
    finally:
        db.close()
    assert auth_client.post(f"/api/projects/{pid}/voiceover/preview").status_code == 409


def test_two_voiceover_previews_at_once_make_only_one(auth_client):
    from app.api import project_audio

    from .test_storyboard_waste import _planned

    pid = _planned(auth_client)
    lock = project_audio._preview_lock(pid)
    assert lock.acquire(blocking=False)  # one already being made
    try:
        r = auth_client.post(f"/api/projects/{pid}/voiceover/preview")
        assert r.status_code == 409 and "already being made" in r.json()["detail"]
    finally:
        lock.release()
    assert auth_client.post(f"/api/projects/{pid}/voiceover/preview").status_code == 200

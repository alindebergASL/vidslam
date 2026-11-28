"""Sound Studio PR3 review fixes: Library sounds judged with the story,
paid takes kept when a retry is stopped, claims that never save over
another, previews that keep their lock (and are capped), free reuse of any
finished take, the post-processing in the fingerprint, sounds made twice at
once, clip cuts that survive an edit, regenerated sounds that are new and
priced right, the QA gate on real (MP3) audio, loudness with a limiter,
unreadable audio and failed sounds."""
from __future__ import annotations

import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from . import test_sound_clips as _clip_tests
from .test_adult_features_off import _clean
from .test_sound_clips import SMALL, _clips, _generate, _spend_rows
from .test_sound_scripts import _db, _new, _story, _written

calls = _clip_tests.calls  # the fixture: every paid mock voice and sound call


def _mp3(path: Path, src: str = "sine=f=440:r=44100:d=2", af: str = "", seconds: float = 0.0) -> Path:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-f", "lavfi", "-i", src]
    if seconds:
        cmd += ["-t", str(seconds)]
    if af:
        cmd += ["-af", af]
    cmd += ["-c:a", "libmp3lame", "-b:a", "128k", str(path)]
    subprocess.run(cmd, capture_output=True, check=True)
    return path


def _asset(tmp_path: Path, prompt: str) -> int:
    from app import models

    f = _mp3(tmp_path / f"asset-{abs(hash(prompt))}.mp3")
    db = _db()
    try:
        a = models.SoundAsset(kind="sfx", name=prompt[:40], prompt=prompt, seconds=2.0, path=str(f),
                              source="generated")
        db.add(a)
        db.commit()
        return a.id
    finally:
        db.close()


def _raw_story(text: str, **kw) -> int:  # noqa: ANN003
    """A story saved straight into the table (before any check)."""
    from app import models

    db = _db()
    try:
        story = models.SoundStory(title="T", premise="", script_text=text, script_version=1, status="ready",
                                  settings_json={"style": "cozy"}, **kw)
        db.add(story)
        db.commit()
        return story.id
    finally:
        db.close()


def _progress(sid: int) -> dict:
    from app import models

    db = _db()
    try:
        return dict(db.get(models.SoundStory, sid).progress_json or {})
    finally:
        db.close()


def _budget(c, usd: float) -> None:  # noqa: ANN001
    assert c.put("/api/spend/budget", json={"monthly_usd": usd, "hard_stop": True,
                                            "alert_pcts": []}).status_code == 200


# ---------- safety:S1 — Library sounds are judged with the story ----------


@pytest.mark.parametrize("mode", ["default", "adult", "clean"])
def test_a_library_sound_is_judged_with_the_story_cast_in_every_mode(auth_client, calls, monkeypatch, tmp_path,
                                                                      mode):
    from app import config

    if mode == "clean":
        _clean(monkeypatch)
    elif mode == "adult":
        monkeypatch.setenv("ADULT_MODE", "true")
        config.get_settings.cache_clear()
    aid = _asset(tmp_path, "Lily moans sexually")
    sid = _raw_story(f"@cast LILY: a 12-year-old girl\nLILY: Hello.\n[sfx: #{aid}]\n")
    assert _generate(auth_client, sid, ok=False).status_code == 422
    assert auth_client.post(f"/api/sound/stories/{sid}/preview", json={"first_minutes": 1}).status_code == 422
    cue = next(b for b in _clips(auth_client, sid)["clips"] if b["kind"] == "sfx")
    r = auth_client.post(f"/api/sound/stories/{sid}/clips/regenerate", json={"cue_key": cue["key"]})
    assert r.status_code == 422
    assert _story(auth_client, sid)["status"] == "ready"
    assert calls["tts"] == [] and calls["sfx"] == [] and _spend_rows() == []


def test_a_library_sound_with_nothing_to_judge_or_gone_is_refused(auth_client, calls, tmp_path):
    blank = _asset(tmp_path, "")
    for text in (f"Hello there.\n[sfx: #{blank}]\n", "Hello there.\n[sfx: #99999]\n"):
        sid = _raw_story(text)
        assert _generate(auth_client, sid, ok=False).status_code == 422
    assert calls["tts"] == [] and _spend_rows() == []
    # A described Library sound is judged with the story and used for free.
    ok = _asset(tmp_path, "gentle rain on a tin roof")
    sid = _raw_story(f"Hello there.\n[sfx: #{ok}]\n")
    _generate(auth_client, sid)
    assert _story(auth_client, sid)["status"] == "ready" and calls["sfx"] == []


# ---------- jobs:J1 / audio-frontend:A3 — a paid take survives a stopped retry ----------


def _fails_once_for(monkeypatch, word: str) -> None:  # noqa: ANN001
    from app.services import sound_clips

    real = sound_clips.qa

    def qa(clip, stats, style):  # noqa: ANN001
        if word in clip.item.text:
            return "too fast"
        return real(clip, stats, style)

    monkeypatch.setattr(sound_clips, "qa", qa)


def test_a_paid_take_is_kept_flagged_when_the_budget_stops_its_retry(auth_client, calls, monkeypatch):
    from app.services import sound_clips, spend

    monkeypatch.setattr(spend, "_all_mocked", lambda: False)
    _fails_once_for(monkeypatch, "pine")
    s = _new(auth_client, script_text=SMALL)
    _budget(auth_client, 5.0)

    def costly(kind, n):  # noqa: ANN001
        if kind == "tts" and n == 1:
            spend.record_rate_card("mock", "tts", "mock-voice", 5.0, note="a dear clip")

    calls["hook"] = costly
    _generate(auth_client, s["id"])
    got = _story(auth_client, s["id"])
    assert got["status"] == "failed" and got["error"] == sound_clips.BUDGET_REACHED
    assert len(calls["tts"]) == 1
    first = next(b for b in _clips(auth_client, s["id"])["clips"] if "pine" in (b.get("text") or ""))
    assert first["state"] == "flagged" and first["takes"][0]["error"] == "too fast"
    calls["hook"] = None
    _budget(auth_client, 50.0)
    assert auth_client.post(f"/api/sound/stories/{s['id']}/resume").status_code == 202
    # The paid first take was not made again.
    assert _story(auth_client, s["id"])["status"] == "ready" and len(calls["tts"]) == 1 + 5


def test_a_paid_take_is_kept_flagged_when_cancel_stops_its_retry(auth_client, calls, monkeypatch):
    from app import models
    from app.services import sound_studio

    _fails_once_for(monkeypatch, "pine")
    s = _new(auth_client, script_text=SMALL)

    def hook(kind, n):  # noqa: ANN001
        if kind == "tts" and n == 1:
            db = _db()
            try:
                sound_studio.cancel(db, db.get(models.SoundStory, s["id"]))
            finally:
                db.close()

    calls["hook"] = hook
    _generate(auth_client, s["id"])
    assert _story(auth_client, s["id"])["status"] == "cancelled" and len(calls["tts"]) == 1
    first = next(b for b in _clips(auth_client, s["id"])["clips"] if "pine" in (b.get("text") or ""))
    assert first["state"] == "flagged"
    calls["hook"] = None
    assert auth_client.post(f"/api/sound/stories/{s['id']}/resume").status_code == 202
    assert _story(auth_client, s["id"])["status"] == "ready" and len(calls["tts"]) == 1 + 5


# ---------- jobs:J2 — Cancel then a new claim while a call is in flight ----------


def test_a_take_in_flight_is_kept_when_cancel_and_a_new_claim_replace_the_run(auth_client, calls):
    from app import models
    from app.services import sound_studio

    s = _new(auth_client, script_text=SMALL)

    def hook(kind, n):  # noqa: ANN001
        if kind == "tts" and n == 2:
            db = _db()
            try:
                story = db.get(models.SoundStory, s["id"])
                sound_studio.cancel(db, story)
                db.refresh(story)
                sound_studio.claim(db, story, sound_studio.CLIPS_KEY, status="generating")  # Resume's claim
            finally:
                db.close()

    calls["hook"] = hook
    _generate(auth_client, s["id"])
    db = _db()
    try:
        rows = db.query(models.SoundClip).filter(models.SoundClip.story_id == s["id"]).all()
    finally:
        db.close()
    # Both paid takes are there (the second one's progress save found no claim).
    assert len(calls["tts"]) == 2 and len(rows) == 2


def test_resume_waits_while_the_cancelled_run_is_still_in_flight(auth_client, calls, monkeypatch):
    from app import models
    from app.workers import queue

    s = _new(auth_client, script_text=SMALL)
    db = _db()
    try:
        db.query(models.SoundStory).filter(models.SoundStory.id == s["id"]).update(
            {"status": "cancelled", "claim_token": "run:abc", "queue_job_id": "rq-1",
             "progress_json": {"task": "clips", "previous": "ready"}}, synchronize_session=False)
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(queue, "queue_entry_state", lambda job_id: "alive")
    assert auth_client.post(f"/api/sound/stories/{s['id']}/resume").status_code == 409
    assert calls["tts"] == []
    monkeypatch.setattr(queue, "queue_entry_state", lambda job_id: "gone")
    assert auth_client.post(f"/api/sound/stories/{s['id']}/resume").status_code == 202
    assert _story(auth_client, s["id"])["status"] == "ready"


# ---------- jobs:J3 — Generate never saves over a claim made while it checked ----------


def test_generate_never_saves_over_a_claim_made_while_it_checked(auth_client, calls, monkeypatch):
    from app import models
    from app.services import sound_clips, sound_studio

    s = _new(auth_client, script_text=SMALL)
    real = sound_clips.prepare

    def slow(story):  # noqa: ANN001
        out = real(story)
        db = _db()
        try:  # a Write claims the story while Generate's checks run
            sound_studio.claim(db, db.get(models.SoundStory, s["id"]), sound_studio.WRITER_KEY)
        finally:
            db.close()
        return out

    monkeypatch.setattr(sound_clips, "prepare", slow)
    assert _generate(auth_client, s["id"], ok=False).status_code == 409
    progress = _progress(s["id"])
    assert progress.get("task") == "writer" and progress.get("previous") == "ready"
    assert "clips" not in progress and calls["tts"] == []


# ---------- jobs:J4 / api-cost:F4 — previews keep their lock and are capped ----------


def test_a_swept_preview_lock_gives_back_a_resumable_clips_run(auth_client, calls):
    from app import models
    from app.services import sound_clips, sound_studio, sweepers

    s = _new(auth_client, script_text=SMALL)
    db = _db()
    try:
        db.query(models.SoundStory).filter(models.SoundStory.id == s["id"]).update(
            {"status": "failed", "error": sound_clips.BUDGET_REACHED,
             "progress_json": {"task": "clips", "previous": "ready", "clips": {"total": 8, "done": 3}}},
            synchronize_session=False)
        db.commit()
        sound_clips._Lock(db, db.get(models.SoundStory, s["id"]), sound_studio.PREVIEW_KEY)
        old = datetime.utcnow() - timedelta(hours=6)
        db.query(models.SoundStory).filter(models.SoundStory.id == s["id"]).update(
            {"updated_at": old, "heartbeat_at": old}, synchronize_session=False)
        db.commit()
    finally:
        db.close()
    assert sweepers.run_once()["sound"] == {"failed": 1}
    got = _story(auth_client, s["id"])
    assert got["status"] == "failed" and got["clips"]["can_resume"]
    assert _progress(s["id"]).get("task") == "clips"


def test_a_preview_refreshes_its_heartbeat_clip_by_clip(auth_client, calls):
    from app import models

    s = _new(auth_client, script_text=SMALL)
    beats = []

    def hook(kind, n):  # noqa: ANN001
        db = _db()
        try:
            beats.append(db.get(models.SoundStory, s["id"]).heartbeat_at)
        finally:
            db.close()

    calls["hook"] = hook
    r = auth_client.post(f"/api/sound/stories/{s['id']}/preview", json={"chapter": 1})
    assert r.status_code == 200, r.text
    assert len(beats) >= 3 and len(set(beats)) == len(beats)


def test_range_and_chapter_previews_are_capped(auth_client, calls):
    from app import models
    from app.services import sound_clips

    got = _written(auth_client)
    db = _db()
    try:
        story = db.get(models.SoundStory, got["id"])
        doc, clips = sound_clips.plan(story)
    finally:
        db.close()
    limit = sound_clips.PREVIEW_MAX_MINUTES * 60.0
    for scope in ({"start_line": 1}, {"start_line": 1, "end_line": 10 ** 6}, {"chapter": 1}):
        chosen = sound_clips.scope_clips(doc, clips, **scope)
        assert chosen and len(chosen) < len(clips)
        assert sum(c.seconds for c in chosen) <= limit
    est = auth_client.post("/api/sound/estimate", json={"story_id": got["id"],
                                                        "preview": {"start_line": 1}}).json()["preview"]
    assert est["clips"] == len(sound_clips.scope_clips(doc, clips, start_line=1))


# ---------- jobs:J5 / api-cost:F2 — any finished take with the fingerprint is free ----------


def test_going_back_to_an_earlier_voice_or_line_costs_nothing(auth_client, calls):
    s = _new(auth_client, script_text=SMALL)
    _generate(auth_client, s["id"])
    assert len(calls["tts"]) == 6
    r = auth_client.patch(f"/api/sound/stories/{s['id']}", json={"cast": {"Mara": {"voice_id": "voice-b"}}})
    assert r.status_code == 200, r.text
    _generate(auth_client, s["id"])
    assert len(calls["tts"]) == 6 + 3  # Mara's three clips in the new voice
    # Back to the first voice: its takes are still there.
    assert auth_client.patch(f"/api/sound/stories/{s['id']}", json={"cast": {}}).status_code == 200
    est = auth_client.post("/api/sound/estimate", json={"story_id": s["id"]}).json()["generate"]
    assert est["missing"] == 0 and est["total"] == 0
    _generate(auth_client, s["id"])
    assert len(calls["tts"]) == 9
    blocks = _clips(auth_client, s["id"])["clips"]
    assert all(b["state"] == "done" for b in blocks)
    # An edit undone is free too.
    story = _story(auth_client, s["id"])
    text = story["script_text"]
    edited = text.replace("It was only the wind after all, I think.", "It was only the wind.")
    for t in (edited, text):
        v = _story(auth_client, s["id"])["script_version"]
        assert auth_client.patch(f"/api/sound/stories/{s['id']}",
                                 json={"script_text": t, "script_version": v}).status_code == 200
        _generate(auth_client, s["id"])
    assert len(calls["tts"]) == 10


# ---------- jobs:J6 — the post-processing is in the fingerprint ----------


def test_the_fingerprint_follows_the_post_processing():
    from app.services import sound_clips, story_script

    doc = story_script.parse("@cast MARA: adult\nMARA: Hello there, it is late.\n", clean=False, max_minutes=90)
    cozy = sound_clips.build(doc, {}, {"style": "cozy"})[0]
    asmr = sound_clips.build(doc, {}, {"style": "asmr"})[0]
    loud = sound_clips.build(doc, {"MARA": {"loudness": -16}}, {"style": "cozy"})[0]
    assert len({cozy.fingerprint, asmr.fingerprint, loud.fingerprint}) == 3
    assert asmr.whisper and asmr.target == sound_clips.WHISPER_TARGET_LUFS and loud.target == -16


# ---------- jobs:J7 — the same sound made twice at once ----------


def test_a_sound_made_by_another_run_meanwhile_is_used_not_overwritten(auth_client, calls, tmp_path):
    from app import models
    from app.services import sound_clips

    key = sound_clips.sfx_key("door creaks", 3.0, False, sound_clips.sound_model())
    winner: dict = {}

    def hook(kind, n):  # noqa: ANN001
        if kind == "sfx":
            f = _mp3(tmp_path / "winner.mp3")
            winner["bytes"] = f.read_bytes()
            db2 = _db()
            try:
                a = models.SoundAsset(key_hash=key, kind="sfx", prompt="door creaks", seconds=3.0, path=str(f),
                                      source="generated")
                db2.add(a)
                db2.commit()
                winner["id"], winner["path"] = a.id, str(f)
            finally:
                db2.close()

    calls["hook"] = hook
    db = _db()
    try:
        asset, cached = sound_clips.generate_sound(db, "door creaks", 3.0)
        assert asset.id == winner["id"] and asset.path == winner["path"] and not cached
        assert db.query(models.SoundAsset).filter(models.SoundAsset.key_hash == key).count() == 1
    finally:
        db.close()
    assert Path(winner["path"]).read_bytes() == winner["bytes"]
    assert not list(sound_clips.assets_dir().glob(f"{key[:32]}*"))


# ---------- api-cost:F1 — an edit moves only the cuts next to it ----------


def test_growing_or_shrinking_one_line_of_a_long_narration_rebills_at_most_two_clips():
    from app.services import sound_clips, story_script

    body = "the slow tide rolls in over the sand while the harbour lamps glow softly in the rain. "
    text = "\n".join(f"Line {i}: {body * 2}".strip() for i in range(80))
    doc = story_script.parse(text, clean=False, max_minutes=90)
    base = [c for c in sound_clips.build(doc) if c.kind == "voice"]
    assert len(base) >= 8
    before = {c.fingerprint for c in base}
    for where in (0, 40):
        for change in ("add 5", "add 95", "add 400", "cut"):
            lines = text.split("\n")
            if change == "cut":
                lines[where] = f"Line {where}: short now."
            else:
                lines[where] += " And more" * (int(change.split()[1]) // 9 + 1)
            edited = story_script.parse("\n".join(lines), clean=False, max_minutes=90)
            after = [c for c in sound_clips.build(edited) if c.kind == "voice"]
            changed = [c for c in after if c.fingerprint not in before]
            assert len(changed) <= 2, (where, change, len(changed), len(after))
            assert all(len(c.item.voice_text) <= sound_clips.MAX_CLIP_CHARS for c in after)


# ---------- api-cost:F3 — a regenerated sound is new and priced right ----------


def test_a_regenerated_sound_is_new_and_paid_and_a_library_cue_is_free(auth_client, calls):
    r = auth_client.post("/api/sound/effects", json={"prompt": "door creak", "seconds": 3, "variations": 4})
    assert r.status_code == 200, r.text
    effects = {a["id"] for a in r.json()["assets"]}
    assert len(calls["sfx"]) == 4
    s = _new(auth_client, script_text="She waits.\n[sfx: door creak | 3s]\n")
    _generate(auth_client, s["id"])
    assert len(calls["sfx"]) == 4  # the cached sound
    cue = next(b for b in _clips(auth_client, s["id"])["clips"] if b["kind"] == "sfx")
    for n in (1, 2):
        est = auth_client.post("/api/sound/estimate", json={"story_id": s["id"], "cue_key": cue["key"]}).json()
        assert est["regenerate"]["total"] == pytest.approx(3 * 0.002)
        r = auth_client.post(f"/api/sound/stories/{s['id']}/clips/regenerate", json={"cue_key": cue["key"]})
        assert r.status_code == 200, r.text
        assert len(calls["sfx"]) == 4 + n
    from app import models

    db = _db()
    try:
        used = {(r.params_json or {}).get("asset_id") for r in
                db.query(models.SoundClip).filter(models.SoundClip.story_id == s["id"],
                                                  models.SoundClip.take_no > 1)}
    finally:
        db.close()
    assert len(used) == 2 and not used & effects
    # A Library cue: a new take is free, and priced so.
    lib = _new(auth_client, title="Lib", script_text=f"She waits.\n[sfx: #{min(effects)}]\n")
    _generate(auth_client, lib["id"])
    cue = next(b for b in _clips(auth_client, lib["id"])["clips"] if b["kind"] == "sfx")
    est = auth_client.post("/api/sound/estimate", json={"story_id": lib["id"], "cue_key": cue["key"]}).json()
    assert est["regenerate"]["total"] == 0
    n = len(calls["sfx"])
    assert auth_client.post(f"/api/sound/stories/{lib['id']}/clips/regenerate",
                            json={"cue_key": cue["key"]}).status_code == 200
    assert len(calls["sfx"]) == n


# ---------- audio-frontend:A1 / A2 — the QA gate and loudness on real audio ----------


def test_a_clipped_mp3_take_is_caught_and_a_loud_clean_one_is_not(tmp_path):
    from app.services import sound_clips

    clipped = _mp3(tmp_path / "clipped.mp3", af="volume=30dB", seconds=3)
    fine = _mp3(tmp_path / "fine.mp3", af="volume=17.5dB", seconds=3)
    assert sound_clips.process_voice(clipped.read_bytes(), tmp_path / "a.wav", whisper=False,
                                     target=-20.0)["clipped"] is True
    assert sound_clips.process_voice(fine.read_bytes(), tmp_path / "b.wav", whisper=False,
                                     target=-20.0)["clipped"] is False


def test_clips_reach_the_target_loudness_whatever_their_peaks_or_length(tmp_path):
    from app.services import sound_clips

    # Quiet speech-like tone with sharp peaks near full scale: the old gain
    # cap (never past -1 dBFS) left it ~10 LU short of the target.
    peaky = _mp3(tmp_path / "peaky.mp3",
                 src="aevalsrc=0.03*sin(2*PI*220*t)+0.85*lt(mod(t\\,0.5)\\,0.00003):s=44100:d=3")
    out = tmp_path / "peaky.wav"
    stats = sound_clips.process_voice(peaky.read_bytes(), out, whisper=True, target=-24.0)
    assert stats["gain_db"] > 8
    got = sound_clips.measure(out)
    assert abs(got["lufs"] - -24.0) < 1.5, got
    assert got["peak"] <= -0.9
    # A 0.3 s "Yes." is measured for what it is, not -70 LUFS.
    short = _mp3(tmp_path / "short.mp3", src="sine=f=300:r=44100", af="volume=-12dB", seconds=0.3)
    out = tmp_path / "short.wav"
    stats = sound_clips.process_voice(short.read_bytes(), out, whisper=False, target=-20.0)
    assert stats["lufs"] > -60 and abs(sound_clips.measure(out)["lufs"] - -20.0) < 1.5


# ---------- audio-frontend:A4 / A5 — unreadable audio, failed sounds ----------


def test_unreadable_provider_audio_is_retried_then_flagged_not_a_story_failure(auth_client, calls, monkeypatch):
    from app.providers.mock_tts import MockTTSProvider

    recorded = MockTTSProvider.synthesize

    def bad(self, *, text, voice_id, settings=None):  # noqa: ANN001
        out = recorded(self, text=text, voice_id=voice_id, settings=settings)
        return b"<html>upstream error</html>" if "porch" in text else out

    monkeypatch.setattr(MockTTSProvider, "synthesize", bad)
    s = _new(auth_client, script_text=SMALL)
    _generate(auth_client, s["id"])
    got = _story(auth_client, s["id"])
    assert got["status"] == "ready", got["error"]
    assert got["clips"]["flagged"] == 1 and len(calls["tts"]) == 6 + 1
    porch = next(b for b in _clips(auth_client, s["id"])["clips"] if "porch" in (b.get("text") or ""))
    assert porch["state"] == "flagged" and "unreadable" in porch["takes"][0]["error"]
    assert porch["takes"][0]["attempts"] == 2


def test_a_sound_failing_its_check_is_retried_and_never_cached(auth_client, calls, monkeypatch, tmp_path):
    from app.providers.sound import MockSoundProvider

    silent = _mp3(tmp_path / "silent.mp3", src="anullsrc=r=44100:cl=mono", seconds=4).read_bytes()
    recorded = MockSoundProvider.generate
    n = {"calls": 0}

    def gen(self, *, prompt, duration_seconds=30.0, loop=True, prompt_influence=0.5):  # noqa: ANN001
        out = recorded(self, prompt=prompt, duration_seconds=duration_seconds, loop=loop,
                       prompt_influence=prompt_influence)
        n["calls"] += 1
        return silent if n["calls"] == 1 else out

    monkeypatch.setattr(MockSoundProvider, "generate", gen)
    a = _new(auth_client, script_text="The wind rises.\n[sfx: distant wind | 4s]\n")
    _generate(auth_client, a["id"])
    assert _story(auth_client, a["id"])["status"] == "ready"
    cue = next(b for b in _clips(auth_client, a["id"])["clips"] if b["kind"] == "sfx")
    assert cue["state"] == "done" and cue["takes"][0]["attempts"] == 2 and len(calls["sfx"]) == 2
    # The next story gets the good sound from the cache, not the silent one.
    b = _new(auth_client, title="B", script_text="Later.\n[sfx: distant wind | 4s]\n")
    _generate(auth_client, b["id"])
    cue = next(x for x in _clips(auth_client, b["id"])["clips"] if x["kind"] == "sfx")
    assert cue["state"] == "done" and len(calls["sfx"]) == 2

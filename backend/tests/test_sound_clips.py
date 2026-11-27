"""Sound Studio PR3: the clips of a story (voices and one-shot sound
effects): building and merging, fingerprint reuse, the clips job (Cancel,
Resume after a killed worker, the budget), the QA gate, the shared sound
cache, regenerate and takes, previews, the effects tool, and the safety
checks before anything is queued or paid."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from .test_adult_features_off import _clean
from .test_sound_scripts import MINOR_PREMISE, _db, _new, _story, _written

EXPLICIT_LINE = "An explicit sex scene between two adult lovers."


class Killed(BaseException):
    """A worker killed mid-run (nothing in the job catches it)."""


@pytest.fixture
def calls(monkeypatch):
    """Every paid provider call of the mock voice and sound providers, each
    recorded in the spend ledger as a real provider records it."""
    from app.providers.mock_tts import MockTTSProvider
    from app.providers.sound import MockSoundProvider
    from app.services import spend

    out = {"tts": [], "sfx": [], "hook": None}
    real_tts = MockTTSProvider.synthesize
    real_sfx = MockSoundProvider.generate

    def tts(self, *, text, voice_id, settings=None):  # noqa: ANN001
        out["tts"].append({"text": text, "voice": voice_id, "settings": dict(settings or {})})
        if out["hook"]:
            out["hook"]("tts", len(out["tts"]))
        spend.record_rate_card("mock", "tts", "mock-voice", 0.001, note="clip")
        return real_tts(self, text=text, voice_id=voice_id, settings=settings)

    def sfx(self, *, prompt, duration_seconds=30.0, loop=True, prompt_influence=0.5):  # noqa: ANN001
        out["sfx"].append({"prompt": prompt, "seconds": duration_seconds, "loop": loop})
        if out["hook"]:
            out["hook"]("sfx", len(out["sfx"]))
        spend.record_rate_card("mock", "sound", "mock/sound", 0.001, note="sfx")
        return real_sfx(self, prompt=prompt, duration_seconds=duration_seconds, loop=loop,
                        prompt_influence=prompt_influence)

    monkeypatch.setattr(MockTTSProvider, "synthesize", tts)
    monkeypatch.setattr(MockSoundProvider, "generate", sfx)
    return out


SMALL = """@cast NARRATOR [center]: calm adult narrator
@cast MARA [left]: adult woman in her thirties

# Chapter 1: The cabin
The rain had not stopped for three days, and the cabin smelled of pine and old smoke.
MARA: Did you hear that? Something moved out on the porch.
[sfx: old door creaks | 3s]
The door swung open on its own, and a cold wind came in.
MARA [whispers]: Close it, please. Close it now.
(pause 1)

# Chapter 2: Morning
Morning came grey and quiet over the hills behind the cabin.
MARA: It was only the wind after all, I think.
[sfx: kettle whistles | 2s]
"""


def _spend_rows():
    from app import models

    db = _db()
    try:
        return db.query(models.SpendEntry).all()
    finally:
        db.close()


def _clips(c, sid):
    r = c.get(f"/api/sound/stories/{sid}/clips")
    assert r.status_code == 200, r.text
    return r.json()


def _generate(c, sid, ok=True):
    r = c.post(f"/api/sound/stories/{sid}/generate")
    if ok:
        assert r.status_code == 202, r.text
    return r


# ---------- building the clips ----------


def test_lines_merge_by_speaker_and_tags_and_long_lines_split_at_sentences():
    from app.services import sound_clips, story_script

    long_line = " ".join(f"Sentence number {i} goes on for a little while." for i in range(80))
    text = ("@cast MARA: adult woman\n"
            "The first line.\nThe second line.\n"
            "[whispers] A whispered line.\n[whispers] Another whispered line.\n"
            "MARA: Her line.\nMARA: Her second line.\n"
            "[sfx: thunder | 2s]\n"
            "After the thunder.\n(pause 1)\nAfter the pause.\n"
            f"{long_line}\n")
    doc = story_script.parse(text, clean=False, max_minutes=90)
    clips = sound_clips.build(doc)
    voice = [c for c in clips if c.kind == "voice"]
    said = [c.item.voice_text for c in voice]
    assert said[0] == "The first line. The second line."
    assert said[1] == "[whispers] A whispered line. Another whispered line."
    assert said[2] == "Her line. Her second line." and voice[2].item.speaker == "MARA"
    assert said[3] == "After the thunder." and said[4].startswith("After the pause.")
    # The long line merged after "After the pause." is cut at sentence ends.
    tail = voice[4:]
    assert len(tail) >= 2 and all(len(c.item.voice_text) <= sound_clips.MAX_CLIP_CHARS for c in tail)
    assert all(c.item.voice_text.endswith(".") for c in tail)
    assert " ".join(c.item.text for c in tail) == "After the pause. " + " ".join(long_line.split())
    # One sound clip, between the lines it splits; keys and fingerprints are distinct.
    assert [c.kind for c in clips].count("sfx") == 1 and clips[3].kind == "sfx"
    assert len({c.key for c in clips}) == len(clips) and len({c.fingerprint for c in clips}) == len(clips)
    # Cue lines are never voiced text.
    assert not any("[sfx" in t or "(pause" in t or "thunder |" in t for t in said)


def test_the_fingerprint_follows_voice_model_seed_and_text():
    from app.services import sound_clips, story_script

    doc = story_script.parse("@cast MARA: adult\nMARA: Hello there.\n", clean=False, max_minutes=90)
    base = sound_clips.build(doc)[0]
    assert base.provider == "elevenlabs" and base.model == "eleven_v4"
    for cast, settings in (({"MARA": {"voice_id": "v2"}}, {}), ({"MARA": {"seed": 7}}, {}),
                           ({}, {"voice_model": "eleven_v3"}),
                           ({"MARA": {"provider": "openrouter", "model": "openai/gpt-4o-mini-tts"}}, {})):
        other = sound_clips.build(doc, cast, settings)[0]
        assert other.key == base.key and other.fingerprint != base.fingerprint
    assert sound_clips.build(doc, {"MARA": {}}, {})[0].fingerprint == base.fingerprint


# ---------- the clips job ----------


def test_a_30_minute_story_generates_every_clip_with_the_mock_providers(auth_client, calls):
    from app.services import sound_clips, story_script

    got = _written(auth_client)
    doc = story_script.parse(got["script_text"], style="cozy")
    assert doc.est_seconds / 60 > 25
    r = _generate(auth_client, got["id"])
    assert r.json()["price"]["total"] > 0
    story = _story(auth_client, got["id"])
    assert story["status"] == "ready", story["error"]
    out = _clips(auth_client, got["id"])
    assert out["clips"] and all(b["state"] == "done" for b in out["clips"]), \
        [b for b in out["clips"] if b["state"] != "done"][:2]
    voice = [b for b in out["clips"] if b["kind"] == "voice"]
    sfx = [b for b in out["clips"] if b["kind"] == "sfx"]
    # A line said again with the same voice (the mock writer repeats some) is made once.
    assert len(calls["tts"]) == len({b["takes"][-1]["fingerprint"] for b in voice}) and sfx
    assert len(calls["tts"]) > 10
    # Merged: fewer clips than lines, none over the limit.
    assert len(voice) < len(doc.speech())
    assert all(len(c["text"]) <= sound_clips.MAX_CLIP_CHARS for c in voice)
    # Cue lines are never sent to a voice; one sound per distinct sound.
    for t in (c["text"] for c in calls["tts"]):
        assert "[sfx" not in t and "(pause" not in t and "@bed" not in t
    assert len(calls["sfx"]) == len({(i.prompt.lower(), i.seconds or 3.0) for i in doc.sfx()})
    assert all(not c["loop"] for c in calls["sfx"])
    # 48 kHz float WAV clips, every one of a character at the same loudness.
    from pathlib import Path

    from app import models

    db = _db()
    try:
        rows = db.query(models.SoundClip).filter(models.SoundClip.kind == "voice").all()
    finally:
        db.close()
    probe = sound_clips.measure(Path(rows[0].path))
    assert probe["duration"] > 0.5
    lufs = {round(r.params_json["qa"]["lufs"]) for r in rows}
    assert len(lufs) == 1, lufs
    assert story["clips"]["done"] == len(out["clips"]) and story["clips"]["finished"]
    # Spend went to the story, with no project.
    assert {(e.ref, e.project_id) for e in _spend_rows()} == {(f"sound_story:{got['id']}", None)}
    # Nothing left to pay.
    est = auth_client.post("/api/sound/estimate", json={"story_id": got["id"]}).json()
    assert est["generate"]["total"] == 0 and est["generate"]["missing"] == 0
    evs = auth_client.get("/api/events/recent?limit=100").json()["events"]
    mine = [e for e in evs if e["kind"] == "sound_story" and e["entity_id"] == got["id"]]
    assert any(e["title"].startswith("Voices ready") for e in mine)
    assert any(e["status"] == "progress" and "clips" in e["title"] for e in mine)


def test_cue_lines_are_never_voiced(auth_client, calls):
    s = _new(auth_client, script_text=SMALL)
    _generate(auth_client, s["id"])
    assert _story(auth_client, s["id"])["status"] == "ready"
    voiced = " ".join(c["text"] for c in calls["tts"])
    assert "creaks" not in voiced and "whistles" not in voiced and "pause" not in voiced
    assert [c["prompt"] for c in calls["sfx"]] == ["old door creaks", "kettle whistles"]
    # The whispered line carries its tag for the tag-reading model.
    assert any(c["text"].startswith("[whispers]") for c in calls["tts"])


def test_editing_one_line_rebills_only_that_clip(auth_client, calls):
    s = _new(auth_client, script_text=SMALL)
    _generate(auth_client, s["id"])
    first = len(calls["tts"])
    assert first == 6
    story = _story(auth_client, s["id"])
    text = story["script_text"].replace("It was only the wind after all, I think.", "It was only the wind.")
    r = auth_client.patch(f"/api/sound/stories/{s['id']}", json={"script_text": text,
                                                                  "script_version": story["script_version"]})
    assert r.status_code == 200, r.text
    est = auth_client.post("/api/sound/estimate", json={"story_id": s["id"]}).json()["generate"]
    assert est["missing"] == 1 and est["total"] > 0
    calls["tts"].clear()
    calls["sfx"].clear()
    _generate(auth_client, s["id"])
    assert [c["text"] for c in calls["tts"]] == ["It was only the wind."] and calls["sfx"] == []
    assert all(b["state"] == "done" for b in _clips(auth_client, s["id"])["clips"])


def test_cancel_stops_before_the_next_call_and_resume_makes_the_rest(auth_client, calls):
    from app import models
    from app.services import sound_studio

    s = _new(auth_client, script_text=SMALL)

    def hook(kind, n):  # noqa: ANN001
        if kind == "tts" and n == 2:
            db = _db()
            try:
                sound_studio.cancel(db, db.get(models.SoundStory, s["id"]))
            finally:
                db.close()

    calls["hook"] = hook
    _generate(auth_client, s["id"])
    got = _story(auth_client, s["id"])
    # The call in flight is paid for and kept; nothing after it.
    assert got["status"] == "cancelled" and got["clips"]["can_resume"]
    assert len(calls["tts"]) == 2 and calls["sfx"] == []
    assert sum(b["state"] == "done" for b in _clips(auth_client, s["id"])["clips"]) == 2
    calls["hook"] = None
    calls["tts"].clear()
    r = auth_client.post(f"/api/sound/stories/{s['id']}/resume")
    assert r.status_code == 202, r.text
    assert _story(auth_client, s["id"])["status"] == "ready"
    assert len(calls["tts"]) == 4 and len(calls["sfx"]) == 2


def test_a_killed_worker_is_swept_and_resume_pays_only_for_the_missing_clips(auth_client, calls):
    from app import models
    from app.services import sound_studio, sweepers
    from app.workers.jobs import story_clips_job

    s = _new(auth_client, script_text=SMALL)

    def hook(kind, n):  # noqa: ANN001
        if kind == "tts" and n == 4:
            raise Killed()

    calls["hook"] = hook
    db = _db()
    try:
        story = db.get(models.SoundStory, s["id"])
        token, _ = sound_studio.claim(db, story, sound_studio.CLIPS_KEY, status="generating")
    finally:
        db.close()
    with pytest.raises(Killed):
        story_clips_job(s["id"], claim_token=token)
    assert _story(auth_client, s["id"])["status"] == "generating"
    paid = len(_spend_rows())
    assert paid == 4  # three voice clips and one sound, before the call the worker died in
    old = datetime.utcnow() - timedelta(hours=6)
    db = _db()
    try:
        db.query(models.SoundStory).filter(models.SoundStory.id == s["id"]).update(
            {"updated_at": old, "heartbeat_at": old}, synchronize_session=False)
        db.commit()
    finally:
        db.close()
    assert sweepers.run_once()["sound"] == {"failed": 1}
    got = _story(auth_client, s["id"])
    assert got["status"] == "failed" and got["error"] == sound_studio.STOPPED and got["clips"]["can_resume"]
    calls["hook"] = None
    before = {"tts": len(calls["tts"]), "sfx": len(calls["sfx"])}
    est = auth_client.post("/api/sound/estimate", json={"story_id": s["id"]}).json()["generate"]
    assert est["missing"] == 8 - 4  # 6 voice clips + 2 sounds, 3 voices and 1 sound kept
    assert auth_client.post(f"/api/sound/stories/{s['id']}/resume").status_code == 202
    assert _story(auth_client, s["id"])["status"] == "ready"
    # Paid again only for the clip the worker died in and those after it.
    assert len(calls["tts"]) - before["tts"] == 3 and len(calls["sfx"]) - before["sfx"] == 1
    assert len(_spend_rows()) == paid + 4


def test_budget_reached_mid_run_is_a_resumable_failure(auth_client, calls, monkeypatch):
    from app.services import sound_clips, spend

    monkeypatch.setattr(spend, "_all_mocked", lambda: False)
    s = _new(auth_client, script_text=SMALL)
    assert auth_client.put("/api/spend/budget", json={"monthly_usd": 5.0, "hard_stop": True,
                                                      "alert_pcts": []}).status_code == 200

    def costly(kind, n):  # noqa: ANN001
        spend.record_rate_card("mock", "tts", "mock-voice", 2.5, note="a dear clip")

    calls["hook"] = costly
    _generate(auth_client, s["id"])
    got = _story(auth_client, s["id"])
    assert got["status"] == "failed" and got["error"] == sound_clips.BUDGET_REACHED
    assert got["clips"]["can_resume"] and len(calls["tts"]) == 2
    # Over the cap, Resume is refused before anything is claimed...
    assert auth_client.post(f"/api/sound/stories/{s['id']}/resume").status_code == 402
    # ...and goes on once it is raised.
    calls["hook"] = None
    assert auth_client.put("/api/spend/budget", json={"monthly_usd": 50.0, "hard_stop": True,
                                                      "alert_pcts": []}).status_code == 200
    assert auth_client.post(f"/api/sound/stories/{s['id']}/resume").status_code == 202
    assert _story(auth_client, s["id"])["status"] == "ready" and len(calls["tts"]) == 6


def test_a_clip_failing_qa_is_retried_once_then_flagged(auth_client, calls, monkeypatch):
    from app.services import sound_clips

    real = sound_clips.qa
    seen: dict[str, int] = {}

    def qa(clip, stats, style):  # noqa: ANN001
        seen[clip.item.text] = seen.get(clip.item.text, 0) + 1
        if "porch" in clip.item.text:
            return "too fast"  # always
        if "Close it" in clip.item.text and seen[clip.item.text] == 1:
            return "silent"  # once
        return real(clip, stats, style)

    monkeypatch.setattr(sound_clips, "qa", qa)
    s = _new(auth_client, script_text=SMALL)
    _generate(auth_client, s["id"])
    got = _story(auth_client, s["id"])
    assert got["status"] == "ready" and got["clips"]["flagged"] == 1
    assert len(calls["tts"]) == 6 + 2
    seeds = [c["settings"].get("seed") for c in calls["tts"] if "porch" in c["text"]]
    assert len(seeds) == 2 and seeds[1] is not None and seeds[0] != seeds[1]
    blocks = {b["text"]: b for b in _clips(auth_client, s["id"])["clips"] if b["kind"] == "voice"}
    porch = next(b for t, b in blocks.items() if "porch" in t)
    assert porch["state"] == "flagged" and porch["takes"][0]["error"] == "too fast"
    assert porch["takes"][0]["attempts"] == 2
    close = next(b for t, b in blocks.items() if "Close it" in t)
    assert close["state"] == "done" and close["takes"][0]["attempts"] == 2
    # A flagged clip is paid for: Resume or Generate don't make it again.
    assert auth_client.post("/api/sound/estimate", json={"story_id": s["id"]}).json()["generate"]["missing"] == 0


def test_the_real_qa_gate_flags_silence_and_a_bad_duration(tmp_path):
    from app.services import sound_clips, story_script

    doc = story_script.parse("A line that is long enough to have a duration band of its own here.\n",
                             clean=False, max_minutes=90)
    clip = sound_clips.build(doc)[0]
    assert sound_clips.qa(clip, {"silent": True}, "") == "The clip came back silent."
    assert sound_clips.qa(clip, {"duration": 4.0}, "") is None
    assert "outside" in sound_clips.qa(clip, {"duration": 40.0}, "")
    assert "outside" in sound_clips.qa(clip, {"duration": 0.5}, "asmr")
    # Silence from a provider comes out silent after the trim.
    import subprocess

    raw = tmp_path / "silence.mp3"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "2", str(raw)],
                   capture_output=True, check=True)
    stats = sound_clips.process_voice(raw.read_bytes(), tmp_path / "out.wav", whisper=True, target=-24.0)
    assert stats["silent"] is True


def test_one_shot_sounds_are_cached_across_stories(auth_client, calls):
    a = _new(auth_client, script_text=SMALL)
    _generate(auth_client, a["id"])
    assert len(calls["sfx"]) == 2
    b = _new(auth_client, title="Another", script_text="Somebody knocks.\n[sfx: Old door  creaks | 3s]\n")
    est = auth_client.post("/api/sound/estimate", json={"story_id": b["id"]}).json()["generate"]
    assert not any(line["item"].startswith("Sound effects") for line in est["lines"])
    _generate(auth_client, b["id"])
    # The same door, from the cache: no new sound, nothing paid for it.
    assert len(calls["sfx"]) == 2
    sfx = [x for x in _clips(auth_client, b["id"])["clips"] if x["kind"] == "sfx"]
    assert sfx[0]["state"] == "done" and sfx[0]["takes"][0]["cost_usd"] == 0.0


# ---------- regenerate, takes, preview ----------


def test_regenerate_makes_a_new_selected_take_and_takes_can_be_picked(auth_client, calls):
    s = _new(auth_client, script_text=SMALL)
    _generate(auth_client, s["id"])
    block = next(b for b in _clips(auth_client, s["id"])["clips"] if b["kind"] == "voice")
    est = auth_client.post("/api/sound/estimate", json={"story_id": s["id"], "cue_key": block["key"]}).json()
    assert est["regenerate"]["total"] > 0 and est["regenerate"]["total"] == round(block["cost"], 4)
    n = len(calls["tts"])
    r = auth_client.post(f"/api/sound/stories/{s['id']}/clips/regenerate", json={"cue_key": block["key"]})
    assert r.status_code == 200, r.text
    assert len(calls["tts"]) == n + 1 and r.json()["take"]["take_no"] == 2
    again = next(b for b in _clips(auth_client, s["id"])["clips"] if b["key"] == block["key"])
    assert [t["take_no"] for t in again["takes"]] == [1, 2] and again["selected"] == again["takes"][1]["id"]
    first = again["takes"][0]["id"]
    assert auth_client.post(f"/api/sound/stories/{s['id']}/clips/{first}/select").status_code == 200
    again = next(b for b in _clips(auth_client, s["id"])["clips"] if b["key"] == block["key"])
    assert again["selected"] == first and again["state"] == "done"
    audio = auth_client.get(again["takes"][0]["url"])
    assert audio.status_code == 200 and audio.content[:4] == b"RIFF"
    # The story is free again (the lock was given back).
    assert _story(auth_client, s["id"])["status"] == "ready"
    # A sound cue gets a new sound too, not the cached one.
    cue = next(b for b in _clips(auth_client, s["id"])["clips"] if b["kind"] == "sfx")
    k = len(calls["sfx"])
    assert auth_client.post(f"/api/sound/stories/{s['id']}/clips/regenerate",
                            json={"cue_key": cue["key"]}).status_code == 200
    assert len(calls["sfx"]) == k + 1


def test_preview_makes_only_its_scope(auth_client, calls):
    got = _written(auth_client)
    total = len(_clips(auth_client, got["id"])["clips"])
    est = auth_client.post("/api/sound/estimate", json={"story_id": got["id"],
                                                        "preview": {"first_minutes": 1}}).json()["preview"]
    assert 0 < est["clips"] < total
    r = auth_client.post(f"/api/sound/stories/{got['id']}/preview", json={"first_minutes": 1})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["clips"] == est["clips"] and len(calls["tts"]) + len(calls["sfx"]) <= out["clips"]
    assert 20 < out["duration"] < 120
    audio = auth_client.get(out["url"])
    assert audio.status_code == 200 and audio.headers["content-type"] == "audio/mpeg"
    made = [b for b in _clips(auth_client, got["id"])["clips"] if b["state"] == "done"]
    assert len(made) == out["clips"]
    assert _story(auth_client, got["id"])["status"] == "ready"
    # A preview over 3 minutes is refused; a chapter preview makes that chapter only.
    assert auth_client.post(f"/api/sound/stories/{got['id']}/preview", json={"first_minutes": 4}).status_code == 422
    r = auth_client.post(f"/api/sound/stories/{got['id']}/preview", json={"chapter": 1})
    assert r.status_code == 200, r.text
    chapters = {b["chapter"] for b in _clips(auth_client, got["id"])["clips"] if b["state"] == "done"}
    assert chapters <= {0, 1}


# ---------- the effects tool ----------


def test_effects_variations_are_saved_to_the_library_and_cached(auth_client, calls):
    est = auth_client.post("/api/sound/effects/estimate", json={"prompt": "Wooden door creaks open",
                                                                "seconds": 3, "variations": 3}).json()
    assert est["total"] == pytest.approx(3 * 3 * 0.002)
    r = auth_client.post("/api/sound/effects", json={"prompt": "Wooden door creaks open", "seconds": 3,
                                                     "variations": 3})
    assert r.status_code == 200, r.text
    assets = r.json()["assets"]
    assert len(assets) == 3 and len(calls["sfx"]) == 3 and not any(a["cached"] for a in assets)
    lib = auth_client.get("/api/sound/assets").json()
    assert {a["id"] for a in assets} <= {a["id"] for a in lib}
    assert auth_client.get(assets[0]["url"]).status_code == 200
    # The same request again is free; variation 1 is the sound a story gets.
    r = auth_client.post("/api/sound/effects", json={"prompt": "wooden door creaks open", "seconds": 3,
                                                     "variations": 3})
    assert all(a["cached"] for a in r.json()["assets"]) and len(calls["sfx"]) == 3
    s = _new(auth_client, script_text="She opens it.\n[sfx: wooden door creaks open | 3s]\n")
    _generate(auth_client, s["id"])
    assert len(calls["sfx"]) == 3
    assert auth_client.post("/api/sound/effects", json={"prompt": "x", "variations": 5}).status_code == 422
    assert auth_client.delete(f"/api/sound/assets/{assets[2]['id']}").status_code == 204


# ---------- safety ----------


def test_clean_mode_refuses_explicit_voiced_text_and_sound_prompts_before_enqueue(auth_client, calls, monkeypatch):
    monkeypatch.delenv("ADULT_FEATURES", raising=False)
    from .test_adult_features_off import _reset

    _reset()
    line = _new(auth_client, script_text=f"NARRATOR: {EXPLICIT_LINE}\n")
    sound = _new(auth_client, title="Sounds", script_text="They kiss.\n[sfx: explicit sex sounds | 3s]\n")
    _clean(monkeypatch)
    for sid in (line["id"], sound["id"]):
        r = _generate(auth_client, sid, ok=False)
        assert r.status_code == 422, r.text
        assert _story(auth_client, sid)["status"] == "ready"
    r = auth_client.post(f"/api/sound/stories/{sound['id']}/preview", json={"first_minutes": 1})
    assert r.status_code == 422
    r = auth_client.post("/api/sound/effects", json={"prompt": "explicit sex sounds", "seconds": 3})
    assert r.status_code == 422
    assert calls["tts"] == [] and calls["sfx"] == [] and _spend_rows() == []


@pytest.mark.parametrize("mode", ["default", "adult", "clean"])
def test_a_minors_sexual_line_is_refused_in_every_mode(auth_client, calls, monkeypatch, mode):
    from app import config, models

    if mode == "clean":
        _clean(monkeypatch)
    elif mode == "adult":
        monkeypatch.setenv("ADULT_MODE", "true")
        config.get_settings.cache_clear()
    # Saved before these checks (straight into the table): Generate checks again.
    db = _db()
    try:
        story = models.SoundStory(title="T", premise="", script_text=f"NARRATOR: {MINOR_PREMISE}\n",
                                  script_version=1, status="ready", settings_json={"style": "cozy"})
        db.add(story)
        db.commit()
        sid = story.id
    finally:
        db.close()
    r = _generate(auth_client, sid, ok=False)
    assert r.status_code == 422 and "minors" in r.json()["detail"]
    r = auth_client.post(f"/api/sound/stories/{sid}/preview", json={"first_minutes": 1})
    assert r.status_code == 422
    r = auth_client.post("/api/sound/effects", json={"prompt": "sexual sounds of a 14-year-old girl"})
    assert r.status_code == 422
    assert calls["tts"] == [] and calls["sfx"] == [] and _spend_rows() == []


def test_a_minors_sexual_sound_prompt_is_refused_with_the_cast_in_view(auth_client, calls):
    from app import models

    db = _db()
    try:
        story = models.SoundStory(
            title="T", script_text="@cast LILY: a 12-year-old girl\nLILY: Hello.\n[sfx: Lily moans sexually | 2s]\n",
            script_version=1, status="ready", settings_json={})
        db.add(story)
        db.commit()
        sid = story.id
    finally:
        db.close()
    assert _generate(auth_client, sid, ok=False).status_code == 422
    assert calls["tts"] == [] and calls["sfx"] == []


def test_voices_per_character_are_validated_and_used(auth_client, calls):
    s = _new(auth_client, script_text=SMALL)
    r = auth_client.patch(f"/api/sound/stories/{s['id']}", json={"cast": {"Mara": {"provider": "nope"}}})
    assert r.status_code == 422
    r = auth_client.patch(f"/api/sound/stories/{s['id']}", json={"cast": {"Mara": {"model": "eleven_v1"}}})
    assert r.status_code == 422
    r = auth_client.patch(f"/api/sound/stories/{s['id']}",
                          json={"cast": {"Mara": {"voice_id": "mara-voice", "model": "eleven_v3", "seed": 11}}})
    assert r.status_code == 200, r.text
    cfg = auth_client.get("/api/sound/config").json()["voices"]
    assert cfg["default"] == {"provider": "elevenlabs", "model": "eleven_v4"}
    assert set(cfg["elevenlabs_models"]) == {"eleven_v4", "eleven_v3", "eleven_v4_turbo"}
    _generate(auth_client, s["id"])
    assert _story(auth_client, s["id"])["status"] == "ready"
    mara = [c for c in calls["tts"] if c["voice"] == "mara-voice"]
    assert len(mara) == 3 and all(c["settings"].get("seed") == 11 for c in mara)
    blocks = [b for b in _clips(auth_client, s["id"])["clips"] if b.get("speaker") == "MARA"]
    assert {b["model"] for b in blocks} == {"eleven_v3"}

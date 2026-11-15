"""Sound Studio foundations: the 0012 tables, per-request voice settings,
voice tags on providers without them, one-shot sound effects, the guarded
sound and music providers, the long-script safety check and the price."""
from __future__ import annotations

import json
import subprocess

import httpx
import pytest
import respx

from .test_adult_features_off import _clean, _reset
from .test_models_catalog import OR, _real

_SEXUAL = "the two adults kiss, then have sex on the bed, fully naked. "
_BEDTIME = ("Little Sam, who is six, pulls the blanket up to his chin while the rain taps on the window. "
            "His grandmother reads him a story about a sleepy owl and a lantern in the woods. ")


# ---------- migration 0012 ----------


def test_migration_0012_sound_tables(tmp_path, monkeypatch):
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import inspect, text

    from app import config
    from app import db as dbmod

    url = f"sqlite:///{tmp_path / 'm.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config.get_settings.cache_clear()
    dbmod.reset_engine(url)
    command.upgrade(dbmod._alembic_config(), "0011")
    assert "sound_stories" not in inspect(dbmod.engine).get_table_names()
    command.upgrade(dbmod._alembic_config(), "head")
    insp = inspect(dbmod.engine)
    assert {"sound_stories", "sound_clips", "sound_assets", "sound_renders"} <= set(insp.get_table_names())
    assert {"title", "kind", "premise", "target_minutes", "settings_json", "script_text", "script_version",
            "cast_json", "mix_json", "status", "claim_token", "queue_job_id", "heartbeat_at", "progress_json",
            "estimated_cost", "actual_cost", "error", "created_at", "updated_at"} <= {
        c["name"] for c in insp.get_columns("sound_stories")}
    assert {i["name"] for i in insp.get_indexes("sound_stories")} >= {
        "ix_sound_stories_status", "ix_sound_stories_updated_at"}
    idx = {i["name"]: i["column_names"] for i in insp.get_indexes("sound_clips")}
    assert idx["ix_sound_clips_story_chapter_seq"] == ["story_id", "chapter", "seq"]
    assert idx["ix_sound_clips_fingerprint"] == ["fingerprint"]
    fk = insp.get_foreign_keys("sound_clips")[0]
    assert fk["referred_table"] == "sound_stories" and fk["options"].get("ondelete") == "CASCADE"
    (afk,) = insp.get_foreign_keys("sound_assets")
    assert afk["options"].get("ondelete") == "SET NULL"
    assert any(u["column_names"] == ["key_hash"] for u in insp.get_unique_constraints("sound_assets"))
    (rfk,) = insp.get_foreign_keys("sound_renders")
    assert rfk["referred_table"] == "sound_stories" and rfk["options"].get("ondelete") == "CASCADE"
    with dbmod.engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0012"
        diff = compare_metadata(MigrationContext.configure(conn), dbmod.Base.metadata)
    assert diff == [], diff  # same as `alembic check`
    command.downgrade(dbmod._alembic_config(), "0011")
    assert not {"sound_stories", "sound_clips", "sound_assets", "sound_renders"} & set(
        inspect(dbmod.engine).get_table_names())
    command.upgrade(dbmod._alembic_config(), "head")
    assert "sound_renders" in inspect(dbmod.engine).get_table_names()


def test_models_store_and_cascade(client):
    from app import models
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        story = models.SoundStory(title="The cabin", premise="rain", cast_json={"MARA": {"pan": "left"}})
        db.add(story)
        db.flush()
        db.add_all([models.SoundClip(story_id=story.id, cue_key="vabc", kind="voice", status="done"),
                    models.SoundRender(story_id=story.id),
                    models.SoundAsset(prompt="door", key_hash="k1", story_id=story.id)])
        db.commit()
        assert (story.status, story.kind, story.script_version) == ("draft", "story", 0)
        clip = db.query(models.SoundClip).one()
        assert (clip.selected, clip.take_no, clip.cost_usd) == (True, 1, 0.0)
        db.delete(story)
        db.commit()
        # Clips and renders go with the story; the library keeps its sound.
        assert db.query(models.SoundClip).count() == 0 and db.query(models.SoundRender).count() == 0
        (asset,) = db.query(models.SoundAsset).all()
        assert asset.story_id is None
    finally:
        db.close()


# ---------- config ----------


def test_config_rates_and_caps():
    from app.config import get_settings

    s = get_settings()
    assert s.elevenlabs_story_model_id == "eleven_v4"
    assert s.sound_story_max_minutes == 90
    assert s.cost_sound_per_second == 0.002 and s.cost_chat_per_story_call == 0.005
    assert (s.tts_rate("eleven_v4"), s.tts_rate("eleven_v4_turbo"), s.tts_rate("eleven_v3")) == (0.08, 0.04, 0.08)
    assert s.tts_rate("some_other_model") == s.cost_tts_per_1k_chars == s.tts_rate("")


# ---------- ElevenLabs voice: per-request settings ----------


@respx.mock
def test_elevenlabs_default_request_is_unchanged(client, monkeypatch):
    _real(monkeypatch)
    from app.providers.elevenlabs_tts import ElevenLabsTTSProvider

    route = respx.post(url__regex=r".*/text-to-speech/v1.*").mock(return_value=httpx.Response(
        200, content=b"mp3", headers={"character-cost": "5"}))
    ElevenLabsTTSProvider().synthesize(text="Line one.\nLine two.", voice_id="v1")
    req = route.calls[0].request
    assert json.loads(req.content) == {"text": "Line one.\nLine two.", "model_id": "eleven_multilingual_v2",
                                       "voice_settings": {"stability": 0.5, "similarity_boost": 0.7}}
    assert "output_format" not in req.url.params and req.headers["Accept"] == "audio/mpeg"


@respx.mock
def test_elevenlabs_per_request_settings(client, monkeypatch):
    _real(monkeypatch)
    from app.providers.elevenlabs_tts import ElevenLabsTTSProvider

    from .test_spend import _entries

    route = respx.post(url__regex=r".*/text-to-speech/v9.*").mock(return_value=httpx.Response(
        200, content=b"pcm", headers={"character-cost": "1000"}))
    ElevenLabsTTSProvider().synthesize(
        text="[whispers] Did you hear that?", voice_id="v9",
        settings={"model_id": "eleven_v4", "output_format": "pcm_48000", "seed": 42, "story_markup": True,
                  "voice_settings": {"stability": 0.3, "similarity_boost": 0.8}})
    req = route.calls[0].request
    assert json.loads(req.content) == {"text": "[whispers] Did you hear that?", "model_id": "eleven_v4",
                                       "voice_settings": {"stability": 0.3, "similarity_boost": 0.8},
                                       "seed": 42}
    assert req.url.params["output_format"] == "pcm_48000" and req.headers["Accept"] == "audio/pcm"
    # A story voice: the model's own rate.
    (row,) = _entries()
    assert row["model"] == "eleven_v4" and row["cost_usd"] == pytest.approx(0.08)


@respx.mock
def test_elevenlabs_model_on_the_provider_and_tags_off_older_models(client, monkeypatch):
    _real(monkeypatch)
    from app.providers.elevenlabs_tts import ElevenLabsTTSProvider

    route = respx.post(url__regex=r".*/text-to-speech/v1.*").mock(return_value=httpx.Response(200, content=b"x"))
    ElevenLabsTTSProvider(model_id="eleven_v3").synthesize(text="[sighs] Hi [Verse 1].", voice_id="v1")
    assert json.loads(route.calls[0].request.content)["text"] == "[sighs] Hi [Verse 1]."
    # A model without audio tags would read "[sighs]" aloud: in story markup
    # it's taken out (other bracketed text stays).
    ElevenLabsTTSProvider().synthesize(text="[sighs] Hi [Verse 1].", voice_id="v1",
                                       settings={"story_markup": True})
    assert json.loads(route.calls[1].request.content)["text"] == "Hi [Verse 1]."


# ---------- OpenRouter voice: tags become instructions ----------


@respx.mock
def test_openrouter_speech_translates_tags_to_instructions(monkeypatch):
    _real(monkeypatch)
    from app.providers.openrouter_tts import OpenRouterTTSProvider

    route = respx.post(f"{OR}/audio/speech").mock(
        return_value=httpx.Response(200, content=b"MP3", headers={"content-type": "audio/mpeg"}))
    p = OpenRouterTTSProvider(model="openai/gpt-4o-mini-tts", voices=["nova"], chat_audio=False)
    p.synthesize(text="[whispers][slowly] Close your eyes. [short pause] Breathe [left].", voice_id="nova",
                 settings={"story_markup": True})
    body = json.loads(route.calls[0].request.content)
    assert body["input"] == "Close your eyes. … Breathe."
    assert body["instructions"] == ("Read the text aloud exactly as written. Delivery: whisper; speak slowly; "
                                    "brief pauses.")
    p.synthesize(text="[sighs] Fine.", voice_id="nova",
                 settings={"instructions": "A tired adult voice.", "story_markup": True})
    body = json.loads(route.calls[1].request.content)
    assert body["input"] == "Fine." and body["instructions"].startswith("A tired adult voice. Read the text")
    # Text without tags: exactly as before.
    p.synthesize(text="Plain [Verse 1] text.", voice_id="nova")
    assert json.loads(route.calls[2].request.content) == {
        "model": "openai/gpt-4o-mini-tts", "input": "Plain [Verse 1] text.", "voice": "nova",
        "response_format": "mp3"}


@respx.mock
def test_openrouter_chat_audio_gets_instructions_as_system(monkeypatch):
    _real(monkeypatch)
    from app.providers.openrouter_tts import OpenRouterTTSProvider

    sse = "data: " + json.dumps({"choices": [{"delta": {"audio": {"data": "AAAA"}}}]}) + "\n\ndata: [DONE]\n\n"
    route = respx.post(f"{OR}/chat/completions").mock(
        return_value=httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"}))
    OpenRouterTTSProvider(model="openai/gpt-audio-mini").synthesize(text="[giggles] Hello.", voice_id="",
                                                                    settings={"story_markup": True})
    msgs = json.loads(route.calls[0].request.content)["messages"]
    assert "a giggle" in msgs[0]["content"] and msgs[1]["content"] == "Hello."


def _seconds(data: bytes, tmp_path) -> float:
    f = tmp_path / "a.m4a"
    f.write_bytes(data)
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(f)],
                         capture_output=True, text=True).stdout
    return float(out.strip())


def test_mock_voice_ignores_tags(tmp_path):
    from app.providers.mock_tts import MockTTSProvider

    tags = "[whispers] [slowly] [sighs] " * 20
    data = MockTTSProvider().synthesize(text=tags + "one two three four five", voice_id="",
                                        settings={"story_markup": True})
    assert _seconds(data, tmp_path) == pytest.approx(2.0, abs=0.2)  # 5 words: the 2 s minimum


# ---------- voices per story and per character ----------


def test_tts_for_a_story_or_character(client, monkeypatch):
    from app.providers import registry
    from app.providers.elevenlabs_tts import ElevenLabsTTSProvider
    from app.providers.mock_tts import MockTTSProvider
    from app.providers.openrouter_tts import OpenRouterTTSProvider

    assert isinstance(registry.get_tts_for("elevenlabs", "eleven_v3"), MockTTSProvider)  # mock mode
    _real(monkeypatch)
    el = registry.get_tts_for("elevenlabs")
    assert type(el) is registry.GuardedTTSProvider and isinstance(el, ElevenLabsTTSProvider)
    assert el.model_id == "eleven_v4"
    assert registry.get_tts_for("elevenlabs", "eleven_v4_turbo").model_id == "eleven_v4_turbo"
    orv = registry.get_tts_for("openrouter", "openai/gpt-4o-mini-tts", voice="nova")
    assert isinstance(orv, OpenRouterTTSProvider) and orv.model == "openai/gpt-4o-mini-tts" and orv.voice == "nova"
    assert isinstance(registry.get_tts_for("mock"), MockTTSProvider)
    with pytest.raises(ValueError):
        registry.get_tts_for("acme")
    with pytest.raises(ValueError):
        registry.get_tts_for("openrouter")


def test_tts_for_refuses_a_minor_sexual_line(client):
    from app.providers import registry
    from app.services.safety import UnsafeScriptError

    with pytest.raises(UnsafeScriptError):
        registry.get_tts_for("elevenlabs").synthesize(text="the 14-year-old girl gets naked", voice_id="")


# ---------- sound effects: one-shots ----------


@respx.mock
def test_elevenlabs_one_shot_sound_is_billed_by_the_second(client, monkeypatch):
    _real(monkeypatch)
    from app.providers.sound import ElevenLabsSoundProvider

    from .test_spend import _entries

    route = respx.post("https://api.elevenlabs.io/v1/sound-generation").mock(
        return_value=httpx.Response(200, content=b"ID3"))
    ElevenLabsSoundProvider().generate(prompt="old door creaks", duration_seconds=3, loop=False,
                                       prompt_influence=0.8)
    body = json.loads(route.calls[0].request.content)
    assert body == {"text": "old door creaks", "duration_seconds": 3.0, "loop": False, "prompt_influence": 0.8,
                    "model_id": "eleven_text_to_sound_v3"}
    (row,) = _entries()
    assert row["cost_usd"] == pytest.approx(3 * 0.002)


def test_mock_one_shot_sound(tmp_path):
    from app.providers.sound import MockSoundProvider

    data = MockSoundProvider().generate(prompt="knock", duration_seconds=2, loop=False)
    assert _seconds(data, tmp_path) == pytest.approx(2.0, abs=0.15)


# ---------- guarded sound and music ----------


@pytest.fixture
def paid_audio(monkeypatch):
    from app.providers import mock_music, sound

    calls: list[str] = []
    real_sound = sound.MockSoundProvider.generate
    real_music = mock_music.MockMusicProvider.generate

    def s(self, *, prompt, **kw):
        calls.append("sound:" + prompt)
        return real_sound(self, prompt=prompt, **kw)

    def m(self, *, prompt, **kw):
        calls.append("music:" + prompt)
        return real_music(self, prompt=prompt, **kw)

    monkeypatch.setattr(sound.MockSoundProvider, "generate", s)
    monkeypatch.setattr(mock_music.MockMusicProvider, "generate", m)
    return calls


def test_sound_and_music_are_guarded(client, paid_audio):
    from app.providers import registry
    from app.providers.mock_music import MockMusicProvider
    from app.providers.sound import MockSoundProvider

    snd, mus = registry.get_sound(), registry.get_music()
    assert type(snd) is registry.GuardedSoundProvider and isinstance(snd, MockSoundProvider)
    assert type(mus) is registry.GuardedMusicProvider and isinstance(mus, MockMusicProvider)
    assert snd.__class__.__name__ == "MockSoundProvider"  # what the provider log records
    assert snd.generate(prompt="rain on glass", duration_seconds=1, loop=False)
    assert mus.generate(prompt="soft piano", duration_seconds=2)
    assert paid_audio == ["sound:rain on glass", "music:soft piano"]


@pytest.mark.parametrize("prompt", [
    "a 14-year-old girl moaning during sex",
    "schoolgirl naked in the shower, giggling",
])
def test_minor_sexual_sound_and_music_prompts_are_refused_in_every_mode(client, paid_audio, prompt):
    from app.providers import registry
    from app.services.safety import UnsafeScriptError

    with pytest.raises(UnsafeScriptError):
        registry.get_sound().generate(prompt=prompt, duration_seconds=2)
    with pytest.raises(UnsafeScriptError):
        registry.get_music().generate(prompt=prompt, duration_seconds=2)
    assert paid_audio == []


def test_the_ai_check_judges_a_sound_prompt_that_mentions_a_minor(client, paid_audio):
    from app.providers import mock_chat, registry
    from app.services.safety import UnsafeScriptError

    mock_chat.MINOR_REPLY = {"minor_sexual": True, "reason": "test"}
    with pytest.raises(UnsafeScriptError):
        registry.get_sound().generate(prompt="a child whispering in a dark bedroom", duration_seconds=2)
    assert mock_chat.MINOR_CALLS and paid_audio == []
    # Round 11: a prompt that is neither sexual nor about a minor is asked
    # about too (and plays when the judge says ok).
    mock_chat.MINOR_CALLS.clear()
    mock_chat.MINOR_REPLY = None
    registry.get_sound().generate(prompt="waves on a pebble beach", duration_seconds=1)
    assert len(mock_chat.MINOR_CALLS) == 1


def test_adult_sound_prompt_is_allowed_by_default(client, paid_audio):
    from app.providers import registry

    registry.get_sound().generate(prompt="two adults kissing, soft sighs, sex", duration_seconds=1)
    assert len(paid_audio) == 1


def test_clean_deployment_refuses_explicit_sound_and_music(client, monkeypatch, paid_audio):
    from app.providers import registry
    from app.services.explicit_check import ExplicitPromptError

    _clean(monkeypatch)
    for get in (registry.get_sound, registry.get_music):
        with pytest.raises(ExplicitPromptError):
            get().generate(prompt="a couple having sex on the bed, fully naked", duration_seconds=2)
    registry.get_sound().generate(prompt="rain on a tin roof", duration_seconds=1)
    assert paid_audio == ["sound:rain on a tin roof"]
    monkeypatch.delenv("ADULT_FEATURES")
    _reset()


# ---------- the long-script check ----------


def _long(chunk: str, n: int = 27000) -> str:
    return (chunk * (n // len(chunk) + 1))[:n]


def test_long_sexual_script_with_a_minor_is_refused(client):
    from app.services import minor_check
    from app.services.safety import UnsafePromptError

    # A family word ("daughter") isn't in the script word filter (ad copy
    # uses it about adults all the time): only the model sees the minor.
    text = _long(_BEDTIME, 13000) + " His daughter is in the room for the sex. " + _long(_SEXUAL, 14000)
    assert len(text) > 27000
    from app.services import safety

    safety.check_script(text)  # the word filter alone lets it through
    # Round 10: a long script is judged in overlapping windows (not refused
    # unjudged as before).
    with pytest.raises(UnsafePromptError, match="minor in a sexual context"):
        minor_check.check_generation(text, kind="script")
    with pytest.raises(UnsafePromptError, match="minor in a sexual context"):
        minor_check.check_long(text, kind="script", context="Cast:\nNARRATOR: adult narrator")
    # The obvious case is caught by the word filter in its window.
    blunt = _long(_BEDTIME, 13000) + " Later the 15-year-old girl undresses for sex. " + _long(_SEXUAL, 14000)
    with pytest.raises(UnsafePromptError, match="minors"):
        minor_check.check_long(blunt, context="Cast:\nNARRATOR: adult narrator")


def test_long_check_reads_every_window_with_the_cast(client):
    from app.providers import mock_chat
    from app.providers.mock_chat import CONTEXT_MARK
    from app.services import minor_check
    from app.services.safety import UnsafePromptError

    header = "Cast:\nLILY: the narrator's younger sister\nNARRATOR: adult woman"
    text = _long("They whisper by the fire. ", 15000) + _long(_SEXUAL, 12000)
    # The cast tells the model who is who; the mock judge is told to refuse.
    mock_chat.MINOR_REPLY = {"minor_sexual": True, "reason": "the sister is a minor"}
    with pytest.raises(UnsafePromptError):
        minor_check.check_long(text, context=header)
    asked = mock_chat.MINOR_CALLS
    # The cast is the app's block, in front of every window.
    assert asked and all(c.startswith(CONTEXT_MARK + "Cast:\nLILY") for c in asked)
    assert all(len(c) <= minor_check.JUDGE_WINDOW for c in asked)
    # Only windows that are sexual are asked about: the first ones aren't.
    mock_chat.MINOR_REPLY = {"minor_sexual": False, "reason": "adults"}
    mock_chat.MINOR_CALLS.clear()
    minor_check.reset_cache()
    minor_check.check_long(text, context="Cast:\nNARRATOR: adult woman\nMARK: adult man")
    assert 2 <= len(mock_chat.MINOR_CALLS) < 6


def test_long_innocent_bedtime_story_is_allowed(client):
    from app.providers import mock_chat
    from app.services import minor_check

    text = _long(_BEDTIME, 40000)
    mock_chat.MINOR_REPLY = {"minor_sexual": False, "reason": "a bedtime story"}
    minor_check.check_long(text, context="Cast:\nSAM: a six-year-old boy\nGRANDMA: his grandmother")
    # A minor in the cast: every part is asked about, with the cast in front.
    assert mock_chat.MINOR_CALLS
    assert all("six-year-old" in c for c in mock_chat.MINOR_CALLS)
    mock_chat.MINOR_CALLS.clear()
    minor_check.check_long(text.replace("Sam", "Tom").replace("six", "sixty"),
                           context="Cast:\nNARRATOR: an adult")
    assert mock_chat.MINOR_CALLS  # a story part: always asked


def test_long_check_fails_closed(client):
    from app.providers import mock_chat
    from app.services import minor_check
    from app.services.safety import UnsafePromptError

    mock_chat.MINOR_REPLY = RuntimeError("planner down")
    text = _long(_SEXUAL, 20000)
    with pytest.raises(UnsafePromptError, match="couldn't run"):
        minor_check.check_long(text, context="Cast:\nNARRATOR: adult")
    # A story part fails closed whatever it says.
    with pytest.raises(UnsafePromptError, match="couldn't run"):
        minor_check.check_long(_long(_BEDTIME, 20000), context="Cast:\nNARRATOR: adult")
    from app.providers.openrouter_chat import ModelDeclined

    mock_chat.MINOR_REPLY = ModelDeclined("no")
    with pytest.raises(UnsafePromptError, match="declined"):
        minor_check.check_long(text, context="Cast:\nNARRATOR: adult")


def test_long_check_with_a_cast_too_long(client):
    from app.services import minor_check
    from app.services.safety import UnsafePromptError

    huge = "Cast:\n" + "\n".join(f"P{i}: an adult with a very long description of their voice" for i in range(60))
    # A story's cast is always read whole with every part: one longer than
    # LONG_CONTEXT_LIMIT is refused, whatever the lines say.
    for text in (_long(_SEXUAL, 8000), _long(_BEDTIME, 8000), ""):
        with pytest.raises(UnsafePromptError, match="Character descriptions are too long"):
            minor_check.check_long(text, context=huge)


def test_explicit_parts_size():
    from app.services.explicit_check import PART, parts

    text = "word " * 3000
    assert all(len(p) <= PART for p in parts(text))
    small = parts(text, size=1000)
    assert all(len(p) <= 1000 for p in small) and len(small) > len(parts(text))


# ---------- the price ----------

_STORY = """@cast NARRATOR: adult narrator
@cast MARA: adult woman
@bed ambience "rain"
Hello there.
MARA [whispers]: Come here.
[sfx: door | 3s]
[sfx: door | 3s]
[sfx: owl]
[sfx: #4]
@bed music "piano"
"""
_CAST = {"MARA": {"provider": "openrouter", "model": "openai/gpt-4o-mini-tts"}}


def _by_unit(est: dict) -> dict:
    return {line["unit"]: line for line in est["lines"]}


def test_estimate_matches_a_hand_calculation():
    from app.services import sound_estimate, story_script

    doc = story_script.parse(_STORY, clean=False, max_minutes=90)
    est = sound_estimate.estimate(doc, {}, cast=_CAST)
    lines = est["lines"]
    voice = [line for line in lines if line["unit"] == "char"]
    # NARRATOR on ElevenLabs v4: "Hello there." = 12 characters at $0.08/1k.
    # MARA on gpt-4o-mini-tts (no tags): "Come here." = 10 at the $0.20/1k default.
    assert [(v["qty"], v["unit_cost"]) for v in voice] == [(12, 0.00008), (10, 0.0002)]
    assert "elevenlabs eleven_v4" in voice[0]["item"] and "Narrator" in voice[0]["item"]
    by = _by_unit(est)
    # The same door twice is one generation; #4 is from the library; the owl
    # gets the default 3 s.
    assert by["sec"]["qty"] == 6.0 and by["sec"]["cost"] == pytest.approx(0.012)
    assert by["sound"]["qty"] == 1 and by["sound"]["cost"] == 0.10
    assert by["track"]["qty"] == 1 and by["track"]["cost"] == 0.05
    assert by["render"]["cost"] == 0.0
    assert "call" not in by  # a script, no writer
    hand = round(12 * 0.08 / 1000, 4) + round(10 * 0.20 / 1000, 4) + 0.012 + 0.10 + 0.05
    assert est["total"] == pytest.approx(hand)
    assert est["is_estimate"] and est["currency"] == "USD"


def test_estimate_counts_generated_clips_as_free():
    from app.services import sound_estimate, story_script

    doc = story_script.parse(_STORY, clean=False, max_minutes=90)
    fp = sound_estimate.cue_fingerprint
    hello = fp(doc.speech()[0], _CAST, {})
    door = fp(doc.sfx()[0], _CAST, {})
    rain = fp(next(i for i in doc.items if i.kind == "bed"), _CAST, {})
    full = sound_estimate.estimate(doc, {}, cast=_CAST)
    est = sound_estimate.estimate(doc, {}, cast=_CAST, done_fingerprints={hello, door, rain})
    by = _by_unit(est)
    assert [line["qty"] for line in est["lines"] if line["unit"] == "char"] == [10]
    assert by["sec"]["qty"] == 3.0 and "sound" not in by
    assert by["line"]["cost"] == 0.0 and "1 lines" in by["line"]["item"]
    assert est["total"] < full["total"]


def test_estimate_of_a_stored_story_reads_its_done_clips_and_prices_writing(client):
    from app import models
    from app.db import SessionLocal
    from app.services import sound_estimate, story_script

    db = SessionLocal()
    try:
        story = models.SoundStory(title="t", premise="a quiet cabin in the rain", target_minutes=30,
                                  settings_json={"style": "sleep", "voice_provider": "elevenlabs",
                                                 "voice_model": "eleven_v4_turbo"})
        db.add(story)
        db.commit()
        est = sound_estimate.estimate(story, db=db)
        by = _by_unit(est)
        # An outline plus one call per 5 minutes.
        assert by["call"]["qty"] == 7 and by["call"]["cost"] == pytest.approx(0.035)
        assert est["total"] == pytest.approx(0.035)

        story.script_text = "Hello there.\nGood night."
        db.commit()
        first = story_script.parse(story.script_text).speech()[0]
        db.add(models.SoundClip(story_id=story.id, cue_key=first.key, kind="voice", status="done",
                                fingerprint=sound_estimate.cue_fingerprint(first, {}, story.settings_json)))
        db.add(models.SoundClip(story_id=story.id, cue_key="other", kind="voice", status="failed"))
        db.commit()
        est = sound_estimate.estimate(story, db=db)
        by = _by_unit(est)
        assert "call" not in by
        (voice,) = [line for line in est["lines"] if line["unit"] == "char"]
        assert voice["qty"] == len("Good night.") and voice["unit_cost"] == pytest.approx(0.04 / 1000)
        assert "eleven_v4_turbo" in voice["item"]
        assert est["minutes"] >= 0 and est["voiced_chars"] == len("Hello there.Good night.")
    finally:
        db.close()


def test_estimate_mock_voice_is_free_and_music_from_settings():
    from app.services import sound_estimate, story_script

    doc = story_script.parse("Hello.", clean=False, max_minutes=90)
    est = sound_estimate.estimate(doc, {"voice_provider": "mock", "music_prompt": "soft piano"})
    by = _by_unit(est)
    assert by["char"]["cost"] == 0.0 and by["track"]["cost"] == 0.05

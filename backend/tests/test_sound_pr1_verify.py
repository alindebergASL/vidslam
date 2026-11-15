"""Verification fixes for the Sound Studio foundations: story prompts judged
with the cast, open-ended voice directions, delivery instructions, existing
callers unchanged, the round trip of quotes and brackets, voice rates, one
default voice, positional keys and the cast checked on its own."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from .test_adult_features_off import _clean, _reset
from .test_models_catalog import OR, _real

_GIRL_CAST = "Cast:\nLILY: a 12-year-old girl"


def _parse(text: str, **kw):
    from app.services import story_script

    kw.setdefault("clean", False)
    kw.setdefault("max_minutes", 90)
    return story_script.parse(text, **kw)


@pytest.fixture
def paid_audio(monkeypatch):
    from app.providers import mock_music, sound

    calls: list[tuple[str, dict]] = []
    real_sound = sound.MockSoundProvider.generate
    real_music = mock_music.MockMusicProvider.generate

    def s(self, *, prompt, **kw):
        calls.append(("sound:" + prompt, kw))
        return real_sound(self, prompt=prompt, **kw)

    def m(self, *, prompt, **kw):
        calls.append(("music:" + prompt, kw))
        return real_music(self, prompt=prompt, **kw)

    monkeypatch.setattr(sound.MockSoundProvider, "generate", s)
    monkeypatch.setattr(mock_music.MockMusicProvider, "generate", m)
    return calls


# ---------- HIGH: story prompts are judged with the cast in view ----------


def test_story_check_judges_sound_and_bed_prompts_with_the_cast(client):
    from app.providers import mock_chat
    from app.services import story_script
    from app.services.safety import UnsafePromptError

    doc = _parse("@cast LILY: a 12-year-old girl\nThe night was calm.\n"
                 "[sfx: Lily breathing heavily | 2s]\n@bed ambience \"Lily whimpering in the dark\"")
    mock_chat.MINOR_REPLY = {"minor_sexual": True, "reason": "the girl is a minor"}
    with pytest.raises(UnsafePromptError):
        story_script.check_story(doc)
    asked = "\n".join(mock_chat.MINOR_CALLS)
    assert "Lily breathing heavily" in asked and "12-year-old" in asked
    assert "Lily whimpering in the dark" in asked


def test_story_check_leaves_an_innocent_story_alone(client):
    from app.providers import mock_chat
    from app.services import story_script

    doc = _parse("@cast SAM: a six-year-old boy\nSam yawned.\n[sfx: rain on the roof | 3s]")
    mock_chat.MINOR_REPLY = {"minor_sexual": False, "reason": "bedtime"}
    story_script.check_story(doc)


def test_story_check_reads_prompts_on_a_clean_deployment(client, monkeypatch):
    from app.services import story_script
    from app.services.explicit_check import ExplicitContentError

    _clean(monkeypatch)
    try:
        doc = _parse("Hello there.\n[sfx: a couple having sex, fully naked | 2s]")
        with pytest.raises(ExplicitContentError):
            story_script.check_story(doc)
        story_script.check_story(_parse("Hello there.\n[sfx: rain on a tin roof | 2s]"))
    finally:
        monkeypatch.delenv("ADULT_FEATURES")
        _reset()


def test_guarded_sound_and_music_read_the_cast_context(client, paid_audio):
    from app.providers import mock_chat, registry
    from app.services import story_script
    from app.services.safety import UnsafeScriptError

    from .story_helpers import checked, parse

    # The prompt is judged with the cast in view, by check_story.
    mock_chat.MINOR_REPLY = {"minor_sexual": True, "reason": "the girl is a minor"}
    with pytest.raises(UnsafeScriptError):
        story_script.check_story(parse("@cast LILY: a 12-year-old girl\nHello.\n[sfx: Lily breathing heavily | 1s]"))
    assert any("12-year-old" in c and "Lily breathing heavily" in c for c in mock_chat.MINOR_CALLS)
    # The guards send no story prompt the check didn't pass.
    with pytest.raises(UnsafeScriptError):
        registry.get_sound().generate(prompt="Lily breathing heavily", duration_seconds=1, context=_GIRL_CAST)
    with pytest.raises(UnsafeScriptError):
        registry.get_music().generate(prompt="Lily breathing heavily", duration_seconds=2, context=_GIRL_CAST)
    assert paid_audio == []
    # Allowed: the context isn't passed on to the provider.
    mock_chat.MINOR_REPLY = {"minor_sexual": False, "reason": "fine"}
    _doc, ctx = checked("@cast ANNA: adult\nANNA: Hello.\n[sfx: rain on the roof | 1s]")
    registry.get_sound().generate(prompt="rain on the roof", duration_seconds=1, context=ctx)
    assert paid_audio and "context" not in paid_audio[0][1]


# ---------- MEDIUM: open-ended directions ----------


@pytest.mark.parametrize("text", [
    "[moaning softly] Come here, little girl.",
    "[moans.] Come here, little girl.",
    "[softly moans] Come here, little girl.",
    "[seductive purr] Come here, little girl.",
])
def test_adult_direction_variants_with_a_minor_are_refused(client, text):
    from app.providers import registry
    from app.services.safety import UnsafeScriptError

    with pytest.raises(UnsafeScriptError):
        registry.get_tts_for("elevenlabs").synthesize(text=text, voice_id="")


def test_an_unknown_direction_near_a_minor_goes_to_the_model(client):
    from app.providers import mock_chat
    from app.services import story_script
    from app.services.safety import UnsafeScriptError

    from .story_helpers import checked, parse

    mock_chat.MINOR_REPLY = {"minor_sexual": True, "reason": "test"}
    with pytest.raises(UnsafeScriptError):
        story_script.check_story(parse("[breathes into the mic] Good night, little girl."))
    assert mock_chat.MINOR_CALLS
    # A minor in the cast counts too.
    mock_chat.MINOR_CALLS.clear()
    with pytest.raises(UnsafeScriptError):
        story_script.check_story(parse("@cast LILY: a 12-year-old girl\n[breathes into the mic] Come closer."))
    assert mock_chat.MINOR_CALLS
    # No minor in view: a story line is still asked about (and allowed).
    mock_chat.MINOR_CALLS.clear()
    mock_chat.MINOR_REPLY = {"minor_sexual": False, "reason": "adults"}
    checked("[breathes into the mic] Good night.")
    assert len(mock_chat.MINOR_CALLS) == 1


def test_safety_text_leaves_plain_text_alone():
    from app.services.story_script import safety_text

    for text in ("Good night, little girl.", "Prix : 5 euros.", "Plain [Verse 1] text."):
        assert safety_text(text) == text


@respx.mock
def test_clean_deployment_drops_unknown_directions_on_a_tag_model(client, monkeypatch):
    _real(monkeypatch)
    _clean(monkeypatch)
    try:
        from app.providers import registry
        from app.providers.elevenlabs_tts import ElevenLabsTTSProvider

        monkeypatch.setattr(registry, "_explicit", lambda *texts: None)
        from app.services import minor_check as _mcheck
        from app.services import story_pass

        from .story_helpers import pass_for

        # A line check_story passed (the pass is made here): the guard still
        # takes out every direction a clean deployment doesn't voice.
        assert _mcheck
        line = "[moans softly][whispers] Good night. [panting]"
        ctx = "Cast:\nNARRATOR"
        el = respx.post(url__regex=r".*/text-to-speech/v1.*").mock(return_value=httpx.Response(200, content=b"x"))
        guarded = registry.GuardedTTSProvider(ElevenLabsTTSProvider(model_id="eleven_v4"), story=True)
        guarded.synthesize(text=line, voice_id="v1", context=ctx, speaker="NARRATOR",
                           story_pass=pass_for(ctx, story_pass.line_item("NARRATOR", line), voices={"NARRATOR": "v1"}))
        assert json.loads(el.calls[0].request.content)["text"] == "[whispers] Good night."
    finally:
        monkeypatch.delenv("ADULT_FEATURES")
        _reset()


# ---------- MEDIUM: delivery instructions are checked ----------


def test_instructions_are_checked_with_the_text(client):
    from app.providers import registry
    from app.services.safety import UnsafeScriptError

    with pytest.raises(UnsafeScriptError):
        registry.get_tts_for("mock").synthesize(
            text="Come here.", voice_id="", settings={"instructions": "a naked 12-year-old girl, moaning"})
    with pytest.raises(UnsafeScriptError):
        registry.get_tts().synthesize(
            text="Come here.", voice_id="", settings={"instructions": "a naked 12-year-old girl, moaning"})


def test_instructions_get_the_explicit_check_on_a_clean_deployment(client, monkeypatch):
    from app.providers import registry
    from app.services.explicit_check import ExplicitContentError

    _clean(monkeypatch)
    try:
        from .story_helpers import checked

        assert registry
        # A story line's instructions are read by check_story's explicit check.
        with pytest.raises(ExplicitContentError):
            checked("Hello.", instructions={0: "describe them having sex, fully naked"}, clean=True)
    finally:
        monkeypatch.delenv("ADULT_FEATURES")
        _reset()


# ---------- LOW: existing callers send what they sent before ----------

_TAGGED = "[whispers] Hello there.\n\n[short pause] Line two [Verse 1]."


@respx.mock
def test_existing_callers_send_the_same_requests(client, monkeypatch):
    _real(monkeypatch)
    from app.providers import registry
    from app.providers.elevenlabs_tts import ElevenLabsTTSProvider
    from app.providers.openrouter_tts import OpenRouterTTSProvider
    from app.services import minor_check

    # Round 11: every script is judged; the judge (not under test) says ok.
    monkeypatch.setattr(minor_check, "_classify", lambda p, n, c="": (False, "ok", "test-judge"))
    checked: list[str] = []
    real_check = minor_check.check_generation
    monkeypatch.setattr(minor_check, "check_generation",
                        lambda t, n="", **kw: (checked.append((t, n, kw)), real_check(t, n, **kw)))

    el = respx.post(url__regex=r".*/text-to-speech/v1.*").mock(return_value=httpx.Response(200, content=b"x"))
    registry.GuardedTTSProvider(ElevenLabsTTSProvider()).synthesize(text=_TAGGED, voice_id="v1")
    # The request body wip-m sent.
    assert json.loads(el.calls[0].request.content) == {
        "text": _TAGGED, "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.7}}
    assert checked == [(_TAGGED, "", {"kind": "script"})]

    speech = respx.post(f"{OR}/audio/speech").mock(
        return_value=httpx.Response(200, content=b"MP3", headers={"content-type": "audio/mpeg"}))
    p = registry.GuardedTTSProvider(OpenRouterTTSProvider(model="openai/gpt-4o-mini-tts", chat_audio=False))
    p.synthesize(text=_TAGGED, voice_id="")
    assert json.loads(speech.calls[0].request.content) == {
        "model": "openai/gpt-4o-mini-tts", "input": _TAGGED, "voice": "alloy", "response_format": "mp3"}

    sse = "data: " + json.dumps({"choices": [{"delta": {"audio": {"data": "AAAA"}}}]}) + "\n\ndata: [DONE]\n\n"
    chat = respx.post(f"{OR}/chat/completions").mock(
        return_value=httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"}))
    c = registry.GuardedTTSProvider(OpenRouterTTSProvider(model="openai/gpt-audio-mini"))
    c.synthesize(text=_TAGGED, voice_id="")
    msgs = json.loads(chat.calls[0].request.content)["messages"]
    assert msgs == [{"role": "system", "content": "Read the user's text aloud exactly as written, warmly and "
                                                  "naturally. Do not add anything."},
                    {"role": "user", "content": _TAGGED}]
    c.synthesize(text="Hello.", voice_id="", settings={"instructions": "Warm  pirate voice."})
    assert json.loads(chat.calls[1].request.content)["messages"][0]["content"] == "Warm  pirate voice."


@respx.mock
def test_old_caller_safety_check_reads_the_raw_text(client, monkeypatch):
    from app.providers import registry
    from app.services import minor_check

    checked: list = []
    monkeypatch.setattr(minor_check, "check_generation", lambda t, n="", **kw: checked.append((t, n, kw)))
    monkeypatch.setattr(minor_check, "check_long", lambda t, **kw: pytest.fail("check_long off the story path"))
    registry.get_tts().synthesize(text="[lustful] Hello.", voice_id="")
    assert checked == [("[lustful] Hello.", "", {"kind": "script"})]


# ---------- LOW: chat audio without a tag direction ----------


@respx.mock
def test_story_chat_audio_without_tags_keeps_the_guard_first(monkeypatch):
    _real(monkeypatch)
    from app.providers.openrouter_tts import OpenRouterTTSProvider

    sse = "data: " + json.dumps({"choices": [{"delta": {"audio": {"data": "AAAA"}}}]}) + "\n\ndata: [DONE]\n\n"
    route = respx.post(f"{OR}/chat/completions").mock(
        return_value=httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"}))
    OpenRouterTTSProvider(model="openai/gpt-audio-mini").synthesize(
        text="She listened.", voice_id="", settings={"instructions": "Warm  voice.", "story_markup": True})
    from app.providers.openrouter_tts import CHAT_AUDIO_SYSTEM

    # The read-it-verbatim guard first, the caller's instructions after it.
    assert json.loads(route.calls[0].request.content)["messages"][0]["content"] == f"{CHAT_AUDIO_SYSTEM} Warm voice."


# ---------- LOW: the round trip ----------


@pytest.mark.parametrize("prompt", ['a sign reading "Open" in the wind', 'she says "hush"', '"hush" she says'])
def test_bed_prompt_keeps_inner_quotes(prompt):
    from app.services import story_script

    doc = _parse(f'@bed ambience "{prompt}"\nHello.')
    assert doc.items[0].prompt == prompt and doc.warnings == []
    again = _parse(story_script.serialize(doc))
    assert again.items[0].prompt == prompt and again.items[0].key == doc.items[0].key


@pytest.mark.parametrize("line", ["[sfx: a [loud] bang | 2s]", "[sfx: door [creak | 2s]", "[sfx: a ]bang | 2s]"])
def test_brackets_inside_a_cue_are_refused_with_a_warning(line):
    from app.services import story_script

    doc = _parse(f"{line}\nHello.")
    assert [(i.kind, i.text) for i in doc.items] == [("speech", "Hello.")]
    assert any("[" in w.message and "]" in w.message for w in doc.warnings)
    assert story_script.serialize(_parse(story_script.serialize(doc))) == story_script.serialize(doc)


def test_a_cue_followed_by_narration_still_works():
    doc = _parse("[sfx: door | 2s] then silence.")
    assert [i.kind for i in doc.items] == ["speech", "sfx"]


# ---------- LOW: voice rates ----------


def test_voice_rates_for_older_elevenlabs_models():
    from app.config import get_settings

    s = get_settings()
    assert s.tts_rate("eleven_multilingual_v2") == s.tts_rate("eleven_v3") == 0.08
    assert s.tts_rate("eleven_flash_v2_5") == s.tts_rate("eleven_turbo_v2_5") == 0.04


# ---------- LOW: one default voice ----------


def test_estimate_and_generator_agree_on_the_default_voice(client, monkeypatch):
    _real(monkeypatch)
    from app.providers import registry
    from app.services.sound_estimate import voice_route

    for settings in ({}, {"voice_model": "eleven_v3"}):
        provider, model = voice_route("NARRATOR", {}, settings)
        tts = registry.get_tts_for("", settings.get("voice_model", ""))
        assert provider == "elevenlabs" and type(registry.unwrap(tts)).__name__ == "ElevenLabsTTSProvider"
        assert tts.model_id == model


# ---------- LOW: keys of pauses, comments and bed stops ----------


def test_inserting_a_pause_or_comment_keeps_the_other_keys():
    def keys(doc):
        return {(i.kind, i.seconds, i.text, i.layer, i.action): i.key for i in doc.items}

    before = keys(_parse("// intro\n(pause 1)\nHello.\n(pause 2)\n// outro\n@bed ambience \"rain\"\n"
                         "@bed ambience stop"))
    after = keys(_parse("// new\n(pause 3)\n// intro\n(pause 1)\nHello.\n(pause 2)\n// outro\n"
                        "@bed music \"x\"\n@bed music stop\n@bed ambience \"rain\"\n@bed ambience stop"))
    assert all(after[k] == v for k, v in before.items())


# ---------- LOW: the cast alone ----------


def test_long_check_reads_the_cast_when_the_text_is_empty(client):
    from app.services import minor_check
    from app.services.safety import UnsafePromptError

    with pytest.raises(UnsafePromptError):
        minor_check.check_long("", context="Cast:\nLILY: a naked 12-year-old girl")
    minor_check.check_long("", context="Cast:\nANNA: an adult woman")

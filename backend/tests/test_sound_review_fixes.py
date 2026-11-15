"""Review fixes for the Sound Studio foundations: prices of edited lines, text
sent unchanged without tags, tag-only lines, chapters, speakers, the
round trip, the chat-audio guard, adult tags in the minors check, a long
cast list, clean-mode tags at the provider and music parts checked once."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from .test_adult_features_off import _clean, _reset
from .test_models_catalog import OR, _real


def _parse(text: str, **kw):
    from app.services import story_script

    kw.setdefault("clean", False)
    kw.setdefault("max_minutes", 90)
    return story_script.parse(text, **kw)


# ---------- the price of a changed line ----------


def test_estimate_prices_a_line_again_when_its_tags_seconds_or_voice_change():
    from app.services import sound_estimate

    before = _parse("MARA: Did you hear that?\n[sfx: door creak | 3s]")
    done = {sound_estimate.cue_fingerprint(i, {}, {}) for i in before.items}
    assert sound_estimate.estimate(before, {}, done_fingerprints=done)["total"] == 0.0

    edited = _parse("MARA [shouts]: Did you hear that?\n[sfx: door creak | 20s]")
    assert [i.key for i in edited.items] == [i.key for i in before.items]  # the same cues...
    full = sound_estimate.estimate(edited, {})["total"]
    # ...but other audio: priced in full.
    assert full > 0 and sound_estimate.estimate(edited, {}, done_fingerprints=done)["total"] == full

    cast = {"MARA": {"provider": "openrouter", "model": "openai/gpt-4o-mini-tts"}}
    est = sound_estimate.estimate(before, {}, cast=cast, done_fingerprints=done)
    assert est["total"] > 0 and not any(line["unit"] == "line" for line in est["lines"])
    # Another voice id or seed is another clip too.
    for member in ({"voice_id": "v2"}, {"seed": 7}):
        assert sound_estimate.estimate(before, {}, cast={"MARA": member}, done_fingerprints=done)["total"] > 0


def test_music_from_settings_is_free_once_generated():
    from app.services import sound_estimate

    doc = _parse("Hello.")
    settings = {"voice_provider": "mock", "music_prompt": "Soft  piano"}
    done = {sound_estimate.music_fingerprint("soft piano")}
    by = {line["unit"] for line in sound_estimate.estimate(doc, settings, done_fingerprints=done)["lines"]}
    assert "track" not in by


# ---------- text without tags is sent exactly as given ----------


@respx.mock
def test_elevenlabs_sends_untagged_text_unchanged(client, monkeypatch):
    _real(monkeypatch)
    from app.providers.elevenlabs_tts import ElevenLabsTTSProvider

    route = respx.post(url__regex=r".*/text-to-speech/v1.*").mock(return_value=httpx.Response(200, content=b"x"))
    for text in ("Bonjour ! Ça va ?", "Prix : 5 euros.", "Plain  [Verse 1]  text ."):
        ElevenLabsTTSProvider().synthesize(text=text, voice_id="v1")
        assert json.loads(route.calls[-1].request.content)["text"] == text


def test_openrouter_untagged_text_unchanged():
    from app.providers.openrouter_tts import with_tag_instructions

    assert with_tag_instructions("Bonjour ! Ça va ?", {}) == ("Bonjour ! Ça va ?", {})
    assert with_tag_instructions("Prix : 5 euros.", {"instructions": "x"}) == ("Prix : 5 euros.", {"instructions": "x"})


# ---------- a line that is only a tag ----------


def test_tag_only_lines_are_kept():
    from app.services import sound_estimate, story_script

    doc = _parse("MARA: [sighs]\n[laughs]\nMARA [yawns]:\nHello (pause 1)")
    assert [(i.speaker, i.tags, i.text) for i in doc.speech()] == [
        ("MARA", ["sighs"], ""), ("NARRATOR", ["laughs"], ""), ("MARA", ["yawns"], ""), ("NARRATOR", [], "Hello")]
    assert doc.warnings == []
    keys = [i.key for i in doc.speech()]
    assert len(set(keys)) == 4 and not any("-" in k for k in keys)
    # The sigh and the yawn count in the length estimate.
    assert doc.est_seconds >= story_script.TAG_SECONDS["sighs"] + story_script.TAG_SECONDS["yawns"]
    # A tag-reading model is sent the tag; the round trip keeps the lines.
    assert doc.speech()[0].voice_text == "[sighs]"
    again = _parse(story_script.serialize(doc))
    assert [(i.speaker, i.tags, i.text, i.key) for i in again.speech()] == \
        [(i.speaker, i.tags, i.text, i.key) for i in doc.speech()]
    est = sound_estimate.estimate(_parse("[sighs]"), {})
    assert any(line["unit"] == "char" and line["qty"] == len("[sighs]") for line in est["lines"])


def test_tag_only_text_is_not_read_aloud_by_a_voice_without_tags():
    from app.providers.openrouter_tts import with_tag_instructions
    from app.services.story_script import NONVERBAL_TEXT

    text, extra = with_tag_instructions("[sighs]", {})
    assert text == NONVERBAL_TEXT and "a sigh" in extra["instructions"]


@respx.mock
def test_elevenlabs_older_model_never_reads_a_tag_only_line(client, monkeypatch):
    _real(monkeypatch)
    from app.providers.elevenlabs_tts import ElevenLabsTTSProvider
    from app.services.story_script import NONVERBAL_TEXT

    route = respx.post(url__regex=r".*/text-to-speech/v1.*").mock(return_value=httpx.Response(200, content=b"x"))
    ElevenLabsTTSProvider().synthesize(text="[sighs]", voice_id="v1", settings={"story_markup": True})
    assert json.loads(route.calls[0].request.content)["text"] == NONVERBAL_TEXT


# ---------- chapters ----------


def test_notes_and_beds_before_the_first_heading_join_that_chapter():
    from app.services import story_script

    doc = _parse("// notes\n@bed ambience \"rain\"\n# Chapter 1\nHello there.\n# Chapter 2\nBye.")
    assert [(c.index, c.title) for c in doc.chapters] == [(0, "Chapter 1"), (1, "Chapter 2")]
    assert [i.chapter for i in doc.items] == [0, 0, 0, 1]
    assert doc.beds[0].chapter == 0
    again = _parse(story_script.serialize(doc))
    assert [c.title for c in again.chapters] == ["Chapter 1", "Chapter 2"]
    # Something heard before the heading still makes an untitled chapter.
    heard = _parse("(pause 1)\n# Chapter 1\nHello.")
    assert [c.title for c in heard.chapters] == ["", "Chapter 1"]


# ---------- speakers ----------


def test_note_words_and_times_are_narration():
    for line in ("11 PM: The house was quiet.", "PS: she never wrote back.", "P.S.: later.", "NB: soon."):
        doc = _parse(line)
        assert [i.speaker for i in doc.speech()] == ["NARRATOR"], line
        assert doc.speech()[0].text == line and set(doc.cast) == {"NARRATOR"}


def test_speakers_in_any_script_and_cast_names_in_any_case():
    assert _parse("ŁUKASZ: hi").speech()[0].speaker == "ŁUKASZ"
    assert _parse("ÉLODIE [whispers]: oui").speech()[0].speaker == "ÉLODIE"
    doc = _parse("@cast MARA: adult woman\nMara: hi\nmara [left]: there")
    assert [(i.speaker, i.text, i.pan) for i in doc.speech()] == [("MARA", "hi", None), ("MARA", "there", "left")]
    assert doc.warnings == []


def test_a_name_not_in_the_cast_is_read_by_the_narrator_with_a_warning():
    doc = _parse("@cast MARA: adult woman\nTom: Hello.")
    assert doc.speech()[0].speaker == "NARRATOR" and doc.speech()[0].text == "Tom: Hello."
    assert [w.message for w in doc.warnings] == ['"Tom:" is read by the narrator: TOM isn\'t in the cast.']
    # Ordinary narration with a colon gets no warning.
    assert _parse("@cast MARA: adult\nThe rule was simple: run.").warnings == []


# ---------- the round trip ----------


@pytest.mark.parametrize("text", [
    "Hello (pause 1) OK: fine",
    "Hello (pause 1) # not a chapter",
    "Hello (pause 1) // not a comment",
    "Hello (pause 1) @bed stop",
    "@cast MARA: adult\nHello (pause 1) Mara: hi",
])
def test_round_trip_keeps_narration_after_an_inline_pause(text):
    from app.services import story_script

    doc = _parse(text)
    out = story_script.serialize(doc)
    again = _parse(out)
    shape = [(i.kind, i.key, i.speaker, i.text) for i in doc.items]
    assert [(i.kind, i.key, i.speaker, i.text) for i in again.items] == shape
    assert story_script.serialize(again) == out
    # Narration that reads as narration keeps its plain form.
    assert story_script.serialize(_parse("The rule was simple: run.")) == "The rule was simple: run.\n"


# ---------- chat audio keeps its guard ----------


@respx.mock
def test_chat_audio_keeps_the_verbatim_guard_with_tags(monkeypatch):
    _real(monkeypatch)
    from app.providers.openrouter_tts import CHAT_AUDIO_SYSTEM, OpenRouterTTSProvider

    sse = "data: " + json.dumps({"choices": [{"delta": {"audio": {"data": "AAAA"}}}]}) + "\n\ndata: [DONE]\n\n"
    route = respx.post(f"{OR}/chat/completions").mock(
        return_value=httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"}))
    p = OpenRouterTTSProvider(model="openai/gpt-audio-mini")
    p.synthesize(text="[whispers] She listened at the door.", voice_id="", settings={"story_markup": True})
    system = json.loads(route.calls[0].request.content)["messages"][0]["content"]
    assert system.startswith(CHAT_AUDIO_SYSTEM) and "Do not add anything." in system and "whisper" in system
    p.synthesize(text="She listened.", voice_id="", settings={"story_markup": True})
    assert json.loads(route.calls[1].request.content)["messages"][0]["content"] == CHAT_AUDIO_SYSTEM


# ---------- adult tags in the minors check ----------


@pytest.mark.parametrize("text", [
    "[moans][lustful] Come here, little girl.",
    "[panting][breathless] The schoolgirl climbs into my bed.",
    "[sultry] Hello there, 12-year-old.",
])
def test_adult_tags_with_a_minor_are_refused_at_the_voice(client, text):
    from app.providers import registry
    from app.services.safety import UnsafeScriptError

    with pytest.raises(UnsafeScriptError):
        registry.get_tts_for("elevenlabs").synthesize(text=text, voice_id="")


def test_adult_tags_are_read_by_the_long_check(client):
    from app.services import minor_check, story_script
    from app.services.safety import UnsafePromptError

    doc = _parse("@cast GIRL: a 12-year-old girl\n[moans][lustful][panting] She crawls closer to him.")
    voiced = story_script.voiced_text(doc)
    assert "[moans]" in voiced
    with pytest.raises(UnsafePromptError):
        minor_check.check_long(voiced, context=story_script.cast_header(doc))


def test_adult_tags_between_adults_go_to_the_model(client):
    from app.providers import mock_chat, registry

    from .story_helpers import checked, say

    mock_chat.MINOR_REPLY = {"minor_sexual": False, "reason": "adults"}
    doc, ctx = checked("[lustful] Come here, darling.")
    assert mock_chat.MINOR_CALLS  # asked about, then allowed
    say(registry.get_tts_for("mock"), doc.speech()[0], ctx)
    mock_chat.MINOR_CALLS.clear()
    checked("[whispers] Good night.")
    assert len(mock_chat.MINOR_CALLS) == 1  # a story line: always asked


def test_safety_text_leaves_other_text_alone():
    from app.services.story_script import safety_text

    assert safety_text("Bonjour ! [whispers] Ça va ?") == "Bonjour ! [whispers] Ça va ?"
    assert "seductive" in safety_text("[moans] hi")


# ---------- a cast list too long to ride along ----------


def test_a_long_cast_list_is_still_checked():
    from app.services import minor_check
    from app.services.safety import UnsafePromptError

    filler = " ".join(f"P{i}: an adult with a long description of their voice." for i in range(40))
    context = "Cast: LILY: a naked 12-year-old girl. " + filler
    assert len(context) > minor_check.LONG_CONTEXT_LIMIT
    with pytest.raises(UnsafePromptError, match="Character descriptions are too long"):
        minor_check.check_long("She walked to the river and sat down. " * 300, context=context)
    with pytest.raises(UnsafePromptError, match="Character descriptions are too long"):
        minor_check.check_long("She walked to the river. " * 300,
                               context="Cast: ANNA: a naked adult woman. " + filler)


# ---------- clean deployments: adult tags never voiced ----------


@respx.mock
def test_clean_deployment_never_turns_an_adult_tag_into_a_direction(client, monkeypatch):
    _real(monkeypatch)
    _clean(monkeypatch)
    try:
        from app.providers import registry
        # The explicit-content check has its own tests; here no planner runs.
        monkeypatch.setattr(registry, "_explicit", lambda text: None)
        from app.services import minor_check as _mcheck
        from app.services import story_pass

        from .story_helpers import pass_for

        # Lines check_story passed (the pass is made here).
        assert _mcheck
        ctx = "Cast:\nNARRATOR"
        lines = ("[moans][seductively] hello there", "[sultry, whispers] Good night.")
        token = pass_for(ctx, *(story_pass.line_item("NARRATOR", t) for t in lines))
        from app.providers.openrouter_tts import OpenRouterTTSProvider, with_tag_instructions

        text, extra = with_tag_instructions("[moans][whispers] hello there", {})
        assert text == "hello there" and "moans" not in extra["instructions"] and "whisper" in extra["instructions"]

        route = respx.post(f"{OR}/audio/speech").mock(
            return_value=httpx.Response(200, content=b"MP3", headers={"content-type": "audio/mpeg"}))
        guarded = registry.GuardedTTSProvider(OpenRouterTTSProvider(model="openai/gpt-4o-mini-tts", chat_audio=False),
                                              story=True)
        guarded.synthesize(text=lines[0], voice_id="", context=ctx, speaker="NARRATOR", story_pass=token)
        body = json.loads(route.calls[0].request.content)
        assert body["input"] == "hello there" and "instructions" not in body

        from app.providers.elevenlabs_tts import ElevenLabsTTSProvider

        el = respx.post(url__regex=r".*/text-to-speech/v1.*").mock(return_value=httpx.Response(200, content=b"x"))
        registry.GuardedTTSProvider(ElevenLabsTTSProvider(model_id="eleven_v4"), story=True).synthesize(
            text=lines[1], voice_id="v1", context=ctx, speaker="NARRATOR",
            story_pass=pass_for(ctx, story_pass.line_item("NARRATOR", lines[1]), voices={"NARRATOR": "v1"}))
        assert json.loads(el.calls[0].request.content)["text"] == "[whispers] Good night."
    finally:
        monkeypatch.delenv("ADULT_FEATURES")
        _reset()


def test_drop_tags():
    from app.services.story_script import ADULT_TAGS, drop_tags

    assert drop_tags("Bonjour ! Ça va ?", ADULT_TAGS) == "Bonjour ! Ça va ?"
    assert drop_tags("[moans] Hi [Verse 1].", ADULT_TAGS) == "Hi [Verse 1]."
    assert drop_tags("[whispers] Hi.", ADULT_TAGS) == "[whispers] Hi."


# ---------- music parts: checked once ----------


def test_music_parts_are_checked_once(client, monkeypatch):
    from app.providers import mock_chat, registry

    _clean(monkeypatch)
    try:
        mock_chat.EXPLICIT_CALLS.clear()
        music = registry.get_music()
        for i in (1, 2, 3):
            music.generate(prompt=registry.music_part_prompt("soft piano by a fire", i, 3), duration_seconds=2)
        assert len(mock_chat.EXPLICIT_CALLS) == 1
        assert mock_chat.EXPLICIT_CALLS[0].endswith("soft piano by a fire")
        # Anything after the note is the caller's and is checked.
        assert registry._base_music_prompt(registry.music_part_prompt("x", 1, 2) + " more") != "x"
    finally:
        monkeypatch.delenv("ADULT_FEATURES")
        _reset()


# ---------- review follow-ups ----------


def test_adult_tags_with_a_minor_are_refused_in_a_sound_prompt(client):
    from app.providers import registry
    from app.services.safety import UnsafePromptError, UnsafeScriptError

    with pytest.raises((UnsafePromptError, UnsafeScriptError)):
        registry.get_sound().generate(prompt="[moans][lustful] a little girl breathing", duration_seconds=2)


@pytest.mark.parametrize("line", ["AT 3:00 the phone rang.", "At 3:00 the phone rang.", "AT 11:30: silence."])
def test_clock_times_are_narration(line):
    doc = _parse(line)
    assert [i.speaker for i in doc.speech()] == ["NARRATOR"] and doc.speech()[0].text == line
    assert _parse("AGENT 47: hi").speech()[0].speaker == "AGENT 47"


def test_adult_tag_only_line_with_a_minor_cast_is_refused_by_the_long_check(client):
    from app.services import minor_check, story_script
    from app.services.safety import UnsafePromptError

    doc = _parse("@cast GIRL: a 12-year-old girl\nGIRL: [moans]")
    assert doc.speech()[0].tags == ["moans"]
    with pytest.raises(UnsafePromptError):
        minor_check.check_long(story_script.voiced_text(doc), context=story_script.cast_header(doc))

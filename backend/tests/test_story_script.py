"""The audio story script markup (services/story_script.py)."""
from __future__ import annotations

import pytest

from app.services import story_script as ss

EXAMPLE = """// The Cabin, a sleep story
@cast NARRATOR [center]: warm adult voice, close to the mic
@cast MARA [left]: adult woman in her thirties, low whisper
@cast TOM [right]: adult man, calm

# Chapter 1: The cabin
@bed ambience "rain on a cabin roof"
The rain had not stopped for three days.
[whispers][slowly] She listened at the door.
MARA [whispers][left]: Did you hear that?
TOM [softly]: Only the wind. [laughs softly] Go to sleep.
[sfx: old door creaks | 3s]
[sfx: distant thunder | 4s | under]
[sfx: wind gust | 2s | +1.5s]
(pause 2)
@bed music "slow felt piano"
@bed ambience stop

# Chapter 2: Morning
[sighs] The morning came quietly.
[sfx: #12]
@bed stop
"""

ASMR = """@cast NARRATOR: soft adult voice

[whispers][close to the mic] Hello again. (pause 1) Let's breathe together.
[slow breath] In... and out.
[sfx: pages turning | 2s]
[whispers] Good. [short pause] Very good.
"""

DIALOGUE = """# Scene
ANNA [left]: Are you coming?
BEN [right][excited]: Right behind you!
ANNA: Then hurry.
"""


def _parse(text: str, **kw) -> ss.StoryDoc:
    kw.setdefault("clean", False)
    kw.setdefault("max_minutes", 90)
    return ss.parse(text, **kw)


def _shape(doc: ss.StoryDoc) -> list[tuple]:
    """Everything that matters about the items (not their line numbers)."""
    return [(i.kind, i.key, i.chapter, i.speaker, tuple(i.tags), i.pan, i.text, i.prompt, i.seconds,
             i.placement, i.offset, i.asset_id, i.layer, i.action) for i in doc.items]


# ---------- structure ----------


def test_example_parses_into_chapters_cast_items_and_beds():
    doc = _parse(EXAMPLE)
    # The opening comment joins the first chapter (no untitled chapter with
    # nothing to hear).
    assert [c.title for c in doc.chapters] == ["Chapter 1: The cabin", "Chapter 2: Morning"]
    assert doc.items[0].kind == "comment" and doc.items[0].chapter == 0
    assert {n: (c.pan, c.declared) for n, c in doc.cast.items()} == {
        "NARRATOR": ("center", True), "MARA": ("left", True), "TOM": ("right", True)}
    assert doc.cast["MARA"].description == "adult woman in her thirties, low whisper"

    speech = doc.speech()
    assert [(i.speaker, i.tags, i.pan, i.text) for i in speech] == [
        ("NARRATOR", [], None, "The rain had not stopped for three days."),
        ("NARRATOR", ["whispers", "slowly"], None, "She listened at the door."),
        ("MARA", ["whispers"], "left", "Did you hear that?"),
        ("TOM", ["softly"], None, "Only the wind. [laughs softly] Go to sleep."),
        ("NARRATOR", ["sighs"], None, "The morning came quietly."),
    ]
    sfx = doc.sfx()
    assert [(i.prompt, i.seconds, i.placement, i.offset, i.asset_id) for i in sfx] == [
        ("old door creaks", 3.0, "after", None, None),
        ("distant thunder", 4.0, "under", None, None),
        ("wind gust", 2.0, "offset", 1.5, None),
        ("", None, "after", None, 12),
    ]
    pauses = [i.seconds for i in doc.items if i.kind == "pause"]
    assert pauses == [2.0]
    # Beds: the ambience runs until "@bed ambience stop", the music until
    # "@bed stop" in chapter 2.
    assert [(b.layer, b.prompt, b.stop_item is not None) for b in doc.beds] == [
        ("ambience", "rain on a cabin roof", True), ("music", "slow felt piano", True)]
    amb, mus = doc.beds
    assert doc.items[amb.stop_item].kind == "bed" and doc.items[amb.stop_item].layer == "ambience"
    assert doc.items[mus.stop_item].action == "stop" and doc.items[mus.stop_item].layer == ""
    assert doc.chapters[doc.items[mus.stop_item].chapter].title == "Chapter 2: Morning"
    assert doc.warnings == []


def test_items_belong_to_their_chapter_and_chapters_know_their_span():
    doc = _parse(DIALOGUE)
    assert len(doc.chapters) == 1 and doc.chapters[0].title == "Scene"
    ch = doc.chapters[0]
    assert ch.first_item == 0 and ch.item_count == 3
    assert all(i.chapter == 0 for i in doc.items)
    # Speakers not declared with @cast join the cast without a warning when
    # nothing is declared.
    assert set(doc.cast) == {"ANNA", "BEN"} and not any(c.declared for c in doc.cast.values())
    assert doc.warnings == []
    assert [(i.speaker, i.pan, i.tags) for i in doc.speech()] == [
        ("ANNA", "left", []), ("BEN", "right", ["excited"]), ("ANNA", None, [])]


def test_text_before_the_first_heading_is_an_untitled_chapter():
    doc = _parse("Once upon a time.\n# Part two\nThe end.")
    assert [c.title for c in doc.chapters] == ["", "Part two"]
    assert [i.chapter for i in doc.speech()] == [0, 1]
    one = _parse("Just one line.")
    assert len(one.chapters) == 1 and one.chapters[0].title == ""


def test_narration_with_colons_and_lowercase_names_stays_narration():
    doc = _parse("The rule was simple: run.\n10:30 came and went.\nMara: hello?")
    assert [i.speaker for i in doc.speech()] == ["NARRATOR"] * 3
    assert doc.speech()[0].text == "The rule was simple: run."


def test_comments_are_kept_but_never_voiced():
    doc = _parse("// note to self\nHello.")
    assert [i.kind for i in doc.items] == ["comment", "speech"]
    assert ss.voiced_text(doc) == "NARRATOR: Hello."


# ---------- inline pauses, cues and tags ----------


def test_inline_pause_splits_the_line_and_keeps_its_voice():
    doc = _parse("MARA [whispers][left]: One. (pause 1.5) [sighs] Two. (pause) Three.")
    assert [(i.kind, i.text, i.tags, i.pan, i.seconds) for i in doc.items] == [
        ("speech", "One.", ["whispers"], "left", None),
        ("pause", "", [], None, 1.5),
        ("speech", "Two.", ["whispers", "sighs"], "left", None),
        ("pause", "", [], None, 1.0),
        ("speech", "Three.", ["whispers"], "left", None),
    ]


def test_inline_sound_cue_goes_after_the_line_with_a_warning():
    doc = _parse("TOM: Who's there? [sfx: knock knock | 1s]")
    assert [(i.kind, i.text or i.prompt) for i in doc.items] == [("speech", "Who's there?"),
                                                                ("sfx", "knock knock")]
    assert any("own line" in w.message for w in doc.warnings)


def test_unknown_tags_are_removed_with_a_warning_and_known_ones_normalised():
    doc = _parse("ANNA [Shouting][WHISPERS]: Hi [Laughs_Softly] there [dances].")
    (item,) = doc.speech()
    assert item.tags == ["whispers"]
    assert item.text == "Hi [laughs softly] there."
    msgs = [w.message for w in doc.warnings]
    assert any("[shouting]" in m for m in msgs) and any("[dances]" in m for m in msgs)
    assert all(w.line == 1 for w in doc.warnings)


def test_tags_in_one_bracket_and_inline_placement():
    doc = _parse("[whispers, slowly] Close your eyes [right].")
    (item,) = doc.speech()
    assert item.tags == ["whispers", "slowly"] and item.pan == "right"
    assert item.text == "Close your eyes."


def test_clean_deployment_keeps_only_general_audience_tags():
    text = "[seductively][whispers] Come closer. [moans]"
    adult = _parse(text, clean=False)
    assert adult.speech()[0].tags == ["seductively", "whispers"]
    clean = _parse(text, clean=True)
    (item,) = clean.speech()
    assert item.tags == ["whispers"] and item.text == "Come closer."
    assert sum("general audience" in w.message for w in clean.warnings) == 2
    assert ss.allowed_tags(True) == ss.GENERAL_TAGS
    assert ss.ADULT_TAGS.isdisjoint(ss.GENERAL_TAGS)
    assert {"whispers", "whispering", "softly", "breathy", "sighs", "laughs softly", "short pause",
            "long pause", "slowly"} <= ss.GENERAL_TAGS


def test_clean_default_follows_the_deployment(monkeypatch):
    from app import config

    monkeypatch.setenv("ADULT_FEATURES", "false")
    config.get_settings.cache_clear()
    assert ss.parse("[sultry] Hi.").speech()[0].tags == []
    monkeypatch.delenv("ADULT_FEATURES")
    config.get_settings.cache_clear()
    assert ss.parse("[sultry] Hi.").speech()[0].tags == ["sultry"]


# ---------- warnings ----------


@pytest.mark.parametrize("text, fragment", [
    ("[sfx: thunder | 45s]", "0.5–30 seconds"),
    ("[sfx: thunder | loud]", "Unknown sound cue option"),
    ("[sfx:  | 3s]", "needs a description"),
    ("(pause 60)", "0.1–30 seconds"),
    ("@bed stop", "no bed playing"),
    ("@bed music stop", "no music playing"),
    ("@bed rain \"x\"", "Unknown bed"),
    ("@bed ambience", "needs a description"),
    ("@bed ambience rain", "in quotes"),
    ("@volume 3", "Unknown command"),
    ("@cast nobody", "@cast NAME"),
    ("MARA:   ", "empty line"),
])
def test_warnings(text, fragment):
    doc = _parse(text)
    assert any(fragment in w.message for w in doc.warnings), [w.message for w in doc.warnings]


def test_values_out_of_range_are_clamped():
    doc = _parse("[sfx: thunder | 45s]\n(pause 60)\n(pause 0.01)")
    assert [i.seconds for i in doc.items] == [30.0, 30.0, 0.1]


def test_undeclared_speaker_warns_when_a_cast_is_declared():
    doc = _parse("@cast MARA: adult woman\nMARA: Hi.\nTOM: Hello.")
    assert [w.message for w in doc.warnings] == ["TOM speaks but isn't in the cast; added."]
    assert doc.cast["TOM"].declared is False


def test_cast_line_warnings():
    doc = _parse("Hello.\n@cast MARA [whispers][left]: adult\n@cast MARA: twice")
    msgs = [w.message for w in doc.warnings]
    assert any("at the top" in m for m in msgs)
    assert any("placement only" in m for m in msgs)
    assert any("declared twice" in m for m in msgs)
    assert doc.cast["MARA"].description == "twice"


def test_empty_script_and_cap_warnings():
    assert any("Nothing to read" in w.message for w in _parse("").warnings)
    long = "\n".join(["word " * 150] * 100)  # ~15,000 words: over 90 minutes
    doc = _parse(long, max_minutes=90)
    assert doc.est_seconds > 90 * 60
    assert any("90-minute limit" in w.message for w in doc.warnings)


# ---------- estimates ----------


def test_estimate_uses_words_per_minute_by_style():
    text = " ".join(["word"] * 100) + "."
    asmr = _parse(text, style="asmr")
    drama = _parse(text, style="drama")
    default = _parse(text)
    assert asmr.est_seconds == pytest.approx(100 / 100 * 60 + 0.3, abs=0.1)
    assert drama.est_seconds == pytest.approx(100 / 150 * 60 + 0.3, abs=0.1)
    assert default.est_seconds == pytest.approx(100 / ss.DEFAULT_WPM * 60 + 0.3, abs=0.1)


def test_estimate_counts_pauses_after_cues_and_chapter_gaps_but_not_under_cues():
    text = "# A\n(pause 2)\n[sfx: door | 3s]\n[sfx: rain | 10s | under]\n[sfx: owl | 4s | +1s]\n[sfx: bell]\n# B\n(pause 1)"
    doc = _parse(text)
    # 2 + 3 + default 3 for the bell; under/offset cues add nothing.
    assert doc.chapters[0].est_seconds == pytest.approx(2 + 3 + ss.DEFAULT_SFX_SECONDS)
    assert doc.chapters[1].est_seconds == pytest.approx(1.0)
    assert doc.est_seconds == pytest.approx(2 + 3 + 3 + 1 + ss.CHAPTER_GAP)


def test_estimate_adds_pause_tags_and_slows_sleepy_lines():
    plain = _parse("one two three four five six seven eight nine ten.", style="sleep")
    paused = _parse("one two three four five [long pause] six seven eight nine ten.", style="sleep")
    slow = _parse("[slowly] one two three four five six seven eight nine ten.", style="sleep")
    assert paused.est_seconds == pytest.approx(plain.est_seconds + 1.5, abs=0.11)
    assert slow.est_seconds > plain.est_seconds


def test_counts():
    doc = _parse("ANNA [whispers]: Hello there [sighs].")
    assert doc.word_count == 2
    assert doc.speech()[0].voice_text == "[whispers] Hello there [sighs]."
    assert doc.voiced_chars == len("[whispers] Hello there [sighs].")


# ---------- keys ----------


def test_keys_depend_on_content_not_position():
    a = _parse("One.\nTwo.\n[sfx: door | 3s]\nThree.")
    b = _parse("Zero.\nOne.\nTwo, edited.\n[sfx: door | 3s]\nThree.")
    ka = {i.text or i.prompt: i.key for i in a.items}
    kb = {i.text or i.prompt: i.key for i in b.items}
    assert ka["One."] == kb["One."] and ka["Three."] == kb["Three."] and ka["door"] == kb["door"]
    assert ka["Two."] != kb["Two, edited."]
    # Pan and tags change how a line sounds, not which line it is.
    c = _parse("[whispers][left] One.")
    assert c.items[0].key == ka["One."]
    assert all(len(i.key) <= 64 for i in b.items)


def test_repeated_content_gets_numbered_keys_and_kinds_differ():
    doc = _parse("Shh.\n(pause 1)\nShh.\n(pause 1)\nShh.")
    keys = [i.key for i in doc.items]
    assert len(set(keys)) == 5
    assert keys[2] == keys[0] + "-2" and keys[4] == keys[0] + "-3"
    assert keys[0][0] == "v" and keys[1][0] == "p"
    sfx = _parse("[sfx: door]\n@bed ambience \"door\"")
    assert sfx.items[0].key[0] == "x" and sfx.items[1].key[0] == "b"
    assert len({i.key for i in sfx.items}) == 2


def test_speaker_is_part_of_the_key():
    doc = _parse("ANNA: Yes.\nBEN: Yes.")
    assert doc.items[0].key != doc.items[1].key


# ---------- serialize ----------


@pytest.mark.parametrize("text", [EXAMPLE, ASMR, DIALOGUE, "Just one line.",
                                  "Before.\n# After\nText.", "#\nUntitled heading."])
def test_round_trip(text):
    doc = _parse(text)
    out = ss.serialize(doc)
    again = _parse(out)
    assert _shape(again) == _shape(doc)
    assert {n: (c.pan, c.description, c.declared) for n, c in again.cast.items()} == \
        {n: (c.pan, c.description, c.declared) for n, c in doc.cast.items()}
    assert [c.title for c in again.chapters] == [c.title for c in doc.chapters]
    assert [(b.layer, b.prompt, b.chapter) for b in again.beds] == [(b.layer, b.prompt, b.chapter) for b in doc.beds]
    assert again.est_seconds == doc.est_seconds
    assert ss.serialize(again) == out  # canonical: a fixed point
    assert again.warnings == []


def test_serialize_canonical_form():
    doc = _parse("tom [softly , right]:  Hi   there\n[sfx:door|3S|UNDER]\n(pause 2.0)\n@bed ambience 'x'")
    out = ss.serialize(doc)
    assert "[sfx: door | 3s | under]" in out
    assert "(pause 2)" in out
    # "tom" (lower case) isn't a speaker name: it's narration.
    assert doc.speech()[0].speaker == "NARRATOR" and doc.speech()[0].pan == "right"
    assert out.splitlines()[0] == "[right] tom [softly]: Hi there"


def test_serialize_speaker_line_and_unsafe_prompt_characters():
    doc = _parse("MARA [whispers][left]: Hi.")
    assert ss.serialize(doc).strip() == "MARA [whispers][left]: Hi."
    item = ss.Item(kind="sfx", prompt="a | b ] c", seconds=2.0)
    line = ss.serialize_item(item)
    assert _parse(line).sfx()[0].prompt == "a / b ) c"


def test_to_dict_is_json_ready():
    import json

    d = _parse(EXAMPLE).to_dict()
    json.dumps(d)
    assert {c["name"] for c in d["cast"]} == {"NARRATOR", "MARA", "TOM"}
    assert d["items"][0]["kind"] == "comment"


# ---------- helpers ----------


def test_cast_header_voiced_text_and_prompts():
    doc = _parse(EXAMPLE)
    header = ss.cast_header(doc)
    assert header.startswith("Cast:\n") and "MARA: adult woman in her thirties" in header
    voiced = ss.voiced_text(doc)
    # The safety checks read the tags too (how each line is performed).
    assert "TOM: [softly] Only the wind. [laughs softly] Go to sleep." in voiced
    assert ss.prompts_text(doc) == ["rain on a cabin roof", "old door creaks", "distant thunder", "wind gust",
                                    "slow felt piano"]


def test_split_tags_and_instructions():
    tags, text = ss.split_tags("[whispers] Hello [short pause] there [Verse 1] [left]")
    assert tags == ["whispers", "short pause"]
    assert text == "Hello … there [Verse 1]"
    assert ss.strip_tags("[whispers] Hi [Verse 1]", keep_unknown=True) == "Hi [Verse 1]"
    assert ss.strip_tags("[whispers] Hi [Verse 1]") == "Hi"
    said = ss.tags_to_instructions(["whispers", "whispering", "slowly", "laughs softly"])
    assert said == "Read the text aloud exactly as written. Delivery: whisper; speak slowly; a soft laugh."
    assert ss.tags_to_instructions([]) == ""
    assert "teasing" in ss.tags_to_instructions(["teasing"])

"""A story facts reply with "unknown" for a part, or an empty narrator
(#57 review, finding 9).

read_part refused "in_this_part": "unknown", and a narrator of {} failed
read_status: either one made the whole facts reply unreadable, so the
story, long voiceover or lip-sync was paused as "the check couldn't run".
Both now read as null, the answer the prompt asks for (and now names) when
the model can't tell: never less caution than a null reply, so a child
narrator or a minor in a sexual scene is still refused.
"""
from __future__ import annotations

import pytest

from app.services.safety import UnsafePromptError

from .test_sound_story_round11 import _mode, _voice
from .test_sound_story_round12 import LILY, PAD, UNNAMED, narr  # noqa: F401

CALM = "I'm 34 and I finally moved to the coast. " + PAD + "That night Mark and I watched the boats and talked."


def _mark(part):  # noqa: ANN001, ANN202
    return {"name": "Mark", "status": "adult", "age": None, "minor_in_past": False, "in_this_part": part,
            "minor_when": None}


def test_unknown_part_and_empty_narrator_parse_as_null():
    import json

    from app.providers.openrouter_chat import parse_story_characters

    out = parse_story_characters(json.dumps({"characters": [_mark("unknown"), _mark(" Unknown ")],
                                             "narrator": {}}))
    assert out == {"characters": [{"name": "Mark", "status": "adult", "age": None, "minor_in_past": False}] * 2}
    nulls = {"name": None, "status": None, "age": None, "minor_in_past": None, "in_this_part": None,
             "minor_when": None}
    assert "narrator" not in parse_story_characters(json.dumps({"characters": [], "narrator": nulls}))


def test_a_narrator_with_a_status_is_still_read():
    import json

    from app.providers.openrouter_chat import parse_story_characters

    out = parse_story_characters(json.dumps({"characters": [], "narrator": {
        "name": None, "status": "minor", "age": 9, "minor_in_past": False, "in_this_part": "unknown",
        "minor_when": None}}))
    assert out["narrator"]["status"] == "minor" and out["narrator"]["age"] == 9


@pytest.mark.parametrize("bad", [{"status": "teen"}, {"in_this_part": "maybe"}, {"minor_in_past": "yes"}])
def test_other_unreadable_fields_still_fail(bad):
    import json

    from app.providers.openrouter_chat import parse_story_characters

    with pytest.raises(ValueError):
        parse_story_characters(json.dumps({"characters": [{**_mark(None), **bad}]}))


def test_the_prompt_asks_for_null_when_it_cannot_tell():
    from app.providers.openrouter_chat import STORY_FACTS_SYSTEM

    assert 'Use null (never "unknown" or {})' in STORY_FACTS_SYSTEM


@pytest.mark.parametrize("reply", [
    {"characters": [_mark("unknown")], "narrator": {}},
    {"characters": [_mark("unknown")], "narrator": None},
    {"characters": [_mark(None)], "narrator": {}},
])
@pytest.mark.parametrize("mode", ["default", "adult"])
def test_a_long_voiceover_with_such_a_reply_is_made(client, narr, monkeypatch, mode, reply):  # noqa: F811
    """Paused as "couldn't run" before."""
    _mode(monkeypatch, mode)
    narr.STORY_FACTS_REPLY = reply
    _voice(CALM)
    assert narr.STORY_FACTS_CALLS


@pytest.mark.parametrize("text", [LILY, UNNAMED])
@pytest.mark.parametrize("reply", [
    {"characters": [_mark("unknown")], "narrator": {}},
    {"characters": [_mark("unknown")], "narrator": None},
])
@pytest.mark.parametrize("mode", ["default", "adult"])
def test_a_child_narrator_is_still_refused_with_such_a_reply(client, narr, monkeypatch, mode, reply, text):  # noqa: F811
    """As refused as with a null narrator: refused for a minor, not paused."""
    from app.services import minor_check

    _mode(monkeypatch, mode)
    narr.STORY_FACTS_REPLY = reply
    with pytest.raises(UnsafePromptError) as e:
        _voice(text)
    assert str(e.value) == minor_check.REFUSED

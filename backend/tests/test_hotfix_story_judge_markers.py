"""The story judge reads the story as written (hotfix after the #68 live
check).

check_story sent the judge each unit marked by story_script.safety_text:
" (sensual)" after an intimate general tag ([murmurs], [tenderly],
[sighs], [slow breath]...) or any free-form delivery instruction with a
child in the cast, " (seductive)" after an adult one. MINOR_CHECK_SYSTEM
tells the judge every parenthesised direction is the user's, so an
innocent child-cast sleep story was refused as minor_sexual (and cached).
The judge now gets the units unmarked (_Unit.judged), as
registry._audio_guard sends a sound prompt since #58; the local word filter
strips the markers anyway (minor_check.story_word_filter), so it refuses
exactly what it did. A "(sensual)" the user typed is theirs, and the judge
still reads it.
"""
from __future__ import annotations

import pytest

from app.services.safety import UnsafePromptError

OK = {"minor_sexual": False, "reason": "test: ok"}
APP_MARKERS = ("(sensual)", "(seductive)")

SLEEP_STORY = """@cast NARRATOR: calm adult narrator
@cast ELLIE: a 6-year-old girl, sleepy
@cast GRANDPA: her grandfather, 70
NARRATOR: The rain tapped on the window of the little attic room.
ELLIE: [murmurs] Grandpa... will you stay until I fall asleep?
GRANDPA: [tenderly] There. Snug as a bug.
ELLIE: [sighs] The blanket is warm.
GRANDPA: [slow breath] Close your eyes now, little one. The owls are keeping watch.
NARRATOR: And soon the only sound was the rain.
"""


@pytest.fixture
def judge():
    from app.providers import mock_chat

    mock_chat.MINOR_CALLS.clear()
    mock_chat.MINOR_REPLY = None  # the deterministic mock judge
    yield mock_chat.MINOR_CALLS
    mock_chat.MINOR_REPLY = None


def _doc(text: str = SLEEP_STORY, *, instructions: str = ""):
    from app.services import story_script

    doc = story_script.parse(text)
    if instructions:
        for item in doc.speech():
            if item.speaker == "ELLIE":
                item.instructions = instructions
    return doc


def _check(doc, **kw):  # noqa: ANN001, ANN003
    from app.services import story_script

    return story_script.check_story(doc, **kw)


def test_the_app_would_mark_this_story(client):
    """The precondition: every intimate tag and the child's delivery
    instruction get an app marker for the word layer."""
    from app.services import story_script

    doc = _doc(instructions="sleepy, soft and slow")
    header = story_script.cast_header(doc)
    marked = story_script.voiced_text(doc, header)
    for tag in ("[murmurs]", "[tenderly]", "[sighs]", "[slow breath]"):
        assert f"{tag} (sensual)" in marked
    assert "sleepy, soft and slow (sensual)" in marked


def test_the_judge_never_reads_an_app_marker_in_a_child_cast_sleep_story(client, judge):
    _check(_doc(instructions="sleepy, soft and slow"))
    assert judge, "every story window is judged"
    seen = "\n".join(judge)
    assert not any(m in seen for m in APP_MARKERS)
    # It reads the tags and the instruction as written.
    for part in ("[murmurs] Grandpa", "[tenderly] There. Snug as a bug.", "[sighs]", "[slow breath]",
                 "(delivery instructions: sleepy, soft and slow)"):
        assert part in seen


def test_an_innocent_child_cast_sleep_story_is_no_longer_refused(client, judge):
    """The mock judge refuses a minor next to a sexual word, as the live
    judge refused the app's own "(sensual)": with the markers gone, the
    story passes."""
    ctx = _check(_doc(instructions="sleepy, soft and slow"))
    assert ctx.story_pass


def test_adult_markers_never_reach_the_judge_either(client, judge):
    from app.providers import mock_chat

    mock_chat.MINOR_REPLY = OK
    _check(_doc("@cast ANNA: an adult woman, 35\n@cast MARK: an adult man, 40\nANNA [moans]: Mark.\n"
                "MARK [breathy]: Anna.\n"))
    seen = "\n".join(judge)
    assert "[moans] Mark." in seen and not any(m in seen for m in APP_MARKERS)


@pytest.mark.parametrize("line", [
    "NARRATOR: The little girl [moans] in orgasm.",
    "NARRATOR: The 6-year-old [moans] (orgasm).",
])
def test_adult_tags_next_to_a_minor_are_still_refused_by_the_words_alone(client, judge, line):
    """The story word filter (unchanged: it strips the app's markers
    itself) refuses an explicit word next to a minor before any call."""
    from app.providers import mock_chat

    mock_chat.MINOR_REPLY = OK  # the judge would let it through: the words refuse it first
    with pytest.raises(UnsafePromptError):
        _check(_doc(SLEEP_STORY + line + "\n"))
    assert judge == []


@pytest.mark.parametrize("line", ["NARRATOR: The little girl [moans] softly.", "ELLIE [lustful]: Grandpa."])
def test_an_adult_tag_next_to_a_minor_is_judged_with_the_tag_as_written(client, judge, line):
    """Not refused by the words (as before: round 11), so the judge reads
    it, with the adult tag itself in view, and its refusal stands."""
    from app.providers import mock_chat

    mock_chat.MINOR_REPLY = {"minor_sexual": True, "reason": "test: an adult tag on a child"}
    with pytest.raises(UnsafePromptError):
        _check(_doc(SLEEP_STORY + line + "\n"))
    tag = line.split("[")[1].split("]")[0]
    assert any(f"[{tag}]" in c for c in judge) and not any(m in c for c in judge for m in APP_MARKERS)


def test_a_sensual_the_user_typed_still_reaches_the_judge(client, judge):
    from app.providers import mock_chat

    mock_chat.MINOR_REPLY = OK
    _check(_doc(SLEEP_STORY + "GRANDPA: (sensual) Sleep tight.\n"))
    assert any("GRANDPA: (sensual) Sleep tight." in c for c in judge)


def test_the_mock_judge_refuses_a_user_typed_sensual_near_a_child(client, judge):
    """The judge weighs the user's own words: with the mock judge, a
    "(sensual)" typed next to a child is refused."""
    with pytest.raises(UnsafePromptError):
        _check(_doc(SLEEP_STORY + "GRANDPA: (sensual) Sleep tight, little girl.\n"))
    assert judge


def test_the_existing_minors_refusals_still_refuse(client, judge):
    from .test_sound_scripts import MINOR_PREMISE

    with pytest.raises(UnsafePromptError):
        _check(_doc("NARRATOR: Once upon a time.\n"), premise=MINOR_PREMISE)
    with pytest.raises(UnsafePromptError):
        _check(_doc(f"NARRATOR: {MINOR_PREMISE}\n"))
    with pytest.raises(UnsafePromptError):
        _check(_doc(SLEEP_STORY + "GRANDPA: She is a sexy little girl.\n"))


def test_a_refusal_cached_for_the_marked_text_is_not_reused(client, judge):
    """The verdict cache is keyed by exactly what the judge was shown: a
    refusal stored when it was shown the marked window doesn't stand for
    the unmarked one."""
    from app.services import minor_check, story_script
    from app.services.explicit_check import line_windows, piece_size

    doc = _doc()
    header = story_script.cast_header(doc)
    size = minor_check.story_window_size(header)
    units = story_script._units(doc, (), "", "", piece=piece_size(size))
    plain = [u.plain() for u in units]
    ranges = line_windows([len(x) for x in plain], size=size)
    required = [c.name for c in doc.cast.values() if c.description.strip() and c.name != story_script.NARRATOR]
    facts = minor_check.story_facts_detail(header, windows=["\n".join(plain[a:b]) for a, b in ranges],
                                           names=required, known=list(doc.cast))
    context = story_script.story_context(doc, facts=facts.line)
    marked, part_lines = minor_check.story_judge_windows([u.marked(context) for u in units], ranges, facts, context)
    assert any("(sensual)" in w for w in marked)
    # Every marked window was refused (and cached) before the fix...
    from app.providers import mock_chat

    mock_chat.MINOR_REPLY = {"minor_sexual": True, "reason": "test: refused"}
    with pytest.raises(UnsafePromptError):
        minor_check.check_story_windows(marked, part_lines, context=context, summary=facts.summary_line())
    # ...and the story, judged unmarked now, is asked afresh.
    mock_chat.MINOR_REPLY = OK
    judge.clear()
    _check(doc)
    assert judge

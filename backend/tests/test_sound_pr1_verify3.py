"""Third verification pass on the Sound Studio foundations: word lists no
longer decide when the safety model is asked on the story path.

- kind "story": a story window is asked about when it is sexual, marked or
  mentions a minor, always with the cast in front;
- a story-level minor flag (check_story's context starts with the story
  note), so an age stated anywhere reaches every part and every provider call;
- a cast longer than LONG_CONTEXT_LIMIT is refused plainly;
- dash look-alikes in an age are folded;
- non-story voice callers keep the check they had before story voices;
- (...) and *...* action groups are marked like bracket directions;
- music settings are allow-listed."""
from __future__ import annotations

import pytest

from app.services import safety
from app.services.safety import UnsafePromptError

_GIRL_CAST = "Cast:\nLILY: a 12-year-old girl"
_ADULT_CAST = "Cast:\nANNA: an adult woman"
_EUPHEMISM = "He touched her in ways no one should, and she did not stop him."
_REFUSE = {"minor_sexual": True, "reason": "the girl is a minor"}
_OK = {"minor_sexual": False, "reason": "fine"}

DASHES = ["‐", "‑", "‒", "–", "—", "―", "−", "﹘", "﹣", "－"]


class _Inner:
    """A voice / sound / music adapter that records what it is sent."""

    name = "fake"
    model_id = "fake"

    def __init__(self):
        self.sent: list[dict] = []

    def synthesize(self, **kw):
        self.sent.append(kw)
        return b"x"

    def generate(self, **kw):
        self.sent.append(kw)
        return b"x"


def _story_voice(inner=None):
    from app.providers import registry

    return registry.GuardedTTSProvider(inner or _Inner(), story=True)


def _parse(text: str):
    from app.services import story_script

    return story_script.parse(text)


# ---------- 1) kind "story": asked whenever a minor is in view ----------


def test_story_kind_needs_ai():
    from app.services import minor_check

    assert "story" in minor_check.KINDS
    assert minor_check.needs_ai(f"{_GIRL_CAST}\n\nLILY: Good night.", kind="story")
    assert minor_check.needs_ai("[moans] (seductive) Hello.", kind="story")
    assert minor_check.needs_ai("(groaning) (sensual) Hello.", kind="story")
    assert minor_check.needs_ai("Her swimsuit was blue.", kind="story")
    # Every story window is asked about, whatever the words.
    assert minor_check.needs_ai(f"{_ADULT_CAST}\n\nANNA: Good night.", kind="story")
    # A voice script elsewhere (round 8): a minor named in any way is asked
    # about too.
    assert minor_check.needs_ai(f"{_GIRL_CAST}\n\nLILY: Good night.", kind="script")
    # Round 11: every voice script is asked about.
    assert minor_check.needs_ai("ANNA: Good night.", kind="script")
    assert not minor_check.needs_ai("  ", kind="script")


def test_euphemism_near_a_declared_minor_goes_to_the_model(client):
    """Defect 1: prose outside the adult word list, with a 12-year-old in the
    cast, was voiced without asking the model. A guard now sends no line
    check_story didn't pass (see the next test for the check itself)."""
    from app.providers import mock_chat
    from app.services.safety import UnsafePromptError

    assert not safety.is_sexual(_EUPHEMISM)
    mock_chat.MINOR_REPLY = _OK
    inner = _Inner()
    with pytest.raises(UnsafePromptError, match="changed since the story was checked"):
        _story_voice(inner).synthesize(text=_EUPHEMISM, voice_id="", context=_GIRL_CAST, speaker="LILY")
    assert inner.sent == [] and mock_chat.MINOR_CALLS == []


def test_euphemism_near_a_declared_minor_in_check_story(client):
    from app.providers import mock_chat
    from app.services import story_script
    from app.services.safety import UnsafePromptError

    doc = _parse(f"@cast LILY: a 12-year-old girl\n@cast NARRATOR: adult narrator\n{_EUPHEMISM}")
    mock_chat.MINOR_REPLY = _REFUSE
    with pytest.raises(UnsafePromptError):
        story_script.check_story(doc)
    assert any("12-year-old" in c and "touched her" in c for c in mock_chat.MINOR_CALLS)


def test_story_sound_prompt_near_a_minor_is_asked_with_the_cast(client):
    from app.providers import mock_chat, registry
    from app.services import story_script
    from app.services.safety import UnsafePromptError

    from .story_helpers import checked

    mock_chat.MINOR_REPLY = _REFUSE
    with pytest.raises(UnsafePromptError):
        story_script.check_story(_parse("@cast LILY: a 12-year-old girl\n"
                                        "[sfx: Lily, quiet sounds under the covers | 1s]"))
    assert all("12-year-old" in c for c in mock_chat.MINOR_CALLS) and mock_chat.MINOR_CALLS
    # Without a minor in view, a story prompt is still asked about (once,
    # by the check), and the guard then sends it without asking again.
    mock_chat.MINOR_CALLS.clear()
    mock_chat.MINOR_REPLY = _OK
    _doc, ctx = checked("@cast ANNA: an adult woman\n[sfx: rain on a tin roof | 2s]")
    assert len(mock_chat.MINOR_CALLS) == 1
    registry.audio_guard("rain on a tin roof", ctx)
    assert len(mock_chat.MINOR_CALLS) == 1


# ---------- 2) a story-level minor flag ----------


_FILLER = "The wind moved over the hills and the river ran on past the mill. " * 120


def _age_in_narration_story() -> str:
    # The cast says nothing of age; the narration does, far from the lines
    # and the prompts that follow (another window of the check).
    return ("@cast LILY: a girl with red hair\n@cast NARRATOR: adult narrator\n"
            "Lily is twelve years old.\n"
            f"{_FILLER}\n"
            "LILY [breathy]: Come here.\n"
            "[sfx: soft breathing under the blanket | 2s]\n")


def test_an_age_in_the_narration_marks_the_whole_story(client):
    """Defect 4: an age stated in the narration never reached the lines and
    prompts that follow (per-line marking, cast-only context)."""
    from app.providers import mock_chat
    from app.services import minor_check, story_script

    doc = _parse(_age_in_narration_story())
    assert not safety.mentions_minor(story_script.cast_header(doc))
    assert story_script.story_has_minor(doc)
    mock_chat.MINOR_REPLY = _OK
    context = story_script.check_story(doc)
    assert context.startswith(minor_check.STORY_NOTE)
    assert story_script.cast_header(doc) in context
    # Every window was asked, each with the note and the cast in front, and
    # the late window (the breathy line and the sound) among them.
    assert len(mock_chat.MINOR_CALLS) >= 2
    assert all(minor_check.STORY_NOTE in c and "red hair" in c for c in mock_chat.MINOR_CALLS)
    assert any("Come here" in c and "soft breathing" in c for c in mock_chat.MINOR_CALLS)


def test_the_story_context_makes_every_provider_guard_ask(client):
    """Every provider guard on the story path sends only what check_story
    passed, with the context it judged it with; anything else is refused
    without a model call."""
    from app.providers import mock_chat, registry
    from app.services import story_script
    from app.services.safety import UnsafePromptError

    from .story_helpers import say

    doc = _parse(_age_in_narration_story())
    mock_chat.MINOR_REPLY = _OK
    context = story_script.check_story(doc)
    mock_chat.MINOR_CALLS.clear()
    mock_chat.MINOR_REPLY = _REFUSE
    for call in (
        lambda: _story_voice().synthesize(text="Come here.", voice_id="", context=context, speaker="LILY"),
        lambda: registry.get_sound().generate(prompt="soft breathing", duration_seconds=1, context=context),
        lambda: registry.get_music().generate(prompt="slow warm strings", duration_seconds=2, context=context),
    ):
        with pytest.raises(UnsafePromptError, match="changed since the story was checked"):
            call()
    # What it passed goes through, without asking again.
    for it in doc.speech():
        say(_story_voice(), it, context)
    registry.get_sound().generate(prompt="soft breathing under the blanket", duration_seconds=1, context=context)
    assert mock_chat.MINOR_CALLS == []
    # The cast alone (no pass) is refused.
    with pytest.raises(UnsafePromptError):
        say(_story_voice(), doc.speech()[0], story_script.cast_header(doc))


@pytest.mark.parametrize("where", ["title", "premise", "music"])
def test_a_minor_in_the_title_premise_or_music_marks_the_story(client, where):
    from app.services import minor_check, story_script

    doc = _parse("@cast NARRATOR: adult narrator\nThe night was quiet.")
    context = story_script.check_story(doc)
    assert not context.startswith(minor_check.STORY_NOTE)
    assert context == "Story facts: NARRATOR is an adult.\n" + story_script.cast_header(doc)
    kw = {"title": {"title": "Lily, age 12"}, "premise": {"premise": "a 12-year-old girl at camp"},
          "music": {"music_prompts": ("a lullaby for a little girl",)}}[where]
    assert story_script.check_story(doc, **kw).startswith(minor_check.STORY_NOTE)


def test_story_note_mentions_a_minor():
    from app.services import minor_check

    assert safety.mentions_minor(minor_check.STORY_NOTE)


# ---------- 3) the cast cap ----------


_LONG_CAST = _GIRL_CAST + "\n" + "ANNA: an adult woman who tends a large garden by the old mill. " * 40


def test_a_long_cast_is_refused_plainly_for_every_story_call(client):
    """Defect 2: with a cast over LONG_CONTEXT_LIMIT a story prompt was judged
    without the cast, so the minor in it was lost."""
    from app.providers import mock_chat, registry
    from app.services import minor_check, story_script
    from app.services.safety import UnsafePromptError

    assert len(_LONG_CAST) > minor_check.LONG_CONTEXT_LIMIT
    msg = "Character descriptions are too long; keep them under about 2,000 characters in total"
    mock_chat.MINOR_REPLY = _OK
    for call in (
        lambda: registry.get_sound().generate(prompt="Lily breathing softly", duration_seconds=1,
                                              context=_LONG_CAST),
        lambda: registry.get_music().generate(prompt="Lily, slow strings", duration_seconds=2,
                                              context=_LONG_CAST),
        lambda: _story_voice().synthesize(text="Good night.", voice_id="", context=_LONG_CAST, speaker="LILY"),
    ):
        # No pass covers anything read with this cast.
        with pytest.raises(UnsafePromptError):
            call()
    with pytest.raises(UnsafePromptError, match=msg):
        minor_check.check_long("Good night.", context=_LONG_CAST)
    cast = "\n".join(f"@cast P{i}: an adult with a long description of a warm, low voice" for i in range(40))
    with pytest.raises(UnsafePromptError, match=msg):
        story_script.check_story(_parse(cast + "\nHello."))
    assert mock_chat.MINOR_CALLS == []


def test_a_cast_just_under_the_cap_rides_with_every_part(client):
    from app.providers import mock_chat
    from app.services import minor_check

    from .story_helpers import checked

    cast = _GIRL_CAST + "\nANNA: " + "x" * (minor_check.LONG_CONTEXT_LIMIT - len(_GIRL_CAST) - 20)
    assert len(cast) <= minor_check.LONG_CONTEXT_LIMIT
    mock_chat.MINOR_REPLY = _OK
    checked("@cast LILY: a 12-year-old girl\n@cast ANNA: " + "x" * (minor_check.LONG_CONTEXT_LIMIT
                                                                     - len(_GIRL_CAST) - 20)
            + "\n[sfx: Lily breathing softly | 1s]")
    assert mock_chat.MINOR_CALLS and all("12-year-old" in c for c in mock_chat.MINOR_CALLS)
    # The story note does not count against the cap.
    minor_check.check_cast_length(f"{minor_check.STORY_NOTE}\n{cast}")


def test_the_long_context_branch_is_gone():
    import inspect

    from app.services import minor_check

    assert not hasattr(minor_check, "LONG_CONTEXT_TOO_LONG")
    assert "context = \"\"" not in inspect.getsource(minor_check.check_long)


# ---------- 4) dash look-alikes ----------


@pytest.mark.parametrize("dash", DASHES)
def test_dash_variants_still_name_an_age(dash):
    """Defect 3: "a 12‑year‑old girl" (and en dash, em dash, minus...) was not
    read as a minor."""
    from app.services import story_script

    # The audio and story paths read dash look-alikes as "-" (the filter
    # everyone else uses reads them as it did on wip-m).
    assert safety.mentions_minor_wide(f"a 12{dash}year{dash}old girl")
    assert story_script.normalize(f"12{dash}year{dash}old") == "12-year-old"


@pytest.mark.parametrize("dash", DASHES)
def test_dash_variants_in_the_cast_reach_the_story_guards(client, dash):
    from app.providers import mock_chat, registry
    from app.services import story_script
    from app.services.safety import UnsafePromptError

    cast = f"Cast:\nLILY: a 12{dash}year{dash}old girl"
    # An intimate tag and an unknown direction near the minor are marked.
    assert "(sensual)" in story_script.safety_text("[breathy] Come closer.", cast, prose=True)
    assert "(sensual)" in story_script.safety_text("[whimpering] Come closer.", cast, prose=True)
    mock_chat.MINOR_REPLY = _REFUSE
    head = f"@cast LILY: a 12{dash}year{dash}old girl\n"
    for script, said in ((head + "LILY [breathy]: Come closer.", None),
                         (head + "LILY: Hi.\n[sfx: soft breathing | 1s]", None),
                         (head + "LILY: Come closer.", "slow, close and personal")):
        doc = _parse(script)
        if said:
            doc.speech()[0].instructions = said
        n = len(mock_chat.MINOR_CALLS)
        with pytest.raises(UnsafePromptError):
            story_script.check_story(doc)
        assert len(mock_chat.MINOR_CALLS) > n
    assert registry
    # And with the voice tag on the line itself.
    doc = _parse(f"@cast LILY: a 12{dash}year{dash}old girl\nLILY [breathy]: Come closer.")
    with pytest.raises(UnsafePromptError):
        story_script.check_story(doc)


def test_a_voice_is_sent_its_dashes(client):
    from .story_helpers import checked, say

    doc, ctx = checked("@cast ANNA: an adult woman\nANNA: She paused—then left.")
    inner = _Inner()
    say(_story_voice(inner), doc.speech()[0], ctx)
    assert inner.sent[0]["text"] == "She paused—then left."


# ---------- 5) non-story voice callers: the check they had on wip-m ----------


def _long_script(sentence: str) -> str:
    text = sentence + " " + "The shop opens at nine and the coffee is warm. " * 160
    assert len(text) > 6000
    return text


def test_video_voiceover_long_hard_sexual_script_is_judged_in_windows(client, monkeypatch):
    """Defect 5 (round 10): a long voiceover is judged in overlapping
    windows (check_long), every window asked about, not refused unjudged."""
    from app.providers import mock_chat, registry
    from app.services import minor_check

    seen: list = []
    real = minor_check.check_long
    monkeypatch.setattr(minor_check, "check_long", lambda *a, **k: seen.append(k) or real(*a, **k))
    text = _long_script("She undressed, naked on the bed.")
    inner = _Inner()
    mock_chat.MINOR_REPLY = _OK
    registry.GuardedTTSProvider(inner).synthesize(text=text, voice_id="")
    assert seen and seen[0]["kind"] == "script" and len(mock_chat.MINOR_CALLS) >= 2
    assert all(len(c) <= minor_check.JUDGE_WINDOW for c in mock_chat.MINOR_CALLS)
    # Each window fails closed.
    mock_chat.MINOR_REPLY = TimeoutError("down")
    with pytest.raises(UnsafePromptError, match="couldn't run"):
        minor_check.check_generation(_long_script("He undressed her slowly on the bed."), "", kind="script")
    assert len(inner.sent) == 1


def test_video_voiceover_long_ordinary_script_is_judged_and_allowed(client):
    """Round 11: every voice script is judged, a long one window by window
    (with the facts of the whole text read first)."""
    from app.providers import mock_chat, registry

    text = _long_script("Our new garden chairs are here.")
    inner = _Inner()
    registry.GuardedTTSProvider(inner).synthesize(text=text, voice_id="")
    assert len(mock_chat.MINOR_CALLS) >= 2
    assert inner.sent[0]["text"] == text


def test_short_voiceover_is_judged_on_its_raw_text(client):
    from app.providers import mock_chat, registry

    text = "Her swimsuit,\n\n   bright   red."
    mock_chat.MINOR_REPLY = _OK
    registry.GuardedTTSProvider(_Inner()).synthesize(text=text, voice_id="")
    assert len(mock_chat.MINOR_CALLS) == 1 and text in mock_chat.MINOR_CALLS[0]


def test_hook_lab_and_pipeline_use_the_plain_voice():
    """The non-story callers get get_tts(), whose guard is not a story one."""
    import inspect

    from app.services import hook_lab, pipeline

    assert "get_tts_for" not in inspect.getsource(hook_lab)
    assert "get_tts_for" not in inspect.getsource(pipeline)


# ---------- 6) (...) and *...* action groups ----------


@pytest.mark.parametrize("line", ["(groaning with pleasure) Don't stop.", "*groans with pleasure* Don't stop."])
def test_action_groups_are_marked_like_bracket_directions(client, line):
    """Defect 1: only square brackets were directions."""
    from app.providers import mock_chat
    from app.services import story_script
    from app.services.safety import UnsafePromptError

    assert "(sensual)" in story_script.safety_text(line, _GIRL_CAST, prose=True)
    assert story_script.is_marked(story_script.safety_text(line, _GIRL_CAST, prose=True))
    # No minor in view: an unknown action is left alone, an adult one marked.
    assert story_script.safety_text(line, _ADULT_CAST, prose=True) == line
    assert "(seductive)" in story_script.safety_text("(moaning softly) Hi.", _ADULT_CAST, prose=True)
    assert "(seductive)" in story_script.safety_text("*moans* Hi.", "", prose=True)
    mock_chat.MINOR_REPLY = _REFUSE
    with pytest.raises(UnsafePromptError):
        story_script.check_story(_parse("@cast LILY: a 12-year-old girl\nLILY: " + line))


def test_action_groups_leave_pauses_and_markers_alone():
    from app.services import story_script

    for text in ("(pause 2s) Hello.", "(pause) Hello."):
        assert story_script.safety_text(text, _GIRL_CAST, prose=True) == text
    once = story_script.safety_text("(groaning) [moans] Lily moans.", _GIRL_CAST, prose=True)
    assert story_script.safety_text(once, _GIRL_CAST, prose=True) == once


# ---------- 7) music settings ----------


def _music(inner):
    from app.providers import registry

    return registry.GuardedMusicProvider(inner)


@pytest.mark.parametrize("settings", [
    {"prompt": "anything"},
    {"text": "anything"},
    {"force_instrumental": "no, sing about it"},
    {"unknown_key": 3},
])
def test_music_settings_outside_the_allow_list_are_refused(client, settings):
    """Defect 6: settings reached the request body unchecked."""
    inner = _Inner()
    with pytest.raises(ValueError, match="not allowed"):
        _music(inner).generate(prompt="calm piano", duration_seconds=2, settings=settings)
    assert inner.sent == []


@pytest.mark.parametrize("settings", [
    {"lyrics": "Lily is twelve, come to bed and undress"},
    {"composition_plan": {"sections": [{"section_name": "Verse", "lines": ["a 12‑year‑old girl, naked"]}]}},
])
def test_music_text_settings_are_checked_with_the_prompt_guard(client, settings):
    from app.providers import mock_chat
    from app.services.safety import UnsafePromptError

    mock_chat.MINOR_REPLY = _REFUSE
    inner = _Inner()
    with pytest.raises(UnsafePromptError):
        _music(inner).generate(prompt="calm piano", duration_seconds=2, settings=settings)
    assert inner.sent == []


def test_music_plain_and_checked_settings_pass(client):
    from app.providers import mock_chat

    mock_chat.MINOR_REPLY = _OK
    inner = _Inner()
    settings = {"force_instrumental": False, "seed": 7,
                "composition_plan": {"sections": [{"section_name": "Intro", "lines": ["the sea at dawn"]}]}}
    _music(inner).generate(prompt="calm piano", duration_seconds=2, settings=settings)
    assert inner.sent[0]["settings"] == settings

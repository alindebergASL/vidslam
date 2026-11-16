"""Hook lab: each place the voiceover may pick up after the opening is
judged as heard, to the end of the script (#57 fixes, round 3).

Round 2 cut each join into the first sentence short (the opening, the rest
of that sentence and a 160-character tail), so a minor named in the heard
part of the first sentence and a sexual act later in the script were never
in one text the model read; and it judged near cuts only before the end of
the first sentence, so a form that skips a clarifying sentence after it
("Lily is 30.") was only word-filtered.

Now:
- The window where the lab found the first sentence's end: the cut is
  exact, and the only heard form is the script the opening makes (one
  call).
- Anywhere else: every word near the guess (about 0.75 s of speech either
  side, on both sides of the first sentence's end) is a place the
  voiceover may pick up; each is heard to the end of the script and judged
  on its own. At most MAX_JOINS of them (MAX_LONG_JOINS when they are long
  reads), else a plain 422.
"""
from __future__ import annotations

import types

import pytest

from app.services.hook_lab import HookLabError

from .test_hook_lab_heard import _found_at

_FILLER = ("We walk along the old harbour wall while the fishing boats come in, the gulls circle over the "
           "nets, and the lamps along the quay flicker on one by one as the evening tide rolls slowly in. ")
_ANNA = ("This is my wife Anna, not the girl of nine next door. " + _FILLER
         + "Tonight I touch her everywhere in the dark.")
_LILY_12 = ("My wife Lily is thirty, and at twelve she was the girl next door. We love long walks, "
            + _FILLER + "and quiet evenings at home with our dog. Tonight she is all mine, every inch of her, "
            "in my bed. Follow for more.")
_LILY_30 = "Hi there, friends of the channel. Lily is 30. Tonight she wants sex with me. Follow for more."

_MINOR = ("girl of nine", "at twelve", "pigtails")
_ACT = ("touch her", "in my bed", "sex")


def _setup(monkeypatch, tmp_path, script: str, total: float = 20.0, end_of_first: float | None = 4.0):
    from app.services import hook_lab, renderer

    audio = tmp_path / "vo.m4a"
    audio.write_bytes(b"x")

    def found(base, s=None):  # noqa: ANN001, ANN202
        if end_of_first is None:
            raise HookLabError("no pause found")
        return _found_at(end_of_first, script, total)

    monkeypatch.setattr(hook_lab, "base_script", lambda db, base: script)
    monkeypatch.setattr(hook_lab, "hook_window", found)
    monkeypatch.setattr(renderer, "_probe_duration", lambda p: total)
    return types.SimpleNamespace(audio_path=str(audio))


def _refuses(text: str) -> bool:
    """A minor marker and the act in one text, unless it says the person
    is 30 (as the model would read "Lily is 30.")."""
    return any(m in text for m in _MINOR) and any(a in text for a in _ACT) and "30" not in text


def _recording_judge(monkeypatch, *, judge: bool = True) -> list[str]:
    """Every text the model is asked about; refused (when `judge`) when it
    holds both a minor marker and the act."""
    from app.services import minor_check
    from app.services.safety import UnsafePromptError

    seen: list[str] = []

    def record(prompt, negative="", *, kind):  # noqa: ANN001, ANN202
        seen.append(prompt)
        if judge and _refuses(prompt):
            raise UnsafePromptError("refused")

    monkeypatch.setattr(minor_check, "check_generation", record)
    return seen


# ---------- 1. a minor in the heard part of the first sentence, the act far later ----------


@pytest.mark.parametrize("window", [2.5, 3.0])
def test_a_minor_heard_in_the_first_sentence_is_read_with_the_act_later_on(monkeypatch, tmp_path, window):
    from app.services import hook_lab
    from app.services.safety import check_script

    assert 170 <= len(_FILLER) <= 190
    check_script(_ANNA)  # the word filter alone lets it through: the model must see both
    base = _setup(monkeypatch, tmp_path, _ANNA)
    seen = _recording_judge(monkeypatch)
    with pytest.raises(HookLabError) as e:
        hook_lab.check_opening(None, base, "Meet my neighbour.", window)
    assert e.value.status == 422
    if str(e.value) not in (hook_lab.JOINS_TOO_MANY, hook_lab.LONG_JOINS_TOO_MANY):
        assert any(_refuses(t) for t in seen)


@pytest.mark.parametrize("window", [0.3, 0.4, 0.5])
def test_lily_at_twelve_heard_before_her_night_in_bed_is_refused(monkeypatch, tmp_path, window):
    from app.services import hook_lab
    from app.services.safety import check_script

    check_script(_LILY_12)
    base = _setup(monkeypatch, tmp_path, _LILY_12, total=10.0, end_of_first=2.5)
    seen = _recording_judge(monkeypatch)
    with pytest.raises(HookLabError) as e:
        hook_lab.check_opening(None, base, "Meet Lily.", window)
    assert e.value.status == 422
    assert any(_refuses(t) for t in seen)
    # The script the opening makes ("Meet Lily. We love long walks, ...") passes.
    assert not _refuses(hook_lab.replace_first_sentence(_LILY_12, "Meet Lily."))


# ---------- 2. near cuts after the first sentence ----------


@pytest.mark.parametrize("window,end_of_first", [(4.0, None), (4.4, None), (4.0, 3.0), (4.4, 3.0)])
def test_a_form_that_skips_the_sentence_after_the_first_is_judged(monkeypatch, tmp_path, window, end_of_first):
    """The voiceover may pick up past "Lily is 30.": "Lily wears her
    pigtails and braces. Tonight she wants sex with me." is heard, and
    refused."""
    from app.services import hook_lab

    hook = "Lily wears her pigtails and braces."
    skipped = f"{hook} Tonight she wants sex with me. Follow for more."
    base = _setup(monkeypatch, tmp_path, _LILY_30, total=10.0, end_of_first=end_of_first)
    seen = _recording_judge(monkeypatch)
    with pytest.raises(HookLabError) as e:
        hook_lab.check_opening(None, base, hook, window)
    assert e.value.status == 422
    assert skipped in seen
    assert not _refuses(hook_lab.replace_first_sentence(_LILY_30, hook))  # "... Lily is 30. ..." passes


# ---------- 3. costs ----------


def test_an_ordinary_hook_at_the_end_of_the_first_sentence_costs_one_call(monkeypatch, tmp_path):
    from app.services import hook_lab

    script = "Calm words about the sea and the boats today. Then the harbour lights come on. Follow for more."
    base = _setup(monkeypatch, tmp_path, script, total=8.0, end_of_first=3.6)
    seen = _recording_judge(monkeypatch)
    hook_lab.check_opening(None, base, "Hello there.", 3.6)
    assert seen == ["Hello there. Then the harbour lights come on. Follow for more."]
    # A few hundredths of a second off is the same pause.
    seen.clear()
    hook_lab.check_opening(None, base, "Hello there.", 3.63)
    assert len(seen) == 1


def test_a_window_inside_a_71_word_first_sentence_is_capped(monkeypatch, tmp_path):
    from app.services import hook_lab

    first = " ".join(f"word{i}" for i in range(70)) + " finally."
    script = f"{first} We walk to the market for bread. Follow for more."
    assert len(first.split()) == 71
    base = _setup(monkeypatch, tmp_path, script, total=40.0, end_of_first=34.0)
    seen = _recording_judge(monkeypatch, judge=False)
    try:
        hook_lab.check_opening(None, base, "Here is a calm opening.", 17.0)
    except HookLabError as e:
        assert e.status == 422 and str(e) == hook_lab.JOINS_TOO_MANY and seen == []
    else:
        assert 2 <= len(seen) <= hook_lab.MAX_JOINS + 1  # the joins, and the script it makes
        assert len(set(seen)) == len(seen)
        assert all(t.endswith("Follow for more.") for t in seen)


def test_a_window_inside_the_first_sentence_of_a_13_8k_script_makes_few_long_reads(monkeypatch, tmp_path):
    from app.services import hook_lab, minor_check

    first = "My calm first sentence is about the walk we take on most mornings by the sea."
    body = "We walk to the market and buy some bread and fresh fruit for the week. "
    script = f"{first} " + (body * 200)[:13800 - len(first) - 1].rsplit(" ", 1)[0] + "."
    assert 13700 < len(script) <= 13800 and len(script) > minor_check.JUDGE_WINDOW
    base = _setup(monkeypatch, tmp_path, script, total=900.0, end_of_first=5.0)
    seen = _recording_judge(monkeypatch, judge=False)
    try:
        hook_lab.check_opening(None, base, "Here is a calm opening.", 2.5)
    except HookLabError as e:
        assert e.status == 422 and str(e) == hook_lab.LONG_JOINS_TOO_MANY and seen == []
        assert "shorten the script" in str(e)
    else:
        longs = [t for t in seen if not hook_lab.fits_one_call(t)]
        assert 2 <= len(longs) <= hook_lab.MAX_LONG_JOINS + 1  # the joins, and the script it makes


def test_too_many_long_joins_fail_closed_before_anything_is_judged(monkeypatch, tmp_path):
    from app.services import hook_lab

    first = "My calm first sentence is about the walk we take on most mornings by the sea."
    script = f"{first} " + "We walk to the market and buy some bread and fresh fruit for the week. " * 200
    base = _setup(monkeypatch, tmp_path, script.strip(), total=900.0, end_of_first=5.0)
    monkeypatch.setattr(hook_lab, "MAX_LONG_JOINS", 1)
    seen = _recording_judge(monkeypatch, judge=False)
    with pytest.raises(HookLabError) as e:
        hook_lab.check_opening(None, base, "Here is a calm opening.", 2.5)
    assert e.value.status == 422 and str(e.value) == hook_lab.LONG_JOINS_TOO_MANY
    assert seen == []


# ---------- 4. how the joins are made ----------


def test_every_join_runs_to_the_end_of_the_script():
    from app.services import hook_lab

    rest_at = _ANNA.index("We walk")
    cuts = hook_lab.word_starts(_ANNA, rest_at)
    texts = hook_lab.heard_joins("Meet my neighbour.", _ANNA, cuts)
    assert len(texts) == len(cuts)
    assert all(t.endswith("Tonight I touch her everywhere in the dark.") for t in texts)
    assert texts[0] == f"Meet my neighbour. {_ANNA}"


def test_the_band_is_about_three_quarters_of_a_second_either_side():
    from app.services import hook_lab

    # 1000 characters over 100 s: 0.75 s is 7.5 characters, so the band is
    # 12 either side of the guess (500), not a quarter of it.
    script = " ".join(["abcd"] * 200) + "."
    best, near = hook_lab.heard_cuts(script, 50.0, 100.0)
    assert abs(best - 500) <= 3
    assert near and min(near) >= 500 - 12 and max(near) <= 500 + 12


def test_a_join_the_same_as_a_text_already_judged_is_not_sent_again(monkeypatch, tmp_path):
    """The best guess is also one of the joins, and a join at the end of
    the first sentence is the script the opening makes: each is asked
    about once."""
    from app.services import hook_lab

    script = "Calm words about the sea and the boats today. Then the harbour lights come on."
    base = _setup(monkeypatch, tmp_path, script, total=8.0, end_of_first=5.0)
    seen = _recording_judge(monkeypatch)
    hook_lab.check_opening(None, base, "Hello there.", 4.4)
    best, near = hook_lab.heard_cuts(script, 4.4, 8.0, 5.0)
    rest_at = script.index("Then")
    assert best < rest_at and rest_at in near
    assert seen.count(f"Hello there. {script[best:]}") == 1
    assert seen.count("Hello there. Then the harbour lights come on.") == 1
    assert len(seen) == len(set(seen)) >= 3

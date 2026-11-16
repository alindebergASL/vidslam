"""Hook lab: every join into the original first sentence is judged, each
one short (#57 fixes, round 2).

1. heard_cuts' places near the guess can reach back into the original
   first sentence even when the window is at its end; those joins were
   only word-filtered. Each is now read by the model as its own text.
2. Each join was the opening plus the whole rest of the script: a long
   base made every join a long read (check_long, a facts call and several
   windows each). A join is now cut to what it can change: the opening,
   the rest of the first sentence and a short tail. The whole rest of the
   script is read once, with the opening (the script it makes).
3. With short joins, a window inside an ordinary long first sentence (more
   than 40 words) is no longer refused.
"""
from __future__ import annotations

import types

import pytest

from .test_hook_lab import _db
from .test_hook_lab_heard import _post, _spliced_base, _window_at

_EMMA = "Meet my wife Lily, and her daughter Emma who is nine. Tonight we have a party at home."
_UNDRESS = "Watch me undress her slowly."


def _judged(monkeypatch, *, judge: bool = True) -> list[str]:
    """Every text the model is asked about (check_generation), recorded;
    `judge` False: recorded only, so every text is seen."""
    from app.services import minor_check

    seen: list[str] = []
    real = minor_check.check_generation

    def record(prompt, negative="", *, kind):  # noqa: ANN001, ANN202
        seen.append(prompt)
        if judge:
            real(prompt, negative, kind=kind)

    monkeypatch.setattr(minor_check, "check_generation", record)
    return seen


def test_joins_near_the_window_inside_the_first_sentence_are_judged(monkeypatch, tmp_path):
    """The window at the end of the first sentence: the best guess is the
    script the opening makes, but the voiceover may pick up a few words
    earlier ("Emma who is nine."), and the model reads that join too."""
    from app.services import hook_lab, renderer

    audio = tmp_path / "vo.m4a"
    audio.write_bytes(b"x")
    monkeypatch.setattr(hook_lab, "base_script", lambda db, base: _EMMA)
    monkeypatch.setattr(hook_lab, "find_hook_window", lambda base, script=None: 4.0)
    monkeypatch.setattr(renderer, "_probe_duration", lambda p: 7.0)
    base = types.SimpleNamespace(audio_path=str(audio))
    rest_at = _EMMA.index("Tonight")
    best, near = hook_lab.heard_cuts(_EMMA, 4.0, 7.0, 4.0)
    assert best == rest_at and [c for c in near if c < rest_at]  # near reaches into the first sentence

    seen = _judged(monkeypatch, judge=False)
    hook_lab.check_opening(None, base, _UNDRESS, 4.0)
    assert f"{_UNDRESS} Tonight we have a party at home." in seen  # the script it makes
    assert f"{_UNDRESS} Emma who is nine. Tonight we have a party at home." in seen
    assert f"{_UNDRESS} daughter Emma who is nine. Tonight we have a party at home." in seen
    assert all(t.count(_UNDRESS) == 1 and "Meet my wife" not in t for t in seen)


def _long_base(first_words: int) -> tuple[str, str]:
    first = " ".join(f"word{i}" for i in range(first_words - 1)) + " finally."
    body = "We walk to the market and buy some bread and fresh fruit for the week. "
    rest = (body * (7850 // len(body) + 1))[: 7850 - len(first) - 1].rsplit(" ", 1)[0] + "."
    return f"{first} {rest}", first


def test_each_join_is_short_and_a_long_base_makes_few_long_reads(auth_client, monkeypatch):
    from app.providers import mock_chat
    from app.services import hook_lab, minor_check

    script, first = _long_base(30)
    assert 7700 < len(script) <= 7850 and len(script) > minor_check.JUDGE_WINDOW
    from app import models
    from app.services import renderer

    pid, base_id = _spliced_base(auth_client, script)
    # About eight minutes of speech, the first sentence about 15 s of it,
    # the window a third of the way into that sentence.
    monkeypatch.setattr(renderer, "_probe_duration", lambda p: 480.0)
    monkeypatch.setattr(hook_lab, "find_hook_window", lambda base, s=None: 15.0)

    seen = _judged(monkeypatch)
    longs: list[str] = []
    real_long = minor_check.check_long

    def count_long(text, **kw):  # noqa: ANN001, ANN202
        longs.append(text)
        return real_long(text, **kw)

    monkeypatch.setattr(minor_check, "check_long", count_long)
    facts: list[str] = []
    real_facts = mock_chat.MockChatProvider.story_characters

    def count_facts(self, text):  # noqa: ANN001, ANN202
        facts.append(text)
        return real_facts(self, text)

    monkeypatch.setattr(mock_chat.MockChatProvider, "story_characters", count_facts)
    minor_check.reset_cache()
    db = _db()
    try:
        hook_lab.check_opening(db, db.get(models.Render, base_id), "Here is a calm opening", 5.0)
    finally:
        db.close()

    joins = [t for t in seen if t.startswith("Here is a calm opening word")]
    assert len(joins) >= 25  # every word of the first sentence (inside)
    limit = len("Here is a calm opening") + 1 + len(first) + 1 + hook_lab._JOIN_TAIL
    # Each join is short; only the best guess is read as heard to the end.
    assert len([t for t in joins if len(t) > limit]) <= 1
    # Only the long forms (the script the opening makes, the best guess as
    # heard) are read whole: never one per join.
    assert len(longs) <= 2 and len(facts) <= 2 * len(longs), (len(longs), len(facts))
    assert len(seen) <= len(joins) + 4


def test_join_tail_ends_at_a_word():
    from app.services import hook_lab

    script = "Short first. " + "alpha beta gamma " * 40
    rest_at = script.index("alpha")
    tail = hook_lab.join_tail(script, rest_at)
    assert len(tail) <= hook_lab._JOIN_TAIL and script[rest_at:].startswith(tail)
    assert tail.split()[-1] in ("alpha", "beta", "gamma")
    assert script[rest_at + len(tail)] == " "
    assert hook_lab.join_tail("One. Two three.", 5) == "Two three."


def test_a_window_inside_a_45_word_first_sentence_is_checked_not_refused(auth_client):
    from app.services import hook_lab

    script, _first = _long_base(45)
    script = script[:1200].rsplit(" ", 1)[0] + "."
    assert hook_lab.MAX_JOINS >= 45
    pid, base_id = _spliced_base(auth_client, script)
    r = _post(auth_client, pid, base_id, "Here is a calm opening", _window_at(base_id, "word20"))
    assert r.status_code == 201, r.text


def test_too_many_joins_still_fail_closed(monkeypatch):
    from app.services import hook_lab, renderer

    script = "one two three four five six seven eight nine ten. Follow for more."
    monkeypatch.setattr(hook_lab, "MAX_JOINS", 3)
    monkeypatch.setattr(hook_lab, "base_script", lambda db, base: script)
    monkeypatch.setattr(hook_lab, "find_hook_window", lambda base, s=None: 4.0)
    monkeypatch.setattr(renderer, "_probe_duration", lambda p: 8.0)
    monkeypatch.setattr(hook_lab.Path, "is_file", lambda self: True)
    base = types.SimpleNamespace(audio_path="/vo.m4a")
    with pytest.raises(hook_lab.HookLabError) as e:
        hook_lab.heard_checks(None, base, "Hi there", 2.0)
    assert e.value.status == 422 and str(e.value) == hook_lab.JOINS_TOO_MANY

"""Hook lab: every join into the original first sentence is judged
(#57 fixes, round 2; round 3: each join is read as heard, to the end of
the script, see test_hook_lab_joins_as_heard).

1. heard_cuts' places near the guess can reach back into the original
   first sentence; those joins were only word-filtered. Each is now read
   by the model as its own text. Round 3: a window exactly where the lab
   found the first sentence's end is an exact cut (one text, the script
   the opening makes); anywhere else, every place near the guess is read.
2. Each join is the opening plus the whole rest of the script, so on a
   long base every join is a long read (check_long, a facts call and
   several windows each). Round 2 cut joins short, which split a minor in
   the first sentence from an act later on; round 3 reads them whole and
   instead caps the long ones (MAX_LONG_JOINS), failing closed beyond it.
3. On an ordinary-length script, a window inside a long first sentence
   (more than 40 words) is checked, not refused: only the places near the
   guess are read (at most MAX_JOINS).
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


def test_the_window_at_the_end_of_the_first_sentence_is_exact(monkeypatch, tmp_path):
    """Round 3: the window where the lab found the first sentence's end is
    an exact cut, and the only text heard is the script the opening makes:
    one call. Just before that pause, the voiceover may pick up a few words
    earlier ("Emma who is nine."), and the model reads that join too."""
    from app.services import hook_lab, renderer

    audio = tmp_path / "vo.m4a"
    audio.write_bytes(b"x")
    monkeypatch.setattr(hook_lab, "base_script", lambda db, base: _EMMA)
    monkeypatch.setattr(hook_lab, "find_hook_window", lambda base, script=None: 4.0)
    monkeypatch.setattr(renderer, "_probe_duration", lambda p: 7.0)
    base = types.SimpleNamespace(audio_path=str(audio))

    seen = _judged(monkeypatch, judge=False)
    hook_lab.check_opening(None, base, _UNDRESS, 4.0)
    assert seen == [f"{_UNDRESS} Tonight we have a party at home."]  # the script it makes

    seen.clear()
    hook_lab.check_opening(None, base, _UNDRESS, 3.5)
    assert seen[0] == f"{_UNDRESS} Tonight we have a party at home."
    assert f"{_UNDRESS} Emma who is nine. Tonight we have a party at home." in seen
    assert all(t.count(_UNDRESS) == 1 and "Meet my wife" not in t for t in seen)
    assert len(seen) == len(set(seen))


def _long_base(first_words: int) -> tuple[str, str]:
    first = " ".join(f"word{i}" for i in range(first_words - 1)) + " finally."
    body = "We walk to the market and buy some bread and fresh fruit for the week. "
    rest = (body * (7850 // len(body) + 1))[: 7850 - len(first) - 1].rsplit(" ", 1)[0] + "."
    return f"{first} {rest}", first


def test_a_window_inside_the_first_sentence_of_a_long_script_fails_closed(auth_client, monkeypatch):
    """Every join runs to the end of the script, so on a long script each
    is a long read (check_long, several calls). More than MAX_LONG_JOINS of
    them: refused before anything is judged, with a plain message."""
    from app import models
    from app.services import hook_lab, minor_check, renderer

    script, first = _long_base(30)
    assert 7700 < len(script) <= 7850 and len(script) > minor_check.JUDGE_WINDOW
    pid, base_id = _spliced_base(auth_client, script)
    # About eight minutes of speech, the first sentence about 15 s of it,
    # the window a third of the way into that sentence.
    monkeypatch.setattr(renderer, "_probe_duration", lambda p: 480.0)
    monkeypatch.setattr(hook_lab, "find_hook_window", lambda base, s=None: 15.0)
    monkeypatch.setattr(hook_lab, "MAX_LONG_JOINS", 1)
    seen = _judged(monkeypatch, judge=False)
    db = _db()
    try:
        with pytest.raises(hook_lab.HookLabError) as e:
            hook_lab.check_opening(db, db.get(models.Render, base_id), "Here is a calm opening", 5.0)
    finally:
        db.close()
    assert e.value.status == 422 and str(e.value) == hook_lab.LONG_JOINS_TOO_MANY
    assert "shorten the script" in str(e.value)
    assert seen == []


def test_a_few_long_joins_are_each_read_whole(monkeypatch, tmp_path):
    """Under MAX_LONG_JOINS, each long join is judged as heard, to the end
    of the script, one text each."""
    from app.services import hook_lab, minor_check, renderer

    first = "My calm first sentence is short."
    body = "We walk to the market and buy some bread and fresh fruit for the week. "
    script = first + " " + (body * 100).strip()
    assert len(script) > minor_check.JUDGE_WINDOW
    audio = tmp_path / "vo.m4a"
    audio.write_bytes(b"x")
    monkeypatch.setattr(hook_lab, "base_script", lambda db, base: script)
    monkeypatch.setattr(hook_lab, "find_hook_window", lambda base, s=None: 3.0)
    monkeypatch.setattr(renderer, "_probe_duration", lambda p: 480.0)
    base = types.SimpleNamespace(audio_path=str(audio))
    seen = _judged(monkeypatch, judge=False)
    hook_lab.check_opening(None, base, "Here is a calm opening", 1.5)
    joins = [t for t in seen if t != f"Here is a calm opening. {script[len(first) + 1:]}"]
    assert 1 <= len(joins) <= hook_lab.MAX_LONG_JOINS
    assert all(t.endswith("fresh fruit for the week.") and len(t) > minor_check.JUDGE_WINDOW for t in joins)
    assert len(set(seen)) == len(seen)


def test_a_window_inside_a_45_word_first_sentence_is_checked_not_refused(auth_client, monkeypatch):
    from app.services import hook_lab, renderer

    script, _first = _long_base(45)
    script = script[:1200].rsplit(" ", 1)[0] + "."
    pid, base_id = _spliced_base(auth_client, script)
    # Spoken at an ordinary pace (about 15 characters a second).
    monkeypatch.setattr(renderer, "_probe_duration", lambda p: 80.0)
    hook_lab._window_cache.clear()
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

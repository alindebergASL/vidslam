"""A safety check verdict written as a string (#57 fixes, round 3).

A picture or clip reply is read leniently. "minor_sexual": "true" was
unreadable ("the check couldn't run"), which outside adult mode lets a
picture or clip prompt with no plainly sexual word go ahead. Now a string
that is exactly "true"/"yes" (any case, spaces around it) is a true, and
any true still wins. A string that is exactly "false"/"no" is a false: it
can't hide a yes. Anything else ("maybe", "not true", a null) stays
unreadable, never a no. Strict reading (story parts) is unchanged.
"""
from __future__ import annotations

import pytest
import respx

from app.providers.openrouter_chat import parse_minor_verdict
from app.services import minor_check, safety

from .test_minor_check import OR, _adult, _real_planner, _reply

STRING_TRUE = [
    '{"minor_sexual": "true", "reason": "a child"}',
    '{"minor_sexual": "TRUE"}',
    '{"minor_sexual": " True "}',
    '{"minor_sexual": "yes", "reason": "a child"}',
    '{"minor_sexual": "Yes"}',
    '{"minor_sexual": false} {"minor_sexual": "true", "reason": "a child"}',
    '{"minor_sexual": "true"} {"minor_sexual": false}',
    '{"minor_sexual": "no"} {"minor_sexual": "yes"}',
    '{"minor_sexual": null} {"minor_sexual": "true"}',
    'Verdict: {"minor_sexual": "true"} as asked.',
]

STRING_FALSE = [
    '{"minor_sexual": "false", "reason": "adults"}',
    '{"minor_sexual": "FALSE"}',
    '{"minor_sexual": " no "}',
    '{"minor_sexual": false} {"minor_sexual": "false"}',
    '{"minor_sexual": "No"} {"minor_sexual": false} {"minor_sexual": false}',
]

UNREADABLE = [
    '{"minor_sexual": "maybe"}',
    '{"minor_sexual": "not true"}',
    '{"minor_sexual": "false, but a child"}',
    '{"minor_sexual": false} {"minor_sexual": "unsure"}',
    '{"minor_sexual": "false"} {"minor_sexual": null}',
    '{"minor_sexual": ""}',
]


# #57 round 4: the first word decides a yes ("True.", "yes - a child"),
# with punctuation and zero-width characters around it; the number 1 and a
# list holding a true are a yes too. Only an exact "false"/"no" (trailing
# punctuation allowed) is a no; any yes still wins.
STRING_TRUE += [
    '{"minor_sexual": "True."}',
    '{"minor_sexual": "TRUE!"}',
    '{"minor_sexual": "yes - a child"}',
    '{"minor_sexual": "Yes."}',
    '{"minor_sexual": 1}',
    '{"minor_sexual": [true]}',
    '{"minor_sexual": "true\\u200b"}',
    '{"minor_sexual": "\u200btrue\u200b"}',
    '{"minor_sexual": "true-ish"}',
    '{"minor_sexual": "no"} {"minor_sexual": "yes."}',
    '{"minor_sexual": "No."} {"minor_sexual": [false, "yes"]}',
]
STRING_FALSE += [
    '{"minor_sexual": "No."}',
    '{"minor_sexual": "false!"}',
    '{"minor_sexual": "\u200bfalse "}',
]
UNREADABLE += [
    '{"minor_sexual": 0}',
    '{"minor_sexual": 2}',
    '{"minor_sexual": [false]}',
    '{"minor_sexual": []}',
    '{"minor_sexual": "no, but"}',
    '{"minor_sexual": "nope"}',
]


@pytest.mark.parametrize("value,verdict", [
    ("True.", True), ("TRUE!", True), ("yes - a child", True), ("Yes.", True), (1, True), ([True], True),
    ("true\u200b", True), (False, False), ("no", False), (" FALSE. ", False), (0, None), (1.5, None),
    ("not true", None), ("false, but a child", None), ([False], None),
])
def test_lenient_verdict_values(value, verdict):
    from app.providers.openrouter_chat import _lenient_verdict

    assert _lenient_verdict(value) is verdict


@pytest.mark.parametrize("reply", STRING_TRUE)
def test_a_string_true_or_yes_is_true(reply):
    assert parse_minor_verdict(reply)["minor_sexual"] is True


@pytest.mark.parametrize("reply", STRING_FALSE)
def test_a_string_false_or_no_is_false(reply):
    assert parse_minor_verdict(reply)["minor_sexual"] is False


@pytest.mark.parametrize("reply", UNREADABLE)
def test_anything_else_stays_unreadable(reply):
    with pytest.raises(ValueError):
        parse_minor_verdict(reply)


def test_the_reason_is_kept():
    out = parse_minor_verdict('{"minor_sexual": "yes", "reason": "a child, sexualised"}')
    assert out == {"minor_sexual": True, "reason": "a child, sexualised"}


@pytest.mark.parametrize("reply", ['{"minor_sexual": "true"}', '{"minor_sexual": "false"}'])
def test_strict_reading_is_unchanged(reply):
    with pytest.raises(ValueError):
        parse_minor_verdict(reply, strict=True)


@pytest.mark.parametrize("kind", ["image", "video"])
@respx.mock
def test_a_picture_or_clip_with_a_string_true_is_refused(client, monkeypatch, kind):
    """Outside adult mode, through the real OpenRouter reply path: refused,
    never "couldn't run" (which would let this prompt go ahead)."""
    _real_planner(monkeypatch)
    _adult(False)
    route = respx.post(f"{OR}/chat/completions").mock(
        return_value=_reply('{"minor_sexual": "true", "reason": "a child"}'))
    with pytest.raises(safety.UnsafePromptError) as e:
        minor_check.check_generation("a young girl posing on a bed in a sheer nightgown", kind=kind)
    assert str(e.value) == minor_check.REFUSED
    assert route.call_count == 1

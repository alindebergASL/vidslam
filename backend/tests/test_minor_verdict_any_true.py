"""A safety check reply with a "true" verdict anywhere in it (#57 review,
finding 6).

A picture or clip reply is read leniently. With two or more verdict
objects in it, the reply was unreadable, and an unreadable reply is "the
check couldn't run": outside adult mode a picture or clip prompt with no
plainly sexual word then goes ahead with no verdict at all. Now any
"minor_sexual": true in the reply is the answer (refused); a reply whose
verdicts all say false is a no (test_minor_verdict_all_false.py), and one
with none stays unreadable.
"""
from __future__ import annotations

import pytest
import respx

from app.services import minor_check, safety

from .test_minor_check import OR, _adult, _real_planner, _reply

# Not plainly sexual (no hard word): outside adult mode, a check that
# can't run lets it go ahead.
PROMPT = "a young girl posing on a bed in a sheer nightgown"

MULTI_TRUE = [
    '{"minor_sexual": false, "reason": "a"}\n{"minor_sexual": true, "reason": "a child, sexualised"}',
    '{"minor_sexual": true, "reason": "a child"}\n{"minor_sexual": false, "reason": "b"}',
    '{"minor_sexual": false} or maybe {"minor_sexual": true}',
    '```json\n{"minor_sexual": false}\n```\nActually: {"minor_sexual": true, "reason": "a child"}',
    '{"minor_sexual": true, "reason": "x"} {"minor_sexual": true, "reason": "y"}',
]


@pytest.mark.parametrize("reply", MULTI_TRUE)
def test_any_true_verdict_is_read_as_true(reply):
    from app.providers.openrouter_chat import parse_minor_verdict

    assert parse_minor_verdict(reply)["minor_sexual"] is True


@pytest.mark.parametrize("reply", ["no verdict here"])
def test_no_verdict_stays_unreadable(reply):
    from app.providers.openrouter_chat import parse_minor_verdict

    with pytest.raises(ValueError):
        parse_minor_verdict(reply)


def test_one_false_is_still_false():
    from app.providers.openrouter_chat import parse_minor_verdict

    assert parse_minor_verdict('Sure: {"minor_sexual": false, "reason": "adults"}')["minor_sexual"] is False


@pytest.mark.parametrize("kind", ["image", "video"])
@pytest.mark.parametrize("reply", MULTI_TRUE[:2])
@respx.mock
def test_a_picture_or_clip_with_a_true_among_several_verdicts_is_refused(client, monkeypatch, kind, reply):
    """Outside adult mode, through the real OpenRouter reply path: refused,
    never "couldn't run" (which would let this prompt go ahead)."""
    _real_planner(monkeypatch)
    _adult(False)
    route = respx.post(f"{OR}/chat/completions").mock(return_value=_reply(reply))
    with pytest.raises(safety.UnsafePromptError) as e:
        minor_check.check_generation(PROMPT, kind=kind)
    assert str(e.value) == minor_check.REFUSED
    assert route.call_count == 1

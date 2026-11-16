"""A safety check reply whose verdicts all say false (#57 fixes, round 2).

A picture or clip reply is read leniently. One with two or more verdict
objects that all said "minor_sexual": false was unreadable ("the check
couldn't run"), though no object in it said yes. Now every verdict
agreeing on false is a no. Any true still wins, and a verdict that isn't
a plain true/false (a string "true", a null) never reads as a no.
"""
from __future__ import annotations

import pytest
import respx

from app.providers.openrouter_chat import parse_minor_verdict
from app.services import minor_check

from .test_check_availability import SCRIPT
from .test_minor_check import OR, _adult, _real_planner, _reply

ALL_FALSE = [
    '{"minor_sexual": false, "reason": "a"}\n{"minor_sexual": false, "reason": "b"}',
    '```json\n{"minor_sexual": false, "reason": "adults"}\n```\nSo: {"minor_sexual": false}',
    '{"minor_sexual": false} {"minor_sexual": false} {"minor_sexual": false, "reason": "c"}',
]


@pytest.mark.parametrize("reply", ALL_FALSE)
def test_every_verdict_false_is_read_as_false(reply):
    assert parse_minor_verdict(reply)["minor_sexual"] is False


@pytest.mark.parametrize("reply", [
    '{"minor_sexual": false} {"minor_sexual": true, "reason": "a child"}',
    '{"minor_sexual": true, "reason": "a child"} {"minor_sexual": false} {"minor_sexual": false}',
    '{"minor_sexual": "true"} {"minor_sexual": true}',
])
def test_any_true_still_wins(reply):
    assert parse_minor_verdict(reply)["minor_sexual"] is True


@pytest.mark.parametrize("reply", [
    '{"minor_sexual": "true", "reason": "a child"}',
    '{"minor_sexual": false} {"minor_sexual": "true", "reason": "a child"}',
    '{"minor_sexual": "true"} {"minor_sexual": false}',
    '{"minor_sexual": false} {"minor_sexual": null}',
    '{"minor_sexual": "false"}',
    "no verdict here",
])
def test_a_verdict_that_is_not_a_plain_bool_stays_unreadable(reply):
    with pytest.raises(ValueError):
        parse_minor_verdict(reply)


def test_strict_story_reading_is_unchanged():
    with pytest.raises(ValueError):
        parse_minor_verdict(ALL_FALSE[0], strict=True)


@respx.mock
def test_a_voice_script_with_several_false_verdicts_is_not_paused(client, monkeypatch):
    """Through the real OpenRouter reply path, for a kind that waits when
    the check can't run: read as a no, not as "couldn't run"."""
    _real_planner(monkeypatch)
    _adult(False)
    route = respx.post(f"{OR}/chat/completions").mock(return_value=_reply(ALL_FALSE[0]))
    minor_check.check_generation(SCRIPT, kind="script")
    assert route.call_count == 1

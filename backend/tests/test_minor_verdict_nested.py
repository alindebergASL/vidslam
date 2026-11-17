"""A safety check verdict anywhere in the reply (#57 fixes, round 5).

Every "minor_sexual" value counts: in a nested object or list, with the key
in any case, and each value of a key written twice (json.loads keeps only
the last one). Any value that reads true makes the verdict true, in strict
reading (story parts) and lenient reading alike; a mix of false and
unreadable values is unreadable, never a no. Lenient strings are read
after NFKC with zero-width characters and punctuation taken off: "1",
1.0, "y", "t" and full-width "yes" are a yes.
"""
from __future__ import annotations

import pytest

from app.providers.openrouter_chat import _lenient_verdict, parse_minor_verdict

ANY_TRUE = [
    '{"minor_sexual": false, "details": {"minor_sexual": true}}',
    '{"result": {"minor_sexual": true}, "minor_sexual": false}',
    '{"minor_sexual": true, "minor_sexual": false}',
    '{"minor_sexual": false, "minor_sexual": true}',
    '{"minor_sexual": "false"} {"Minor_sexual": true}',
    '{"MINOR_SEXUAL": true}',
    '{"checks": [{"minor_sexual": false}, {"minor_sexual": true}], "minor_sexual": false}',
    '{"minor_sexual": false, "reason": "x", "minor_sexual": "yes"}',
]

# Strict reading takes one object and only JSON true and false.
STRICT_TRUE = [r for r in ANY_TRUE if "} {" not in r and '"yes"' not in r]

MIXED_UNREADABLE = [
    '{"minor_sexual": false, "details": {"minor_sexual": null}}',
    '{"minor_sexual": false, "minor_sexual": "maybe"}',
    '{"result": {"minor_sexual": "unsure"}, "minor_sexual": false}',
    '{"minor_sexual": false} {"MINOR_SEXUAL": null}',
]

ALL_FALSE = [
    '{"minor_sexual": false, "details": {"minor_sexual": false}}',
    '{"minor_sexual": false, "minor_sexual": "no"}',
]


@pytest.mark.parametrize("reply", ANY_TRUE)
def test_any_true_anywhere_is_a_yes(reply):
    assert parse_minor_verdict(reply)["minor_sexual"] is True


@pytest.mark.parametrize("reply", STRICT_TRUE)
def test_any_true_anywhere_is_a_yes_in_strict_reading(reply):
    assert parse_minor_verdict(reply, strict=True)["minor_sexual"] is True


@pytest.mark.parametrize("reply", MIXED_UNREADABLE)
def test_false_beside_an_unreadable_value_is_unreadable(reply):
    with pytest.raises(ValueError):
        parse_minor_verdict(reply)
    if "} {" not in reply:
        with pytest.raises(ValueError):
            parse_minor_verdict(reply, strict=True)


@pytest.mark.parametrize("reply", ALL_FALSE)
def test_every_value_false_is_a_no(reply):
    assert parse_minor_verdict(reply)["minor_sexual"] is False


def test_strict_all_false_nested_is_a_no():
    out = parse_minor_verdict('{"minor_sexual": false, "x": {"minor_sexual": false}, "reason": "ok"}', strict=True)
    assert out == {"minor_sexual": False, "reason": "ok"}


@pytest.mark.parametrize("value", ["1", 1.0, "y", "t", "ｙｅｓ", "Y.", '"yes"', "​yes​",
                                   "...true", "T", "1!", "ＴＲＵＥ"])
def test_lenient_strings_and_numbers_read_as_yes(value):
    assert _lenient_verdict(value) is True


@pytest.mark.parametrize("value", ["no", "ｎｏ", "false.", "​No!", '"false"'])
def test_lenient_exact_no(value):
    assert _lenient_verdict(value) is False


@pytest.mark.parametrize("value", ["0", 0, 0.0, 2, "n", "f", "maybe", "not true", "10", {}, None])
def test_lenient_anything_else_is_unreadable(value):
    assert _lenient_verdict(value) is None


def test_the_reason_comes_from_the_object_that_says_yes():
    out = parse_minor_verdict('{"minor_sexual": false, "reason": "a"} {"minor_sexual": true, "reason": "b"}')
    assert out == {"minor_sexual": True, "reason": "b"}

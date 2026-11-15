"""Round 21 red-team fix (round 20's ways below 2e2a9cb, and child phrasings
every version missed).

Two red teams attacked round 20 (rt20_0: facts-level differential against
2e2a9cb with adult34, adult18 and past_minor replies, and a miss probe;
rt20_1: over-refusal against 6fe5892):

1. HIGH: "I'm a tutor. I'm in fourth grade." lost its minor line under an
   adult reply with an age: a tutor counts as teaching only where round
   17's teacher words (_FP_TEACH_OLD_RE) are near too.
2. HIGH: the foot-mark height rule took "I am 5’" and "I'm only just 5’":
   it now takes only the ALLOWLIST's own shape ("I'm", "Im", "I m", at
   most one word before the number).
3. HIGH: "Iam 34." set the adult flag (the wider _FP_I fed _FP_ADULT_AGE_RE):
   the adult age keeps round 17's "I'm" / "I am" prefix (_FP_I_ADULT).
4. HIGH: the ALLOWLIST's readings applied under a facts reply that marks
   the person a past minor: now only for an explicit adult age (18 or
   more) the reply never marks a past minor (`allow`, _fp_minor_hits); a
   past minor gets 2e2a9cb's readings alone. Round 19's invariant now has
   adult18 and past_minor replies.
5. MEDIUM: child phrasings every version missed ("I'm 9 years of age", "I'm
   9 too", "I'm 9 (nearly 10)", "I'm 10 next month", "I turned 9 today",
   "I'm Lily, 9.", "I'm in 5th.", "I'm a sophomore", ...): add-only
   patterns in both tiers.
6. MEDIUM (over-refusal against 6fe5892): a decimal or a clock time with
   a unit ("I'm 9.5 months pregnant", "I'm 9:30 late again") is not an age,
   only under the ALLOWLIST's replies (an adult with an age).
7. LOW: plural teaching ("we've taught there for years", "we teach at the
   same school") counts as teaching (the ALLOWLIST's teach entry).

Unchanging rule: sexual content involving minors is always refused; the
backstop only adds caution, never lowers what the facts reply said.
"""
from __future__ import annotations

import pytest

from .test_sound_story_round13 import _entry, judge  # noqa: F401
from .test_sound_story_round17 import _old
from .test_sound_story_round19 import BASE, _allowed, _caution, _less

T = "stepdad"


def _nar(status, age, past=False):  # noqa: ANN001, ANN202
    return {"characters": [_entry(T, "adult", None, "adult")],
            "narrator": _entry(None, status, age, status if status != "unknown" else None, past)}


def _cast(status, age, past=False):  # noqa: ANN001, ANN202
    return {"characters": [_entry("TOM", "adult", None, "adult"),
                           _entry("LILY", status, age, status if status != "unknown" else None, past)]}


REPLIES = {
    "adult34": (_nar("adult", 34), _cast("adult", 34)),
    "adult18": (_nar("adult", 18), _cast("adult", 18)),
    "past_minor": (_nar("adult", 34, True), _cast("adult", 34, True)),
    "adult_no_age": (_nar("adult", None), _cast("adult", None)),
    "unknown": (_nar("unknown", None), _cast("unknown", None)),
    "missing": ({"characters": [_entry(T, "adult", None, "adult")]},
                {"characters": [_entry("TOM", "adult", None, "adult")]}),
}
AGED = ("adult34", "adult18")
WHO = ["narrator", "cast"]


def _marked(c) -> bool:  # noqa: ANN001
    """The sexual part is marked: refused, or a minor in the facts line or
    the sexual part's own line."""
    return c == "refused" or "line" in c or "P1" in c


def _both(text: str):  # noqa: ANN202
    from app.services import minor_check

    return minor_check.first_person_age(text), minor_check.first_person_age(text, broad=True)


def _check_floor(who: str, reply: str, text: str) -> tuple:
    """(2e2a9cb's caution, ours) for `text` under `reply`; never less."""
    from app.services import minor_check

    rep = REPLIES[reply][0 if who == "narrator" else 1]
    before = _caution(_old(BASE), who, rep, text)
    after = _caution(minor_check, who, rep, text)
    return before, after


# ---------- 1. a tutor ----------

TUTOR = ["I'm a tutor. I'm in fourth grade.", "I am a tutor. I am in year 6.", "I'm a tutor. I'm in grade 4.",
         "I'm also a tutor. I'm in middle school.", "I'm a maths tutor. I'm in year 6 and I live with my stepdad.",
         "I'm a tutor. I'm a fifth grader.", "I'm a tutor. I'm in year 6 of primary."]


@pytest.mark.parametrize("text", TUTOR)
def test_1_a_tutor_is_not_teaching_by_itself(text):
    assert all(x is not None for x in _both(text))
    assert not _allowed(text)


@pytest.mark.parametrize("who", WHO)
@pytest.mark.parametrize("reply", list(REPLIES))
def test_1_a_tutor_keeps_the_minor_line(client, judge, who, reply):  # noqa: F811
    for text in TUTOR:
        before, after = _check_floor(who, reply, text)
        assert not _less(before, after), (text, before, after)
        assert _marked(after), (text, after)


def test_1_a_tutor_with_round_17_words_still_teaches():
    from app.services import minor_check

    # round 17's own words (students) make a tutor a teacher, as in 2e2a9cb
    assert minor_check.first_person_age("I'm a tutor with twenty students. I'm in year 6 this year.") is None


# ---------- 2. the foot mark: only the ALLOWLIST's shape ----------

FOOT_CHILD = ["I am 5’.", "I'm only just 5’.", "I am 4’.", "I am 7’.", "I am 5’ and he towers over me.",
              "I'm only just 4’.", "I'm only just 7’ and he towers over me.", "I am now about 5’."]
FOOT_ADULT = ["I'm 5’.", "I'm only 5’ and he towers over me.", "Im 5’.", "I m 7’.", "I'm just 4’."]


@pytest.mark.parametrize("text", FOOT_CHILD)
def test_2_a_foot_mark_outside_the_allowlist_shape_is_an_age(text):
    assert all(x is not None for x in _both(text))
    assert not _allowed(text)


@pytest.mark.parametrize("text", FOOT_ADULT)
def test_2_the_allowlist_shape_is_a_height_only_with_allow(text):
    from app.services import minor_check

    assert _allowed(text)
    assert minor_check.first_person_age(text) is None
    assert minor_check.first_person_age(text, allow=False) is not None
    assert minor_check.first_person_age(text, broad=True) is not None


@pytest.mark.parametrize("who", WHO)
@pytest.mark.parametrize("reply", list(REPLIES))
def test_2_foot_marks_keep_the_floor(client, judge, who, reply):  # noqa: F811
    for text in FOOT_CHILD:
        before, after = _check_floor(who, reply, text)
        assert not _less(before, after), (text, before, after)
        assert _marked(after), (text, after)


# ---------- 3. "Iam 34" is no adult signal ----------

IAM = ["Iam 34. I'm in year 6.", "Iam 21. I am in year 6.", "I'm in year 6. Iam 21.", "I'm in year 3 now. Iam 34.",
       "I'm in year 6 and I live with my stepdad. Iam 34."]


def test_3_the_adult_age_keeps_round_17s_prefix():
    from app.services import minor_check

    assert not minor_check._FP_ADULT_AGE_RE.search("Iam 34.")
    assert minor_check._FP_ADULT_AGE_RE.search("I am 34.") and minor_check._FP_ADULT_AGE_RE.search("I'm 34.")
    # the wider prefix still adds minor hits
    assert all(x is not None for x in _both("Iam 9."))


@pytest.mark.parametrize("text", IAM)
def test_3_iam_adult_age_does_not_drop_a_school_year(text):
    assert all(x is not None for x in _both(text))


@pytest.mark.parametrize("who", WHO)
@pytest.mark.parametrize("reply", list(REPLIES))
def test_3_iam_keeps_the_floor(client, judge, who, reply):  # noqa: F811
    for text in IAM:
        before, after = _check_floor(who, reply, text)
        assert not _less(before, after), (text, before, after)
        assert _marked(after), (text, after)


# ---------- 4. the ALLOWLIST: an adult age, never a past minor ----------

ALLOWED = ["How far along? I'm 9.", "How far along are you? I'm nine.", "I'm 5’ and he towers over me.", "I m 5’.",
           "I have taught at Pine Street. I'm in fourth grade.", "What hole are you on? I'm nine.",
           "Your handicap? I'm nine.", "We both teach there. I'm in year 6.", "I've taught for years. I'm in grade 4."]


@pytest.mark.parametrize("who", WHO)
@pytest.mark.parametrize("reply", list(REPLIES))
def test_4_allowlisted_texts_keep_2e2a9cb_outside_an_adult_age(client, judge, who, reply):  # noqa: F811
    for text in ALLOWED:
        assert _allowed(text)
        before, after = _check_floor(who, reply, text)
        if reply in AGED:
            # an explicit adult age, never a past minor: sent
            assert after == frozenset(), (text, after)
        else:
            assert not _less(before, after), (text, before, after)
            assert _marked(after), (text, after)


def test_4_the_past_minor_reply_reads_2e2a9cb_alone(client, judge):  # noqa: F811
    from app.services import minor_check

    for text in ALLOWED:
        f = _caution(minor_check, "narrator", REPLIES["past_minor"][0], text)
        assert f != frozenset() and "P1" in f, (text, f)
        assert minor_check.first_person_age(text, allow=False) is not None


# ---------- 5. child phrasings, both tiers ----------

CHILD = {
    "I'm 9 years of age.": 9, "I'm nine years of age.": 9, "I'm 9 too.": 9, "I'm 9 already.": 9,
    "I'm 9 as well.": 9, "I'm 9 lol.": 9, "I'm 9 haha.": 9, "I'm 9 (nearly 10).": 9, "I'm 9 years.": 9,
    "I'm 9 y.o.": 9, "I'm a 9yo girl.": 9, "I'm nine-and-a-half.": 9, "I'm 9 1/2.": 9,
    "I'm 9 years and 3 months old.": 9, "I'm 9 going on 10.": 9, "I'm nine going on ten.": 9,
    "I'm 10 next month.": 9, "I'm ten in two weeks.": 9, "I turned 9 today.": 9, "I'm a girl of nine.": 9,
    "I'm a boy of nine.": 9, "I'm Lily, 9.": 9, "I'm in 5th.": 10, "I'm in fourth.": 9, "I'm a sophomore.": 15,
}


@pytest.mark.parametrize("text", list(CHILD))
def test_5_child_phrasings_are_hits_in_both_tiers(text):
    from app.services import minor_check

    narrow, broad = _both(text)
    assert narrow and broad and narrow[0] == broad[0] == CHILD[text]
    assert minor_check.first_person_age(text, allow=False)[0] == CHILD[text]


@pytest.mark.parametrize("text", list(CHILD))
def test_5_child_phrasings_feed_no_adult_signal(text):
    from app.services import minor_check

    assert not minor_check._FP_ADULT_AGE_RE.search(text)
    # next to a stated adult age they still add
    assert minor_check.first_person_age("I am 34. " + text) is not None


@pytest.mark.parametrize("who", WHO)
@pytest.mark.parametrize("reply", list(REPLIES))
def test_5_child_phrasings_mark_the_sexual_part(client, judge, who, reply):  # noqa: F811
    from app.services import minor_check

    for text in CHILD:
        after = _caution(minor_check, who, REPLIES[reply][0 if who == "narrator" else 1], text)
        assert _marked(after), (text, after)


@pytest.mark.parametrize("text", ["I'm a senior.", "I'm in fourth gear.", "I'm 10 minutes away.",
                                  "I'm 9 too many drinks in.", "I'm Jake, 34.", "I'm 5 1/2 feet tall.",
                                  "I'm 9 1/2 months pregnant.", "I'm 12 years into my career.",
                                  "I'm a sophomore at college."])
def test_5_adult_controls_stay_unread(text):
    assert _both(text) == (None, None)


# ---------- 6. a decimal with a unit (ALLOWLIST) ----------

DECIMAL_UNIT = ["I'm 9.5 months pregnant and huge.", "I'm 7.5 months along.", "I'm 5.5 hours into this flight.",
                "I'm 9:30 late again."]


@pytest.mark.parametrize("text", DECIMAL_UNIT)
def test_6_a_decimal_with_a_unit_is_allowlisted_and_narrow_only(text):
    from app.services import minor_check

    assert _allowed(text)
    assert minor_check.first_person_age(text) is None
    assert minor_check.first_person_age(text, allow=False) is not None
    assert minor_check.first_person_age(text, broad=True) is not None


@pytest.mark.parametrize("text", ["I'm 9.5.", "I'm 9.5 and in fourth grade.", "I'm only 9.5.", "I'm 9:30."])
def test_6_a_decimal_alone_stays_an_age(text):
    from app.services import minor_check

    assert not _allowed(text)
    assert all(x is not None for x in _both(text))
    assert minor_check.first_person_age(text, allow=False) is not None


@pytest.mark.parametrize("who", WHO)
@pytest.mark.parametrize("reply", list(REPLIES))
def test_6_decimal_units_sent_only_with_an_adult_age(client, judge, who, reply):  # noqa: F811
    for text in DECIMAL_UNIT:
        before, after = _check_floor(who, reply, text)
        if reply in AGED:
            assert after == frozenset(), (text, after)
        else:
            assert not _less(before, after), (text, before, after)
    for text in ["I'm 9.5.", "I'm 9.5 and in fourth grade."]:
        before, after = _check_floor(who, reply, text)
        assert not _less(before, after) and _marked(after), (text, after)


# ---------- 7. plural teaching (ALLOWLIST teach entry) ----------

PLURAL = ["I'm in third grade, Jake's in fifth, at the same school; we've taught there for years.",
          "I'm in year 6, Jake's in year 4; we teach at the same school."]


@pytest.mark.parametrize("text", PLURAL)
def test_7_plural_teaching_is_allowlisted_and_narrow_only(text):
    from app.services import minor_check

    assert _allowed(text)
    assert minor_check.first_person_age(text) is None
    assert minor_check.first_person_age(text, allow=False) is not None
    assert minor_check.first_person_age(text, broad=True) is not None


@pytest.mark.parametrize("text", ["We teach our little brother games. I'm in year 6.",
                                  "We've taught my sister to read. I'm in fourth grade."])
def test_7_plural_teaching_keeps_the_not_after_guard(text):
    assert all(x is not None for x in _both(text))


@pytest.mark.parametrize("who", WHO)
@pytest.mark.parametrize("reply", list(REPLIES))
def test_7_plural_teaching_sent_only_with_an_adult_age(client, judge, who, reply):  # noqa: F811
    for text in PLURAL:
        before, after = _check_floor(who, reply, text)
        if reply == "adult34":
            assert after == frozenset(), (text, after)
        elif reply not in AGED:
            assert not _less(before, after), (text, before, after)

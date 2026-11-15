"""Round 22 red-team fix (round 21's decimal reading below 2e2a9cb, child
phrasings every version missed, and over-refusals in round 21's own new
patterns).

Two red teams attacked round 21 (rt21_0: facts-level differential against
2e2a9cb and a miss probe; rt21_1: over-refusal against f5e8078/6fe5892):

1. HIGH: the decimal reading (_FP_DECIMAL_UNIT_RE) took a number written
   as a word ("I'm nine. 5 hours later he came.") and a sentence end
   ("I'm 9. 5 hours later", "I am eleven.\\n40 kg of snow fell."): now
   digits only, no space or line break around the separator; the
   ALLOWLIST entry likewise. The repros are in the round-19 corpus.
2. MEDIUM: "I'm 9 yo girl", "I'm 9 y/o girl" (NARROW), "My age: 9", "I'm
   Lily. Age: 9", "Being nine, I was scared", "I'm 9 in grade 4": add-only
   patterns, no adult signal.
3. Over-refusals in round 21's new patterns only (never a hit 2e2a9cb
   had): a ranking ("the race", "lap", "the standings") for "I'm in 5th";
   the count and rating asks for "I'm 9 too / already / lol", "I'm 9
   years.", "I'm Lily, 9.", "I'm 9 1/2", "I'm 10 in two weeks", "I turned
   9 today"; no unit after "going on 10" / "(nearly 10)"; university
   context for "I'm a sophomore" in the next sentence too (college,
   campus, majoring, dorm, "at Ohio State"); no foot or inch mark for
   "I'm Mia, 5’2”", whose name never replaces the reply's narrator.

Unchanging rule: sexual content involving minors is always refused; the
backstop only adds caution, never lowers what the facts reply said.
"""
from __future__ import annotations

import pytest

from .test_sound_story_round13 import _entry, judge  # noqa: F401
from .test_sound_story_round19 import _allowed, _caution, _less
from .test_sound_story_round21 import AGED, DECIMAL_UNIT, REPLIES, WHO, _both, _check_floor, _marked

SEX = "That night he climbed on top of me and I moaned, don't stop."


# ---------- 1. the decimal reading: one token of digits ----------

DECIMAL_CHILD = [
    "I'm nine. 5 hours later he came.", "I'm twelve: 30 minutes later we left.", "I'm twelve.2 miles from home.",
    "I am Nine: 5 hours later he came.", "I am eleven.\n40 kg of snow fell.", "I am now seven.15 minutes late.",
    "I m Nine.1 late bus.", "I am seventeen. 40 kg of snow fell.", "I am NINE. 2 miles from home.",
    "I'm 9. 5 hours later he came.", "I'm 12: 5 hours later he came.", "I am 17 . 3 stone heavier now.",
    "I'm 9.\n2 miles from home.", "I'm 9 . 5 hours later he came.", "i'm 5.\n30 minutes later.",
]


@pytest.mark.parametrize("text", DECIMAL_CHILD)
def test_1_a_word_number_or_a_sentence_end_is_an_age(text):
    from app.services import minor_check

    assert not _allowed(text)
    assert all(x is not None for x in _both(text))
    assert minor_check.first_person_age(text, allow=False) is not None


def test_1_the_decimal_reading_is_one_token():
    from app.services import minor_check

    rx = minor_check._FP_DECIMAL_UNIT_RE
    assert rx.match(".5 months pregnant") and rx.match(":30 late") and rx.match(".5 hours")
    for after in [". 5 hours", ".\n5 hours", " . 5 hours", ": 30 minutes", " .5 hours"]:
        assert not rx.match(after), after


@pytest.mark.parametrize("text", DECIMAL_UNIT)
def test_1_adult_decimals_stay_allowlisted(text):
    from app.services import minor_check

    assert _allowed(text)
    assert minor_check.first_person_age(text) is None
    assert minor_check.first_person_age(text, allow=False) is not None


@pytest.mark.parametrize("who", WHO)
@pytest.mark.parametrize("reply", list(REPLIES))
def test_1_decimal_repros_keep_the_floor(client, judge, who, reply):  # noqa: F811
    for text in DECIMAL_CHILD:
        before, after = _check_floor(who, reply, text)
        assert not _less(before, after), (text, before, after)
        assert _marked(after), (text, after)


@pytest.mark.parametrize("reply", list(REPLIES))
def test_1_cast_line_repro(client, judge, reply):  # noqa: F811
    before, after = _check_floor("cast", reply, "I'm nine. 5 hours later he came.")
    assert not _less(before, after) and _marked(after), (before, after)


# ---------- 2. child phrasings every version missed ----------

CHILD = {
    "I'm 9 yo girl.": 9, "I'm 9 y/o girl.": 9, "I'm 9 yo boy.": 9, "I'm 9 y/o kid.": 9, "My age: 9.": 9,
    "My age: nine.": 9, "I'm Lily. Age: 9.": 9, "Me? Age: 9.": 9, "Being nine, I was scared.": 9,
    "Being 9, I don't get a say.": 9, "I'm 9 in grade 4.": 9, "I'm 9 in year 4.": 9, "I'm nine in the fourth grade.": 9,
}


@pytest.mark.parametrize("text", list(CHILD))
def test_2_child_phrasings_are_hits_in_both_tiers(text):
    from app.services import minor_check

    narrow, broad = _both(text)
    assert narrow and broad and narrow[0] == broad[0] == CHILD[text]
    assert minor_check.first_person_age(text, allow=False)[0] == CHILD[text]


@pytest.mark.parametrize("text", list(CHILD))
def test_2_child_phrasings_feed_no_adult_signal(text):
    from app.services import minor_check

    assert not minor_check._FP_ADULT_AGE_RE.search(text)
    assert minor_check.first_person_age("I am 34. " + text) is not None


@pytest.mark.parametrize("who", WHO)
@pytest.mark.parametrize("reply", list(REPLIES))
def test_2_child_phrasings_mark_the_sexual_part(client, judge, who, reply):  # noqa: F811
    for text in CHILD:
        before, after = _check_floor(who, reply, text)
        assert not _less(before, after), (text, before, after)
        assert _marked(after), (text, after)


@pytest.mark.parametrize("text", ["My age: 34.", "Age: 9.", "Being 10 minutes late, I ran.",
                                  "Being 2 for 3, I smiled.", "I'm 34 in year 6.", "Being nine, she was scared.",
                                  "The wine's age: 9 years.", "I'm 9 yo."])
def test_2_adult_controls_add_nothing_new(text):
    from app.services import minor_check

    # none of round 22's patterns reads these ("I'm 9 yo." is round 18's
    # yrs form: BROAD only, as before)
    for rx, kind in minor_check._FP_AGE_RES[-4:]:
        assert not rx.search(text), (text, kind)


# ---------- 3. over-refusals in round 21's new patterns ----------

OVER = {
    "ord": ["The race leaderboard updated; I'm in 5th.", "After the first lap I'm in fourth.",
            "In the standings I'm in third.", "We're halfway through the heat. I'm in 2nd."],
    "child_tail": ["Shoe size? I'm 9 too.", "Jake wears a size 9. I'm 9 too.", "How many drinks? I'm 9 already.",
                   "On a scale of one to ten, I'm 9 lol.", "Rate the date out of ten. I'm 8 haha.",
                   "Which floor? I'm 12 as well."],
    "next": ["I'm 9 going on 10 hours without sleep.", "Shift report: I'm 9 going on 10 hours in.",
             "I'm 9 (nearly 10) weeks along.", "I'm 9 (going on 10) hours into this shift."],
    "hs_bare": ["I'm a sophomore. College is hard.", "I'm a sophomore now, at Ohio State.",
                "I'm a sophomore, majoring in chemistry.", "I'm a sophomore at Boston College.",
                "I'm a sophomore. My dorm is tiny.", "I'm a freshman on campus."],
    "named_age": ["I'm Mia, 5’2” of pure attitude.", "I'm Ana, 5’ nothing.", "Which room? I'm Ana, 12.",
                  "I'm Mia, 5″ taller in heels."],
    "frac_will_turned": ["I turned 9 today on the scoreboard.", "Laps so far: I turned 9 this morning.",
                         "Shoe size? I'm 9 1/2.", "Which floor? I'm 10 next month."],
}
OVER_ALL = [t for v in OVER.values() for t in v]
CHILD_CONTROLS = ["I'm in 5th.", "I'm in fourth.", "I'm 9 too.", "I'm 9 already.", "I'm 9 lol.", "I'm 9 years.",
                  "I'm 9 going on 10.", "I'm 9 (nearly 10).", "I'm 9 going on 10 years old.", "I'm a sophomore.",
                  "I'm a freshman.", "I'm Lily, 9.", "I'm Lily, 9 years old.", "I turned 9 today.",
                  "I'm 10 next month.", "I'm ten in two weeks.", "I'm 9 1/2.", "I'm nine-and-a-half.",
                  "I'm a sophomore. My mom drives me to school.", "I'm in 5th. Mrs Lee is my teacher."]


@pytest.mark.parametrize("text", OVER_ALL)
def test_3_adult_numbers_are_narrow_misses(text):
    from app.services import minor_check

    assert minor_check.first_person_age(text) is None
    assert minor_check.first_person_age(text, allow=False) is None


@pytest.mark.parametrize("text", CHILD_CONTROLS)
def test_3_child_controls_stay_hits(text):
    from app.services import minor_check

    assert all(x is not None for x in _both(text))
    assert minor_check.first_person_age(text, allow=False) is not None


@pytest.mark.parametrize("who", WHO)
@pytest.mark.parametrize("reply", list(REPLIES))
def test_3_over_refusals_sent_with_an_adult_age_and_never_below_2e2a9cb(client, judge, who, reply):  # noqa: F811
    for text in OVER_ALL:
        before, after = _check_floor(who, reply, text)
        assert not _less(before, after), (text, before, after)
        if reply in AGED:
            assert after == frozenset(), (text, after)


@pytest.mark.parametrize("who", WHO)
@pytest.mark.parametrize("reply", list(REPLIES))
def test_3_child_controls_mark_the_sexual_part(client, judge, who, reply):  # noqa: F811
    for text in CHILD_CONTROLS:
        before, after = _check_floor(who, reply, text)
        assert not _less(before, after), (text, before, after)
        assert _marked(after), (text, after)


# ---------- 3e. "I'm <Name>, <number>" never replaces the reply's narrator ----------

def _summary(text: str) -> str:
    from app.providers import mock_chat
    from app.services import minor_check

    minor_check.reset_cache()
    mock_chat.STORY_FACTS_REPLY = REPLIES["adult34"][0]
    try:
        f = minor_check.story_facts_detail("", windows=["I live with my stepdad by the sea. " + text, SEX],
                                           whole_text=True)
    finally:
        mock_chat.STORY_FACTS_REPLY = None
    return f.line + "\n" + f.summary_line()


@pytest.mark.parametrize("text", ["I'm Mia, 9.", "I'm Mia, 9 years old.", "I'm Lily. Age: 9."])
def test_3e_a_named_age_keeps_the_replys_adult_age(client, judge, text):  # noqa: F811
    from app.services import minor_check

    assert minor_check.first_person_age(text)[1] is None
    s = _summary(text)
    assert "an adult now (34); shown as a minor (9)" in s, s
    assert "MIA" not in s and "LILY" not in s and "unknown age" not in s, s


@pytest.mark.parametrize("text", ["I'm Mia, 5’2” of pure attitude.", "I'm Ana, 5’ nothing."])
def test_3e_a_height_after_a_name_is_nothing(client, judge, text):  # noqa: F811
    s = _summary(text)
    assert " minor" not in s and "MIA" not in s and "ANA" not in s, s


def test_3e_2e2a9cbs_own_hit_still_names(client, judge):  # noqa: F811
    from app.services import minor_check

    # an "I'm nine" hit (2e2a9cb's) still takes the name the text gives
    assert minor_check.first_person_age("I'm Mia. I'm nine.") == (9, "Mia")


@pytest.mark.parametrize("who", WHO)
def test_3e_past_minor_still_reads_2e2a9cb(client, judge, who):  # noqa: F811
    from app.services import minor_check

    rep = REPLIES["past_minor"][0 if who == "narrator" else 1]
    for text in ["I'm Mia, 9.", "I'm 9 too.", "I'm in 5th.", "I'm a sophomore."]:
        assert _marked(_caution(minor_check, who, rep, text)), text

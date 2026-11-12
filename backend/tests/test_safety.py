"""The word filter (services/safety.py): evasions still caught, and the
false positives fixed (threats need a Capitalised name; "kids" and
"explicit instructions" in separate sentences)."""
from __future__ import annotations

import pytest

from app.services import safety


@pytest.mark.parametrize("text", [
    "We kill time at the cafe",
    "murder mystery night",
    "Murder mystery night at the inn",
    "Kill the lights and dance",
    "Join our Murder Mystery Night this Friday!",
    "Kill Time at our cafe",
    "Book the Murder Mystery Dinner Party",
    "Our kids love it. Explicit instructions inside.",
    "Kids love it, explicit instructions inside",
    "A family recipe the children adore",
    "Explicit lyrics warning on the album",
])
def test_validate_script_false_positives_pass(text):
    safety.validate_script(text)


@pytest.mark.parametrize("text", [
    "kill John",
    "We will murder the Mayor tonight",
    "assassinate Smith",
    "Kill John Smith tomorrow",
    "We will Kill John",
    "teen nude pics",
    "sexual content with kids",
    "children naked on a bed",
    "loli art",
    "cp collection",
])
def test_validate_script_still_blocks(text):
    with pytest.raises(safety.UnsafeScriptError):
        safety.validate_script(text)


def test_validate_script_refuses_empty():
    with pytest.raises(safety.UnsafeScriptError):
        safety.validate_script("   ")


@pytest.mark.parametrize("text", [
    "nude teenage girl",
    "naked 14-year-old",
    "a 13 yo in lingerie",
    "sexy schoolgirl",
    "ｎｕｄｅ ｃｈｉｌｄ",              # fullwidth letters
    "nu​de child",              # hidden character inside a word
    "nаked tееn",                    # Cyrillic look-alikes
    "n4ked ch1ld",                   # digits as letters
    "little_girl nude",              # tag joiner
    "loli",
    "child porn",
    "girl, 12, topless",
])
def test_check_prompt_still_catches_evasions(text):
    with pytest.raises(safety.UnsafePromptError):
        safety.check_prompt(text)


@pytest.mark.parametrize("text", [
    "kids building a sandcastle at the beach",
    "a sexy woman on a beach at sunset",
    "kill time at the cafe, murder mystery night",
    "explicit instructions for assembling a desk with the kids",
    "breast cancer awareness walk",
])
def test_check_prompt_passes_ordinary_text(text):
    safety.check_prompt(text)


def test_check_script_keeps_family_words_and_explicit_instructions():
    safety.check_script("My daughter and I started this boutique; sexy evening dresses for women.")
    safety.check_script("Our kids love it. Explicit instructions inside.")
    with pytest.raises(safety.UnsafePromptError):
        safety.check_script("a sexy 14-year-old")


def test_signals():
    assert safety.mentions_minor("a teenage boy")
    assert not safety.mentions_minor("a grown woman")
    assert safety.is_sexual("a woman in a bikini")
    assert not safety.is_sexual("a bike ride")
    assert safety.steers_young("sexy pose in lingerie", "adult, mature, woman")
    assert not safety.steers_young("a mountain lake", "adult")


def test_hard_sexual_leaves_out_everyday_words():
    for text in ("grilled chicken breast with herbs", "Breast Cancer Awareness walk",
                 "kids splashing in swimsuits at the beach", "a swimwear ad"):
        assert safety.is_sexual(text)
        assert not safety.is_hard_sexual(text)
    assert safety.is_hard_sexual("a sexy woman on a beach")


# ---------- adults with an age under 18 (gap 2) ----------


@pytest.mark.parametrize("text", [
    "woman, 16, in a bikini",
    "nude woman (16)",
    "woman who is 16 in lingerie",
    "16 year old woman",
    "a woman in her teens",
    "lady, fifteen, sensual pose",
    "female, 14",
    "the bride (15)",
    "model, 15, topless",
    "model 15 nude",
    "Model 15 nude",
    "woman 16 nude",
    "16 woman, topless",
    "girl, 15 in a bikini",          # "in" here isn't inches
    "she is 15 in a bikini",
    "she's 14 and amazing",          # "am" inside a word isn't a time
    "baby, 9 months old",
    "nude girl in year 7",
    "she's in year 9",
    "year 9 student",
    "sweet sixteen",
    "16f, nude",
    "sixteen woman",
])
def test_ages_under_18_are_minor_mentions(text):
    assert safety.mentions_minor(text)


@pytest.mark.parametrize("text", [
    "nude 15 year old",
    "naked girl, 15",
    "she is 14 and topless",
    "loli",
    "t33n nude",
    "schoolgirl lingerie",
    "model, 15, topless",
    "Model 15 nude",
    "model 15 nude",
    "nude woman (16)",
    "woman who is 16, nude",
    "topless woman, sixteen",
    "she's 5, nude",
    "nude girl, 15 in a bedroom",
    "woman in her teens, nude",
    "nude woman, 16 years",
    "lingerie, sweet sixteen",
    "tesla model 15, nude",
    "MODEL 15 nude",
    "nude girl, 15.5",
    "naked girl, 15/16",
    "nude girl, 15'",
])
def test_minors_are_still_refused(text):
    with pytest.raises(safety.UnsafePromptError):
        safety.check_prompt(text)


# ---------- adult prompts the word filter refused (gap 7) ----------


@pytest.mark.parametrize("text", [
    "model, 5'9\", topless",
    "she's 5'2\", nude",
    "she's 5’2”, nude",
    "model, 5′9″, nude",
    "she is 5 ft 9, nude",
    "model, 1.75 m, nude",
    "woman in lingerie next to a Tesla Model 3",
    "nude woman by a Model S",
    "TESLA MODEL 3 and a woman in lingerie",
    "sexy babe 9 out of 10",
    "sexy babe, 10/10",
    "year 1 anniversary lingerie",
    "Year 2026 lingerie collection",
    "student 15 minutes late, sexy outfit",
    "she was 10 minutes late, in lingerie",
    "woman 1 and woman 2, both nude",
    "woman #2 on the left, topless",
    "person 2 in the scene, nude",
    "nude couple, 2 years together",
    "nude woman, 25 years old",
    "a woman, 20, in a bikini",
    "sexy model at 10 pm",
    "naked model, 6' tall",
    "man 2 man massage, nude",
])
def test_adult_prompts_pass_the_word_filter(text):
    safety.check_prompt(text)


def test_product_and_label_numbers_still_get_the_ai_check():
    """Not refused by the word filter, but a hard sexual word means the AI
    check always runs on them (minor_check.needs_ai)."""
    for text in ("Model 3 nude", "model 3 nude", "woman 2, nude"):
        safety.check_prompt(text)
        assert safety.is_hard_sexual(text)


def test_validate_script_refuses_cp_only_in_the_csam_sense():
    for fine in ("Cyberpunk CP 2077 sale", "CP violation notice on a door", "Our CP 500 printer is back"):
        safety.validate_script(fine)
    for bad in ("cp collection", "cp videos", "trading cp", "CP pics here"):
        with pytest.raises(safety.UnsafeScriptError):
            safety.validate_script(bad)


# ---------- avoid lists (steers_young) ----------


@pytest.mark.parametrize("negative", [
    "old photo", "old film, grainy", "old-fashioned", "old camera", "old style", "oldschool", "grainy old",
    "blurry, old photo look", "aged paper texture",
])
def test_old_things_in_an_avoid_list_dont_steer_young(negative):
    assert not safety.steers_young("nude woman on a bed", negative)


@pytest.mark.parametrize("prompt, negative", [
    ("two women kissing, nude", "men, male"),
    ("two men in bed, naked", "women"),
    ("nude man on a beach", "beard, stubble"),
    ("nude woman in lingerie", "curvy"),
])
def test_gender_and_body_words_dont_steer_young_when_the_prompt_names_an_adult(prompt, negative):
    assert not safety.steers_young(prompt, negative)


@pytest.mark.parametrize("prompt, negative", [
    ("sexy pose in lingerie", "adult, mature, woman"),
    ("nude woman on a bed", "old"),
    ("nude woman on a bed", "older, wrinkles"),
    ("nude woman on a bed", "old-looking"),
    ("nude woman on a bed", "looking old, blurry"),
    ("nude woman on a bed", "old face"),
    ("nude girl on a bed", "woman"),
    ("nude boy", "beard"),
    ("nude figure", "curvy, voluptuous"),
    ("nude woman", "milf"),
    ("lingerie shoot", "adult, woman"),
])
def test_avoid_lists_that_remove_adulthood_still_steer_young(prompt, negative):
    assert safety.steers_young(prompt, negative)


# ---------- a Cast character's card (describes_minor) ----------


@pytest.mark.parametrize("fields", [
    ("Mia", "Mia, 15"),
    ("Mia (15)", ""),
    ("Mia", "Mia, fifteen"),
    ("Mia", "looks about 15, petite"),
    ("Mia", "sweet sixteen"),
    ("Mia", "15, petite blonde"),
    ("Mia", "Mia - 14 - loves skating"),
    ("Mia", "she's 15F"),
    ("Mia", "a 15-year-old daughter"),
    ("Mia", "daughter, 15"),
    ("Mia", "in her teens"),
    ("Mia", "teenage gamer"),
    ("Mia", "barely legal"),
    ("Mia", "could pass for 16"),
    ("Mia", "19 but looks younger than 18"),
    ("Mia", "a grade 9 student"),
    ("Mia", "a woman, 16"),
    ("Mia", "born in 2012"),
    ("Mia", "Mia, 15 years"),
    ("Mia", "Mia, a kid"),
    ("Mia", "one of 3 kids at school"),
    ("Mia", "Mia, with a child's face"),
    ("Mia", "her kid sister"),
    ("Mia", "a high school student"),
    ("Mia", "the high school teacher's pet"),
    ("Mia", "goes to a middle school"),
    ("Mia", "Mia, his little girl"),
    ("Mia", "Mia, 15, mother of two kids"),
    ("Mia", "mother of a 5-year-old"),       # an age on the card counts, even someone else's
])
def test_character_cards_with_an_age_under_18_name_a_minor(fields):
    assert safety.character_minor(*fields)


@pytest.mark.parametrize("fields", [
    ("Mia", "Mia, 25"),
    ("Mia", "her father's daughter"),
    ("Mia", "a stepdaughter who runs the family bakery"),
    ("Mia", "mother of two kids"),
    ("Mia", "a mom of 3 who loves yoga"),
    ("Mia", "has two kids and a dog"),
    ("Mia", "Mia, 5'9\", size 8"),
    ("Mia", "Mia, 170 cm, 2 kids"),        # her two kids: a count, not her age
    ("Mia", "single mom, three children"),
    ("Mia", "drives a Tesla Model 3"),
    ("Mia", "born in 1990, 10/10 smile"),
    ("Mia", "Mia, 9 out of 10 fans agree"),
    ("Mia", "Mia, 24/7 online"),
    ("Mia", "Mia, 1 of 3 sisters"),
    ("Mia", "Mia, 15 years of experience as a nurse"),
    ("Mia", "a 32-year-old nurse; size 8"),
    ("Mia", "Mia, #1 fan"),
    ("Mia", "Mia, $15 an hour"),
    ("Mia", "a high school teacher, 34"),
    ("Mia", "teaches maths at a middle school"),
    ("Mia", "a teacher at a high school"),
    ("Agent 7", "a spy in her thirties"),
])
def test_character_cards_without_a_minor_pass(fields):
    assert not safety.character_minor(*fields)

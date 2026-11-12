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
    for fine in ("Cyberpunk CP 2077 sale", "CP violation notice on a door", "Our CP 500 printer is back",
                 "Cyberpunk CP 2077 sale. Kids love it."):
        safety.validate_script(fine)
        safety.check_script(fine)
    for bad in ("cp collection", "cp videos", "trading cp", "CP pics here"):
        with pytest.raises(safety.UnsafeScriptError):
            safety.validate_script(bad)


@pytest.mark.parametrize("text", [
    "DM me for cp of young girls.", "Got cp? Teens only, message me.", "New cp drop with kids, link in bio.",
    "Send cp of little girls to my inbox.", "Fresh cp, preteen, follow for more.",
])
def test_cp_next_to_a_child_word_is_refused_everywhere(text):
    for check in (safety.validate_script, safety.check_script, safety.check_prompt):
        with pytest.raises(safety.UnsafeScriptError):
            check(text)


# ---------- a number after an adult word (review: false refusals) ----------


@pytest.mark.parametrize("text", [
    "topless woman, one hand in her hair", "nude woman, three-quarter view", "nude woman, one eye closed",
    "woman, 2 braids, lingerie", "nude woman, 3 tattoos on her back", "woman, 3 outfits, lingerie lookbook",
    "guy, 6 pack abs, shirtless, sexy", "man, 8-pack, naked", "two women, 1 blonde and 1 brunette, kissing, nude",
    "nude couple, man and woman, 2 shot", "nude woman, 1 light source", "woman, 1/250s, topless",
    "my wife of 10 years, lingerie", "girlfriend of 2 years, nude selfie",
    "lingerie fashion show with 12 female models", "a crew of 11 male dancers, shirtless",
    "top 10 model posing nude", "sexy woman, one hand on her hip", "naked woman: one leg raised",
    "nude woman, 3/4 view", "topless man, 5 o'clock shadow", "nude woman — 3 angles",
    "erotic shot of a woman, 3 lights, softbox", "a woman, one hand on her hip, in lingerie",
    "girl, 9 months pregnant, nude", "nude woman, 9 months pregnant", "lingerie set (2), nude",
])
def test_counts_and_labels_after_an_adult_word_pass(text):
    safety.check_prompt(text)
    assert not safety.mentions_minor(text)


@pytest.mark.parametrize("text", [
    "Ladies, 3 tips to drive him wild. Tonight I strip for you.", "Guys, 5 signs she wants you naked.",
    "Ladies, 2 for 1 on lingerie this weekend!", "My wife of 10 years, tonight she strips for you.",
    "Ladies, 5 ways to look sexy for date night.", "Ladies, 10 ways to look sexy tonight.",
    "Hey guys, three reasons this sexy cologne sells out every week.", "Women, two words: sexy confidence...",
    "Hey ladies! 2 words: take it off. Naked Friday.",
])
def test_counts_after_an_adult_word_pass_in_scripts(text):
    safety.check_script(text)


@pytest.mark.parametrize("text", [
    # an adult word with a teen age
    "woman, 16, in a bikini, nude", "lady, fifteen, sensual pose, nude", "woman 16 nude", "16 woman, topless",
    "sixteen woman, nude", "a woman, 15 and nude", "woman of 16 years old, nude", "nude woman, 5'2\", 15",
    # a height before the age; ages the duration rule used to skip
    "nude girl, 5'2\", 15", "nude girl, 5'2, 15", "nude girl, 5'2\", fifteen", "naked girl, 15 years at most",
    "nude girl who is 15 years on this earth",
    # next to a sexual word: a school year, a label number that ends the phrase, a lone teen number
    "topless, Year 10", "nude Year 8 cutie", "nude Model 9", "topless model 9", "nude babe 9", "naked person 9",
    "nude girl, petite, 15", "Emma, 15, nude", "topless cheerleader, 15", "nude woman, 9", "woman, 9, nude",
    # babies and children in months, weeks or days
    "nude baby, 9 months", "naked baby 6 months", "naked baby, 2 weeks", "nude baby (6 months)",
    "a baby, 9 months, nude", "naked girl, 9 months", "nude boy 10 months",
])
def test_more_minors_are_refused(text):
    with pytest.raises(safety.UnsafePromptError):
        safety.check_prompt(text)


def test_a_lone_teen_number_or_school_year_forces_the_filter_with_everyday_words():
    for text in ("Emma, 15, bikini", "Year 10, bikini", "woman, 16, in a bikini"):
        assert safety.mentions_minor(text)
    for text in ("bikini, 15 colours", "Year 10, maths homework", "lingerie set, size 12"):
        assert not safety.mentions_minor(text)


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
    ("nude woman", "body hair, pubic hair"),
    ("nude woman lying on a bed", "wrinkled sheets, blurry"),
    ("sexy girl, 25, in lingerie on a bed", "men in background, crowds"),
    ("nude model in studio", "body hair, stubble"),
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
    ("nude woman on a bed", "wrinkled skin"),
    # adult ages, "old" about a body, "or", and body words with a young body in the prompt
    ("nude woman, petite, flat chest, smooth skin", "21 year old, 25 year old"),
    ("nude woman, petite, flat chest, smooth skin", "older looking skin"),
    ("nude woman, petite, flat chest, smooth skin", "older-looking body"),
    ("nude woman, petite, flat chest, smooth skin", "old or aged"),
    ("nude woman, petite, flat chest, smooth skin", "curvy, voluptuous, older looking skin"),
    ("nude woman, petite, flat chest, smooth skin", "curvy, voluptuous, pubic hair, body hair"),
    ("nude woman, petite, flat chest, smooth skin", "over 18"),
    ("nude woman, petite, flat chest, smooth skin", "18+"),
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
    ("Mia", "a 5-year-old"),
    ("Mia", "Mia: 15"),
    ("Mia Rose", "Mia: 15"),
    ("Mia", "Age: 15"),
    ("Mia", "Mia (2)"),                      # a label or an age: the words can't tell, so it counts
    ("Mia", "Mia, 9"),
    ("Mia", "Mia; 15; blonde"),
    ("Mia", "Mia, his teenage daughter"),    # "his": about Mia
    ("Mia", "Mia, his 16-year-old daughter"),
    ("Mia", "her 15-year-old self"),
    ("Mia", "in grade 8"),
    ("Mia", "Mia, ex-teen model, 15"),       # the past is left out, her age now isn't
    ("Mia", "mother of a teenage son; she is 15"),
    ("Mia", "Kids: 2, she is 14"),
    ("Mia", "Mia is in her early teens"),
    ("Mia", "a high school senior"),
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
    # Fields, measurements and counts (finding: a comp card refused a tick).
    ("Mia", "Model. Height: 5'9\", Bust: 34, Waist: 24, Hips: 35, Dress: 4, Shoe: 8"),
    ("Mia", "Dress size: 6"), ("Mia", "Shoe size: 7"), ("Mia", "Kids: 2"), ("Mia", "Siblings: 3"),
    ("Mia", "Tattoos: 3"), ("Mia", "Rating: 9"), ("Mia", "Mia, 30. Number: 7"), ("Mia", "Cup: D, size: 4"),
    ("Mia", "Piercings: 3, tattoos: 4"), ("Mia", "Children: 2"), ("Mia", "Lucky number: 7"), ("Mia", "Episode: 3"),
    ("Mia", "Mia, one in a million"), ("Mia", "Mia - one with nature"), ("Mia", "Mia, 5 and a half feet tall"),
    ("Mia", "Mia, 34 - 12 years as a nurse"), ("Mia", "Mia, 30, 10 years dancing"), ("Mia", "Pianist (Grade 8)"),
    ("Mia", "Lives in a Grade 2 listed cottage"), ("Mia", "Top 10 model"),
    # Other people's ages and the character's past: left to the AI check.
    ("Jane", "Jane, 42, mother of a 15-year-old"), ("Mia", "mother of a 5-year-old"),
    ("Mia", "Mother of a teenage son"), ("Mia", "Her 16th birthday was 20 years ago."),
    ("Mia", "Mia, 30, started modelling in her teens"), ("Mia", "Mia, 30, ex-teen model"),
    ("Mia", "Former teen pop star, now 32"), ("Mia", "Married her high school sweetheart; 38"),
    ("Mia", "A high school English teacher, 41"), ("Mia", "Nanny, 24, works with kids"),
    ("Mia", "Kindergarten teacher, 30"), ("Mia", "Teaches 5th grade, 30"), ("Mia", "Single mom with a kid"),
    ("Mia", "Single mom, 1 kid"), ("Mia", "Empty-nester with grown-up kids"), ("Mia", "Youth pastor, 38"),
    ("Mia", "In her youth she was a dancer"),
])
def test_character_cards_without_a_minor_pass(fields):
    assert not safety.character_minor(*fields)


def test_a_refused_card_says_which_words():
    assert safety.character_minor_reason("Mia", "Mia, 15, blonde") == "Mia, 15"
    assert safety.character_minor_reason("Mia (15)", "") == "Mia (15)"
    assert safety.character_minor_reason("Mia", "a petite gamer", "", "in her teens") == "in her teens"
    assert safety.character_minor_reason("Mia", "a painter in her thirties") is None

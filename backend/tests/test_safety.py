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

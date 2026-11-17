"""A clean deployment's story check judges the text the voice gets (#57
review, finding 11).

check_story's explicit-content check (clean deployment) read each line as
typed, but the voice guard sends story_script.normalize(...) of it (NFKC:
"𝒻𝓊𝒸𝓀" and fullwidth "ｆｕｃｋ" are plain "fuck") and, under the pass,
doesn't ask the explicit check again. The check now judges the normalised
text: the same string the voice gets.
"""
from __future__ import annotations

import pytest

from app.providers import mock_chat
from app.services.explicit_check import ExplicitContentError

from .story_helpers import checked
from .test_adult_features_off import _clean

MATH = "\U0001d4bb\U0001d4ca\U0001d4b8\U0001d4c0"  # 𝒻𝓊𝒸𝓀
FULL = "ｆｕｃｋ"


def _ascii_judge(self, text):  # noqa: ANN001, ANN202
    """A content check that reads plain letters only, as a model given the
    obfuscated form may not: explicit for the plain word."""
    mock_chat.EXPLICIT_CALLS.append(text)
    if "fuck" in text.lower():
        return {"explicit": True, "reason": "Sexual language."}
    return {"explicit": False, "reason": "Nothing sexual."}


@pytest.fixture
def clean(client, monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setattr(mock_chat.MockChatProvider, "classify_explicit", _ascii_judge)
    mock_chat.EXPLICIT_CALLS.clear()
    yield
    monkeypatch.delenv("ADULT_FEATURES")
    from .test_adult_features_off import _reset

    _reset()


@pytest.mark.parametrize("word", [MATH, FULL])
def test_an_obfuscated_line_is_judged_as_the_voice_gets_it(clean, word):
    story = f"MIA: Come here, I want you to {word} me tonight.\nNARRATOR: The door closed."
    with pytest.raises(ExplicitContentError):
        checked(story)
    asked = "\n".join(mock_chat.EXPLICIT_CALLS)
    assert "fuck me tonight" in asked
    assert word not in asked


@pytest.mark.parametrize("word", [MATH, FULL])
def test_obfuscated_instructions_are_judged_as_sent(clean, word):
    story = "MIA: Come here.\nNARRATOR: The door closed."
    with pytest.raises(ExplicitContentError):
        checked(story, instructions={0: f"say it like you want to {word}"})
    assert word not in "\n".join(mock_chat.EXPLICIT_CALLS)


def test_a_plain_story_still_passes(clean):
    doc, ctx = checked("MIA: Come here, the soup is ready.\nNARRATOR: The door closed.")
    assert ctx
    assert all(MATH not in c and FULL not in c for c in mock_chat.EXPLICIT_CALLS)

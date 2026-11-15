"""A speech cue retaken with new delivery instructions is new audio."""
from __future__ import annotations

from app.services import sound_estimate
from app.services.story_script import Item


def _line(instructions: str = "") -> Item:
    return Item(kind="speech", key="l1", speaker="NARRATOR", text="Hello there.",
                instructions=instructions)


def test_instructions_change_the_speech_fingerprint():
    plain = sound_estimate.cue_fingerprint(_line())
    soft = sound_estimate.cue_fingerprint(_line("whisper, slower"))
    softer = sound_estimate.cue_fingerprint(_line("whisper, much slower"))
    assert len({plain, soft, softer}) == 3


def test_same_instructions_keep_the_fingerprint():
    assert (sound_estimate.cue_fingerprint(_line("whisper"))
            == sound_estimate.cue_fingerprint(_line("whisper")))

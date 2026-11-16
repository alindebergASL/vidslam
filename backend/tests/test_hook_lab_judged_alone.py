"""Hook lab: the model judges an opening on its own words, as heard.

#57 review, finding 1: check_opening asked the model once, about a text
whose first line was the opening in front of the WHOLE original script,
including the first sentence the opening replaces (never heard). That
sentence can clear the opening: "Lily is my 30-year-old wife." in front of
"Lily wears her pigtails and braces. Tonight she wants sex with me." Each
text that can be heard is now judged as its own text, one call each.

Finding 2: an "ok" for the opening was recorded as if the model had read
it alone (vouch_ok), so the same sentence later skipped the model as a new
voiceover or lip-sync script. Nothing is vouched for any more.
"""
from __future__ import annotations

import pytest

from .test_hook_lab import _db
from .test_hook_lab_heard import _post, _spliced_base
from .test_sound_story_round11 import _Inner, _mode

LILY_BASE = "Lily is my 30-year-old wife. Tonight she wants sex with me."
LILY_HOOK = "Lily wears her pigtails and braces."
LILY_OPENING = "Lily wears her pigtails and braces. Tonight she wants sex with me."
REPLACED = "30-year-old"


def _recording(monkeypatch) -> list[str]:
    """A classifier that records every text it reads, and is misled the way
    the review showed: it refuses the opening read on its own, and clears it
    when the replaced sentence is beside it."""
    from app.providers import mock_chat

    seen: list[str] = []

    def judge(self, text):  # noqa: ANN001, ANN202
        seen.append(text.removeprefix("Prompt: "))
        bad = "pigtails" in text and "sex" in text and REPLACED not in text
        return {"minor_sexual": bad, "reason": "recording"}

    monkeypatch.setattr(mock_chat.MockChatProvider, "classify_minor", judge)
    return seen


@pytest.mark.parametrize("mode", ["adult", "default", "clean"])
def test_the_lily_opening_is_judged_without_the_sentence_it_replaces(auth_client, monkeypatch, mode):
    from app import models
    from app.services import hook_lab, minor_check

    _mode(monkeypatch, mode)
    seen = _recording(monkeypatch)
    pid, base_id = _spliced_base(auth_client, LILY_BASE)
    minor_check.reset_cache()
    seen.clear()
    r = _post(auth_client, pid, base_id, LILY_HOOK)
    assert r.status_code == 422, r.text
    assert "can't be used" in r.json()["detail"]
    # The published opening was read as its own text, and no text the model
    # read has the replaced sentence beside the opening.
    assert LILY_OPENING in seen
    assert not [t for t in seen if "pigtails" in t and REPLACED in t]

    # The refusal is the opening's own (remembered for that text alone), and
    # it holds directly, with the window where the original first sentence ends.
    assert minor_check.cached(minor_check.text_hash(LILY_OPENING)) == "minor_sexual"
    seen.clear()
    db = _db()
    try:
        base = db.get(models.Render, base_id)
        window = hook_lab.find_hook_window(base, LILY_BASE)
        with pytest.raises(hook_lab.HookLabError):
            hook_lab.check_opening(db, base, LILY_HOOK, window)
    finally:
        db.close()
    assert not [t for t in seen if "pigtails" in t and REPLACED in t]


def test_each_heard_form_is_its_own_call(auth_client, monkeypatch):
    """One call per text the model reads: the script the opening makes, and
    each form heard with the window inside the original first sentence."""
    from app.providers import mock_chat
    from app.services import minor_check

    from .test_hook_lab_heard import _window_at

    seen: list[str] = []

    def judge(self, text):  # noqa: ANN001, ANN202
        seen.append(text.removeprefix("Prompt: "))
        return {"minor_sexual": False, "reason": "ok"}

    monkeypatch.setattr(mock_chat.MockChatProvider, "classify_minor", judge)
    script = "My wife is thirty and tonight she dances for you! Follow for more."
    pid, base_id = _spliced_base(auth_client, script)
    minor_check.reset_cache()
    seen.clear()
    hook = "She still wears braces and"
    assert _post(auth_client, pid, base_id, hook, _window_at(base_id, "tonight")).status_code == 201
    assert all("\n" not in t and t.count(hook) == 1 for t in seen)
    assert f"{hook}. Follow for more." in seen  # the script it makes
    assert f"{hook} tonight she dances for you! Follow for more." in seen  # heard at the window
    assert f"{hook} {script}" in seen  # heard when the voiceover picks up at the first word
    assert len(seen) == len(set(seen))


# ---------- finding 2: nothing is vouched for ----------


def _drafted(auth_client, hook: str) -> None:
    """A hook-lab draft of `hook` (check_opening ran on it); the calls it
    made are then forgotten."""
    from app.providers import mock_chat
    from app.services import minor_check

    pid, base_id = _spliced_base(auth_client, "Stop scrolling now! Here is the rest of it. And one more line.")
    minor_check.reset_cache()
    r = _post(auth_client, pid, base_id, hook)
    assert r.status_code == 201, r.text
    mock_chat.MINOR_CALLS.clear()


@pytest.mark.parametrize("mode", ["adult", "default", "clean"])
@pytest.mark.parametrize("where", ["check_or_422", "tts", "lipsync"])
def test_a_drafted_hook_still_costs_a_call_elsewhere(auth_client, monkeypatch, mode, where):
    from app.providers import mock_chat, registry
    from app.services import minor_check

    _mode(monkeypatch, mode)
    hook = f"A brand new opening for {where} in {mode} mode."
    _drafted(auth_client, hook)
    if where == "check_or_422":
        minor_check.check_or_422(hook, kind="script")  # a new voiceover (API)
    elif where == "tts":
        registry.GuardedTTSProvider(_Inner()).synthesize(text=hook, voice_id="v")  # pipeline / TTS guard
    else:
        registry.GuardedAvatarProvider(_Inner()).submit_avatar(script=hook)  # a lip-sync script
    assert mock_chat.MINOR_CALLS == [f"Prompt: {hook}"]


def test_the_hook_alone_is_judged_before_its_voice_is_made(auth_client, monkeypatch):
    """Make: the opening alone (what synth_hook voices) is asked about in
    the precheck, by the model, before anything is paid for."""
    from app import models
    from app.providers import mock_chat
    from app.services import minor_check

    from .test_hook_lab import _create, _get, _make

    seen: list[str] = []

    def judge(self, text):  # noqa: ANN001, ANN202
        seen.append(text.removeprefix("Prompt: "))
        return {"minor_sexual": text.removeprefix("Prompt: ") == "Alone it is refused.", "reason": "probe"}

    monkeypatch.setattr(mock_chat.MockChatProvider, "classify_minor", judge)
    pid, base_id = _spliced_base(auth_client, "Stop scrolling now! Here is the rest of it. And one more line.")
    minor_check.reset_cache()
    made = _create(auth_client, pid, base_id, [{"hook_text": "Alone it is refused.", "style": "custom"}])
    assert "Alone it is refused." not in seen  # the draft judges the script it makes
    vid = made["variants"][0]["id"]
    assert _make(auth_client, pid, [vid]).status_code == 202
    v = _get(models.HookVariant, vid)
    assert v.status == "failed" and "refused" in v.error and not v.audio_path
    assert "Alone it is refused." in seen

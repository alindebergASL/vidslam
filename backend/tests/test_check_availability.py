"""The safety check's availability (#57 review, finding 5).

Every script, lip-sync, sound and music prompt asks the model and waits
when it can't run (fail closed: kept). But:

(a) the app's own sound and music presets (fixed text in code) are vetted
    once, here, and need no live call; only their exact text counts;
(b) a 429, a 5xx or a timeout is tried once more after a short wait;
(c) a 402 or 403 (OpenRouter credit or a key's limit) says so, not "check
    the planner model".
"""
from __future__ import annotations

import httpx
import pytest
import respx

from app.providers import mock_chat
from app.services import minor_check, safety

from .test_ambient_spend_music import _scene
from .test_minor_check import OR, _real_planner, _reply

SCRIPT = "Welcome back to the channel. Today we bake a lemon loaf together."


def _presets() -> list[str]:
    from app.services.ambient import preset_audio_prompts

    return preset_audio_prompts()


def _clean(monkeypatch) -> None:
    from .test_adult_features_off import _clean as clean

    clean(monkeypatch)


# ---------- (a) vetted presets ----------


def test_every_preset_is_vetted_and_nothing_else_is():
    """A new or changed preset (or a policy bump) fails here until its text
    is read and its hash added to services/vetted_audio.py."""
    from app.services import vetted_audio

    presets = _presets()
    assert len(presets) >= 15 and len(set(presets)) == len(presets)
    assert {vetted_audio.vetted_hash(t) for t in presets} == set(vetted_audio.VETTED)


@pytest.mark.parametrize("text", _presets())
def test_each_preset_passes_the_vetting(text):
    """What vetting a preset means: no minor, nothing sexual or marked, and
    the checks (word filter, mock judge, mock content check) pass it."""
    from app.services import story_script

    safety.check_prompt(text)
    assert not safety.mentions_minor(text) and not safety.mentions_minor_wide(text)
    assert not safety.is_sexual(text) and not safety.is_hard_sexual(text)
    assert not minor_check.minor_hint(text) and not minor_check.sexual_any(text)
    assert story_script.safety_text(text, prose=True) == story_script.normalize(text)
    assert mock_chat.mock_minor_verdict(text)["minor_sexual"] is False


@pytest.mark.parametrize("text", _presets())
def test_a_preset_needs_no_live_call(text):
    from app.providers.registry import audio_guard

    mock_chat.MINOR_REPLY = TimeoutError("the check is down")
    audio_guard(text)
    assert mock_chat.MINOR_CALLS == []


@pytest.mark.parametrize("variant", [
    "{} with children giggling",
    "{}, and a little girl moaning",
    "kids nearby, {}",
    "{}.",
    "{} ",  # stripped: the same text (vetted), checked below separately
])
def test_only_the_exact_preset_text_is_vetted(variant):
    from app.providers.registry import audio_guard

    text = variant.format(_presets()[0])
    mock_chat.MINOR_REPLY = TimeoutError("the check is down")
    if text.strip() == _presets()[0]:
        audio_guard(text)
        assert mock_chat.MINOR_CALLS == []
        return
    with pytest.raises(safety.UnsafePromptError):
        audio_guard(text)


def test_a_preset_in_another_case_is_asked():
    from app.providers.registry import audio_guard

    mock_chat.MINOR_REPLY = {"minor_sexual": False, "reason": "ok"}
    audio_guard(_presets()[0].upper())
    assert len(mock_chat.MINOR_CALLS) == 1


def test_a_preset_with_lyrics_joined_is_asked():
    from app.providers.registry import get_music

    music = [t for t in _presets() if "music" in t or "piano" in t][0]
    mock_chat.MINOR_REPLY = TimeoutError("the check is down")
    with pytest.raises(safety.UnsafePromptError):
        get_music().generate(prompt=music, duration_seconds=10, settings={"lyrics": "la la la"})


def test_a_preset_music_part_needs_no_live_call():
    from app.providers.registry import get_music, music_part_prompt

    music = [t for t in _presets() if "music" in t][0]
    mock_chat.MINOR_REPLY = TimeoutError("the check is down")
    get_music().generate(prompt=music_part_prompt(music, 1, 3), duration_seconds=10)
    assert mock_chat.MINOR_CALLS == []


def test_preset_beds_are_made_while_the_check_is_down(auth_client):
    sid = _scene(auth_client, sound="rain", music_mood="spa")
    mock_chat.MINOR_REPLY = TimeoutError("the check is down")
    assert auth_client.post(f"/api/ambient/{sid}/sound").status_code == 200
    assert auth_client.post(f"/api/ambient/{sid}/music").status_code == 200
    assert mock_chat.MINOR_CALLS == []


def test_a_custom_bed_still_waits_while_the_check_is_down(auth_client):
    sid = _scene(auth_client, sound="rain", sound_prompt=_presets()[0] + " and a dog barking")
    mock_chat.MINOR_REPLY = TimeoutError("the check is down")
    r = auth_client.post(f"/api/ambient/{sid}/sound")
    assert r.status_code == 422, r.text


def test_clean_deployment_preset_needs_no_content_check_call(monkeypatch):
    from app.providers.registry import audio_guard
    from app.services import explicit_check
    from app.services.pipeline import check_audio_prompt

    _clean(monkeypatch)
    assert explicit_check.active()
    mock_chat.EXPLICIT_REPLY = TimeoutError("the check is down")
    mock_chat.MINOR_REPLY = TimeoutError("the check is down")
    for text in _presets():
        check_audio_prompt(text)
        audio_guard(text)
    assert mock_chat.EXPLICIT_CALLS == [] and mock_chat.MINOR_CALLS == []
    with pytest.raises(safety.UnsafeScriptError):
        check_audio_prompt(_presets()[0] + " at dusk")


# ---------- (b) one retry ----------


@pytest.fixture
def waits(monkeypatch) -> list[float]:
    from app.providers import openrouter_chat

    seen: list[float] = []
    monkeypatch.setattr(openrouter_chat, "_sleep", seen.append)
    return seen


@pytest.mark.parametrize("first", [
    httpx.Response(503, json={}),
    httpx.Response(500, json={}),
    httpx.Response(429, json={}, headers={"retry-after": "2"}),
    httpx.ReadTimeout("slow"),
    httpx.ConnectError("dropped"),
])
@respx.mock
def test_a_blip_is_tried_once_more(client, monkeypatch, waits, first):
    _real_planner(monkeypatch)
    route = respx.post(f"{OR}/chat/completions").mock(side_effect=[
        first, _reply('{"minor_sexual": false, "reason": "a recipe"}')])
    minor_check.check_generation(SCRIPT, kind="script")
    assert len(route.calls) == 2 and len(waits) == 1
    assert waits[0] == (2.0 if isinstance(first, httpx.Response) and first.status_code == 429 else 1.5)


@respx.mock
def test_two_blips_and_the_script_waits(client, monkeypatch, waits):
    _real_planner(monkeypatch)
    route = respx.post(f"{OR}/chat/completions").mock(side_effect=[
        httpx.Response(502, json={}), httpx.ReadTimeout("slow")])
    with pytest.raises(safety.UnsafePromptError) as e:
        minor_check.check_generation(SCRIPT, kind="script")
    assert str(e.value) == minor_check.UNAVAILABLE
    assert len(route.calls) == 2 and len(waits) == 1


@respx.mock
def test_a_long_retry_after_is_capped(client, monkeypatch, waits):
    _real_planner(monkeypatch)
    respx.post(f"{OR}/chat/completions").mock(side_effect=[
        httpx.Response(429, json={}, headers={"retry-after": "120"}),
        _reply('{"minor_sexual": false, "reason": "ok"}')])
    minor_check.check_generation(SCRIPT, kind="script")
    assert waits == [5.0]


# ---------- (c) credit and key limits ----------


@pytest.mark.parametrize("code", [402, 403])
@respx.mock
def test_credit_or_key_limit_says_so_and_is_not_retried(client, monkeypatch, waits, code):
    _real_planner(monkeypatch)
    route = respx.post(f"{OR}/chat/completions").mock(
        return_value=httpx.Response(code, json={"error": {"message": "Insufficient credits"}}))
    with pytest.raises(safety.UnsafePromptError) as e:
        minor_check.check_generation(SCRIPT, kind="script")
    msg = str(e.value)
    assert msg == minor_check.CREDIT
    assert msg.startswith("OpenRouter credit or key limit reached, so the safety check couldn't run")
    assert "planner model" not in msg
    assert len(route.calls) == 1 and waits == []


@respx.mock
def test_credit_limit_on_a_clean_deployment(client, monkeypatch, waits):
    _real_planner(monkeypatch)
    _clean(monkeypatch)
    respx.post(f"{OR}/chat/completions").mock(return_value=httpx.Response(402, json={}))
    with pytest.raises(safety.UnsafePromptError) as e:
        minor_check.check_generation(SCRIPT, kind="script")
    assert str(e.value) == minor_check.CREDIT_CLEAN


@respx.mock
def test_credit_limit_on_the_content_check(client, monkeypatch, waits):
    from app.services import explicit_check

    _real_planner(monkeypatch)
    _clean(monkeypatch)
    respx.post(f"{OR}/chat/completions").mock(return_value=httpx.Response(403, json={}))
    with pytest.raises(explicit_check.ExplicitContentError) as e:
        explicit_check.check(SCRIPT)
    assert str(e.value) == explicit_check.CREDIT
    with pytest.raises(explicit_check.ExplicitPromptError) as e:
        explicit_check.check_prompt("a quiet lake at dawn")
    assert str(e.value) == explicit_check.PROMPT_CREDIT


@respx.mock
def test_credit_limit_on_a_long_scripts_facts(client, monkeypatch, waits):
    _real_planner(monkeypatch)
    respx.post(f"{OR}/chat/completions").mock(return_value=httpx.Response(402, json={}))
    long = " ".join([SCRIPT] * 120)
    assert len(long) > minor_check.JUDGE_WINDOW
    with pytest.raises(safety.UnsafePromptError) as e:
        minor_check.check_generation(long, kind="script")
    assert str(e.value) == minor_check.CREDIT


@respx.mock
def test_other_outages_keep_their_message(client, monkeypatch, waits):
    _real_planner(monkeypatch)
    respx.post(f"{OR}/chat/completions").mock(return_value=httpx.Response(400, json={}))
    with pytest.raises(safety.UnsafePromptError) as e:
        minor_check.check_generation(SCRIPT, kind="script")
    assert str(e.value) == minor_check.UNAVAILABLE

"""Ticking a Cast character "Adult content allowed": the word filter for a
card (any age under 18 on it), the AI check on the card, what an edit does
to the tick, the safety check memory (count and clear refusals), and adult
mode failing closed for everyday sexual words when the check can't run."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.providers import mock_chat
from app.services import content_mode, minor_check, safety

from .test_minor_check import OR, _real_planner, _reply


def _avatar(client, name="Mia", **kw) -> int:
    r = client.post("/api/avatars", json={"name": name, **kw})
    assert r.status_code == 201, r.text
    mock_chat.MINOR_CALLS.clear()  # the look check on save may have asked
    return r.json()["id"]


def _tick(client, aid: int, value: bool = True):
    return client.put(f"/api/settings/content/avatars/{aid}", json={"adult_ok": value})


def _ticked() -> set[int]:
    return content_mode.adult_ok_avatars()


def _adult(on: bool) -> None:
    content_mode.set_adult_mode(on)


# ---------- the tick ----------


@pytest.mark.parametrize("kw", [
    {"description": "Mia, 15"},
    {"name": "Mia (15)"},
    {"description": "Mia, fifteen, petite"},
    {"persona": "looks about 15"},
    {"visual_identity": "sweet sixteen, braces"},
    {"description": "a 15-year-old daughter"},
    {"description": "daughter, 15"},
    {"description": "woman, 16"},
])
def test_a_card_with_an_age_under_18_cannot_be_ticked(auth_client, kw):
    aid = _avatar(auth_client, **kw)
    r = _tick(auth_client, aid)
    assert r.status_code == 422 and r.json()["detail"] == content_mode.DESCRIBED_AS_MINOR
    assert aid not in _ticked()
    assert mock_chat.MINOR_CALLS == []  # the word filter answered: no AI call


@pytest.mark.parametrize("description", ["her father's daughter, 29", "a stepdaughter who runs the family bakery",
                                         "mother of two kids", "5'9\", size 8, drives a Tesla Model 3"])
def test_family_words_heights_and_cars_dont_block_a_tick(auth_client, description):
    aid = _avatar(auth_client, description=description)
    r = _tick(auth_client, aid)
    assert r.status_code == 200, r.text
    assert aid in r.json()["adult_ok_avatars"]
    assert len(mock_chat.MINOR_CALLS) == 1 and description in mock_chat.MINOR_CALLS[0]


def test_the_ai_check_can_refuse_a_tick_and_remembers_it(auth_client):
    aid = _avatar(auth_client, description="a petite gamer")
    mock_chat.MINOR_REPLY = {"minor": True, "reason": "Looks under 18."}
    r = _tick(auth_client, aid)
    assert r.status_code == 422 and r.json()["detail"] == minor_check.CHARACTER_REFUSED
    mock_chat.MINOR_REPLY = None
    assert _tick(auth_client, aid).status_code == 422  # remembered: not asked again
    assert len(mock_chat.MINOR_CALLS) == 1
    assert aid not in _ticked()


def test_the_mock_check_reads_a_young_look(auth_client):
    aid = _avatar(auth_client, visual_identity="looks very young, school uniform")
    assert _tick(auth_client, aid).json()["detail"] == minor_check.CHARACTER_REFUSED


@pytest.mark.parametrize("reply", [RuntimeError("HTTP 500"), {"verdict": "maybe"}, {"minor": "no"}])
def test_a_tick_is_refused_when_the_check_cant_run(auth_client, reply):
    aid = _avatar(auth_client, description="a painter in her thirties")
    mock_chat.MINOR_REPLY = reply
    r = _tick(auth_client, aid)
    assert r.status_code == 422 and r.json()["detail"] == minor_check.CHARACTER_UNAVAILABLE
    assert "couldn't run" in r.json()["detail"]
    assert aid not in _ticked()
    mock_chat.MINOR_REPLY = None
    assert _tick(auth_client, aid).status_code == 200  # never cached: asked again


def test_a_tick_is_refused_without_a_planner_key(auth_client, monkeypatch):
    from app import config

    aid = _avatar(auth_client, description="a painter in her thirties")
    monkeypatch.setenv("MOCK_PROVIDERS", "false")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    config.get_settings.cache_clear()
    r = _tick(auth_client, aid)
    assert r.status_code == 422 and r.json()["detail"] == minor_check.CHARACTER_UNAVAILABLE
    assert mock_chat.MINOR_CALLS == []
    assert _tick(auth_client, aid, False).status_code == 200  # clearing always works


def test_unticking_never_asks(auth_client):
    aid = _avatar(auth_client, description="Mia, 15")
    assert _tick(auth_client, aid, False).status_code == 200
    assert mock_chat.MINOR_CALLS == []


def test_a_card_changed_during_the_check_is_not_ticked(auth_client, monkeypatch):
    from app import models
    from app.db import SessionLocal

    aid = _avatar(auth_client, description="a painter in her thirties")
    real = content_mode.adult_ok_problem

    def edited_meanwhile(card):
        with SessionLocal() as db:
            db.get(models.Avatar, aid).description = "a painter, 15"
            db.commit()
        return real(card)

    monkeypatch.setattr(content_mode, "adult_ok_problem", edited_meanwhile)
    r = _tick(auth_client, aid)
    assert r.status_code == 409, r.text
    assert aid not in _ticked()


@respx.mock
def test_the_planner_gets_its_own_question_about_the_card(auth_client, monkeypatch):
    from app import models
    from app.db import SessionLocal

    aid = _avatar(auth_client, description="a petite gamer", persona="shy")
    bob = _avatar(auth_client, "Bob", description="a carpenter")
    _real_planner(monkeypatch)
    route = respx.post(f"{OR}/chat/completions").mock(side_effect=[
        _reply('{"minor": true, "reason": "Looks under 18."}'),
        _reply('```json\n{"minor": false, "reason": "An adult."}\n```'),
        _reply("I'm sorry, I can't help with that."),
    ])
    assert _tick(auth_client, aid).json()["detail"] == minor_check.CHARACTER_REFUSED
    assert _tick(auth_client, bob).status_code == 200
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "acme/planner-1" and body["temperature"] == 0
    assert body["messages"][0]["content"] == minor_check.CHARACTER_SYSTEM
    text = body["messages"][1]["content"][0]["text"]
    assert "Name: Mia" in text and "Description: a petite gamer" in text and "Persona: shy" in text
    with SessionLocal() as db:
        db.get(models.Avatar, bob).persona = "gruff"
        db.commit()
    r = _tick(auth_client, bob)  # a new card: asked again, and the planner declines
    assert r.status_code == 422 and r.json()["detail"] == minor_check.CHARACTER_UNAVAILABLE
    with SessionLocal() as db:
        assert "safety check" in [e.note for e in db.query(models.SpendEntry).all()]


# ---------- edits ----------


def test_an_edit_that_names_a_minor_drops_the_tick_and_says_so(auth_client):
    aid = _avatar(auth_client, description="a painter in her thirties")
    assert _tick(auth_client, aid).status_code == 200
    r = auth_client.patch(f"/api/avatars/{aid}", json={"description": "a painter, 15"})
    assert r.status_code == 200, r.text
    assert r.json()["adult_ok_notice"] == "Adult content is no longer allowed for Mia: the new text describes a minor."
    assert r.json()["description"] == "a painter, 15"
    assert aid not in _ticked()


def test_an_edit_the_ai_check_refuses_drops_the_tick(auth_client):
    aid = _avatar(auth_client, description="a painter in her thirties")
    _tick(auth_client, aid)
    mock_chat.MINOR_REPLY = {"minor": True, "reason": "Looks under 18."}
    r = auth_client.patch(f"/api/avatars/{aid}", json={"persona": "bubbly"})
    assert "under 18" in r.json()["adult_ok_notice"]
    assert aid not in _ticked()


def test_an_edit_when_the_check_cant_run_drops_the_tick(auth_client):
    aid = _avatar(auth_client, description="a painter in her thirties")
    _tick(auth_client, aid)
    mock_chat.MINOR_REPLY = RuntimeError("timeout")
    r = auth_client.patch(f"/api/avatars/{aid}", json={"name": "Mia Rose"})
    assert r.status_code == 200 and r.json()["name"] == "Mia Rose"
    assert "couldn't run" in r.json()["adult_ok_notice"]
    assert aid not in _ticked()


def test_an_edit_that_passes_keeps_the_tick(auth_client):
    aid = _avatar(auth_client, description="a painter in her thirties")
    _tick(auth_client, aid)
    calls = len(mock_chat.MINOR_CALLS)
    r = auth_client.patch(f"/api/avatars/{aid}", json={"persona": "warm and funny"})
    assert r.status_code == 200 and r.json()["adult_ok_notice"] is None
    assert aid in _ticked() and len(mock_chat.MINOR_CALLS) == calls + 1
    # Not the card's text (or the same text again): nothing is asked.
    r = auth_client.patch(f"/api/avatars/{aid}", json={"elevenlabs_voice_id": "v1", "persona": "warm and funny"})
    assert r.json()["adult_ok_notice"] is None and len(mock_chat.MINOR_CALLS) == calls + 1
    assert aid in _ticked()


def test_an_unticked_character_is_not_asked_about_on_edit(auth_client):
    aid = _avatar(auth_client, description="a painter in her thirties")
    r = auth_client.patch(f"/api/avatars/{aid}", json={"persona": "warm"})
    assert r.json()["adult_ok_notice"] is None and mock_chat.MINOR_CALLS == []


def test_the_word_filter_at_render_time_ignores_an_old_tick_on_a_bare_age(auth_client):
    """A tick made before this check existed, on a card that says "Mia, 15",
    gives no exemption: renders use describes_minor."""
    from app import models
    from app.db import SessionLocal

    aid = _avatar(auth_client, description="Mia, 15")
    content_mode.set_avatar_adult_ok(aid, True)  # an old tick
    with SessionLocal() as db:
        assert content_mode.describes_minor(db.get(models.Avatar, aid))


# ---------- safety check memory ----------


def test_memory_counts_and_clears_refusals_only(auth_client):
    with pytest.raises(safety.UnsafePromptError):
        minor_check.check_generation("a teenage girl in a bikini, provocative pose", kind="image")
    minor_check.check_generation("a sexy woman on a beach at sunset", kind="image")
    aid = _avatar(auth_client, description="a petite gamer")
    mock_chat.MINOR_REPLY = {"minor": True, "reason": "Looks under 18."}
    assert _tick(auth_client, aid).status_code == 422
    mock_chat.MINOR_REPLY = None
    assert auth_client.get("/api/settings/content/safety-memory").json() == {"refusals": 2, "oks": 1}
    r = auth_client.post("/api/settings/content/safety-memory/clear-refusals")
    assert r.status_code == 200 and r.json() == {"cleared": 2, "refusals": 0, "oks": 1}
    calls = len(mock_chat.MINOR_CALLS)
    assert _tick(auth_client, aid).status_code == 200  # asked again, and the mock says adult
    minor_check.check_generation("a sexy woman on a beach at sunset", kind="image")  # "ok" kept
    assert len(mock_chat.MINOR_CALLS) == calls + 1


def test_clearing_never_lets_a_word_filter_refusal_through(auth_client):
    with pytest.raises(safety.UnsafePromptError):
        minor_check.check_generation("nude teenage girl", kind="image")
    auth_client.post("/api/settings/content/safety-memory/clear-refusals")
    with pytest.raises(safety.UnsafePromptError):
        minor_check.check_generation("nude teenage girl", kind="image")
    assert mock_chat.MINOR_CALLS == []


def test_a_refusal_cleared_elsewhere_is_never_served_from_memory(client):
    """Another process clears the table: this one's memory holds only "ok"
    verdicts, so the refusal is gone here too."""
    from app import models
    from app.db import SessionLocal

    refused = "a teenage girl in a bikini, provocative pose"
    with pytest.raises(safety.UnsafePromptError):
        minor_check.check_generation(refused, kind="image")
    minor_check.check_generation("a sexy woman on a beach at sunset", kind="image")
    assert {v[0] for v in minor_check._lru.values()} == {"ok"}
    with SessionLocal() as db:  # what the other process's clear does
        db.query(models.PromptCheck).filter_by(verdict="minor_sexual").delete()
        db.commit()
    assert minor_check.cached(minor_check.text_hash(refused)) is None
    mock_chat.MINOR_REPLY = {"minor_sexual": False, "reason": "fine"}
    minor_check.check_generation(refused, kind="image")  # asked again, and allowed this time


def test_a_refusal_drops_an_ok_this_process_remembered(client):
    key = minor_check.text_hash("borderline prompt")
    judge = minor_check._identity()
    minor_check._store(key, "ok", judge, "fine")
    assert key in minor_check._lru
    minor_check._store(key, "minor_sexual", judge, "a minor")
    assert key not in minor_check._lru
    assert minor_check.cached(key) == "minor_sexual"


# ---------- adult mode fails closed for everyday sexual words ----------


@pytest.mark.parametrize("prompt", ["woman, 16, in a bikini", "a woman in a bikini, sensual pose",
                                    "provocative swimsuit shot"])
def test_adult_mode_pauses_suggestive_prompts_when_the_check_cant_run(client, prompt):
    mock_chat.MINOR_REPLY = RuntimeError("HTTP 500")
    _adult(True)
    for kind in ("image", "video", "lipsync"):
        with pytest.raises(safety.UnsafePromptError) as e:
            minor_check.check_generation(prompt, kind=kind)
        assert str(e.value) == minor_check.UNAVAILABLE
    minor_check.check_generation(prompt, kind="script")  # a voice script: no picture, as before
    minor_check.check_generation("a mountain lake at dawn", kind="image")  # not sexual: goes ahead
    _adult(False)
    minor_check.check_generation(prompt, kind="image")  # provider filters on: goes ahead, as before


def test_woman_16_in_a_bikini_is_a_minor_mention_everywhere(client):
    prompt = "woman, 16, in a bikini"
    safety.check_prompt(prompt)  # an everyday word: not refused by the words alone...
    assert safety.mentions_minor(prompt)  # ...but the provider filters are forced on
    _adult(True)
    with pytest.raises(safety.UnsafePromptError) as e:
        minor_check.check_generation(prompt, kind="image")  # and the mock check refuses it
    assert str(e.value) == minor_check.REFUSED


def test_the_settings_line_says_what_waits(auth_client, monkeypatch):
    from app import config

    body = auth_client.get("/api/settings/content").json()
    assert body["safety_check"]["available"] is True and "$0.0005" in body["safety_check"]["note"]
    monkeypatch.setenv("MOCK_PROVIDERS", "false")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    config.get_settings.cache_clear()
    note = auth_client.get("/api/settings/content").json()["safety_check"]["note"]
    assert "sexual prompts are paused" in note and "suggestive" not in note
    _adult(True)
    note = auth_client.get("/api/settings/content").json()["safety_check"]["note"]
    assert "suggestive" in note and "Adult content allowed" in note


def test_old_photo_in_an_avoid_list_doesnt_refuse_a_plan(client):
    from app.schemas.storyboard import ShotPlan, StoryboardPlan
    from app.services import pipeline

    shot = dict(shot_type="b_roll", duration_seconds=3, reference_strategy="input_references",
                recommended_asset_types=[], recommended_cast_roles=[], caption_text="", camera_direction="")
    plan = StoryboardPlan(title="t", cleaned_voice_script="Hello.", estimated_duration_seconds=6,
                          content_warning_notes="", disclosure_text="", end_card_text="", caption_chunks=[],
                          shots=[ShotPlan(order=1, visual_prompt="a woman in lingerie on a bed",
                                          negative_prompt="old photo, grainy, old-fashioned", **shot)])
    pipeline._check_plan(plan)
    plan.shots[0].negative_prompt = "adult, mature"
    with pytest.raises(safety.UnsafePromptError):
        pipeline._check_plan(plan)


@respx.mock
def test_a_real_planner_error_on_the_card_is_never_a_yes(auth_client, monkeypatch):
    aid = _avatar(auth_client, description="a painter in her thirties")
    _real_planner(monkeypatch)
    respx.post(f"{OR}/chat/completions").mock(return_value=httpx.Response(500, json={}))
    assert _tick(auth_client, aid).json()["detail"] == minor_check.CHARACTER_UNAVAILABLE
    assert aid not in _ticked()

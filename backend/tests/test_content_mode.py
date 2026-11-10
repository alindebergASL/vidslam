"""Adult mode (Settings → Adult content): one switch for the text model
(planning, Enhance, scene rewrites) and the picture/video filters."""
from __future__ import annotations

import json

import httpx
import respx

from .test_runware import _CORE, _IMG, _fake_model, _ref

SOGNI_WF = "https://api.sogni.ai/v1/creative-agent/workflows"


def _reset():
    from app import config
    from app.services import provider_keys

    config.get_settings.cache_clear()
    provider_keys.invalidate()


def test_the_switch_defaults_off_and_round_trips(auth_client):
    r = auth_client.get("/api/settings/content").json()
    assert r["adult"] is False and r["default"] is False and r["explicit"] is False
    assert r["text_model"] == "deepseek/deepseek-v4-flash"
    assert auth_client.put("/api/settings/content", json={"adult": True}).json()["adult"] is True
    assert auth_client.get("/api/settings/content").json()["explicit"] is True
    back = auth_client.put("/api/settings/content", json={"adult": None}).json()
    assert back["adult"] is False and back["explicit"] is False
    for bad in ({}, {"adult": "yes"}, {"enabled": True}, []):
        assert auth_client.put("/api/settings/content", json=bad).status_code == 422


def test_the_env_default_applies_until_the_switch_is_set(auth_client, monkeypatch):
    monkeypatch.setenv("ADULT_MODE", "true")
    _reset()
    assert auth_client.get("/api/settings/content").json()["adult"] is True
    auth_client.put("/api/settings/content", json={"adult": False})
    assert auth_client.get("/api/settings/content").json()["adult"] is False
    _reset()


def test_planning_follows_adult_mode(auth_client, monkeypatch):
    from app.providers import get_chat
    from app.services import content_mode

    monkeypatch.setenv("MOCK_PROVIDERS", "false")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-abcdefgh1234")
    monkeypatch.setenv("OPENROUTER_CHAT_MODEL", "anthropic/claude-haiku-5.5")
    _reset()
    assert get_chat().model == "anthropic/claude-haiku-5.5"
    content_mode.set_adult_mode(True)
    assert get_chat().model == "deepseek/deepseek-v4-flash"  # storyboards and scene rewrites too
    content_mode.set_adult_mode(False)
    assert get_chat().model == "anthropic/claude-haiku-5.5"
    _reset()


def test_sogni_safe_filter_is_on_unless_adult_mode(auth_client, monkeypatch):
    from app.providers.sogni import SogniClient
    from app.services import content_mode

    monkeypatch.setenv("SOGNI_API_KEY", "sk-test-123456789")
    _reset()
    bodies = []

    def started(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"status": "success", "data": {"workflow": {"workflowId": "wf", "status": "queued"}}})

    with respx.mock() as router:
        router.post(SOGNI_WF).mock(side_effect=started)
        SogniClient().start(title="t", tool="generate_image", arguments={"prompt": "a lake"})
        content_mode.set_adult_mode(True)
        SogniClient().start(title="t", tool="generate_image", arguments={"prompt": "a lake"})
    assert [b["safe_content_filter"] for b in bodies] == [True, False]
    _reset()


def test_runware_check_and_vendor_knobs_follow_adult_mode(client, monkeypatch):
    from app.providers import runware
    from app.services import content_mode, provider_keys

    monkeypatch.setenv("MOCK_PROVIDERS", "false")
    monkeypatch.setenv("RUNWARE_API_KEY", "rw-test-abcdefgh1234")
    _reset()
    model = _fake_model(monkeypatch, "test:knobs@1", {
        "type": "object", "additionalProperties": False, "required": ["taskType", "taskUUID", "model"],
        "properties": {**_CORE,
                       "safety": {"type": "object", "properties": {"checkContent": {"type": "boolean"}}},
                       "providerSettings": {"type": "object", "properties": {"bfl": {"type": "object", "properties": {
                           "safetyTolerance": {"type": "integer", "minimum": 0, "maximum": 6}}}}},
                       "inputs": {"type": "object", "properties": {"referenceImages": {"type": "array", "items": _IMG}}}}},
        modes=[{"id": "text"}], caps=["text-to-image"])
    off = runware.build_image_task(model, "a quiet street at dusk", [])
    assert off["safety"]["checkContent"] is True and "providerSettings" not in off
    provider_keys.set_option("runware", "check_content", False)  # ignored while adult mode is off
    assert runware.build_image_task(model, "a quiet street at dusk", [])["safety"]["checkContent"] is True
    content_mode.set_adult_mode(True)
    on = runware.build_image_task(model, "a quiet street at dusk", [])
    assert on["safety"]["checkContent"] is False and on["providerSettings"]["bfl"]["safetyTolerance"] == 6
    # A Cast picture is checked in adult mode too.
    cast = runware.build_image_task(model, "her portrait in the street", [_ref(role="character", name="Naina")])
    assert cast["safety"]["checkContent"] is True
    _reset()


def test_sogni_refuses_minors_and_guards_cast_pictures_in_adult_mode(auth_client, monkeypatch):
    import pytest

    from app.providers.sogni import SogniClient
    from app.services import content_mode
    from app.services.safety import UnsafePromptError

    monkeypatch.setenv("SOGNI_API_KEY", "sk-test-123456789")
    _reset()
    content_mode.set_adult_mode(True)
    bodies = []

    def started(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"status": "success", "data": {"workflow": {"workflowId": "wf", "status": "queued"}}})

    with respx.mock() as router:
        router.post(SOGNI_WF).mock(side_effect=started)
        with pytest.raises(UnsafePromptError):
            SogniClient().start(title="t", tool="generate_image", arguments={"prompt": "naked teenage girl"})
        assert not bodies  # never sent
        SogniClient().start(title="t", tool="edit_image", arguments={"prompt": "her on a beach"}, cast=True)
        SogniClient().start(title="t", tool="generate_image", arguments={"prompt": "kids at a beach"})
        SogniClient().start(title="t", tool="generate_image", arguments={"prompt": "a lake at dawn"})
    assert [b["safe_content_filter"] for b in bodies] == [True, True, False]
    _reset()


def test_an_unreadable_setting_counts_as_off(monkeypatch):
    from sqlalchemy.exc import OperationalError

    from app.services import content_mode

    monkeypatch.setenv("ADULT_MODE", "true")
    _reset()

    class Broken:
        def get(self, *a, **k):
            raise OperationalError("select", {}, Exception("db down"))

        def close(self):
            pass

    import app.db as db_mod

    monkeypatch.setattr(db_mod, "SessionLocal", lambda: Broken())
    assert content_mode.adult_mode() is False  # filters on, never off by accident
    _reset()


def test_number_words_are_not_ages():
    import pytest

    from app.services.safety import UnsafePromptError, check_prompt

    for fine in ("a nude couple celebrating ten years of marriage", "after five years apart, they undress each other",
                 "one person in frame, nude", "two models in lingerie", "12 girls on a runway in lingerie"):
        check_prompt(fine)
    for bad in ("nude 14-year-old girl", "13 girl nude", "fifteen years old, lingerie", "13 years of age, nude",
                "girl, 13, topless", "a teen girl stripped naked"):
        with pytest.raises(UnsafePromptError):
            check_prompt(bad)


def test_planning_checks_its_input_and_its_output(auth_client, monkeypatch):
    import pytest

    from app import models
    from app.db import SessionLocal
    from app.schemas.storyboard import ShotPlan, StoryboardPlan
    from app.services.pipeline import _check_plan
    from app.services.safety import UnsafePromptError

    db = SessionLocal()
    try:
        p = models.VideoProject(title="Summer", original_script="A day at the lake.",
                                creative_direction="she is 14; nude scenes")
        db.add(p)
        db.commit()
        pid = p.id
    finally:
        db.close()
    r = auth_client.post(f"/api/projects/{pid}/generate-plan")
    assert r.status_code == 422 and "refused" in r.json()["detail"]

    def plan(prompt, negative=""):
        return StoryboardPlan(title="t", cleaned_voice_script="Hello.", estimated_duration_seconds=5,
                              shots=[ShotPlan(order=0, visual_prompt=prompt, negative_prompt=negative)])

    _check_plan(plan("a couple on a beach at dusk"))
    with pytest.raises(UnsafePromptError):
        _check_plan(plan("a naked teenage girl on a beach"))
    with pytest.raises(UnsafePromptError):
        _check_plan(plan("a nude figure on a beach", negative="adult, mature, woman"))


def test_openrouter_images_refuse_minors_before_any_call(monkeypatch):
    import pytest

    from app.providers.openrouter_image import OpenRouterImageProvider
    from app.services.safety import UnsafePromptError

    monkeypatch.setenv("MOCK_PROVIDERS", "false")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-abcdefgh1234")
    _reset()
    with respx.mock() as router:
        with pytest.raises(UnsafePromptError):
            OpenRouterImageProvider(model="google/nano").generate(model="google/nano", prompt="naked teenage girl",
                                                                  references=[])
        assert not router.calls
    _reset()


def test_the_running_planner_is_what_gates_and_labels_show(auth_client, monkeypatch):
    from app.services import content_mode

    monkeypatch.setenv("OPENROUTER_CHAT_MODEL", "anthropic/claude-haiku-5.5")
    _reset()
    assert content_mode.chat_model() == "anthropic/claude-haiku-5.5"
    content_mode.set_adult_mode(True)
    assert content_mode.chat_model() == "deepseek/deepseek-v4-flash"
    assert auth_client.get("/api/system/info").json()["models"]["chat"] == "deepseek/deepseek-v4-flash"
    _reset()

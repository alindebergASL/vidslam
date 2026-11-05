"""Regressions found during the first live deploy:

* OpenRouter rejecting a response_format with HTTP 200 + an error body
  (`Schema is too complex.`) must fall back to the next format, not crash
  on `data["choices"]` — and must not record spend for the error.
* A pending spend entry whose generation is never queryable (404 forever)
  must stop being polled after PENDING_GIVE_UP.
* put_setting must survive losing an insert race on the same key.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import httpx
import pytest
import respx

from .test_spend import _entries

OR = "https://openrouter.ai/api/v1"

PLAN = {
    "title": "Hello",
    "cleaned_voice_script": "Test script",
    "estimated_duration_seconds": 10.0,
    "disclosure_text": "AI-generated virtual creator",
    "end_card_text": "",
    "caption_chunks": [{"start_hint": 0.0, "text": "Test"}],
    "shots": [{"order": 1, "shot_type": "hero", "duration_seconds": 10.0,
               "visual_prompt": "hero shot", "negative_prompt": "",
               "reference_strategy": "input_references", "recommended_asset_types": ["hero"],
               "recommended_cast_roles": ["host"], "caption_text": "Test",
               "camera_direction": "", "notes": ""}],
}
PROJECT = {"title": "Hello", "mode": "reel_montage", "target_duration_seconds": 10,
           "cta_text": "", "include_disclosure": True, "original_script": "Test script"}


def _chat_provider(monkeypatch):
    monkeypatch.setenv("MOCK_PROVIDERS", "false")
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")
    from app import config

    config.get_settings.cache_clear()
    from app.providers.openrouter_chat import OpenRouterChatProvider

    p = OpenRouterChatProvider(model="test/model", json_mode=True)
    p.structured = True  # json_schema → json_object → plain
    return p


def _cast():
    from app.providers.base import CastContext

    return CastContext(avatars=[{"name": "A", "role": "host"}], ingredients=[])


ERROR_200 = httpx.Response(200, json={"id": "gen-err", "error": {
    "message": "Schema is too complex.", "code": 400, "metadata": {"error_type": "invalid_request"}}})


@respx.mock
def test_chat_falls_back_when_format_rejected_in_200_body(client, monkeypatch):
    provider = _chat_provider(monkeypatch)
    route = respx.post(f"{OR}/chat/completions").mock(side_effect=[
        ERROR_200,
        httpx.Response(200, json={"id": "gen-ok", "model": "test/model", "usage": {"cost": 0.002},
                                  "choices": [{"message": {"content": json.dumps(PLAN)}}]}),
    ])
    plan = provider.generate_storyboard(project=PROJECT, cast=_cast())
    assert plan.title == "Hello"
    sent = [json.loads(c.request.content).get("response_format", {}).get("type") for c in route.calls]
    assert sent == ["json_schema", "json_object"]
    (row,) = _entries()  # only the successful call is billed
    assert (row["generation_id"], row["cost_usd"], row["source"]) == ("gen-ok", 0.002, "provider")


@respx.mock
def test_chat_error_body_on_last_format_raises_clearly(client, monkeypatch):
    provider = _chat_provider(monkeypatch)
    respx.post(f"{OR}/chat/completions").mock(return_value=ERROR_200)
    with pytest.raises(RuntimeError, match="Schema is too complex"):
        provider.generate_storyboard(project=PROJECT, cast=_cast())
    assert _entries() == []  # an error body is not recorded as spend


@respx.mock
def test_pending_entry_that_404s_forever_is_given_up(client, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
    from app import config, models
    from app.db import SessionLocal
    from app.services import spend

    config.get_settings.cache_clear()
    spend.record_openrouter("chat", "m", cost=None, generation_id="gen-fresh", estimate=0.01)
    spend.record_openrouter("chat", "m", cost=None, generation_id="gen-old", estimate=0.01)
    db = SessionLocal()
    old = db.query(models.SpendEntry).filter_by(generation_id="gen-old").one()
    old.created_at = datetime.utcnow() - spend.PENDING_GIVE_UP - timedelta(minutes=1)
    db.commit()
    db.close()

    respx.get(f"{OR}/generation").mock(return_value=httpx.Response(404, json={"error": "nf"}))
    assert spend.reconcile_pending() == 1
    by_gen = {e["generation_id"]: e for e in _entries()}
    assert by_gen["gen-fresh"]["source"] == "pending"  # still inside the window
    assert by_gen["gen-old"]["source"] == "rate_card" and by_gen["gen-old"]["cost_usd"] == 0.01


def test_put_setting_survives_a_lost_insert_race(client, monkeypatch):
    from app import models
    from app.db import SessionLocal
    from app.services import model_registry as reg

    calls = {"n": 0}

    def racing_get(self, entity, ident, *a, **kw):
        # First lookup sees no row; meanwhile "another worker" inserts it.
        if entity is models.AppSetting and calls["n"] == 0:
            calls["n"] += 1
            other = SessionLocal()
            other.add(models.AppSetting(key=ident, value={"v": "other"}))
            other.commit()
            other.close()
            return None
        return orig_get(self, entity, ident, *a, **kw)

    from sqlalchemy.orm import Session

    orig_get = Session.get
    monkeypatch.setattr(Session, "get", racing_get)
    reg.put_setting("race.key", {"v": "mine"})
    monkeypatch.setattr(Session, "get", orig_get)
    assert reg.get_setting("race.key") == {"v": "mine"}

"""One retry budget for a whole story facts ask (#57 fixes, round 2).

A story facts ask is up to two calls (the re-ask with more room after a
reply cut off for length), and each went through _post_check, which
retries a 429/5xx/timeout once: one ask could make four requests with
30 s timeouts. The calls now share one retry, so an ask makes at most
three. A single check's own retry is unchanged.
"""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from .test_minor_check import OR, _real_planner

FULL = json.dumps({"characters": [{"name": "Ann", "status": "adult", "age": 40}], "narrator": None})


def _ok(content: str, finish: str = "stop") -> httpx.Response:
    return httpx.Response(200, json={"id": "gen-1", "model": "acme/planner-1", "usage": {"cost": 0.0004},
                                     "choices": [{"message": {"content": content}, "finish_reason": finish}]})


def _cut() -> httpx.Response:
    return _ok(FULL[:20], "length")


def _provider():
    from app.providers.openrouter_chat import OpenRouterChatProvider

    return OpenRouterChatProvider(model="acme/planner-1")


@pytest.fixture
def waits(monkeypatch) -> list[float]:
    from app.providers import openrouter_chat

    seen: list[float] = []
    monkeypatch.setattr(openrouter_chat, "_sleep", seen.append)
    return seen


@pytest.mark.parametrize("sequence", [
    # A cut-off, then the bigger re-ask keeps failing.
    lambda: [_cut(), httpx.Response(503, json={}), httpx.Response(503, json={}), _ok(FULL)],
    # A blip, a cut-off, then a blip on the re-ask: no second retry.
    lambda: [httpx.Response(503, json={}), _cut(), httpx.Response(503, json={}), _ok(FULL)],
    lambda: [httpx.ReadTimeout("slow"), _cut(), httpx.ReadTimeout("slow"), _ok(FULL)],
])
@respx.mock
def test_a_facts_ask_makes_at_most_three_requests(client, monkeypatch, waits, sequence):
    _real_planner(monkeypatch)
    route = respx.post(f"{OR}/chat/completions").mock(side_effect=sequence())
    with pytest.raises(httpx.HTTPError):
        _provider().story_characters("A story about Ann.")
    assert route.call_count == 3 and len(waits) == 1


@respx.mock
def test_a_cut_off_then_one_blip_is_still_retried(client, monkeypatch, waits):
    _real_planner(monkeypatch)
    route = respx.post(f"{OR}/chat/completions").mock(side_effect=[
        _cut(), httpx.Response(503, json={}), _ok(FULL)])
    out = _provider().story_characters("A story about Ann.")
    assert [c["name"] for c in out["characters"]] == ["Ann"]
    assert route.call_count == 3 and len(waits) == 1


@respx.mock
def test_a_blip_on_the_first_ask_is_still_retried(client, monkeypatch, waits):
    _real_planner(monkeypatch)
    route = respx.post(f"{OR}/chat/completions").mock(side_effect=[httpx.Response(429, json={}), _ok(FULL)])
    out = _provider().story_characters("A story about Ann.")
    assert [c["name"] for c in out["characters"]] == ["Ann"]
    assert route.call_count == 2 and len(waits) == 1


@respx.mock
def test_each_check_keeps_its_own_retry(client, monkeypatch, waits):
    """Separate checks don't share a budget: each gets its one retry."""
    _real_planner(monkeypatch)
    p = _provider()
    route = respx.post(f"{OR}/chat/completions").mock(side_effect=[
        httpx.Response(503, json={}), _ok('{"minor_sexual": false, "reason": "ok"}'),
        httpx.Response(502, json={}), _ok('{"minor_sexual": false, "reason": "ok"}')])
    assert p.classify_minor("a recipe")["minor_sexual"] is False
    assert p.classify_minor("a recipe")["minor_sexual"] is False
    assert route.call_count == 4 and len(waits) == 2

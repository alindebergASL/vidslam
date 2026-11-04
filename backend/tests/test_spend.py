"""Spend ledger: provider-reported costs, rate-card estimates, reconciliation,
per-render actual cost, monthly budget (alerts + hard stop) and the API."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import httpx
import respx

from .test_cancel_render import _planned
from .test_video_webhooks import _hook, _jobs, _status, live_video  # noqa: F401 — fixture

OR = "https://openrouter.ai/api/v1"


def _entries(**filters):
    from app import models
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        return [{c: getattr(e, c) for c in ("provider", "kind", "model", "project_id", "render_id", "ref",
                                            "cost_usd", "is_estimate", "source", "generation_id")}
                for e in db.query(models.SpendEntry).filter_by(**filters).order_by(models.SpendEntry.id)]
    finally:
        db.close()


def _add(cost, *, kind="video", project_id=None, days_ago=0, estimate=False):
    from app import models
    from app.db import SessionLocal

    db = SessionLocal()
    db.add(models.SpendEntry(provider="openrouter", kind=kind, model="m", cost_usd=cost, project_id=project_id,
                             is_estimate=estimate, created_at=datetime.utcnow() - timedelta(days=days_ago)))
    db.commit()
    db.close()


def test_mock_render_records_nothing(auth_client):
    pid = _planned(auth_client)
    auth_client.post(f"/api/projects/{pid}/generate-video?force=true")
    st = _status(auth_client, pid)
    assert st["latest_render"]["status"] == "completed"
    assert _entries() == []
    assert st["latest_render"]["actual_cost"] == 0


def test_video_job_costs_roll_up_to_the_render(auth_client, live_video):  # noqa: F811
    pid = _planned(auth_client)
    auth_client.post(f"/api/projects/{pid}/generate-video?force=true")
    jobs = _jobs(project_id=pid)
    for i, (_, token, jid, *_) in enumerate(jobs):
        _hook(auth_client, token, jid, cost=0.5 + i)
    _hook(auth_client, jobs[0][1], jobs[0][2], cost=99)  # duplicate delivery: not billed twice
    st = _status(auth_client, pid)
    rid = st["latest_render"]["id"]
    rows = _entries(kind="video")
    assert len(rows) == len(jobs)
    assert all(r["project_id"] == pid and r["render_id"] == rid and r["source"] == "provider"
               and not r["is_estimate"] and r["generation_id"].startswith("v") for r in rows)
    want = sum(0.5 + i for i in range(len(jobs)))
    assert abs(st["latest_render"]["actual_cost"] - want) < 1e-6
    summary = auth_client.get("/api/spend/summary").json()
    assert abs(summary["total_usd"] - want) < 1e-6
    assert summary["top_projects"][0]["project_id"] == pid
    assert {k["key"] for k in summary["by_kind"]} == {"video"}


def test_video_job_without_reported_cost_uses_rate_card(auth_client, live_video):  # noqa: F811
    pid = _planned(auth_client)
    auth_client.post(f"/api/projects/{pid}/generate-video?force=true")
    _, token, jid, *_ = _jobs(project_id=pid)[0]
    raw = json.dumps({"type": "video.generation.completed",
                      "data": {"id": jid, "unsigned_urls": [f"{OR}/videos/{jid}/content"]}})
    _hook(auth_client, token, jid, raw=raw)
    (row,) = _entries(kind="video")
    assert row["is_estimate"] and row["source"] == "rate_card" and row["cost_usd"] > 0


@respx.mock
def test_chat_cost_is_recorded_in_scope(client, monkeypatch):  # client → tables exist
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
    from app import config
    from app.providers.base import CastContext
    from app.providers.mock_chat import MockChatProvider
    from app.providers.openrouter_chat import OpenRouterChatProvider
    from app.services import spend

    config.get_settings.cache_clear()
    plan = MockChatProvider().generate_storyboard(
        project={"title": "t", "mode": "reel_montage", "original_script": "Hi.", "target_duration_seconds": 10},
        cast=CastContext()).model_dump(mode="json")
    respx.post(f"{OR}/chat/completions").mock(return_value=httpx.Response(200, json={
        "id": "gen-abc", "model": "anthropic/claude-haiku-5.5",
        "choices": [{"message": {"content": json.dumps(plan)}}],
        "usage": {"prompt_tokens": 900, "completion_tokens": 400, "cost": 0.0031}}))
    with spend.scope(project_id=7):
        OpenRouterChatProvider(model="x/y", json_mode=True).generate_storyboard(project={"title": "t"},
                                                                                cast=CastContext())
    (row,) = _entries()
    assert row == {"provider": "openrouter", "kind": "chat", "model": "anthropic/claude-haiku-5.5",
                   "project_id": 7, "render_id": None, "ref": "", "cost_usd": 0.0031,
                   "is_estimate": False, "source": "provider", "generation_id": "gen-abc"}


@respx.mock
def test_tts_cost_is_reconciled_from_generation_lookup(client, monkeypatch):  # client → tables exist
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
    from app import config
    from app.providers.openrouter_tts import OpenRouterTTSProvider
    from app.services import spend

    config.get_settings.cache_clear()
    respx.post(f"{OR}/audio/speech").mock(return_value=httpx.Response(
        200, content=b"ID3fake-mp3", headers={"content-type": "audio/mpeg", "X-Generation-Id": "gen-tts1"}))
    OpenRouterTTSProvider(model="openai/gpt-4o-mini-tts").synthesize(text="x" * 2000, voice_id="alloy")
    (row,) = _entries()
    assert row["source"] == "pending" and row["is_estimate"] and row["generation_id"] == "gen-tts1"
    assert abs(row["cost_usd"] - 2 * config.get_settings().cost_tts_per_1k_chars) < 1e-9

    lookup = respx.get(f"{OR}/generation").mock(side_effect=[
        httpx.Response(404, json={"error": "not found"}),  # not queryable yet
        httpx.Response(200, json={"data": {"id": "gen-tts1", "total_cost": 0.0123}}),
    ])
    assert spend.reconcile_pending() == 0
    assert spend.reconcile_pending() == 1
    assert lookup.calls[-1].request.url.params["id"] == "gen-tts1"
    (row,) = _entries()
    assert (row["source"], row["is_estimate"], row["cost_usd"]) == ("lookup", False, 0.0123)
    assert spend.reconcile_pending() == 0  # nothing left


@respx.mock
def test_elevenlabs_is_a_rate_card_estimate(client, monkeypatch):  # client → tables exist
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el")
    from app import config
    from app.providers.elevenlabs_tts import ElevenLabsTTSProvider

    config.get_settings.cache_clear()
    respx.post(url__regex=r".*/text-to-speech/v1").mock(return_value=httpx.Response(
        200, content=b"mp3", headers={"character-cost": "500"}))
    ElevenLabsTTSProvider().synthesize(text="hello", voice_id="v1")
    (row,) = _entries()
    assert row["provider"] == "elevenlabs" and row["kind"] == "tts" and row["is_estimate"]
    assert abs(row["cost_usd"] - 0.5 * config.get_settings().cost_tts_per_1k_chars) < 1e-9


def test_budget_alerts_fire_once_per_threshold(auth_client):
    from app.services import alerts, spend

    assert auth_client.put("/api/spend/budget", json={"monthly_usd": 10, "hard_stop": False,
                                                      "alert_pcts": [80, 100]}).status_code == 200
    _add(5, days_ago=40)  # last month: doesn't count
    spend.record(provider="openrouter", kind="video", cost=7)
    assert alerts.recent() == []
    spend.record(provider="openrouter", kind="video", cost=1.5)  # 85%
    spend.record(provider="openrouter", kind="video", cost=0.1)  # still 86%: no repeat
    assert [a["key"] for a in alerts.recent()] == [f"spend:{spend._month()}:80"]
    spend.record(provider="openrouter", kind="image", cost=2)  # 106%
    assert [a["key"] for a in alerts.recent()][-1] == f"spend:{spend._month()}:100"
    assert len(alerts.recent()) == 2
    s = auth_client.get("/api/spend/summary").json()
    assert s["total_usd"] == 10.6 and s["budget_pct"] == 106.0 and s["blocked"] is False  # alert-only


def test_hard_stop_blocks_paid_endpoints(auth_client, monkeypatch):
    from app import config

    pid = _planned(auth_client)
    auth_client.put("/api/spend/budget", json={"monthly_usd": 2, "hard_stop": True, "alert_pcts": []})
    _add(2.5, project_id=pid)
    # Mock mode never blocks (nothing is billed).
    assert auth_client.post(f"/api/projects/{pid}/generate-video?force=true").status_code == 202

    monkeypatch.setenv("MOCK_PROVIDERS", "false")
    config.get_settings.cache_clear()
    for url in (f"/api/projects/{pid}/generate-video?force=true", f"/api/projects/{pid}/generate-plan",
                "/api/studio/generate-image"):
        r = auth_client.post(url, json={})
        assert r.status_code == 402, (url, r.status_code)
        assert "Monthly budget of $2.00 reached" in r.json()["detail"]
    assert auth_client.get("/api/spend/summary").json()["blocked"] is True
    # FFmpeg-only work is never blocked.
    assert auth_client.post(f"/api/projects/{pid}/recompose").status_code != 402
    # Raising the cap unblocks.
    auth_client.put("/api/spend/budget", json={"monthly_usd": 50, "hard_stop": True, "alert_pcts": []})
    assert auth_client.get("/api/spend/summary").json()["blocked"] is False


def test_entries_api_filters_and_validation(auth_client):
    _add(1.0, kind="video", project_id=1)
    _add(0.2, kind="tts", project_id=2, estimate=True)
    _add(3.0, kind="video", project_id=1, days_ago=40)
    month = datetime.utcnow().strftime("%Y-%m")
    rows = auth_client.get(f"/api/spend/entries?month={month}").json()
    assert [r["kind"] for r in rows] == ["tts", "video"]
    assert [r["cost_usd"] for r in auth_client.get("/api/spend/entries?project_id=1").json()] == [3.0, 1.0]
    assert auth_client.get("/api/spend/entries?kind=tts").json()[0]["is_estimate"] is True
    s = auth_client.get(f"/api/spend/summary?month={month}").json()
    assert s["total_usd"] == 1.2 and s["estimated_usd"] == 0.2
    assert s["budget_pct"] is None and len(s["daily"]) == 1
    assert auth_client.get("/api/spend/summary?month=2026-13").status_code == 422
    assert auth_client.get("/api/spend/summary").status_code == 200


def test_spend_api_requires_auth(client):
    assert client.get("/api/spend/summary").status_code == 401
    assert client.put("/api/spend/budget", json={"monthly_usd": 1}).status_code == 401


def test_bind_only_inside_a_scope(client):
    from app.services import spend

    spend.bind(render_id=99)  # no scope: ignored
    with spend.scope(project_id=3):
        spend.bind(render_id=5)
        spend.record(provider="openrouter", kind="tts", cost=0.1)
        with spend.scope(ref="x"):
            spend.bind(render_id=6)  # inner only
        spend.record(provider="openrouter", kind="tts", cost=0.2)
    spend.record(provider="openrouter", kind="tts", cost=0.3)
    assert [(e["project_id"], e["render_id"]) for e in _entries()] == [(3, 5), (3, 5), (None, None)]


def test_openrouter_account_never_leaks_the_key(auth_client, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret")
    from app import config

    config.get_settings.cache_clear()
    with respx.mock:
        respx.get(f"{OR}/key").mock(return_value=httpx.Response(200, json={"data": {
            "label": "sk-or-v1-abc...", "usage": 12.5, "usage_monthly": 4.25, "limit": 100, "limit_remaining": 87.5}}))
        body = auth_client.get("/api/spend/openrouter?refresh=true").json()
    assert body["available"] and body["usage_monthly"] == 4.25 and body["limit_remaining"] == 87.5
    assert "sk-or" not in json.dumps(body)

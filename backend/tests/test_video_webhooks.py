"""Event-driven video jobs: OpenRouter webhooks, the polling sweeper, and
the render lifecycle built on them (parallel submit, compose exactly once)."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import subprocess
import time
from datetime import timedelta

import httpx
import pytest
import respx

from .test_cancel_render import _planned

OR = "https://openrouter.ai/api/v1"
_CLIP: bytes | None = None


def _clip() -> bytes:
    global _CLIP
    if _CLIP is None:
        _CLIP = subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=green:s=320x568:d=10:r=30",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "mp4", "-movflags", "frag_keyframe", "pipe:1"],
            capture_output=True, check=True).stdout
    return _CLIP


@pytest.fixture
def live_video(monkeypatch):
    """Real OpenRouter video adapter (HTTP mocked by respx) behind an HTTPS
    public URL, so jobs are submitted with a callback_url and wait for a
    webhook. Everything else stays mocked."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://studio.example.com")
    monkeypatch.setenv("VIDEO_WEBHOOKS", "on")
    from app import config
    from app.providers.openrouter_video import OpenRouterVideoProvider
    from app.services import pipeline

    config.get_settings.cache_clear()
    monkeypatch.setattr(pipeline, "get_video", lambda: OpenRouterVideoProvider(api_key="sk-or", model="acme/vid-1"))
    with respx.mock(assert_all_called=False) as router:
        n = {"i": 0}
        bodies: list[dict] = []

        def _submit(request):
            n["i"] += 1
            bodies.append(json.loads(request.content))
            jid = f"v{n['i']}"
            return httpx.Response(202, json={"id": jid, "polling_url": f"{OR}/videos/{jid}", "status": "pending"})

        router.post(f"{OR}/videos").mock(side_effect=_submit)
        router.get(url__regex=rf"{re.escape(OR)}/videos/v\d+/content.*").mock(
            return_value=httpx.Response(200, content=_clip()))
        router.bodies = bodies
        yield router


def _jobs(**filters):
    from app import models
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        return [(j.id, j.callback_token, j.provider_job_id, j.status, j.cost_usd, j.purpose)
                for j in db.query(models.VideoJob).filter_by(**filters).order_by(models.VideoJob.id)]
    finally:
        db.close()


def _hook(client, token, jid, kind="completed", cost=0.25, headers=None, raw=None):
    body = raw if raw is not None else json.dumps({
        "type": f"video.generation.{kind}", "created_at": "2026-11-04T00:00:00Z",
        "data": {"id": jid, "status": kind, "unsigned_urls": [f"{OR}/videos/{jid}/content?index=0"],
                 "usage": {"cost": cost, "is_byok": False},
                 **({"error": "content policy"} if kind == "failed" else {})}})
    return client.post(f"/api/webhooks/openrouter/videos/{token}", content=body,
                       headers={"Content-Type": "application/json", **(headers or {})})


def _status(client, pid):
    return client.get(f"/api/projects/{pid}/status").json()


# ---------- signature ----------


def _sign(secret: str, raw: bytes, t: int | None = None) -> str:
    t = t or int(time.time())
    return f"t={t},v1=" + hmac.new(secret.encode(), f"{t},".encode() + raw, hashlib.sha256).hexdigest()


def test_signature_verification():
    from app.services.video_jobs import WebhookRejected, verify_signature

    raw = b'{"type":"video.generation.completed"}'
    verify_signature(raw, _sign("s3cret", raw), "s3cret")  # valid → no raise
    for header, why in [
        (None, "missing"),
        ("garbage", "malformed"),
        (_sign("other", raw), "mismatch"),
        (_sign("s3cret", raw + b" "), "mismatch"),  # body tampered
        (_sign("s3cret", raw, t=int(time.time()) - 301), "stale"),
    ]:
        with pytest.raises(WebhookRejected) as e:
            verify_signature(raw, header, "s3cret")
        assert e.value.status == 401 and why in str(e.value), why


def test_webhooks_enabled_needs_https(monkeypatch):
    from app import config
    from app.services import video_jobs

    for base, mode, mock, want in [
        ("https://a.example", "auto", "false", True),
        ("https://a.example", "auto", "true", False),  # mock providers complete inline
        ("http://localhost:8000", "on", "false", False),  # OpenRouter needs HTTPS
        ("https://a.example", "off", "false", False),
        ("https://a.example", "on", "true", True),
    ]:
        monkeypatch.setenv("PUBLIC_BASE_URL", base)
        monkeypatch.setenv("VIDEO_WEBHOOKS", mode)
        monkeypatch.setenv("MOCK_PROVIDERS", mock)
        config.get_settings.cache_clear()
        assert video_jobs.webhooks_enabled() is want, (base, mode, mock)


# ---------- render lifecycle ----------


def test_render_submits_all_shots_then_composes_once_on_webhooks(auth_client, live_video):
    pid = _planned(auth_client)
    auth_client.post(f"/api/projects/{pid}/generate-video?force=true")
    jobs = _jobs(project_id=pid)
    assert jobs and all(j[3] == "submitted" for j in jobs)
    # Every body shot went out up front (parallel), each with its own callback.
    assert len(live_video.bodies) == len(jobs)
    for (_, token, *_), body in zip(jobs, live_video.bodies, strict=True):
        assert body["callback_url"] == f"https://studio.example.com/api/webhooks/openrouter/videos/{token}"
        assert body["generate_audio"] is False
    st = _status(auth_client, pid)
    assert st["latest_render"]["status"] == "generating_shots"

    for i, (_, token, jid, *_rest) in enumerate(jobs):
        assert _hook(auth_client, token, jid).status_code == 204
        st = _status(auth_client, pid)
        last = i == len(jobs) - 1
        assert (st["latest_render"]["status"] == "completed") is last, st["latest_render"]
    assert st["project_status"] == "completed"
    assert all(j[3] == "completed" and j[4] == 0.25 for j in _jobs(project_id=pid))

    # A retried / duplicate delivery is a no-op: no second compose.
    renders_before = auth_client.get(f"/api/projects/{pid}/renders").json()
    _, token, jid, *_ = jobs[0]
    assert _hook(auth_client, token, jid).status_code == 204
    renders_after = auth_client.get(f"/api/projects/{pid}/renders").json()
    assert [(r["id"], r["status"], r.get("output_path")) for r in renders_after] == \
           [(r["id"], r["status"], r.get("output_path")) for r in renders_before]


def test_failed_webhook_fails_the_render(auth_client, live_video):
    pid = _planned(auth_client)
    auth_client.post(f"/api/projects/{pid}/generate-video?force=true")
    jobs = _jobs(project_id=pid)
    _, token, jid, *_ = jobs[0]
    assert _hook(auth_client, token, jid, kind="failed").status_code == 204
    for _, t, j, *_ in jobs[1:]:
        _hook(auth_client, t, j)
    st = _status(auth_client, pid)
    assert st["latest_render"]["status"] == "failed"
    failed = [s for s in st["shots"] if s["status"] == "failed"]
    assert failed and "content policy" in failed[0]["error"]


def test_webhook_rejections(auth_client, live_video, monkeypatch):
    pid = _planned(auth_client)
    auth_client.post(f"/api/projects/{pid}/generate-video?force=true")
    _, token, jid, *_ = _jobs(project_id=pid)[0]

    assert _hook(auth_client, "nope", jid).status_code == 404
    assert _hook(auth_client, token, "someone-elses-job").status_code == 400
    assert _hook(auth_client, token, jid, raw="{not json").status_code == 400

    from app import config

    monkeypatch.setenv("OPENROUTER_WEBHOOK_SECRET", "whsec")
    config.get_settings.cache_clear()
    assert _hook(auth_client, token, jid).status_code == 401  # unsigned
    raw = json.dumps({"type": "video.generation.completed",
                      "data": {"id": jid, "unsigned_urls": [f"{OR}/videos/{jid}/content"]}})
    # OpenRouter has no session cookie: the per-job token is the credential.
    from fastapi.testclient import TestClient

    from app.main import app

    anon = TestClient(app)
    assert anon.get(f"/api/projects/{pid}/status").status_code == 401
    r = _hook(anon, token, jid, raw=raw, headers={"X-OpenRouter-Signature": _sign("whsec", raw.encode())})
    assert r.status_code == 204
    assert _jobs(provider_job_id=jid)[0][3] == "completed"


def test_sweeper_completes_jobs_when_no_webhook_arrives(auth_client, live_video):
    """The poll path: webhook never comes; the sweeper polls until done."""
    from app import models
    from app.db import SessionLocal
    from app.services import video_jobs

    pid = _planned(auth_client)
    auth_client.post(f"/api/projects/{pid}/generate-video?force=true")
    jobs = _jobs(project_id=pid)
    state = {"status": "in_progress"}
    live_video.get(url__regex=rf"{re.escape(OR)}/videos/v\d+$").mock(side_effect=lambda req: httpx.Response(
        200, json={"id": req.url.path.rsplit("/", 1)[-1], "status": state["status"], "usage": {"cost": 0.4}}))

    def _make_due():
        db = SessionLocal()
        db.query(models.VideoJob).update({"next_check_at": video_jobs._now() - timedelta(seconds=1)})
        db.commit()
        db.close()

    _make_due()
    assert video_jobs.sweep() == len(jobs)
    assert {j[3] for j in _jobs(project_id=pid)} == {"running"}
    assert video_jobs.sweep() == 0  # rescheduled, not due yet

    state["status"] = "completed"
    _make_due()
    video_jobs.sweep()
    assert {(j[3], j[4]) for j in _jobs(project_id=pid)} == {("completed", 0.4)}
    assert _status(auth_client, pid)["latest_render"]["status"] == "completed"


def test_job_past_deadline_times_out(auth_client, live_video):
    from app import models
    from app.db import SessionLocal
    from app.services import video_jobs

    pid = _planned(auth_client)
    auth_client.post(f"/api/projects/{pid}/generate-video?force=true")
    live_video.get(url__regex=rf"{re.escape(OR)}/videos/v\d+$").mock(
        return_value=httpx.Response(200, json={"status": "pending"}))
    db = SessionLocal()
    past = video_jobs._now() - timedelta(seconds=1)
    db.query(models.VideoJob).update({"next_check_at": past, "deadline_at": past})
    db.commit()
    db.close()
    video_jobs.sweep()
    st = _status(auth_client, pid)
    assert st["latest_render"]["status"] == "failed"
    assert any("timed out" in s["error"] for s in st["shots"])


def test_regenerate_recomposes_after_webhook(auth_client, live_video):
    pid = _planned(auth_client)
    auth_client.post(f"/api/projects/{pid}/generate-video?force=true")
    for _, t, j, *_ in _jobs(project_id=pid):
        _hook(auth_client, t, j)
    st = _status(auth_client, pid)
    first_render = st["latest_render"]["id"]
    shot = next(s for s in st["shots"] if s["shot_type"] != "end_card")

    auth_client.post(f"/api/projects/{pid}/shots/{shot['id']}/regenerate?recompose=true")
    assert _status(auth_client, pid)["project_status"] == "regenerating"
    _, t, j, *_ = _jobs(project_id=pid)[-1]
    assert _hook(auth_client, t, j).status_code == 204
    st = _status(auth_client, pid)
    assert st["project_status"] == "completed"
    assert st["latest_render"]["id"] != first_render and st["latest_render"]["status"] == "completed"

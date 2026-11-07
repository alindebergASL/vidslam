"""Sogni image + video provider: workflow REST contract (respx-mocked),
status mapping, errors, per-model routing, catalog, spend estimates."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

SOGNI = "https://api.sogni.ai"
WF = f"{SOGNI}/v1/creative-agent/workflows"
FAKE_KEY = "sk-test-123456789"
ART_IMG = "https://storage.example.net/signed/img.png?sig=abc"
ART_VID = "https://storage.example.net/signed/clip.mp4?sig=abc"
PNG = b"\x89PNG\r\n\x1a\nfake-sogni"
MP4 = b"\x00\x00\x00\x18ftypmp42fake"


def _reset():
    from app import config
    from app.services import provider_keys

    config.get_settings.cache_clear()
    provider_keys.invalidate()


@pytest.fixture
def live_sogni(monkeypatch):
    """Providers live with a fake Sogni key; nothing sleeps."""
    monkeypatch.setenv("MOCK_PROVIDERS", "false")
    monkeypatch.setenv("SOGNI_API_KEY", FAKE_KEY)
    from app.providers import sogni

    monkeypatch.setattr(sogni, "_sleep", lambda s: None)
    _reset()
    yield
    _reset()


def _wf(status: str, **extra) -> dict:
    return {"status": "success", "data": {"workflow": {"workflowId": "wf_1", "status": status, **extra}}}


def _started(request):
    return httpx.Response(201, json=_wf("queued"))


# ---------- image ----------


@pytest.mark.parametrize("where", ["steps", "artifacts", "events"])
def test_image_start_poll_download(client, live_sogni, where):
    from app.providers.base import ImageRef
    from app.providers.sogni import SogniImageProvider

    done: dict = {"steps": [{"id": "s1", "toolName": "generate_image"}], "artifacts": []}
    if where == "steps":
        done["steps"][0]["artifacts"] = [{"id": "a1", "kind": "image", "url": ART_IMG}]
    elif where == "artifacts":
        done["artifacts"] = [{"id": "x", "kind": "image", "url": "https://wrong", "stepId": "other"},
                             {"id": "a1", "kind": "image", "url": ART_IMG, "stepId": "s1"}]
    with respx.mock(assert_all_called=False) as router:
        start = router.post(WF).mock(side_effect=_started)
        polls = router.get(f"{WF}/wf_1").mock(side_effect=[
            httpx.Response(200, json=_wf("running")),
            httpx.Response(200, json=_wf("completed", **done)),
        ])
        events = router.get(f"{WF}/wf_1/events").mock(return_value=httpx.Response(200, json={
            "status": "success", "data": {"events": [
                {"id": "e1", "type": "workflow_started"},
                {"id": "e2", "type": "artifact_created",
                 "data": {"artifact": {"id": "a1", "kind": "image", "url": ART_IMG, "stepId": "s1"}}},
            ]}}))
        dl = router.get(ART_IMG).mock(return_value=httpx.Response(200, content=PNG))
        out = SogniImageProvider().generate(
            model="sogni/krea-2-turbo", prompt="a misty lake", negative_prompt="people",
            references=[ImageRef(url="https://x/char.png", role="character")],
            settings={"aspect_ratio": "16:9"})
    assert out == PNG
    assert polls.call_count == 2 and dl.called
    assert events.called == (where == "events")
    req = start.calls[0].request
    assert req.headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert req.headers["user-agent"] == "vidslam/1.0"
    assert req.headers["x-app-source"] == "vidslam"
    assert len(req.headers["idempotency-key"]) >= 32
    body = json.loads(req.content)
    assert set(body) == {"input", "token_type", "app_source", "safe_content_filter"}  # snake_case only
    # No NSFW gate: Sogni's content filter is off unless SOGNI_SAFE_CONTENT_FILTER=true.
    assert body["token_type"] == "auto" and body["safe_content_filter"] is False
    step = body["input"]["steps"][0]
    assert step["id"] == "s1" and step["toolName"] == "generate_image"
    assert step["arguments"] == {"prompt": "a misty lake", "model": "krea-2-turbo", "numberOfVariations": 1,
                                 "outputFormat": "png", "negativePrompt": "people", "aspectRatio": "16:9"}
    # The signed URL is fetched as given, without our key.
    assert "authorization" not in dl.calls[0].request.headers


def test_image_img2img_uses_media_references_and_records_estimate(client, live_sogni):
    from app import models
    from app.db import SessionLocal
    from app.providers.base import ImageRef
    from app.providers.sogni import SogniImageProvider

    done = _wf("completed", steps=[{"id": "s1", "artifacts": [{"id": "a", "kind": "image", "url": ART_IMG}]}])
    with respx.mock() as router:
        start = router.post(WF).mock(return_value=httpx.Response(201, json=done))
        router.get(ART_IMG).mock(return_value=httpx.Response(200, content=PNG))
        SogniImageProvider().generate(model="sogni/krea-2-turbo", prompt="p",
                                      references=[ImageRef(url="https://x/src.png", frame_type="first_frame")])
    body = json.loads(start.calls[0].request.content)
    assert body["media_references"] == [{"kind": "image", "url": "https://x/src.png"}]
    args = body["input"]["steps"][0]["arguments"]
    assert args["sourceImageIndex"] == -1 and args["starting_image_strength"] == 0.35
    db = SessionLocal()
    try:
        row = db.query(models.SpendEntry).one()
        assert (row.provider, row.kind, row.model, row.is_estimate, row.source) == \
            ("sogni", "image", "sogni/krea-2-turbo", True, "rate_card")
        assert row.cost_usd == pytest.approx(2.43 * 0.005)
    finally:
        db.close()


def test_image_failure_states_raise_readable_errors(client, live_sogni):
    from app.providers.sogni import SogniImageProvider

    with respx.mock() as router:
        router.post(WF).mock(return_value=httpx.Response(201, json=_wf(
            "waiting_for_user", waitingReason="insufficient_credit", awaitingCostApproval=False)))
        with pytest.raises(RuntimeError, match="waiting for: insufficient_credit"):
            SogniImageProvider().generate(model="sogni/krea-2-turbo", prompt="p", references=[])


# ---------- errors ----------


def test_error_envelope_is_readable_and_never_has_the_key(client, live_sogni):
    from app.providers.sogni import SogniImageProvider

    with respx.mock() as router:
        router.post(WF).mock(return_value=httpx.Response(400, json={
            "status": "error", "errorCode": 102, "message": f"bad input near {FAKE_KEY}"}))
        with pytest.raises(RuntimeError) as ei:
            SogniImageProvider().generate(model="sogni/krea-2-turbo", prompt="p", references=[])
    msg = str(ei.value)
    assert msg.startswith("Sogni 400 102: bad input")
    assert FAKE_KEY not in msg


def test_402_says_not_enough_credit(client, live_sogni):
    from app.providers.sogni import SogniImageProvider

    with respx.mock() as router:
        router.post(WF).mock(return_value=httpx.Response(402, json={
            "status": "error", "errorCode": 4000, "message": "Insufficient balance",
            "details": {"vendorModels": ["gpt-image-2"]}}))
        with pytest.raises(RuntimeError, match=r"Sogni 402 4000: not enough Sogni credit \(Spark\)"):
            SogniImageProvider().generate(model="sogni/gpt-image-2", prompt="p", references=[])


def test_429_is_retried_with_the_same_idempotency_key(client, live_sogni, monkeypatch):
    from app.providers import sogni

    waits: list[float] = []
    monkeypatch.setattr(sogni, "_sleep", waits.append)
    done = _wf("completed", artifacts=[{"id": "a", "kind": "image", "url": ART_IMG, "stepId": "s1"}])
    with respx.mock() as router:
        start = router.post(WF).mock(side_effect=[
            httpx.Response(429, headers={"Retry-After": "3"},
                           json={"status": "error", "errorCode": 126, "message": "slow down"}),
            httpx.Response(201, json=done),
        ])
        router.get(ART_IMG).mock(return_value=httpx.Response(200, content=PNG))
        assert sogni.SogniImageProvider().generate(model="sogni/krea-2-turbo", prompt="p", references=[]) == PNG
    keys = {c.request.headers["idempotency-key"] for c in start.calls}
    assert start.call_count == 2 and len(keys) == 1
    assert waits == [3.0]


def test_429_with_a_long_wait_fails_fast(client, live_sogni):
    from app.providers.sogni import SogniImageProvider

    with respx.mock() as router:
        start = router.post(WF).mock(return_value=httpx.Response(429, json={
            "status": "error", "errorCode": 126, "message": "Creative workflow start rate limit exceeded.",
            "retryAfter": 1837}))
        with pytest.raises(RuntimeError, match="retry after 1837 s"):
            SogniImageProvider().generate(model="sogni/krea-2-turbo", prompt="p", references=[])
    assert start.call_count == 1


# ---------- video ----------


def test_video_text_to_video_submit(client, live_sogni):
    from app.providers.sogni import SogniVideoProvider

    with respx.mock() as router:
        start = router.post(WF).mock(side_effect=_started)
        job = SogniVideoProvider().submit(
            model="sogni/ltx25", prompt="waves", references=[], negative_prompt="text",
            duration_seconds=7.4, settings={"aspect_ratio": "16:9", "generate_audio": False,
                                            "callback_url": "https://x/hook"})
    assert (job.provider, job.model, job.job_id) == ("sogni", "sogni/ltx25", "wf_1")
    body = json.loads(start.calls[0].request.content)
    assert "callback_url" not in json.dumps(body)  # no webhooks on Sogni
    assert "media_references" not in body
    step = body["input"]["steps"][0]
    assert step["toolName"] == "generate_video"
    assert step["arguments"] == {"prompt": "waves", "videoModel": "ltx25", "duration": 8,
                                 "generateAudio": False, "aspectRatio": "16:9", "negativePrompt": "text"}


def test_video_first_frame_uses_animate_photo(client, live_sogni):
    from app.providers.base import ImageRef
    from app.providers.sogni import SogniVideoProvider

    with respx.mock() as router:
        start = router.post(WF).mock(side_effect=_started)
        SogniVideoProvider().submit(
            model="sogni/wan3.0-video", prompt="drift", duration_seconds=10,
            references=[ImageRef(url="https://x/first.png", frame_type="first_frame"),
                        ImageRef(url="https://x/first.png", frame_type="last_frame")],
            reference_strategy="frame_images")
    body = json.loads(start.calls[0].request.content)
    assert body["media_references"] == [{"kind": "image", "url": "https://x/first.png"}]
    step = body["input"]["steps"][0]
    assert step["toolName"] == "animate_photo"
    args = step["arguments"]
    assert args["sourceImageIndex"] == -1 and args["frameRole"] == "start"  # last frame not relied on
    assert args["videoModel"] == "wan3.0-video" and args["duration"] == 10


def test_seedance_first_frame_is_a_loose_reference(client, live_sogni):
    from app.providers.base import ImageRef
    from app.providers.sogni import SogniVideoProvider

    with respx.mock() as router:
        start = router.post(WF).mock(side_effect=_started)
        SogniVideoProvider().submit(
            model="sogni/seedance2-5", prompt="drift", negative_prompt="text", duration_seconds=2,
            references=[ImageRef(url="https://x/first.png", frame_type="first_frame")],
            reference_strategy="frame_images")
    body = json.loads(start.calls[0].request.content)
    args = body["input"]["steps"][0]["arguments"]
    assert body["input"]["steps"][0]["toolName"] == "generate_video"
    assert args["referenceImageIndices"] == [-1] and args["prompt"].startswith("Use @Image1 as the first frame.")
    assert "negativePrompt" not in args and args["duration"] == 4  # snapped to Seedance's minimum


@pytest.mark.parametrize("payload,state,err", [
    ({"status": "queued"}, "queued", None),
    ({"status": "running"}, "running", None),
    ({"status": "completed", "artifacts": [{"id": "a", "kind": "video", "url": ART_VID, "stepId": "s1",
                                            "durationSeconds": 5}]}, "succeeded", None),
    ({"status": "partial_failure", "steps": [{"id": "s1", "artifacts": [{"id": "a", "kind": "video",
                                                                         "url": ART_VID}]}]}, "succeeded", None),
    ({"status": "partial_failure", "artifacts": []}, "failed", "without a video"),
    ({"status": "failed", "error": {"message": "GPU fell over"}}, "failed", "GPU fell over"),
    ({"status": "cancelled"}, "failed", "cancelled"),
    ({"status": "waiting_for_user", "waitingReason": "safety_review_required"}, "failed",
     "waiting for: safety_review_required"),
    ({"status": "waiting_for_user", "waitingReason": "cost_approval_required", "awaitingCostApproval": True},
     "failed", "cost approval"),
])
def test_video_poll_maps_every_status(client, live_sogni, payload, state, err):
    from app.providers.base import SubmittedJob
    from app.providers.sogni import SogniVideoProvider

    with respx.mock(assert_all_called=False) as router:
        router.get(f"{WF}/wf_1").mock(return_value=httpx.Response(
            200, json={"status": "success", "data": {"workflow": {"workflowId": "wf_1", **payload}}}))
        router.get(f"{WF}/wf_1/events").mock(return_value=httpx.Response(200, json={"data": {"events": []}}))
        st = SogniVideoProvider().poll(SubmittedJob(provider="sogni", model="sogni/ltx23", job_id="wf_1"))
    assert st.state == state
    if err:
        assert err in (st.error or "")
    if state == "succeeded":
        assert st.result_url == ART_VID
        assert st.cost == pytest.approx(3.5 * 5 * 0.005)  # LTX-2.3 ≈ 17.5 Spark per 5 s


def test_video_poll_reads_a_bare_data_snapshot(client, live_sogni):
    from app.providers.base import SubmittedJob
    from app.providers.sogni import SogniVideoProvider

    with respx.mock() as router:
        router.get(f"{WF}/wf_1").mock(return_value=httpx.Response(200, json={"data": {
            "workflowId": "wf_1", "status": "completed",
            "steps": [{"id": "s1", "artifacts": [{"id": "a", "kind": "video", "url": ART_VID}]}]}}))
        st = SogniVideoProvider().poll(SubmittedJob(provider="sogni", model="sogni/ltx25", job_id="wf_1"))
    assert st.state == "succeeded" and st.result_url == ART_VID


def test_video_download_has_no_auth_header(client, live_sogni):
    from app.providers.base import SubmittedJob
    from app.providers.sogni import SogniVideoProvider

    with respx.mock() as router:
        dl = router.get(ART_VID).mock(return_value=httpx.Response(200, content=MP4))
        data = SogniVideoProvider().download(SubmittedJob(provider="sogni", model="sogni/ltx25", job_id="wf_1"),
                                             result_url=ART_VID)
    assert data == MP4
    assert "authorization" not in dl.calls[0].request.headers


def test_capabilities():
    from app.providers import sogni

    ltx = sogni.capabilities_for("sogni/ltx25")
    assert ltx.known and ltx.first_frame and not ltx.last_frame
    assert (min(ltx.durations), ltx.max_seconds) == (2, 20)
    s25 = sogni.capabilities_for("sogni/seedance2-5")
    assert not s25.first_frame and (min(s25.durations), s25.max_seconds) == (4, 30)
    assert sogni.capabilities_for("sogni/seedance2").max_seconds == 15
    assert sogni.capabilities_for("sogni/happyhorse-1.1-i2v").durations[0] == 3
    assert sogni.capabilities_for("sogni/wan3.0-video").max_seconds == 30
    assert not sogni.capabilities_for("sogni/no-such-model").known


# ---------- routing ----------


def test_provider_for_rules(client, monkeypatch):
    from app.services import model_registry as reg

    assert reg.provider_for("image", "sogni/krea-2-turbo") == "mock"  # MOCK_PROVIDERS on
    monkeypatch.setenv("MOCK_PROVIDERS", "false")
    _reset()
    assert reg.provider_for("video", "sogni/ltx25") == "mock"  # no key
    monkeypatch.setenv("SOGNI_API_KEY", FAKE_KEY)
    _reset()
    assert reg.provider_for("video", "sogni/ltx25") == "sogni"
    assert reg.provider_for("image", "sogni/krea-2-turbo") == "sogni"
    assert reg.provider_for("image", "openai/gpt-image-2") == "mock"  # no OpenRouter key
    _reset()


def test_mock_mode_keeps_sogni_models_offline(client):
    from app.providers import get_image, get_video, video_capabilities
    from app.providers.mock_image import MockImageProvider
    from app.providers.mock_video import MockVideoProvider

    with respx.mock() as router:  # any real request would fail: no routes
        assert isinstance(get_image("sogni/krea-2-turbo"), MockImageProvider)
        assert isinstance(get_video("sogni/ltx25"), MockVideoProvider)
        video_capabilities("sogni/ltx25")
    assert not router.calls


def test_sogni_model_routes_to_sogni_while_openrouter_is_active(client, monkeypatch, live_sogni):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-1234")
    _reset()
    from app.providers import get_image, get_video, video_capabilities
    from app.providers.openrouter_image import OpenRouterImageProvider
    from app.providers.sogni import SogniImageProvider, SogniVideoProvider
    from app.services import model_registry as reg

    reg.set_active("image", "openai/gpt-image-2")
    reg.set_active("video", "alibaba/wan-3.0")
    assert isinstance(get_image(), OpenRouterImageProvider)
    assert isinstance(get_image("sogni/krea-2-turbo"), SogniImageProvider)
    assert isinstance(get_video("sogni/ltx25"), SogniVideoProvider)
    assert video_capabilities("sogni/seedance2-5").max_seconds == 30  # no HTTP: static table
    # And the other way round: a Sogni model active, an OpenRouter override.
    reg.set_active("image", "sogni/krea-2-turbo")
    assert isinstance(get_image(), SogniImageProvider)
    assert isinstance(get_image("openai/gpt-image-2"), OpenRouterImageProvider)


def test_scene_still_model_goes_to_sogni(client, live_sogni, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-1234")
    _reset()
    from app.db import SessionLocal
    from app.schemas.ambient import AmbientConfig
    from app.services import ambient
    from app.services import model_registry as reg

    from .test_ambient import SHORT

    reg.set_active("image", "openai/gpt-image-2")
    cfg = AmbientConfig(**{**SHORT, "still_model": "sogni/krea-2-turbo"})
    done = _wf("completed", steps=[{"id": "s1", "artifacts": [{"id": "a", "kind": "image", "url": ART_IMG}]}])
    db = SessionLocal()
    try:
        with respx.mock() as router:  # only Sogni is mocked: an OpenRouter call would fail
            start = router.post(WF).mock(return_value=httpx.Response(201, json=done))
            router.get(ART_IMG).mock(return_value=httpx.Response(200, content=PNG))
            png, used = ambient.generate_still(db, cfg, prompt="p", aspect_ratio="9:16")
    finally:
        db.close()
    assert (png, used) == (PNG, "sogni/krea-2-turbo")
    args = json.loads(start.calls[0].request.content)["input"]["steps"][0]["arguments"]
    assert args["aspectRatio"] == "9:16" and args["model"] == "krea-2-turbo"
    assert ambient.still_cost(cfg) == pytest.approx(2.43 * 0.005)


def test_shot_override_submits_to_sogni_and_is_polled(client, live_sogni, monkeypatch):
    """A shot's sogni/ override goes to Sogni (provider "sogni", no webhook,
    even with webhooks on), the sweeper's check completes it, and spend
    records the price-table estimate."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-1234")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://studio.example.com")
    monkeypatch.setenv("VIDEO_WEBHOOKS", "on")
    _reset()
    from app import models
    from app.db import SessionLocal
    from app.services import model_registry as reg
    from app.services import pipeline, video_jobs

    reg.set_active("video", "alibaba/wan-3.0")
    db = SessionLocal()
    try:
        project = models.VideoProject(title="t")
        db.add(project)
        db.commit()
        shot = models.VideoShot(project_id=project.id, shot_order=0, prompt="waves", duration_seconds=5.0,
                                model_override="sogni/ltx23")
        db.add(shot)
        db.commit()
        with respx.mock() as router:
            start = router.post(WF).mock(side_effect=_started)
            job = pipeline.submit_shot_clip(db, project, shot, after="none", references=[])
            assert (job.provider, job.model, job.uses_webhook) == ("sogni", "sogni/ltx23", False)
            assert shot.provider == "sogni" and shot.provider_model == "sogni/ltx23"
            assert "callback_url" not in start.calls[0].request.content.decode()
            assert isinstance(video_jobs.provider_for(job), __import__(
                "app.providers.sogni", fromlist=["SogniVideoProvider"]).SogniVideoProvider)
            router.get(f"{WF}/wf_1").mock(return_value=httpx.Response(200, json=_wf(
                "completed", artifacts=[{"id": "a", "kind": "video", "url": ART_VID, "stepId": "s1",
                                         "durationSeconds": 5}])))
            router.get(ART_VID).mock(return_value=httpx.Response(200, content=MP4))
            assert video_jobs.check(db, job.id) == "completed"
        db.refresh(shot)
        assert shot.status == "completed"
        row = db.query(models.SpendEntry).filter_by(ref=f"video_job:{job.id}").one()
        assert (row.provider, row.is_estimate, row.source) == ("sogni", True, "rate_card")
        assert row.cost_usd == pytest.approx(17.5 * 0.005)
    finally:
        db.close()


def test_requested_duration_survives_submit_for_cost(client, live_sogni, monkeypatch):
    """The snapshot reports no duration: the 20 s actually requested (kept in
    context_json at submit) prices the job and the spend row, not a 5 s default."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-1234")
    _reset()
    from app import models
    from app.db import SessionLocal
    from app.providers.sogni import video_cost_usd
    from app.services import model_registry as reg
    from app.services import pipeline, video_jobs

    reg.set_active("video", "alibaba/wan-3.0")
    db = SessionLocal()
    try:
        project = models.VideoProject(title="t")
        db.add(project)
        db.commit()
        shot = models.VideoShot(project_id=project.id, shot_order=0, prompt="waves", duration_seconds=20.0,
                                model_override="sogni/ltx25")
        db.add(shot)
        db.commit()
        with respx.mock() as router:
            start = router.post(WF).mock(side_effect=_started)
            job = pipeline.submit_shot_clip(db, project, shot, after="none", references=[])
            sent = json.loads(start.calls[0].request.content)["input"]["steps"][0]["arguments"]
            assert sent["duration"] == 20
            assert job.context_json["duration"] == 20
            router.get(f"{WF}/wf_1").mock(return_value=httpx.Response(200, json=_wf(
                "completed", artifacts=[{"id": "a", "kind": "video", "url": ART_VID, "stepId": "s1"}])))
            router.get(ART_VID).mock(return_value=httpx.Response(200, content=MP4))
            assert video_jobs.check(db, job.id) == "completed"
        db.refresh(job)
        expected = video_cost_usd("sogni/ltx25", 20)
        assert expected > video_cost_usd("sogni/ltx25", 5)
        assert job.cost_usd == pytest.approx(expected)
        row = db.query(models.SpendEntry).filter_by(ref=f"video_job:{job.id}").one()
        assert row.cost_usd == pytest.approx(expected)
    finally:
        db.close()


def test_scene_reference_is_the_img2img_source(client, live_sogni):
    """Ambient drift/anchor variants pass the first still as role="scene"
    with no frame_type: on a Sogni still model it must be sent as img2img."""
    from app.providers.base import ImageRef
    from app.providers.sogni import SogniImageProvider

    done = _wf("completed", steps=[{"id": "s1", "artifacts": [{"id": "a", "kind": "image", "url": ART_IMG}]}])
    with respx.mock() as router:
        start = router.post(WF).mock(return_value=httpx.Response(201, json=done))
        router.get(ART_IMG).mock(return_value=httpx.Response(200, content=PNG))
        SogniImageProvider().generate(
            model="sogni/krea-2-turbo", prompt="the same place at night",
            references=[ImageRef(url="https://x/char.png", role="character"),
                        ImageRef(url="https://x/first.png", role="scene")],
            settings={"aspect_ratio": "16:9"})
    body = json.loads(start.calls[0].request.content)
    assert body["media_references"] == [{"kind": "image", "url": "https://x/first.png"}]
    args = body["input"]["steps"][0]["arguments"]
    assert args["sourceImageIndex"] == -1 and args["starting_image_strength"] == 0.35


def test_non_json_2xx_is_a_sogni_error(client, live_sogni):
    from app.providers.sogni import SogniClient, SogniError

    html = httpx.Response(200, text="<html>cloudflare</html>", headers={"content-type": "text/html"})
    with respx.mock() as router:
        router.post(WF).mock(return_value=html)
        router.get(f"{WF}/wf_1").mock(return_value=html)
        router.get(f"{WF}/wf_1/events").mock(return_value=httpx.Response(200, json=["not", "a", "dict"]))
        c = SogniClient()
        for call in (lambda: c.start(title="t", tool="generate_image", arguments={}),
                     lambda: c.get("wf_1"), lambda: c.events("wf_1")):
            with pytest.raises(SogniError) as ei:
                call()
            assert "cloudflare" not in str(ei.value) and FAKE_KEY not in str(ei.value)


def test_image_settings_short_circuit(live_sogni):
    from app.services.image_settings import best_image_settings

    with respx.mock() as router:
        assert best_image_settings("sogni/gpt-image-2.5-sunburst", "9:16") == {"aspect_ratio": "9:16"}
    assert not router.calls


# ---------- catalog ----------


def test_catalog_lists_sogni_models(client, monkeypatch):
    from app.services import model_catalog

    def handler(request):
        return httpx.Response(200, json={"data": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        snap = model_catalog.refresh(client=c)
    sogni = {(e["kind"], e["id"]): e for e in snap["entries"] if e["provider"] == "sogni"}
    assert ("image", "sogni/krea-2-turbo") in sogni and ("video", "sogni/seedance2-5") in sogni
    v = sogni[("video", "sogni/ltx25")]
    assert v["max_seconds"] == 20 and v["frame_images"] == ["first_frame"] and "first frame" in v["notes"]
    assert sogni[("video", "sogni/seedance2-5")]["frame_images"] == []
    assert all(not e["usable"] for e in sogni.values())  # no key yet
    assert model_catalog.find("video", "sogni/wan3.0-video") is not None
    monkeypatch.setenv("SOGNI_API_KEY", FAKE_KEY)
    _reset()
    assert model_catalog.find("image", "sogni/krea-2-turbo")["usable"] is True
    _reset()


def test_old_snapshot_gets_sogni_entries(client):
    from app.services import model_catalog
    from app.services import model_registry as reg

    reg.put_setting(model_catalog.CATALOG_KEY, {"fetched_at": "2026-11-01T00:00:00+00:00", "entries": [
        {"id": "acme/x", "kind": "image", "name": "x", "provider": "openrouter", "usable": False, "new": False}]})
    assert model_catalog.find("image", "sogni/krea-2-turbo") is not None
    assert model_catalog.find("image", "acme/x") is not None


def test_content_filter_can_be_turned_back_on(monkeypatch):
    from app import config
    from app.providers import sogni

    monkeypatch.setenv("SOGNI_SAFE_CONTENT_FILTER", "true")
    config.get_settings.cache_clear()
    try:
        assert config.get_settings().sogni_safe_content_filter is True
    finally:
        monkeypatch.delenv("SOGNI_SAFE_CONTENT_FILTER")
        config.get_settings.cache_clear()
    assert config.get_settings().sogni_safe_content_filter is False
    assert sogni.PREFIX == "sogni/"


def test_project_estimate_prices_sogni_shots_at_their_rate(auth_client):
    """A shot on a sogni/ video model is priced at that model's Spark rate,
    not the flat COST_VIDEO_PER_SECOND."""
    import pytest

    from app import models
    from app.config import get_settings
    from app.db import SessionLocal
    from app.providers import sogni

    from .test_cost_and_bulk import _project_with_plan

    pid = _project_with_plan(auth_client)

    def video_line():
        est = auth_client.get(f"/api/projects/{pid}/cost-estimate").json()
        return next(ln for ln in est["lines"] if ln["item"].startswith("Video clips"))

    before = video_line()
    db = SessionLocal()
    try:
        shots = [s for s in db.get(models.VideoProject, pid).shots if s.shot_type != "end_card"]
        shots[0].model_override = "sogni/ltx25"
        db.commit()
        first, rest = shots[0].duration_seconds, sum(s.duration_seconds for s in shots[1:])
    finally:
        db.close()
    after = video_line()
    want = first * sogni.video_rate_usd("sogni/ltx25") + rest * get_settings().cost_video_per_second
    assert after["cost"] == pytest.approx(want, abs=1e-3)
    assert after["cost"] != before["cost"]

"""A refusal for content is final: the ambient still chain and the lip-sync
fallback never redo a refused request on another model (which may have no
filter at all). Outages, timeouts and other errors still fall back."""
from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx

from .test_ambient import SHORT
from .test_review_fixes import OR, _talking_project

SOGNI_WF = "https://api.sogni.ai/v1/creative-agent/workflows"
OR_IMAGES = f"{OR}/images"
PNG = b"\x89PNG\r\n\x1a\nfake"

# Refusals as providers word them (OpenRouter keeps the vendor's reason).
REFUSALS = [
    "Sogni is waiting for: safety_review_required",
    "OpenRouter image 400: Your request was rejected by the safety system. (OpenAI)",
    'OpenRouter image 403: openai/gpt-image-2 requires moderation on OpenAI. Your input was flagged for "sexual".',
    "OpenRouter image 400: Output data may contain inappropriate content. (Alibaba)",
    "OpenRouter image 400: The content was blocked by Green net (Alibaba)",
    "OpenRouter image 400: Image generation stopped: IMAGE_SAFETY (Google)",
    "OpenRouter image 400: The prompt contains PROHIBITED_CONTENT (Google)",
    "video submit 400: {\"error\": {\"message\": \"content_policy_violation\"}}",
    "video submit 400: {\"code\": \"InputTextSensitiveContentDetected\"}",
    "avatar submit 400: input new_sensitive (1026)",
    "Failure to pass the risk control system",
    "NSFW content detected",
    "Sogni workflow failed: The prompt violates our usage policies",
    "The input image violates the provider's usage guidelines",
    "Generation failed: sexually explicit content",
    "The model refused to make this image",
    "Runware refused this request for its content (contentModeration): flagged.",
    "Your prompt was flagged as potentially violating our policy",
]
# Outages and other errors: these may still fall back.
OUTAGES = [
    "OpenRouter image 429: Rate limit exceeded (Google)",
    "OpenRouter image 503: Service Unavailable",
    "OpenRouter image 404: No endpoints found matching your data policy (Free model publication)",
    "OpenRouter image 403: Key limit exceeded",
    "OpenRouter image 402: Insufficient credits",
    "OpenRouter image 502: Provider returned error (Google)",
    "Sogni unreachable: ConnectError",
    "Sogni image timed out after 300 s (workflow wf_1 still running)",
    "Sogni is waiting for cost approval (not approved automatically)",
    "Sogni is waiting for: insufficient_credit",
    "Sogni 429 rate_limited: slow down (retry after 120 s)",
    "Sogni workflow failed: GPU fell over",
    "video submit 500: internal error",
    'avatar submit 400: {"error": {"message": "duration must be explicit and <= 30"}}',
    "[Errno 111] Connection refused",
    "timed out after 30 min",
    "Runware error 500 internalError: something broke",
    "Runware task 1234 didn't finish in time",
    "Sorry, you have been blocked",
    "unexpected image response shape: keys=[]",
    "child process exited with code 1",
    "submit failed: ReadTimeout",
]


@pytest.mark.parametrize("text", REFUSALS)
def test_refusal_texts_are_refusals(text):
    from app.providers.refusals import is_content_refusal, is_refusal_text

    assert is_refusal_text(text)
    assert is_content_refusal(RuntimeError(text))


@pytest.mark.parametrize("text", OUTAGES)
def test_outage_texts_are_not_refusals(text):
    from app.providers.refusals import is_content_refusal, is_refusal_text

    assert not is_refusal_text(text)
    assert not is_content_refusal(RuntimeError(text))


def test_refusals_marked_by_the_provider_or_the_app():
    from app.providers.refusals import ContentRefused, is_content_refusal
    from app.providers.runware import RunwareError
    from app.providers.sogni import SogniError
    from app.services.safety import UnsafePromptError, UnsafeScriptError

    assert is_content_refusal(RunwareError("Runware error 400 x: y", refused=True))
    assert not is_content_refusal(RunwareError("Runware error 400 x: y"))
    assert is_content_refusal(SogniError("Sogni is waiting for: hold", refused=True))
    assert not is_content_refusal(SogniError("Sogni is waiting for: insufficient_credit"))
    assert is_content_refusal(UnsafeScriptError("refused"))
    assert is_content_refusal(UnsafePromptError("refused"))
    assert is_content_refusal(ContentRefused("refused"))
    try:  # the error it was raised from counts too
        try:
            raise RunwareError("bad", refused=True)
        except RunwareError as inner:
            raise RuntimeError("couldn't make the still") from inner
    except RuntimeError as outer:
        assert is_content_refusal(outer)


def test_runware_model_without_a_content_check_refuses_finally(client, monkeypatch):
    """A model with no content check asked to take a Cast picture: refused,
    and marked so a fallback chain doesn't hand the picture to another model."""
    from app.providers import runware
    from app.providers.base import ImageRef
    from app.providers.refusals import is_content_refusal

    from .test_runware import _CORE, _IMG, _fake_model

    model = _fake_model(monkeypatch, "test:nocheck@1", {
        "type": "object", "additionalProperties": False, "required": ["taskType", "taskUUID", "model"],
        "properties": {**_CORE, "inputs": {"type": "object", "properties": {
            "referenceImages": {"type": "array", "items": _IMG}}}}},
        modes=[{"id": "text"}], caps=["text-to-image"])
    with pytest.raises(runware.RunwareError) as e:
        runware.build_image_task(model, "her portrait", [ImageRef(url="https://x/face.jpg", role="character")])
    assert e.value.refused and is_content_refusal(e.value)


# ---------- Sogni pauses ----------


@pytest.mark.parametrize("wf, held", [
    ({"status": "waiting_for_user", "waitingReason": "safety_review_required"}, True),
    ({"status": "waiting_for_user"}, True),  # a reason we don't know: a person must look at it
    ({"status": "waiting_for_user", "waitingReason": "cost_approval_required", "awaitingCostApproval": True}, False),
    ({"status": "waiting_for_user", "waitingReason": "insufficient_credit"}, False),
    ({"status": "failed", "error": {"message": "GPU fell over"}}, False),
    ({"status": "running"}, False),
])
def test_sogni_pauses_held_for_review(wf, held):
    from app.providers.sogni import held_for_review

    assert held_for_review(wf) is held


@pytest.fixture
def live_sogni_and_openrouter(monkeypatch):
    """Sogni and OpenRouter live with fake keys: the still chain is a Sogni
    model, then the OpenRouter fallback (both mocked with respx)."""
    from app import config
    from app.providers import sogni
    from app.services import model_registry as reg
    from app.services import provider_keys

    monkeypatch.setenv("MOCK_PROVIDERS", "false")
    monkeypatch.setenv("SOGNI_API_KEY", "sk-test-123456789")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-1234")
    monkeypatch.setattr(sogni, "_sleep", lambda s: None)
    config.get_settings.cache_clear()
    provider_keys.invalidate()
    reg.set_active("image", "openai/gpt-image-2")
    yield
    config.get_settings.cache_clear()
    provider_keys.invalidate()


def _sogni_paused(reason: str) -> httpx.Response:
    return httpx.Response(201, json={"status": "success", "data": {"workflow": {
        "workflowId": "wf_1", "status": "waiting_for_user", "waitingReason": reason}}})


def _still(cfg, **kw):
    from app.db import SessionLocal
    from app.services import ambient

    db = SessionLocal()
    try:
        return ambient.generate_still(db, cfg, prompt="a quiet lake at dawn", aspect_ratio="16:9", **kw)
    finally:
        db.close()


def test_sogni_safety_pause_is_final_for_an_ambient_still(client, live_sogni_and_openrouter):
    from app.providers.sogni import SogniError
    from app.schemas.ambient import AmbientConfig
    from app.services import ambient

    cfg = AmbientConfig(**{**SHORT, "still_model": "sogni/krea-2-turbo"})
    assert ambient.still_model_chain(cfg)[1] == "google/gemini-nano-banana-2.1"  # there is a fallback
    with respx.mock(assert_all_called=False) as router:
        start = router.post(SOGNI_WF).mock(return_value=_sogni_paused("safety_review_required"))
        fallback = router.post(OR_IMAGES).mock(return_value=httpx.Response(200, json={
            "data": [{"b64_json": base64.b64encode(PNG).decode()}]}))
        with pytest.raises(SogniError, match="safety_review_required") as e:
            _still(cfg)
    assert e.value.refused
    assert start.called and not fallback.called  # the unfiltered fallback never saw it


def test_sogni_credit_pause_still_falls_back(client, live_sogni_and_openrouter):
    from app.schemas.ambient import AmbientConfig

    cfg = AmbientConfig(**{**SHORT, "still_model": "sogni/krea-2-turbo"})
    with respx.mock() as router:
        router.post(SOGNI_WF).mock(return_value=_sogni_paused("insufficient_credit"))
        fallback = router.post(OR_IMAGES).mock(return_value=httpx.Response(200, json={
            "data": [{"b64_json": base64.b64encode(PNG).decode()}]}))
        png, used = _still(cfg)
    assert (png, used) == (PNG, "google/gemini-nano-banana-2.1")
    assert json.loads(fallback.calls[0].request.content)["model"] == "google/gemini-nano-banana-2.1"


class _Images:
    """Raises `error` for the first model it's asked for; any other works."""

    def __init__(self, error: Exception):
        self.error = error
        self.models: list[str] = []

    def generate(self, *, model, prompt, references, negative_prompt="", settings=None):
        self.models.append(model)
        if len(self.models) == 1:
            raise self.error
        return b"\x89PNG-from-" + model.encode()


@pytest.mark.parametrize("error", [
    httpx.HTTPStatusError("OpenRouter image 400: Your request was rejected by the safety system. (OpenAI)",
                          request=None, response=None),  # type: ignore[arg-type]
    RuntimeError("OpenRouter image 400: Output data may contain inappropriate content. (Alibaba)"),
    "runware",
    "minors",
])
def test_a_refused_still_is_never_retried_on_the_fallback_model(client, monkeypatch, error):
    from app import models
    from app.db import SessionLocal
    from app.providers.runware import RunwareError
    from app.schemas.ambient import AmbientConfig
    from app.services import ambient
    from app.services.safety import UnsafePromptError

    if error == "runware":
        error = RunwareError("Runware refused this request for its content (nsfw): no.", refused=True)
    elif error == "minors":
        error = UnsafePromptError("refused for minors")
    fake = _Images(error)
    monkeypatch.setattr(ambient, "get_image", lambda *a, **k: fake)
    with pytest.raises(type(error)):
        _still(AmbientConfig(**SHORT))
    assert fake.models == ["openai/gpt-image-2.6-sunburst"]  # Nano Banana never got it
    if not isinstance(error, UnsafePromptError):
        with SessionLocal() as db:  # recorded for the provider-error alerts, marked refused
            row = db.query(models.ProviderLog).filter_by(status="error").one()
            assert (row.response_summary_json or {}).get("refused") is True


def test_a_refused_same_place_still_tries_nothing_else(client, monkeypatch):
    """Day to night's "same place" stills: a refusal with the picture isn't
    followed by the other model, with or without the picture."""
    from app.providers.base import ImageRef
    from app.schemas.ambient import AmbientConfig
    from app.services import ambient

    cfg = AmbientConfig(**SHORT)
    assert len(ambient.reference_still_attempts(cfg)) >= 2
    fake = _Images(RuntimeError("OpenRouter image 403: Your input was flagged by moderation (OpenAI)"))
    monkeypatch.setattr(ambient, "get_image", lambda *a, **k: fake)
    with pytest.raises(RuntimeError, match="flagged"):
        _still(cfg, references=[ImageRef(url="https://x/scene.jpg", role="scene", cast_picture=True)],
               edit_prompt="make it night")
    assert len(fake.models) == 1


def test_an_outage_still_falls_back(client, monkeypatch):
    from app.schemas.ambient import AmbientConfig
    from app.services import ambient

    fake = _Images(httpx.ReadTimeout("timed out"))
    monkeypatch.setattr(ambient, "get_image", lambda *a, **k: fake)
    png, used = _still(AmbientConfig(**SHORT))
    assert used == "google/gemini-nano-banana-2.1" and fake.models == ["openai/gpt-image-2.6-sunburst", used]


# ---------- lip-sync ----------


def _lipsync_on(client):
    r = client.put("/api/models/active", json={"kind": "lipsync", "model": "heygen/avatar-iv",
                                               "allow_unlisted": True})
    assert r.status_code == 200, r.text


def _shots_and_jobs(pid: int):
    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        project = db.get(models.VideoProject, pid)
        th = [(s.status, s.provider, s.error) for s in project.shots if s.shot_type == "talking_head"]
        video = db.query(models.VideoJob).filter_by(project_id=pid, purpose="shot").count()
        render = (db.query(models.Render).filter_by(project_id=pid).order_by(models.Render.id.desc()).first())
        return th, video, (render.status, render.error) if render else ("", "")


def test_lipsync_refused_at_submit_fails_the_shot(auth_client, monkeypatch):
    from app.providers import mock_avatar

    def refuse(self, **kw):
        raise RuntimeError('avatar submit 400: {"error": {"message": "Your input was flagged by content moderation"}}')

    monkeypatch.setattr(mock_avatar.MockAvatarProvider, "submit_avatar", refuse)
    _lipsync_on(auth_client)
    pid = _talking_project(auth_client)
    auth_client.post(f"/api/projects/{pid}/generate-video?force=true")
    th, video_jobs, (status, error) = _shots_and_jobs(pid)
    assert status == "failed" and "refused this shot for its content" in error
    assert th[0][0] == "failed" and "wasn't redone on the video model" in th[0][2]
    assert video_jobs == 0  # the video model never got the talking head


def test_lipsync_outage_at_submit_still_uses_the_video_model(auth_client, monkeypatch):
    from app.providers import mock_avatar

    def down(self, **kw):
        raise RuntimeError("avatar submit 503: Service Unavailable")

    monkeypatch.setattr(mock_avatar.MockAvatarProvider, "submit_avatar", down)
    _lipsync_on(auth_client)
    pid = _talking_project(auth_client)
    auth_client.post(f"/api/projects/{pid}/generate-video?force=true")
    th, video_jobs, (status, error) = _shots_and_jobs(pid)
    assert status == "completed", error
    assert th[0][1] == "mock" and th[0][2].startswith("lip-sync failed, used video model instead")
    assert video_jobs >= 1


def _avatar_job_fails(auth_client, monkeypatch, error: str) -> int:
    """A talking-head render whose lip-sync job (on OpenRouter) is accepted,
    then reported failed by webhook with `error`."""
    from app import config, models, providers
    from app.db import SessionLocal
    from app.providers.openrouter_avatar import OpenRouterAvatarProvider

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
    config.get_settings.cache_clear()
    real = OpenRouterAvatarProvider(api_key="sk-or")
    monkeypatch.setattr(providers, "get_avatar", lambda: real)
    pid = _talking_project(auth_client)
    with respx.mock(assert_all_called=False) as router:
        router.post(f"{OR}/videos").mock(return_value=httpx.Response(202, json={
            "id": "a1", "polling_url": f"{OR}/videos/a1"}))
        auth_client.post(f"/api/projects/{pid}/generate-video?force=true")
        with SessionLocal() as db:
            token = db.query(models.VideoJob).filter_by(purpose="avatar").one().callback_token
        r = auth_client.post(f"/api/webhooks/openrouter/videos/{token}", json={
            "type": "video.generation.failed", "data": {"id": "a1", "status": "failed", "error": error}})
        assert r.status_code == 204
    return pid


def test_lipsync_job_refused_later_fails_the_shot(auth_client, monkeypatch):
    pid = _avatar_job_fails(auth_client, monkeypatch, "Content moderation: the input image was flagged")
    th, video_jobs, (status, error) = _shots_and_jobs(pid)
    assert th[0][0] == "failed" and "refused this shot for its content" in th[0][2]
    assert "flagged" in th[0][2]
    assert video_jobs == 0
    assert status == "failed" and "refused this shot for its content" in error


def test_lipsync_job_outage_later_still_uses_the_video_model(auth_client, monkeypatch):
    pid = _avatar_job_fails(auth_client, monkeypatch, "Provider returned error: upstream timeout")
    th, video_jobs, (status, error) = _shots_and_jobs(pid)
    assert video_jobs == 1
    assert th[0][2].startswith("lip-sync failed, used video model instead")
    assert status == "completed", error

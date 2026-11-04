"""OpenRouter video adapter against the documented API contract (respx)."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

BASE = "https://openrouter.ai/api/v1"

MODELS = {
    "data": [
        {"id": "google/veo-3.1", "supported_durations": [4, 6, 8],
         "supported_frame_images": ["first_frame", "last_frame"]},
        {"id": "runway/gen-4.5", "supported_durations": [2, 3, 4, 5, 6, 7, 8, 9, 10],
         "supported_frame_images": ["first_frame"]},
        {"id": "alibaba/wan-3.0", "supported_durations": list(range(2, 31)),
         "supported_frame_images": ["first_frame", "last_frame"]},
        {"id": "black-forest-labs/flux-video-upscale", "supported_durations": None,
         "supported_frame_images": None},
    ]
}


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    from app import config

    config.get_settings.cache_clear()
    from app.providers import openrouter_video as ov

    ov._reset_models_cache()
    return ov.OpenRouterVideoProvider(api_key="sk-test", model="google/veo-3.1")


@respx.mock
def test_capabilities_and_listing(provider):
    route = respx.get(f"{BASE}/videos/models").mock(return_value=httpx.Response(200, json=MODELS))
    veo = provider.capabilities("google/veo-3.1")
    assert veo.durations == [4, 6, 8] and veo.first_frame and veo.last_frame
    runway = provider.capabilities("runway/gen-4.5")
    assert runway.max_seconds == 10 and runway.first_frame and not runway.last_frame
    assert provider.capabilities("alibaba/wan-3.0").max_seconds == 30
    unknown = provider.capabilities("nobody/model")
    assert unknown.known is False and unknown.max_seconds == 10
    ids = [m.id for m in provider.list_models()]
    assert "black-forest-labs/flux-video-upscale" not in ids  # not a generator
    assert any("up to 30s" in m.name for m in provider.list_models())
    assert route.call_count == 1  # cached


@respx.mock
def test_submit_body_matches_contract(provider):
    respx.get(f"{BASE}/videos/models").mock(return_value=httpx.Response(200, json=MODELS))
    sub = respx.post(f"{BASE}/videos").mock(
        return_value=httpx.Response(202, json={"id": "abc", "polling_url": f"{BASE}/videos/abc", "status": "pending"})
    )
    from app.providers.base import ImageRef

    job = provider.submit(
        model="google/veo-3.1",
        prompt="ocean",
        negative_prompt="people",
        references=[
            ImageRef(url="https://x/a.png", role="scene", frame_type="first_frame"),
            ImageRef(url="https://x/b.png", role="scene", frame_type="last_frame"),
        ],
        reference_strategy="frame_images",
        duration_seconds=7,
        settings={"aspect_ratio": "16:9", "generate_audio": False},
    )
    body = json.loads(sub.calls[0].request.content)
    assert body["duration"] == 8  # snapped up to a supported length
    assert "duration_seconds" not in body
    assert body["aspect_ratio"] == "16:9"
    assert body["generate_audio"] is False
    assert body["prompt"].endswith("Avoid: people")
    assert body["frame_images"] == [
        {"type": "image_url", "image_url": {"url": "https://x/a.png"}, "frame_type": "first_frame"},
        {"type": "image_url", "image_url": {"url": "https://x/b.png"}, "frame_type": "last_frame"},
    ]
    assert job.job_id == "abc" and job.polling_url == f"{BASE}/videos/abc"


@respx.mock
def test_last_frame_dropped_for_first_frame_only_model(provider):
    respx.get(f"{BASE}/videos/models").mock(return_value=httpx.Response(200, json=MODELS))
    sub = respx.post(f"{BASE}/videos").mock(return_value=httpx.Response(202, json={"id": "j"}))
    from app.providers.base import ImageRef

    provider.submit(
        model="runway/gen-4.5", prompt="p", reference_strategy="frame_images",
        references=[ImageRef(url="u1", frame_type="first_frame"), ImageRef(url="u2", frame_type="last_frame")],
        duration_seconds=30,
    )
    body = json.loads(sub.calls[0].request.content)
    assert body["duration"] == 10  # capped at the model max
    assert [f["frame_type"] for f in body["frame_images"]] == ["first_frame"]


@respx.mock
def test_poll_and_download_use_unsigned_url_with_auth(provider):
    from app.providers.base import SubmittedJob

    job = SubmittedJob(provider="openrouter", model="m", job_id="abc", polling_url=f"{BASE}/videos/abc")
    respx.get(f"{BASE}/videos/abc").mock(side_effect=[
        httpx.Response(200, json={"status": "in_progress"}),
        httpx.Response(200, json={"status": "completed", "unsigned_urls": [f"{BASE}/videos/abc/content?index=0"]}),
        httpx.Response(200, json={"status": "completed", "unsigned_urls": [f"{BASE}/videos/abc/content?index=0"]}),
    ])
    content = respx.get(f"{BASE}/videos/abc/content").mock(return_value=httpx.Response(200, content=b"MP4DATA"))
    assert provider.poll(job).state == "running"
    assert provider.poll(job).state == "succeeded"
    assert provider.download(job) == b"MP4DATA"
    assert content.calls[0].request.headers["Authorization"] == "Bearer sk-test"


@respx.mock
def test_failed_and_expired_states(provider):
    from app.providers.base import SubmittedJob

    job = SubmittedJob(provider="openrouter", model="m", job_id="z", polling_url=f"{BASE}/videos/z")
    respx.get(f"{BASE}/videos/z").mock(side_effect=[
        httpx.Response(200, json={"status": "failed", "error": {"message": "content policy"}}),
        httpx.Response(200, json={"status": "expired"}),
    ])
    st = provider.poll(job)
    assert st.state == "failed" and "content policy" in st.error
    assert provider.poll(job).state == "failed"


@respx.mock
def test_discovery_failure_falls_back_to_default_cap(provider):
    respx.get(f"{BASE}/videos/models").mock(return_value=httpx.Response(503))
    caps = provider.capabilities("google/veo-3.1")
    assert caps.known is False and caps.max_seconds == 10

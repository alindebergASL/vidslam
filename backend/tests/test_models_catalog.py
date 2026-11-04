"""Model catalog polling, runtime switching, and the new adapters
(Lyria, OpenRouter TTS / gpt-audio, Avatar IV, Image API, Claude JSON)."""
from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx

from .conftest import make_png_bytes

OR = "https://openrouter.ai/api/v1"
GEM = "https://generativelanguage.googleapis.com/v1beta"

MODELS = {"data": [
    {"id": "anthropic/claude-sonnet-x", "name": "Claude Sonnet X", "context_length": 200000,
     "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
     "pricing": {"prompt": "0.000003", "completion": "0.000015"},
     "supported_parameters": ["tools", "temperature"]},
    {"id": "openai/gpt-4o-mini:batch", "name": "GPT-4o mini (batch)",
     "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}},
    {"id": "openai/gpt-4o-mini", "name": "GPT-4o mini",
     "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
     "supported_parameters": ["response_format", "temperature"]},
    {"id": "openai/gpt-audio-mini", "name": "GPT Audio Mini",
     "architecture": {"input_modalities": ["text", "audio"], "output_modalities": ["text", "audio"]}},
    {"id": "openai/gpt-4o-mini-tts", "name": "GPT-4o mini TTS",
     "architecture": {"input_modalities": ["text"], "output_modalities": ["speech"]},
     "supported_voices": ["alloy", "nova", "shimmer"]},
    {"id": "google/gemini-2.5-flash-image", "name": "Nano Banana",
     "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["image", "text"]}},
], "total_count": 5}
IMAGES = {"data": [{"id": "openai/gpt-image-2", "name": "GPT Image 2"},
                   {"id": "google/gemini-2.5-flash-image", "name": "Nano Banana"}]}
VIDEOS = {"data": [
    {"id": "alibaba/wan-3.0", "name": "Wan 3.0", "supported_durations": list(range(2, 31)),
     "supported_frame_images": ["first_frame", "last_frame"]},
    {"id": "heygen/heygen-video-1", "name": "HeyGen Video", "supported_durations": list(range(5, 31)),
     "supported_frame_images": ["first_frame"], "supported_resolutions": ["480p", "768p", "1080p", "2K"]},
    {"id": "heygen/avatar-iv", "name": "HeyGen: Avatar IV", "supported_durations": None,
     "supported_frame_images": None, "pricing_skus": {"duration_seconds": "0.05"}},
    {"id": "black-forest-labs/flux-video-upscale", "supported_durations": None},
]}
GEMINI_MODELS = {"models": [
    {"name": "models/lyria-3.5", "displayName": "Lyria 3.5"},
    {"name": "models/lyria-3-clip-preview", "displayName": "Lyria 3 Clip"},
    {"name": "models/lyria-realtime-exp", "displayName": "Lyria RealTime"},
    {"name": "models/gemini-3-pro", "displayName": "Gemini"},
]}


def _mock_sources(models=MODELS, videos=VIDEOS):
    respx.get(f"{OR}/models").mock(return_value=httpx.Response(200, json=models))
    respx.get(f"{OR}/images/models").mock(return_value=httpx.Response(200, json=IMAGES))
    respx.get(f"{OR}/videos/models").mock(return_value=httpx.Response(200, json=videos))
    respx.get(f"{GEM}/models").mock(return_value=httpx.Response(200, json=GEMINI_MODELS))


def _real(monkeypatch, **extra):
    env = {"MOCK_PROVIDERS": "false", "OPENROUTER_API_KEY": "sk-or", "GEMINI_API_KEY": "g-key",
           "ELEVENLABS_API_KEY": "el-key", **extra}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from app import config

    config.get_settings.cache_clear()


def _ids(snap, kind):
    return {e["id"] for e in snap["entries"] if e["kind"] == kind}


# ---------- catalog ----------


@respx.mock
def test_catalog_groups_models_by_capability(auth_client, monkeypatch):
    _real(monkeypatch)
    _mock_sources()
    snap = auth_client.post("/api/models/refresh").json()
    assert _ids(snap, "chat") == {"anthropic/claude-sonnet-x", "openai/gpt-4o-mini"}
    assert _ids(snap, "tts") == {"openai/gpt-audio-mini", "openai/gpt-4o-mini-tts", "elevenlabs"}
    assert _ids(snap, "image") == {"google/gemini-2.5-flash-image", "openai/gpt-image-2"}
    assert _ids(snap, "video") == {"alibaba/wan-3.0", "heygen/heygen-video-1"}
    assert _ids(snap, "lipsync") == {"heygen/avatar-iv"}
    assert _ids(snap, "music") == {"google/lyria-3.5", "google/lyria-3-clip-preview", "elevenlabs"}
    wan = next(e for e in snap["entries"] if e["id"] == "alibaba/wan-3.0")
    assert wan["max_seconds"] == 30 and "last_frame" in wan["frame_images"]
    tts = next(e for e in snap["entries"] if e["id"] == "openai/gpt-4o-mini-tts")
    assert tts["supported_voices"] == ["alloy", "nova", "shimmer"]
    assert all(e["usable"] for e in snap["entries"])
    assert snap["new_count"] == 0  # first poll flags nothing
    assert snap["errors"] == {}

    # Filtered view + active markers.
    v = auth_client.get("/api/models/catalog?kind=video").json()
    assert {e["kind"] for e in v["entries"]} == {"video"}
    assert v["active"]["video"]["model"] == "mock/video-default"  # no .env model → mock default


@respx.mock
def test_second_poll_flags_new_models_and_survives_source_outage(auth_client, monkeypatch):
    _real(monkeypatch)
    _mock_sources()
    auth_client.post("/api/models/refresh")
    newer = {"data": VIDEOS["data"] + [{"id": "bytedance/seedance-3", "supported_durations": [5, 10, 60],
                                         "supported_frame_images": ["first_frame", "last_frame"]}]}
    respx.get(f"{OR}/videos/models").mock(return_value=httpx.Response(200, json=newer))
    respx.get(f"{OR}/models").mock(return_value=httpx.Response(503))
    snap = auth_client.post("/api/models/refresh").json()
    new = [e for e in snap["entries"] if e["new"]]
    assert [e["id"] for e in new] == ["bytedance/seedance-3"]
    assert "openrouter_models" in snap["errors"]
    # Chat models from the previous poll are kept through the outage.
    assert _ids(snap, "chat") == {"anthropic/claude-sonnet-x", "openai/gpt-4o-mini"}


@respx.mock
def test_catalog_browsable_without_keys(auth_client):
    _mock_sources()
    snap = auth_client.get("/api/models/catalog?refresh=true").json()
    video = [e for e in snap["entries"] if e["kind"] == "video"]
    assert video and not any(e["usable"] for e in video)
    # No Gemini key → documented Lyria models listed as not usable.
    lyria = [e for e in snap["entries"] if e["id"].startswith("google/lyria")]
    assert {e["id"] for e in lyria} >= {"google/lyria-3.5"} and not any(e["usable"] for e in lyria)


def test_poller_not_started_under_pytest(auth_client):
    from app.services import model_catalog

    assert model_catalog._thread is None or not model_catalog._thread.is_alive()


# ---------- switching ----------


@respx.mock
def test_switch_active_models_and_reset(auth_client, monkeypatch):
    _real(monkeypatch)
    _mock_sources()
    auth_client.post("/api/models/refresh")
    r = auth_client.put("/api/models/active", json={"kind": "video", "model": "heygen/heygen-video-1"})
    assert r.status_code == 200, r.text
    assert r.json()["video"] == {"model": "heygen/heygen-video-1", "provider": "openrouter",
                                 "default": "mock/video-default", "is_default": False, "options": {}}
    # Unlisted ids are refused unless explicitly allowed.
    assert auth_client.put("/api/models/active", json={"kind": "video", "model": "nobody/x"}).status_code == 422
    assert auth_client.put("/api/models/active",
                           json={"kind": "video", "model": "nobody/x", "allow_unlisted": True}).status_code == 200
    assert auth_client.put("/api/models/active", json={"kind": "bogus", "model": "x"}).status_code == 422
    # Reset → env default.
    snap = auth_client.put("/api/models/active", json={"kind": "video", "model": None}).json()
    assert snap["video"]["is_default"] is True
    # Status endpoint reflects selections.
    auth_client.put("/api/models/active", json={"kind": "music", "model": "google/lyria-3.5"})
    st = auth_client.get("/api/providers/status").json()
    assert st["music"] == "gemini" and st["models"]["music"] == "google/lyria-3.5"


@respx.mock
def test_registry_builds_provider_for_active_model(auth_client, monkeypatch):
    _real(monkeypatch)
    _mock_sources()
    auth_client.post("/api/models/refresh")
    from app.providers import get_avatar, get_chat, get_music, get_tts, get_video
    from app.providers.lyria_music import LyriaMusicProvider
    from app.providers.openrouter_avatar import OpenRouterAvatarProvider
    from app.providers.openrouter_tts import OpenRouterTTSProvider

    put = lambda k, m, **o: auth_client.put("/api/models/active", json={"kind": k, "model": m, **o})
    put("video", "alibaba/wan-3.0")
    assert get_video().model == "alibaba/wan-3.0"
    put("chat", "anthropic/claude-sonnet-x")
    chat = get_chat()
    assert chat.model == "anthropic/claude-sonnet-x" and chat.json_mode is False
    put("chat", "openai/gpt-4o-mini")
    assert get_chat().json_mode is True
    put("music", "google/lyria-3.5")
    assert isinstance(get_music(), LyriaMusicProvider)
    put("tts", "openai/gpt-audio-mini")
    t = get_tts()
    assert isinstance(t, OpenRouterTTSProvider) and t.chat_audio is True
    put("tts", "openai/gpt-4o-mini-tts")
    t = get_tts()
    assert t.chat_audio is False and t.voices == ["alloy", "nova", "shimmer"]
    assert get_avatar() is None  # lip-sync off by default
    put("lipsync", "heygen/avatar-iv")
    assert isinstance(get_avatar(), OpenRouterAvatarProvider)


# ---------- adapters ----------


@respx.mock
def test_lyria_request_and_parse(monkeypatch):
    _real(monkeypatch)
    from app.providers.lyria_music import LyriaMusicProvider

    route = respx.post(f"{GEM}/interactions").mock(return_value=httpx.Response(200, json={
        "id": "i1", "status": "completed",
        "steps": [{"type": "user_input"},
                  {"type": "model_output", "content": [{"type": "text", "text": "(instrumental)"},
                                                       {"type": "audio", "mime_type": "audio/mpeg",
                                                        "data": base64.b64encode(b"ID3mp3").decode()}]}],
    }))
    out = LyriaMusicProvider(model="google/lyria-3.5").generate(prompt="soft spa pads", duration_seconds=90)
    assert out == b"ID3mp3"
    req = route.calls[0].request
    body = json.loads(req.content)
    assert body["model"] == "lyria-3.5"
    assert "1 minute 30 seconds" in body["input"] and "no vocals" in body["input"].lower()
    assert req.headers["x-goog-api-key"] == "g-key" and req.headers["Api-Revision"]


@respx.mock
def test_openrouter_speech_mp3_then_pcm_fallback(monkeypatch):
    _real(monkeypatch)
    from app.providers.openrouter_tts import OpenRouterTTSProvider

    route = respx.post(f"{OR}/audio/speech").mock(side_effect=[
        httpx.Response(200, content=b"MP3DATA", headers={"content-type": "audio/mpeg"}),
        httpx.Response(400, text="this model only supports pcm"),
        httpx.Response(200, content=b"\x00\x01" * 2400, headers={"content-type": "audio/pcm"}),
    ])
    p = OpenRouterTTSProvider(model="openai/gpt-4o-mini-tts", voices=["nova"], chat_audio=False)
    assert p.synthesize(text="hi", voice_id="elevenlabs-xyz") == b"MP3DATA"
    b0 = json.loads(route.calls[0].request.content)
    assert b0 == {"model": "openai/gpt-4o-mini-tts", "input": "hi", "voice": "nova", "response_format": "mp3"}
    wav = p.synthesize(text="hi", voice_id="")
    assert wav[:4] == b"RIFF" and json.loads(route.calls[2].request.content)["response_format"] == "pcm"


@respx.mock
def test_gpt_audio_streams_pcm_into_wav(monkeypatch):
    _real(monkeypatch)
    from app.providers.openrouter_tts import OpenRouterTTSProvider

    chunks = [base64.b64encode(b"\x01\x00" * 100).decode(), base64.b64encode(b"\x02\x00" * 100).decode()]
    sse = "".join(f"data: {json.dumps({'choices': [{'delta': {'audio': {'data': c}}}]})}\n\n" for c in chunks)
    sse += "data: [DONE]\n\n"
    route = respx.post(f"{OR}/chat/completions").mock(
        return_value=httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"}))
    wav = OpenRouterTTSProvider(model="openai/gpt-audio-mini").synthesize(text="hello", voice_id="")
    assert wav[:4] == b"RIFF" and len(wav) == 44 + 400
    body = json.loads(route.calls[0].request.content)
    assert body["modalities"] == ["text", "audio"] and body["stream"] is True
    assert body["audio"] == {"voice": "alloy", "format": "pcm16"}


@respx.mock
def test_image_api_endpoint_and_parse(monkeypatch):
    _real(monkeypatch)
    from app.providers.base import ImageRef
    from app.providers.openrouter_image import OpenRouterImageProvider

    png = make_png_bytes()
    route = respx.post(f"{OR}/images").mock(return_value=httpx.Response(200, json={
        "data": [{"b64_json": base64.b64encode(png).decode(), "media_type": "image/png"}]}))
    out = OpenRouterImageProvider(model="openai/gpt-image-2").generate(
        model="openai/gpt-image-2", prompt="ocean", negative_prompt="people",
        references=[ImageRef(url="https://x/r.png")], settings={"aspect_ratio": "16:9"})
    assert out == png
    body = json.loads(route.calls[0].request.content)
    assert body["aspect_ratio"] == "16:9" and body["prompt"].endswith("Avoid: people")
    assert body["input_references"] == [{"type": "image_url", "image_url": {"url": "https://x/r.png"}}]


@respx.mock
def test_avatar_iv_request_shape(monkeypatch):
    _real(monkeypatch)
    from app.providers.openrouter_avatar import OpenRouterAvatarProvider

    sub = respx.post(f"{OR}/videos").mock(return_value=httpx.Response(202, json={
        "id": "av1", "polling_url": f"{OR}/videos/av1", "status": "pending"}))
    respx.get(f"{OR}/videos/av1").mock(return_value=httpx.Response(200, json={
        "status": "completed", "unsigned_urls": [f"{OR}/videos/av1/content?index=0"]}))
    content = respx.get(f"{OR}/videos/av1/content").mock(return_value=httpx.Response(200, content=b"MP4"))
    out = OpenRouterAvatarProvider().animate(
        portrait_url="https://h/p.png", audio_url="https://h/a.m4a", script="Hi there",
        aspect_ratio="9:16", voice_id="v42", poll_seconds=0)
    assert out == b"MP4"
    body = json.loads(sub.calls[0].request.content)
    assert body["model"] == "heygen/avatar-iv" and body["prompt"] == "Hi there"
    assert body["input_references"] == [
        {"type": "image_url", "image_url": {"url": "https://h/p.png"}},
        {"type": "audio_url", "audio_url": {"url": "https://h/a.m4a"}},
    ]
    assert body["provider"] == {"options": {"heygen": {"voice_id": "v42"}}}
    assert content.calls[0].request.headers["Authorization"] == "Bearer sk-or"


@respx.mock
def test_claude_storyboard_without_json_mode(monkeypatch):
    _real(monkeypatch)
    from app.providers.base import CastContext
    from app.providers.openrouter_chat import OpenRouterChatProvider

    from app.providers.mock_chat import MockChatProvider

    plan = MockChatProvider().generate_storyboard(
        project={"title": "t", "mode": "reel_montage", "original_script": "Hello world.",
                 "target_duration_seconds": 10}, cast=CastContext()).model_dump(mode="json")
    reply = "Here is the storyboard:\n```json\n" + json.dumps(plan) + "\n```"
    route = respx.post(f"{OR}/chat/completions").mock(return_value=httpx.Response(200, json={
        "choices": [{"message": {"content": reply}}]}))
    p = OpenRouterChatProvider(model="anthropic/claude-sonnet-x")
    try:
        out = p.generate_storyboard(project={"title": "t"}, cast=CastContext())
    except Exception as e:  # schema mismatch is a test-fixture problem, not parsing
        pytest.fail(f"parse failed: {e}")
    assert out.cleaned_voice_script == plan["cleaned_voice_script"] and len(out.shots) == len(plan["shots"])
    assert "response_format" not in json.loads(route.calls[0].request.content)


# ---------- talking-head lip-sync in the pipeline ----------


def _talking_head_project(client) -> int:
    aid = client.post("/api/avatars", json={"name": "Host"}).json()["id"]
    client.post(f"/api/avatars/{aid}/assets", files={"file": ("h.png", make_png_bytes(), "image/png")},
                data={"asset_type": "hero", "rights_confirmed": "true"})
    pid = client.post("/api/projects", json={
        "title": "TH", "original_script": "Hello there, this is a short talking head test.",
        "mode": "talking_head_beta", "target_duration_seconds": 10, "primary_avatar_id": aid,
        "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}]}).json()["id"]
    assert client.post(f"/api/projects/{pid}/generate-plan").status_code == 202
    return pid


def test_talking_head_shot_uses_lipsync_model_when_active(auth_client):
    pid = _talking_head_project(auth_client)
    r = auth_client.put("/api/models/active",
                        json={"kind": "lipsync", "model": "heygen/avatar-iv", "allow_unlisted": True})
    assert r.json()["lipsync"]["provider"] == "mock"
    assert auth_client.post(f"/api/projects/{pid}/generate-video?force=true").status_code == 202
    st = auth_client.get(f"/api/projects/{pid}/status").json()
    assert st["latest_render"]["status"] == "completed", st["latest_render"]["error"]
    th = [s for s in st["shots"] if s["shot_type"] == "talking_head"]
    assert th and all(s["provider"] == "lipsync" and s["provider_model"] == "heygen/avatar-iv" for s in th)


def test_talking_head_falls_back_to_video_model_when_lipsync_fails(auth_client, monkeypatch):
    from app.providers import mock_avatar

    def boom(self, **kw):
        raise RuntimeError("avatar outage")

    monkeypatch.setattr(mock_avatar.MockAvatarProvider, "animate", boom)
    pid = _talking_head_project(auth_client)
    auth_client.put("/api/models/active", json={"kind": "lipsync", "model": "heygen/avatar-iv", "allow_unlisted": True})
    auth_client.post(f"/api/projects/{pid}/generate-video?force=true")
    st = auth_client.get(f"/api/projects/{pid}/status").json()
    assert st["latest_render"]["status"] == "completed"
    th = [s for s in st["shots"] if s["shot_type"] == "talking_head"]
    assert th and th[0]["provider"] == "mock" and "lip-sync failed" in th[0]["error"]


def test_public_media_route(auth_client, tmp_path):
    from app.services import public_media

    f = tmp_path / "v.m4a"
    f.write_bytes(b"audio")
    url = public_media.publish(f)
    path = url.split("http://test.local", 1)[1]
    r = auth_client.get(path)
    assert r.status_code == 200 and r.content == b"audio" and r.headers["content-type"] == "audio/mp4"
    assert auth_client.get("/api/public-media/../../etc/passwd").status_code == 404
    assert auth_client.get("/api/public-media/short.mp3").status_code == 404


@respx.mock
def test_per_job_pickers_include_polled_catalog(auth_client):
    _mock_sources()
    auth_client.post("/api/models/refresh")
    vids = auth_client.get("/api/providers/openrouter/video-models").json()
    ids = [m["id"] for m in vids]
    assert ids[0] == "mock/video-default"  # what the active provider can serve comes first
    assert "alibaba/wan-3.0" in ids and "heygen/heygen-video-1" in ids
    wan = next(m for m in vids if m["id"] == "alibaba/wan-3.0")
    assert "up to 30s" in wan["name"] and "first+last frame" in wan["name"]
    assert "needs API key" in wan["description"]
    imgs = [m["id"] for m in auth_client.get("/api/providers/openrouter/image-models").json()]
    assert "openai/gpt-image-2" in imgs

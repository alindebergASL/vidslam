from __future__ import annotations

import respx
from httpx import Response


def test_studio_image_job_completes_in_mock(auth_client):
    aid = auth_client.post("/api/avatars", json={"name": "N"}).json()["id"]
    r = auth_client.post(
        "/api/studio/generate-image",
        json={
            "owner_kind": "avatar",
            "owner_id": aid,
            "prompt": "N in a Brooklyn rooftop scene, warm light",
            "reference_asset_ids": [],
        },
    )
    assert r.status_code == 202
    job = r.json()
    assert job["status"] in ("pending", "submitted", "polling", "completed")
    # Inline execution should have already finished.
    j = auth_client.get(f"/api/studio/jobs/{job['id']}").json()
    assert j["status"] == "completed"
    assert j["result_path"]
    # Save into the avatar's asset library.
    r = auth_client.post(f"/api/studio/jobs/{job['id']}/save", params={"asset_type": "lifestyle"})
    assert r.status_code == 200
    asset = r.json()
    assert asset["source"] == "generated"
    # Asset is now visible under the avatar.
    assets = auth_client.get(f"/api/avatars/{aid}/assets").json()
    assert any(a["id"] == asset["id"] for a in assets)


def test_studio_clip_job_completes_in_mock(auth_client):
    iid = auth_client.post(
        "/api/ingredients", json={"name": "Rooftop", "kind": "scene"}
    ).json()["id"]
    r = auth_client.post(
        "/api/studio/generate-clip",
        json={
            "owner_kind": "ingredient",
            "owner_id": iid,
            "prompt": "Slow push-in on the rooftop, golden hour glow",
        },
    )
    assert r.status_code == 202
    j = auth_client.get(f"/api/studio/jobs/{r.json()['id']}").json()
    assert j["status"] == "completed"
    assert j["output_kind"] == "video_clip"


def test_elevenlabs_voices_returns_mock_in_mock_mode(auth_client):
    r = auth_client.get("/api/providers/elevenlabs/voices")
    assert r.status_code == 200
    voices = r.json()
    assert isinstance(voices, list)
    assert len(voices) >= 1
    assert "voice_id" in voices[0] and "name" in voices[0]


def test_provider_status_endpoint(auth_client):
    r = auth_client.get("/api/providers/status")
    assert r.status_code == 200
    body = r.json()
    # All channels should be mocked in tests.
    assert body == {
        "mock_providers_env": True,
        "chat": "mock",
        "image": "mock",
        "video": "mock",
        "tts": "mock",
        "music": "mock",
        # The lipsync slot is a Protocol with only a mock impl for now —
        # surfaced in status so a future real adapter is observable.
        "lipsync": "mock",
        # Custom-model training routes by kind: voice clones go through
        # ElevenLabs when the key is set, LoRAs through Replicate.
        # Both default to the mock training provider in tests.
        "training_lora": "mock",
        "training_voice": "mock",
    }


@respx.mock
def test_openrouter_chat_adapter_parses_json_response(monkeypatch):
    monkeypatch.setenv("MOCK_PROVIDERS", "false")
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")
    monkeypatch.setenv("OPENROUTER_CHAT_MODEL", "test/model")
    from app import config as _c

    _c.get_settings.cache_clear()
    from app.providers.base import CastContext
    from app.providers.openrouter_chat import OpenRouterChatProvider

    fake_plan = {
        "title": "Hello",
        "cleaned_voice_script": "Test script",
        "estimated_duration_seconds": 10.0,
        "disclosure_text": "AI-generated virtual creator",
        "end_card_text": "",
        "caption_chunks": [{"start_hint": 0.0, "text": "Test"}],
        "shots": [
            {
                "order": 1,
                "shot_type": "hero",
                "duration_seconds": 10.0,
                "visual_prompt": "Naina hero shot",
                "negative_prompt": "",
                "reference_strategy": "input_references",
                "recommended_asset_types": ["hero"],
                "recommended_cast_roles": ["host"],
                "caption_text": "Test",
                "camera_direction": "",
                "notes": "",
            }
        ],
    }
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": __import__("json").dumps(fake_plan)}}
                ]
            },
        )
    )

    provider = OpenRouterChatProvider()
    plan = provider.generate_storyboard(
        project={
            "title": "Hello",
            "mode": "reel_montage",
            "target_duration_seconds": 10,
            "cta_text": "",
            "include_disclosure": True,
            "original_script": "Test script",
        },
        cast=CastContext(avatars=[{"name": "Naina", "role": "host"}], ingredients=[]),
    )
    assert plan.title == "Hello"
    assert plan.shots[0].visual_prompt == "Naina hero shot"

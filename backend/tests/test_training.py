"""Custom-model training: end-to-end against the mock provider.

We don't exercise the real Replicate or ElevenLabs Voice Lab adapters
here — those are gated by API keys and verified manually via
scripts/test_real_providers.md. The mock provider is deterministic and
instant, so every API path through the training feature can still be
asserted hermetically."""
from __future__ import annotations

from .conftest import make_png_bytes


def _avatar_with_assets(auth_client, n: int = 2) -> tuple[int, list[int]]:
    aid = auth_client.post("/api/avatars", json={"name": "Naina"}).json()["id"]
    asset_ids: list[int] = []
    for i in range(n):
        r = auth_client.post(
            f"/api/avatars/{aid}/assets",
            files={"file": (f"img_{i}.png", make_png_bytes(), "image/png")},
            data={"asset_type": "hero", "rights_confirmed": "true"},
        )
        assert r.status_code == 201, r.text
        asset_ids.append(r.json()["id"])
    return aid, asset_ids


def test_train_lora_completes_via_mock_provider(auth_client):
    aid, asset_ids = _avatar_with_assets(auth_client, n=3)
    r = auth_client.post(
        f"/api/cast/avatar/{aid}/train",
        json={
            "name": "Naina LoRA",
            "kind": "character_lora",
            "training_asset_ids": asset_ids,
            "config": {"steps": 1000, "rank": 16},
        },
    )
    assert r.status_code == 202, r.text
    body = r.json()
    # The enqueue path runs the worker inline when redis is unavailable;
    # by the time the response comes back the mock provider has trained.
    assert body["kind"] == "character_lora"
    assert body["owner_kind"] == "avatar"
    assert body["owner_id"] == aid

    # Look up the resulting job — it should be completed with a
    # provider_model_id the inference path can use.
    final = auth_client.get(f"/api/training-jobs/{body['id']}").json()
    assert final["status"] == "completed", final
    assert final["provider"] == "mock"
    assert final["provider_model_id"].startswith("mock/")
    assert final["progress"] == 1.0


def test_train_voice_clone_writes_voice_id_onto_avatar(auth_client):
    aid, asset_ids = _avatar_with_assets(auth_client, n=1)
    before = auth_client.get(f"/api/avatars/{aid}").json()
    assert before.get("elevenlabs_voice_id", "") == ""

    r = auth_client.post(
        f"/api/cast/avatar/{aid}/train",
        json={
            "name": "Naina voice",
            "kind": "voice_clone",
            "training_asset_ids": asset_ids,
        },
    )
    assert r.status_code == 202, r.text

    # Mock provider trains synchronously inside the request; the avatar
    # should now carry the cloned voice id.
    after = auth_client.get(f"/api/avatars/{aid}").json()
    assert after["elevenlabs_voice_id"]
    assert after["default_voice_provider"] == "elevenlabs"


def test_train_requires_at_least_one_asset(auth_client):
    aid, _ = _avatar_with_assets(auth_client, n=1)
    r = auth_client.post(
        f"/api/cast/avatar/{aid}/train",
        json={
            "name": "empty",
            "kind": "character_lora",
            "training_asset_ids": [],
        },
    )
    assert r.status_code == 422
    assert "asset is required" in r.text


def test_train_rejects_assets_from_a_different_cast_member(auth_client):
    # Two avatars, each with one asset. Train Naina's LoRA on Arjun's
    # asset → should 422.
    a1, a1_assets = _avatar_with_assets(auth_client, n=1)
    a2, a2_assets = _avatar_with_assets(auth_client, n=1)
    r = auth_client.post(
        f"/api/cast/avatar/{a1}/train",
        json={
            "name": "wrong",
            "kind": "character_lora",
            "training_asset_ids": a2_assets,
        },
    )
    assert r.status_code == 422
    assert "doesn't belong" in r.text


def test_voice_clone_against_ingredient_is_rejected(auth_client):
    # Voice clones don't apply to ingredients — they're per-avatar.
    iid = auth_client.post(
        "/api/ingredients", json={"name": "rooftop", "kind": "scene"}
    ).json()["id"]
    r = auth_client.post(
        f"/api/ingredients/{iid}/assets",
        files={"file": ("a.png", make_png_bytes(), "image/png")},
        data={"asset_type": "reference", "rights_confirmed": "true"},
    )
    asset_id = r.json()["id"]
    r = auth_client.post(
        f"/api/cast/ingredient/{iid}/train",
        json={
            "name": "voice on ingredient",
            "kind": "voice_clone",
            "training_asset_ids": [asset_id],
        },
    )
    assert r.status_code == 422
    assert "voice_clone" in r.text


def test_list_training_jobs_filters_by_owner(auth_client):
    a1, a1_assets = _avatar_with_assets(auth_client, n=1)
    a2, a2_assets = _avatar_with_assets(auth_client, n=1)
    auth_client.post(
        f"/api/cast/avatar/{a1}/train",
        json={"name": "a1", "kind": "character_lora", "training_asset_ids": a1_assets},
    )
    auth_client.post(
        f"/api/cast/avatar/{a2}/train",
        json={"name": "a2", "kind": "character_lora", "training_asset_ids": a2_assets},
    )
    all_jobs = auth_client.get("/api/training-jobs").json()
    assert len(all_jobs) == 2
    just_a1 = auth_client.get(
        f"/api/training-jobs?owner_kind=avatar&owner_id={a1}"
    ).json()
    assert len(just_a1) == 1
    assert just_a1[0]["owner_id"] == a1


def test_delete_blocked_on_running_job(auth_client):
    # The mock provider trains instantly in test, so the moment we kick
    # off a job it's already "completed". To exercise the 409 path we
    # need a job stuck mid-training — fake it by directly toggling status.
    aid, asset_ids = _avatar_with_assets(auth_client, n=1)
    r = auth_client.post(
        f"/api/cast/avatar/{aid}/train",
        json={"name": "x", "kind": "character_lora", "training_asset_ids": asset_ids},
    )
    job_id = r.json()["id"]

    # Force "training" status so delete returns 409.
    from app.db import SessionLocal
    from app import models as m

    with SessionLocal() as s:
        cm = s.get(m.CustomModel, job_id)
        cm.status = "training"
        s.commit()

    r = auth_client.delete(f"/api/training-jobs/{job_id}")
    assert r.status_code == 409
    assert "cancel" in r.text.lower()


def test_cancel_completed_job_is_noop(auth_client):
    aid, asset_ids = _avatar_with_assets(auth_client, n=1)
    r = auth_client.post(
        f"/api/cast/avatar/{aid}/train",
        json={"name": "x", "kind": "character_lora", "training_asset_ids": asset_ids},
    )
    job_id = r.json()["id"]
    # Already completed (mock = instant). Cancel should return the current
    # state, not raise.
    final = auth_client.post(f"/api/training-jobs/{job_id}/cancel").json()
    assert final["status"] == "completed"


def test_completed_lora_appears_in_image_models_listing(auth_client):
    aid, asset_ids = _avatar_with_assets(auth_client, n=2)
    auth_client.post(
        f"/api/cast/avatar/{aid}/train",
        json={"name": "Naina LoRA v1", "kind": "character_lora", "training_asset_ids": asset_ids},
    )
    models_list = auth_client.get("/api/providers/openrouter/image-models").json()
    # Custom trained adapter should be at the top of the list.
    assert models_list[0]["id"].startswith("custom:")
    assert models_list[0]["name"] == "Naina LoRA v1"
    assert "Character Lora" in models_list[0]["description"]
    # Base mock model is still present below.
    assert any(m["id"] == "mock/image-default" for m in models_list[1:])


def test_studio_generate_with_trained_lora_uses_resolved_adapter_id(auth_client):
    aid, asset_ids = _avatar_with_assets(auth_client, n=2)
    train_resp = auth_client.post(
        f"/api/cast/avatar/{aid}/train",
        json={"name": "Naina LoRA v1", "kind": "character_lora", "training_asset_ids": asset_ids},
    ).json()
    trained_model_id = train_resp["provider_model_id"]
    assert trained_model_id.startswith("mock/"), train_resp

    # Generate an image picking the trained LoRA via the custom:<id> selector
    # the picker surfaces.
    job = auth_client.post(
        "/api/studio/generate-image",
        json={
            "owner_kind": "avatar",
            "owner_id": aid,
            "prompt": "wide hero portrait",
            "model": f"custom:{train_resp['id']}",
            "reference_asset_ids": [],
        },
    ).json()
    final = auth_client.get(f"/api/studio/jobs/{job['id']}").json()
    assert final["status"] == "completed"
    # The job row reflects the *resolved* adapter id, not the custom:<id>
    # selector — proves the pipeline expanded the trained-model reference
    # before handing it to the provider.
    assert final["provider_model"] == trained_model_id


def test_unresolvable_custom_selector_falls_through_unchanged(auth_client):
    aid, asset_ids = _avatar_with_assets(auth_client, n=1)
    # No training run — custom:999 doesn't exist.
    job = auth_client.post(
        "/api/studio/generate-image",
        json={
            "owner_kind": "avatar",
            "owner_id": aid,
            "prompt": "x",
            "model": "custom:999",
            "reference_asset_ids": [],
        },
    ).json()
    final = auth_client.get(f"/api/studio/jobs/{job['id']}").json()
    # The unresolved string is what the provider got — mock ignores it
    # but a real provider would 404. The studio job still completes
    # because mock doesn't validate model ids.
    assert final["provider_model"] == "custom:999"

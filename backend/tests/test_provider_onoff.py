"""Providers switched on/off in Settings → API keys: off hides their models
from every picker and refuses new work that needs them (409, before any
provider call), never falls back to placeholder output, and lets work
already running finish."""
from __future__ import annotations

import pytest

ENV_VARS = ("OPENROUTER_API_KEY", "ELEVENLABS_API_KEY", "GEMINI_API_KEY", "REPLICATE_API_TOKEN", "SOGNI_API_KEY")
KEY = "or-test-abcdefgh1234"


@pytest.fixture
def live(monkeypatch):
    """Providers live, with OpenRouter, ElevenLabs and Sogni keys set (fake)."""
    from app import config
    from app.services import provider_keys

    for v in ENV_VARS:
        monkeypatch.setenv(v, "")
    monkeypatch.setenv("MOCK_PROVIDERS", "false")
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-test-abcdefgh1234")
    monkeypatch.setenv("SOGNI_API_KEY", "sogni-test-abcdefgh1234")
    config.get_settings.cache_clear()
    provider_keys.invalidate()
    yield
    config.get_settings.cache_clear()
    provider_keys.invalidate()


def _off(name: str) -> None:
    from app.services import provider_keys

    provider_keys.set_enabled(name, False)


# ---------- switches ----------


def test_defaults_and_round_trip(auth_client, live):
    from app.services import provider_keys

    # Providers that exist today are on; one added later is on once keyed.
    assert provider_keys.enabled("openrouter") and provider_keys.enabled("sogni")
    assert not provider_keys.enabled("replicate")  # no key yet
    provider_keys.set_key("replicate", "r8-test-abcdefgh1234")
    assert provider_keys.enabled("replicate")

    r = auth_client.put("/api/settings/keys/sogni/enabled", json={"enabled": False})
    assert r.status_code == 200
    row = r.json()
    assert (row["enabled"], row["enabled_explicit"], row["enabled_default"]) == (False, True, True)
    assert "abcdefgh1234" not in r.text  # never a key value
    # The key is still there (and testable); only the switch changed.
    assert provider_keys.has("sogni") and provider_keys.resolve("sogni")
    assert not provider_keys.usable("sogni")
    r = auth_client.put("/api/settings/keys/sogni/enabled", json={"enabled": None})
    assert r.json()["enabled"] is True and r.json()["enabled_explicit"] is False
    # An explicit Off survives saving a new key.
    provider_keys.set_enabled("replicate", False)
    provider_keys.set_key("replicate", "r8-test-other-key-5678")
    assert not provider_keys.enabled("replicate")

    keys = {k["name"]: k for k in auth_client.get("/api/settings/keys").json()["keys"]}
    assert keys["replicate"]["enabled"] is False and keys["openrouter"]["enabled"] is True


def test_enabled_endpoint_validates(auth_client, live):
    assert auth_client.put("/api/settings/keys/nope/enabled", json={"enabled": False}).status_code == 404
    for body in ({}, {"enabled": "off"}, [], None):
        assert auth_client.put("/api/settings/keys/sogni/enabled", json=body).status_code == 422
    # Switching off reports which active model kinds it was serving.
    from app.services import model_registry as reg

    reg.set_active("video", "alibaba/wan-3.0")
    r = auth_client.put("/api/settings/keys/openrouter/enabled", json={"enabled": False})
    assert {"chat", "video"} <= set(r.json()["in_use"]) and "image" not in r.json()["in_use"]  # image: mock/…


# ---------- routing ----------


def test_off_is_refused_never_faked(auth_client, live):
    from app.providers import get_chat, get_image, get_video
    from app.providers.registry import get_sound
    from app.services import model_registry as reg
    from app.services.provider_keys import ProviderUnavailable

    reg.set_active("image", "openai/gpt-image-2")  # as on the live site (tests default to mock/…)
    reg.set_active("video", "alibaba/wan-3.0")
    assert reg.route_info("image", "openai/gpt-image-2") == ("openrouter", None)
    _off("openrouter")
    assert reg.route_info("image", "openai/gpt-image-2") == ("unavailable", "openrouter")
    for build in (lambda: get_image("openai/gpt-image-2"), lambda: get_video(), get_chat):
        with pytest.raises(ProviderUnavailable, match="OpenRouter is turned off"):
            build()
    _off("sogni")
    with pytest.raises(ProviderUnavailable, match="Sogni"):
        get_video("sogni/ltx25")
    _off("elevenlabs")
    with pytest.raises(ProviderUnavailable):
        get_sound()
    # No key at all: the placeholder provider, exactly as before.
    assert reg.provider_for("music", "google/lyria-3.5") == "mock"


def test_reserved_prefixes_are_refused_not_sent_to_openrouter(auth_client, live):
    from app.providers import get_image
    from app.services import model_registry as reg
    from app.services.provider_keys import ProviderUnavailable

    assert reg.route_info("image", "runware/runware:101@1") == ("unavailable", "runware")
    with pytest.raises(ProviderUnavailable, match="aren't supported"):
        get_image("replicate/black-forest-labs/flux-dev")


def test_lyria_falls_back_to_gemini_when_openrouter_is_off(auth_client, live, monkeypatch):
    from app.services import model_registry as reg
    from app.services import provider_keys

    provider_keys.set_key("gemini", "gm-test-abcdefgh1234")
    monkeypatch.setattr(reg, "_catalog_provider", lambda kind, m: "openrouter")
    assert reg.provider_for("music", "google/lyria-3-pro") == "openrouter"
    _off("openrouter")
    assert reg.provider_for("music", "google/lyria-3-pro") == "gemini"
    _off("gemini")
    assert reg.route_info("music", "google/lyria-3-pro") == ("unavailable", "openrouter")


def test_work_already_running_finishes(auth_client, live):
    from app.services import model_registry as reg
    from app.services import provider_keys

    _off("openrouter")
    assert reg.provider_for("video", "alibaba/wan-3.0") == "unavailable"
    with provider_keys.continuing():
        assert reg.provider_for("video", "alibaba/wan-3.0") == "openrouter"
    assert reg.provider_for("video", "alibaba/wan-3.0") == "unavailable"


def test_training_and_status_report_off(auth_client, live):
    from app.providers import get_training
    from app.providers.registry import provider_status
    from app.services import provider_keys
    from app.services.provider_keys import ProviderUnavailable

    provider_keys.set_key("replicate", "r8-test-abcdefgh1234")
    assert provider_status()["training_lora"] == "replicate"
    _off("replicate")
    assert provider_status()["training_lora"] == "unavailable"
    with pytest.raises(ProviderUnavailable):
        get_training("character_lora")  # not a fake "trained" LoRA


def test_mock_mode_ignores_the_switches(auth_client):
    from app.services import model_registry as reg

    _off("openrouter")
    assert reg.provider_for("video", "alibaba/wan-3.0") == "mock"


# ---------- pickers ----------


def _catalog(entries):
    from app.services import model_catalog
    from app.services import model_registry as reg

    reg.put_setting(model_catalog.CATALOG_KEY, {"fetched_at": "2026-11-09T00:00:00+00:00", "errors": {},
                                                "entries": entries})


def test_pickers_hide_switched_off_providers(auth_client, live):
    from app.services import model_catalog

    _catalog([{"id": "alibaba/wan-3.0", "kind": "video", "provider": "openrouter", "name": "Wan 3.0"}])
    _off("openrouter")  # the active provider's own list goes too
    vids = [m["id"] for m in auth_client.get("/api/providers/openrouter/video-models").json()]
    assert "alibaba/wan-3.0" not in vids and any(v.startswith("sogni/") for v in vids)
    snap = {e["id"]: e for e in model_catalog.get_snapshot()["entries"]}
    assert snap["alibaba/wan-3.0"]["enabled"] is False and snap["sogni/ltx25"]["enabled"] is True
    _off("sogni")
    vids = [m["id"] for m in auth_client.get("/api/providers/openrouter/video-models").json()]
    assert not any(v.startswith("sogni/") or v == "alibaba/wan-3.0" for v in vids)  # mock/… models only
    # Ambient suggestions follow too.
    presets = auth_client.get("/api/ambient/presets").json()
    assert presets["video_models"] == [] and presets["still_models"] == []


def test_ambient_suggestions_hide_sogni_when_off(auth_client, live):
    ids = lambda: [c["id"] for c in auth_client.get("/api/ambient/presets").json()["video_models"]]  # noqa: E731
    assert "sogni/minimax-h3-fasth3-flf2v-turbo" in ids() and "alibaba/wan-3.0" in ids()
    _off("sogni")
    assert "sogni/minimax-h3-fasth3-flf2v-turbo" not in ids() and "alibaba/wan-3.0" in ids()


# ---------- refused before any spend ----------


def test_paid_endpoints_refuse_with_409_and_queue_nothing(auth_client, live, monkeypatch):
    from app import models
    from app.api import studio as studio_api
    from app.db import SessionLocal

    queued: list = []
    monkeypatch.setattr(studio_api, "enqueue", lambda *a, **k: queued.append(a))
    _off("sogni")
    r = auth_client.post("/api/studio/generate-image", json={
        "owner_kind": "avatar", "owner_id": _avatar(), "prompt": "p", "model": "sogni/krea-2-turbo"})
    assert r.status_code == 409
    assert r.json()["code"] == "provider_unavailable" and r.json()["provider"] == "sogni"
    assert "Sogni is turned off" in r.json()["detail"]
    db = SessionLocal()
    try:
        assert db.query(models.AssetGenerationJob).count() == 0 and not queued
    finally:
        db.close()
    # Choosing it as the active model is refused too.
    r = auth_client.put("/api/models/active", json={"kind": "video", "model": "sogni/ltx25", "allow_unlisted": True})
    assert r.status_code == 409


def test_ambient_generation_refused_before_claiming(auth_client, live, monkeypatch):
    from app.api import ambient as ambient_api

    from .test_ambient import SHORT

    monkeypatch.setattr(ambient_api, "enqueue", lambda *a, **k: "job")
    cfg = {**SHORT, "model_override": "sogni/minimax-h3-fasth3-flf2v-turbo", "music_mood": "none"}
    sid = auth_client.post("/api/ambient", json={"config": cfg, "generate": False}).json()["id"]
    _off("sogni")
    r = auth_client.post(f"/api/ambient/{sid}/generate-loop")
    assert r.status_code == 409 and r.json()["provider"] == "sogni"
    assert auth_client.get(f"/api/ambient/{sid}").json()["status"] != "generating"
    # A batch with a switched-off provider creates no scenes at all.
    before = len(auth_client.get("/api/ambient").json())
    r = auth_client.post("/api/ambient/batch", json={"presets": ["waterfall", "ocean"], "config": cfg})
    assert r.status_code == 409 and len(auth_client.get("/api/ambient").json()) == before


def _avatar() -> int:
    from app import models
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        av = models.Avatar(name="Mia")
        db.add(av)
        db.commit()
        return av.id
    finally:
        db.close()


# ---------- review follow-ups ----------


def test_lipsync_off_falls_back_to_the_video_model(auth_client, live):
    from app.providers import get_avatar
    from app.services import model_registry as reg

    reg.set_active("lipsync", "heygen/avatar-iv")
    assert get_avatar() is not None
    _off("openrouter")
    assert get_avatar() is None  # talking heads use the video model; renders don't break
    from app.services import provider_keys

    with provider_keys.continuing():
        assert get_avatar() is None  # even mid-render: the video model was checked at queue time


def test_cancelling_training_still_reaches_replicate_when_off(auth_client, live, monkeypatch):
    from app import models
    from app.db import SessionLocal
    from app.providers import replicate_training
    from app.services import provider_keys

    provider_keys.set_key("replicate", "r8-test-abcdefgh1234")
    cancelled: list = []
    monkeypatch.setattr(replicate_training.ReplicateTrainingProvider, "cancel",
                        lambda self, job: cancelled.append(job.provider_job_id) or True)
    db = SessionLocal()
    try:
        cm = models.CustomModel(name="m", kind="character_lora", owner_kind="avatar", owner_id=1,
                                status="training", provider="replicate", provider_job_id="tr_1")
        db.add(cm)
        db.commit()
        cid = cm.id
    finally:
        db.close()
    _off("replicate")
    assert auth_client.post(f"/api/training-jobs/{cid}/cancel").status_code == 200
    assert cancelled == ["tr_1"]


def test_running_work_prefers_providers_that_are_on(auth_client, live, monkeypatch):
    from app.schemas.ambient import AmbientConfig
    from app.services import ambient, provider_keys
    from app.services import model_registry as reg

    from .test_ambient import SHORT

    provider_keys.set_key("gemini", "gm-test-abcdefgh1234")
    monkeypatch.setattr(reg, "_catalog_provider", lambda kind, m: "openrouter")
    _off("openrouter")
    with provider_keys.continuing():
        assert reg.provider_for("music", "google/lyria-3-pro") == "gemini"  # not the switched-off one
        cfg = AmbientConfig(**{**SHORT, "still_model": "sogni/krea-2-turbo"})
        assert ambient._live_chain(cfg) == ["sogni/krea-2-turbo"]  # the OpenRouter fallback is skipped


def test_gates_match_what_will_actually_run(auth_client, live, tmp_path):
    from app import models
    from app.db import SessionLocal
    from app.schemas.ambient import AmbientConfig
    from app.services import provider_gate, provider_keys
    from app.services.provider_keys import ProviderUnavailable

    from .test_ambient import SHORT

    _off("elevenlabs")
    cfg = AmbientConfig(**{**SHORT, "model_override": "sogni/minimax-h3-fasth3-flf2v-turbo"})
    db = SessionLocal()
    try:
        project = models.VideoProject(title="t", original_script="Hello there.", voiceover_source="silent")
        db.add(project)
        db.commit()
        db.add(models.VideoShot(project_id=project.id, shot_order=0, prompt="p", model_override="sogni/ltx25"))
        db.add(models.VideoShot(project_id=project.id, shot_order=1, prompt="end", shot_type="end_card"))
        db.commit()
        db.refresh(project)
        provider_gate.require_project(db, project)  # silent voiceover: no ElevenLabs needed
        project.voiceover_source = "tts"
        db.commit()
        with pytest.raises(ProviderUnavailable, match="ElevenLabs"):
            provider_gate.require_project(db, project)
        provider_gate.require_project(db, project, shot_ids={project.shots[0].id})  # regen reuses the voiceover
        # A completed shot whose clip is on disk is reused, not resubmitted.
        project.voiceover_source = "silent"
        shot = project.shots[0]
        _off("sogni")
        with pytest.raises(ProviderUnavailable, match="Sogni"):
            provider_gate.require_project(db, project)
        clip = tmp_path / "c.mp4"
        clip.write_bytes(b"x")
        shot.status, shot.clip_path = "completed", str(clip)
        provider_gate.require_project(db, project)
        with pytest.raises(ProviderUnavailable, match="Sogni"):
            provider_gate.require_project(db, project, shot_ids={shot.id})  # a regen does resubmit it
        provider_keys.set_enabled("sogni", None)
        # A loop-only regen of a scene that already has music and sound needs no ElevenLabs.
        music = tmp_path / "m.mp3"
        music.write_bytes(b"x")
        project.music_upload_path = str(music)
        from app.services import ambient

        ambient.sound_path(project).parent.mkdir(parents=True, exist_ok=True)
        ambient.sound_path(project).write_bytes(b"x")
        provider_gate.require_ambient(cfg, regenerate_music=False, project=project)
        with pytest.raises(ProviderUnavailable, match="ElevenLabs"):
            provider_gate.require_ambient(cfg, regenerate_music=True, project=project)
    finally:
        db.close()


def test_create_and_generate_refused_before_the_scene_is_made(auth_client, live):
    from .test_ambient import SHORT

    _off("sogni")
    before = len(auth_client.get("/api/ambient").json())
    r = auth_client.post("/api/ambient", json={"config": {**SHORT, "model_override": "sogni/ltx25",
                                                          "music_mood": "none"}, "generate": True})
    assert r.status_code == 409 and len(auth_client.get("/api/ambient").json()) == before


def test_refresh_returns_the_live_view(auth_client, live, monkeypatch):
    from app.services import model_catalog

    def fake_refresh(client=None):
        snap = {"fetched_at": "2026-11-09T00:00:00+00:00", "errors": {}, "entries": [
            {"id": "alibaba/wan-3.0", "kind": "video", "provider": "openrouter", "name": "Wan 3.0", "usable": True}]}
        from app.services import model_registry as reg

        reg.put_setting(model_catalog.CATALOG_KEY, snap)
        return snap

    monkeypatch.setattr(model_catalog, "refresh", fake_refresh)
    _off("openrouter")
    entries = {e["id"]: e for e in auth_client.post("/api/models/refresh").json()["entries"]}
    assert entries["alibaba/wan-3.0"]["enabled"] is False


def test_reserved_ids_say_not_supported_and_refusals_are_not_runtime_errors(auth_client, live):
    from app.providers import get_image
    from app.services import provider_keys
    from app.services.provider_keys import ProviderUnavailable

    provider_keys.set_key("replicate", "r8-test-abcdefgh1234")  # on, for training
    with pytest.raises(ProviderUnavailable, match="aren't supported") as e:
        get_image("replicate/black-forest-labs/flux-dev")
    assert not isinstance(e.value, RuntimeError)  # broad RuntimeError handlers don't swallow it


def test_presets_mark_a_switched_off_default(auth_client, live):
    from app.services import model_registry as reg

    reg.set_active("video", "alibaba/wan-3.0")
    assert auth_client.get("/api/ambient/presets").json()["default_video_off"] is False
    _off("openrouter")
    assert auth_client.get("/api/ambient/presets").json()["default_video_off"] is True


def test_photo_rewrite_skipped_when_the_planner_is_off(auth_client, live, monkeypatch):
    from app.schemas.ambient import AmbientConfig
    from app.services import ambient, provider_keys

    from .test_ambient import SHORT

    cfg = AmbientConfig(**{**SHORT, "look": "photoreal", "rewrite_prompt": True,
                           "scene_prompt": "a quiet harbour at dawn"})
    assert ambient.needs_photo_rewrite(cfg)
    monkeypatch.setattr(ambient, "_rewrite_affordable", lambda: True)
    _off("openrouter")
    with provider_keys.continuing():  # even inside a running job
        assert ambient.ensure_photo_rewrite(cfg).photo is None  # used as typed, no planner call


def test_trained_loras_hidden_when_their_runner_is_off(auth_client, live):
    from datetime import datetime, timezone

    from app import models
    from app.db import SessionLocal
    from app.services import model_registry as reg

    db = SessionLocal()
    try:
        cm = models.CustomModel(name="Naina", kind="character_lora", owner_kind="avatar", owner_id=1,
                                status="completed", provider="mock", provider_model_id="black-forest-labs/flux-lora",
                                completed_at=datetime.now(timezone.utc))
        db.add(cm)
        db.commit()
        cid = cm.id
    finally:
        db.close()
    reg.set_active("image", "black-forest-labs/flux.2-pro")
    ids = lambda: [m["id"] for m in auth_client.get("/api/providers/openrouter/image-models").json()]  # noqa: E731
    assert f"custom:{cid}" in ids()
    _off("openrouter")
    r = auth_client.get("/api/providers/openrouter/image-models")
    assert r.status_code == 200 and f"custom:{cid}" not in [m["id"] for m in r.json()]

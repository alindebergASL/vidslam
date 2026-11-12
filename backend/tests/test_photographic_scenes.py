"""Photographic scene stills: model choice, best settings, fallback, previews,
and video models that always make audio."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from .test_ambient import SHORT

BASE = "https://openrouter.ai/api/v1"


def _cfg(**kw):
    from app.schemas.ambient import AmbientConfig

    return AmbientConfig(**{**SHORT, **kw})


@pytest.fixture
def live_openrouter(monkeypatch):
    """Providers 'live' (no mock) with a fake key; every HTTP call is respx-mocked."""
    monkeypatch.setenv("MOCK_PROVIDERS", "false")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    from app import config
    from app.services import image_settings

    config.get_settings.cache_clear()
    image_settings.clear_cache()
    yield
    image_settings.clear_cache()
    config.get_settings.cache_clear()


# ---------- best settings per model ----------


@respx.mock
def test_best_settings_come_from_the_models_own_parameter_list(live_openrouter):
    from app.services.image_settings import best_image_settings

    respx.get(f"{BASE}/images/models/openai/gpt-image-2.6-sunburst/endpoints").mock(
        return_value=httpx.Response(200, json={"endpoints": [{"supported_parameters": {
            "aspect_ratio": {"type": "enum", "values": ["1:1", "16:9", "9:16"]},
            "quality": {"type": "enum", "values": ["auto", "low", "medium", "high", "xhigh", "max"]},
        }}]}))
    respx.get(f"{BASE}/images/models/google/gemini-nano-banana-2.1/endpoints").mock(
        return_value=httpx.Response(200, json={"endpoints": [
            {"supported_parameters": {"resolution": {"type": "enum", "values": ["1K", "2K"]}}},
            {"supported_parameters": {"resolution": {"type": "enum", "values": ["1K", "2K", "4K"]},
                                      "aspect_ratio": {"type": "enum", "values": ["16:9", "21:9"]}}},
        ]}))
    assert best_image_settings("openai/gpt-image-2.6-sunburst", "16:9") == {
        "aspect_ratio": "16:9", "quality": "max"}
    # 2K, not 4K: 4K tested crunchier, not better. 1:1 isn't offered, so it isn't sent.
    assert best_image_settings("google/gemini-nano-banana-2.1", "1:1") == {"resolution": "2K"}
    assert best_image_settings("google/gemini-nano-banana-2.1", "16:9") == {
        "aspect_ratio": "16:9", "resolution": "2K"}


@respx.mock
def test_best_settings_fall_back_to_built_in_rules(live_openrouter):
    from app.services.image_settings import best_image_settings

    respx.get(url__regex=rf"{BASE}/images/models/.*/endpoints").mock(return_value=httpx.Response(503))
    assert best_image_settings("openai/gpt-image-2.6-flare") == {"aspect_ratio": "16:9", "quality": "max"}
    assert best_image_settings("openai/gpt-image-1") == {"aspect_ratio": "16:9", "quality": "high"}
    assert best_image_settings("google/gemini-3-pro-image") == {"aspect_ratio": "16:9", "resolution": "2K"}
    assert best_image_settings("x-ai/grok-imagine-image-2.0", "9:16") == {"aspect_ratio": "9:16"}


def test_mock_mode_never_calls_out():
    from app.services.image_settings import best_image_settings

    with respx.mock(assert_all_called=False) as router:
        assert best_image_settings("openai/gpt-image-2.6-sunburst")["quality"] == "max"
        assert not router.calls


@respx.mock
def test_image_request_carries_the_settings(live_openrouter):
    import base64

    from app.providers.openrouter_image import OpenRouterImageProvider

    route = respx.post(f"{BASE}/images").mock(return_value=httpx.Response(
        200, json={"data": [{"b64_json": base64.b64encode(b"PNG").decode()}], "usage": {"cost": 0.13}}))
    out = OpenRouterImageProvider().generate(model="openai/gpt-image-2.6-sunburst", prompt="p", references=[],
                                             settings={"aspect_ratio": "16:9", "quality": "max"})
    assert out == b"PNG"
    body = json.loads(route.calls[0].request.content)
    assert body["quality"] == "max" and body["aspect_ratio"] == "16:9"


# ---------- which model makes the stills ----------


def test_still_model_chain(monkeypatch):
    from app.services import ambient

    assert ambient.still_model_chain(_cfg()) == ["openai/gpt-image-2.6-sunburst",
                                                 "google/gemini-nano-banana-2.1"]
    # The scene's own choice wins; the fallback stays behind it.
    assert ambient.still_model_chain(_cfg(still_model="black-forest-labs/flux-3-image")) == [
        "black-forest-labs/flux-3-image", "google/gemini-nano-banana-2.1"]
    # Choosing the fallback itself leaves nothing to fall back to.
    assert ambient.still_model_chain(_cfg(still_model="google/gemini-nano-banana-2.1")) == [
        "google/gemini-nano-banana-2.1"]
    # Older Still + drift scenes kept their image model in model_override.
    assert ambient.still_model_chain(_cfg(source="image_drift", model_override="x/old"))[0] == "x/old"
    # A video scene's model_override is a video model: not used for stills.
    assert ambient.still_model_chain(_cfg(model_override="alibaba/wan-3.0"))[0] == "openai/gpt-image-2.6-sunburst"
    # AMBIENT_STILL_MODEL empty = the active image model (Settings → Models).
    monkeypatch.setenv("AMBIENT_STILL_MODEL", "")
    from app import config

    config.get_settings.cache_clear()
    try:
        assert ambient.still_model_chain(_cfg())[0] == ambient._active_model("image")
    finally:
        config.get_settings.cache_clear()


class _FlakyImages:
    """Fails for the first model, succeeds for any other."""

    def __init__(self, fail_model: str):
        self.fail_model = fail_model
        self.calls: list[tuple[str, dict]] = []

    def generate(self, *, model, prompt, references, negative_prompt="", settings=None):
        self.calls.append((model, settings or {}))
        if model == self.fail_model:
            # An outage: a content refusal never falls back (test_refusal_fallbacks.py).
            raise httpx.HTTPStatusError("OpenRouter image 503: Service Unavailable",  # type: ignore[arg-type]
                                        request=None, response=None)
        return b"\x89PNG-from-" + model.encode()


def test_still_falls_back_when_the_first_model_fails(client, monkeypatch):
    from app.db import SessionLocal
    from app.services import ambient

    fake = _FlakyImages(fail_model="openai/gpt-image-2.6-sunburst")
    monkeypatch.setattr(ambient, "get_image", lambda: fake)
    db = SessionLocal()
    try:
        png, used = ambient.generate_still(db, _cfg(), prompt="p", aspect_ratio="9:16")
    finally:
        db.close()
    assert used == "google/gemini-nano-banana-2.1" and png.endswith(b"nano-banana-2.1")
    (m1, s1), (m2, s2) = fake.calls
    assert (m1, s1["quality"], s1["aspect_ratio"]) == ("openai/gpt-image-2.6-sunburst", "max", "9:16")
    assert (m2, s2["resolution"], s2["aspect_ratio"]) == ("google/gemini-nano-banana-2.1", "2K", "9:16")


def test_still_raises_when_every_model_fails(client, monkeypatch):
    from app.db import SessionLocal
    from app.services import ambient

    fake = _FlakyImages(fail_model="google/gemini-nano-banana-2.1")
    monkeypatch.setattr(ambient, "get_image", lambda: fake)
    db = SessionLocal()
    try:
        with pytest.raises(httpx.HTTPStatusError):
            ambient.generate_still(db, _cfg(still_model="google/gemini-nano-banana-2.1"),
                                   prompt="p", aspect_ratio="16:9")
    finally:
        db.close()


# ---------- what the builder shows and charges ----------


def test_presets_offer_model_choices(auth_client):
    p = auth_client.get("/api/ambient/presets").json()
    stills = {m["id"]: m for m in p["still_models"]}
    assert stills["openai/gpt-image-2.6-sunburst"]["recommended"] is True
    assert "google/gemini-nano-banana-2.1" in stills
    videos = {m["id"]: m for m in p["video_models"]}
    assert videos["alibaba/wan-3.0"]["recommended"] is True
    assert videos["google/gemini-omni-1.2-flash"]["experimental"] is True
    assert p["default_still_model"] == "openai/gpt-image-2.6-sunburst"


def test_estimate_prices_the_still_on_its_model(auth_client):
    def still_line(cfg):
        lines = auth_client.post("/api/ambient/estimate", json={"config": cfg}).json()["lines"]
        return next(ln for ln in lines if "still" in ln["item"].lower())

    assert still_line(SHORT)["unit_cost"] == pytest.approx(0.13)
    assert still_line({**SHORT, "still_model": "google/gemini-nano-banana-2.1"})["unit_cost"] == pytest.approx(0.055)


def test_changing_the_still_model_needs_a_new_loop(auth_client):
    sid = auth_client.post("/api/ambient", json={"config": SHORT, "generate": True}).json()["id"]
    assert auth_client.get(f"/api/ambient/{sid}").json()["loop_status"] == "completed"
    r = auth_client.patch(f"/api/ambient/{sid}?discard_loop=true",
                          json={"config": {**SHORT, "still_model": "google/gemini-nano-banana-2.1"}})
    assert r.status_code == 200, r.text
    assert r.json()["loop_status"] != "completed"


def test_previews_made_from_an_older_prompt_are_not_shown(client, monkeypatch):
    from app.db import SessionLocal
    from app.services import ambient

    ambient.preview_path("ocean").write_bytes(b"old preview, no prompt stamp")
    assert not ambient.preview_is_current("ocean")
    assert next(s for s in ambient.presets().scenes if s.id == "ocean").preview_url is None

    db = SessionLocal()
    try:
        ambient.generate_preview(db, "ocean")
    finally:
        db.close()
    assert ambient.preview_is_current("ocean")
    assert next(s for s in ambient.presets().scenes if s.id == "ocean").preview_url

    monkeypatch.setitem(ambient.PRESET_CAMERA, "ocean", "Shot on a phone.")
    assert not ambient.preview_is_current("ocean")


def test_preview_is_served_with_its_real_image_type(auth_client):
    import io

    from PIL import Image

    from app.services import ambient

    buf = io.BytesIO()
    Image.new("RGB", (32, 18), (10, 20, 30)).save(buf, format="JPEG")
    ambient.preview_path("lavender").write_bytes(buf.getvalue())
    r = auth_client.get("/api/ambient/presets/lavender/preview")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"


# ---------- video models that always make audio ----------


@respx.mock
def test_video_submit_retries_without_generate_audio_for_models_that_always_make_audio(live_openrouter):
    from app.providers import openrouter_video as ov

    ov._reset_models_cache()
    ov._ALWAYS_AUDIO.discard("google/gemini-omni-1.2-flash")
    respx.get(f"{BASE}/videos/models").mock(return_value=httpx.Response(200, json={"data": [
        {"id": "google/gemini-omni-1.2-flash", "supported_durations": [5, 10],
         "supported_frame_images": ["first_frame", "last_frame"]}]}))
    calls = []

    def videos(request):
        body = json.loads(request.content)
        calls.append(body)
        if body.get("generate_audio") is False:
            return httpx.Response(400, json={"error": {"code": 400, "message":
                "This model always generates audio with the video. generate_audio cannot be set to false."}})
        return httpx.Response(202, json={"id": f"gen-{len(calls)}", "status": "pending"})

    respx.post(f"{BASE}/videos").mock(side_effect=videos)
    provider = ov.OpenRouterVideoProvider(model="google/gemini-omni-1.2-flash")
    job = provider.submit(model="google/gemini-omni-1.2-flash", prompt="waterfall", references=[],
                          settings={"aspect_ratio": "16:9", "generate_audio": False})
    assert job.job_id == "gen-2"
    assert calls[0]["generate_audio"] is False and "generate_audio" not in calls[1]
    # Remembered: the next submit for this model doesn't send the flag at all.
    provider.submit(model="google/gemini-omni-1.2-flash", prompt="waterfall", references=[],
                    settings={"aspect_ratio": "16:9", "generate_audio": False})
    assert len(calls) == 3 and "generate_audio" not in calls[2]
    ov._ALWAYS_AUDIO.discard("google/gemini-omni-1.2-flash")
    ov._reset_models_cache()


@respx.mock
def test_other_video_errors_are_not_retried(live_openrouter):
    from app.providers import openrouter_video as ov

    ov._reset_models_cache()
    respx.get(f"{BASE}/videos/models").mock(return_value=httpx.Response(200, json={"data": []}))
    route = respx.post(f"{BASE}/videos").mock(return_value=httpx.Response(
        400, json={"error": {"message": "duration not supported"}}))
    with pytest.raises(RuntimeError, match="video submit 400"):
        ov.OpenRouterVideoProvider(model="alibaba/wan-3.0").submit(
            model="alibaba/wan-3.0", prompt="p", references=[], settings={"generate_audio": False})
    assert route.call_count == 1
    ov._reset_models_cache()


# ---------- review follow-ups ----------


def test_weather_replaces_the_presets_sky_and_stays_outside_indoors():
    from app.services.ambient import PRESET_LIGHT, light_phrase

    # The waterfall's own light describes an overcast sky: a chosen weather replaces it.
    assert light_phrase(_cfg(weather="clear")) == "A clear sky."
    assert PRESET_LIGHT["waterfall"] not in light_phrase(_cfg(weather="heavy_rain"))
    # A chosen time of day still comes first, with the weather after it.
    assert light_phrase(_cfg(time_of_day="dusk", weather="fog")).startswith("About twenty minutes after sunset")
    # Indoors the fire stays the light; time and weather are outside.
    fire = light_phrase(_cfg(scene_preset="fireplace", time_of_day="night", weather="heavy_rain"))
    assert fire.startswith(PRESET_LIGHT["fireplace"])
    assert "It is night outside." in fire and "Outside, heavy, steady rain is falling." in fire


def test_picture_video_prompt_keeps_indoor_scenes_lit_by_their_fire():
    from app.services.ambient import resolve_scene_prompt

    p = resolve_scene_prompt(_cfg(scene_preset="candles", picture_origin="upload", time_of_day="night"))
    assert "It is night outside." in p and "moonlight" not in p


def test_a_video_model_left_in_model_override_is_not_used_for_stills():
    from app.services import ambient

    chain = ambient.still_model_chain(_cfg(source="image_drift", model_override="alibaba/wan-3.0"))
    assert chain[0] == "openai/gpt-image-2.6-sunburst"
    plan = ambient.plan_loop(_cfg(source="image_drift", model_override="alibaba/wan-3.0"))
    assert plan.model == "openai/gpt-image-2.6-sunburst"


def test_a_custom_trained_still_model_has_no_fallback():
    from app.services import ambient

    assert ambient.still_model_chain(_cfg(still_model="custom:7")) == ["custom:7"]


def test_cost_ambient_still_prices_the_default_model(monkeypatch):
    from app import config
    from app.services import ambient

    monkeypatch.setenv("COST_AMBIENT_STILL", "0.2")
    config.get_settings.cache_clear()
    try:
        assert ambient.still_cost(_cfg()) == pytest.approx(0.2)
        assert ambient.still_cost(_cfg(still_model="google/gemini-nano-banana-2.1")) == pytest.approx(0.055)
    finally:
        config.get_settings.cache_clear()


def test_still_model_change_keeps_the_loop_when_no_still_is_made(auth_client):
    from .conftest import make_png_bytes

    sid = auth_client.post("/api/ambient", json={"config": SHORT, "generate": False}).json()["id"]
    r = auth_client.post(f"/api/ambient/{sid}/picture",
                         files={"file": ("p.png", make_png_bytes((854, 480)), "image/png")})
    assert r.status_code == 200, r.text
    auth_client.post(f"/api/ambient/{sid}/generate-loop")
    assert auth_client.get(f"/api/ambient/{sid}").json()["loop_status"] == "completed"
    cfg = auth_client.get(f"/api/ambient/{sid}").json()["config"]
    r = auth_client.patch(f"/api/ambient/{sid}",
                          json={"config": {**cfg, "still_model": "google/gemini-nano-banana-2.1"}})
    assert r.status_code == 200, r.text
    assert r.json()["loop_status"] == "completed"  # the scene starts from its picture


def test_previews_job_skips_presets_that_are_already_current(client, monkeypatch):
    from app.db import SessionLocal
    from app.services import ambient
    from app.workers import jobs

    db = SessionLocal()
    try:
        ambient.generate_preview(db, "ocean")
    finally:
        db.close()
    made = []
    monkeypatch.setattr(ambient, "generate_preview", lambda _db, pid: made.append(pid))
    jobs.generate_previews_job(["ocean", "candles"])
    assert made == ["candles"]  # ocean was already made from the current prompt


def test_a_failed_still_attempt_is_recorded(client, monkeypatch):
    from app import models
    from app.db import SessionLocal
    from app.services import ambient

    monkeypatch.setattr(ambient, "get_image", lambda: _FlakyImages(fail_model="openai/gpt-image-2.6-sunburst"))
    db = SessionLocal()
    try:
        ambient.generate_still(db, _cfg(), prompt="p", aspect_ratio="16:9", endpoint="image.generate(test)")
        rows = db.query(models.ProviderLog).filter(models.ProviderLog.endpoint == "image.generate(test)").all()
        assert sorted(r.status for r in rows) == ["error", "ok"]
    finally:
        db.close()

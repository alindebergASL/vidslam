"""Typed scene descriptions rewritten as a photograph, and the tuned preset /
time-of-day wording (blind-tested, Nov 2026)."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from .test_ambient import SHORT

HYPE = "beautiful tropical beach with crystal clear turquoise water and palm trees, 8k ultra realistic"


def _cfg(**kw):
    from app.schemas.ambient import AmbientConfig

    return AmbientConfig(**{**SHORT, **kw})


@pytest.fixture
def rewrites(monkeypatch):
    """Count calls to the (mock) planner's rewrite."""
    from app.providers.mock_chat import MockChatProvider

    calls: list[str] = []
    real = MockChatProvider.rewrite_scene

    def counted(self, text):
        calls.append(text)
        return real(self, text)

    monkeypatch.setattr(MockChatProvider, "rewrite_scene", counted)
    return calls


def test_a_typed_description_is_rewritten_once_and_used_in_the_prompts(auth_client, rewrites):
    cfg = {**SHORT, "scene_preset": "custom", "scene_prompt": HYPE}
    r = auth_client.post("/api/ambient", json={"config": cfg, "generate": False})
    assert r.status_code == 201, r.text
    scene = r.json()
    assert scene["config"]["scene_prompt"] == HYPE  # kept as typed
    photo = scene["config"]["photo"]
    assert photo and photo["scene"] and "8k" not in photo["scene"] and "beautiful" not in photo["scene"].lower()
    prompt = scene["loop_shot"]["prompt"]
    assert photo["scene"] in prompt and "8k" not in prompt
    assert len(rewrites) == 1

    # Saving again with the same text reuses the rewrite (no second call),
    # even from a page that didn't send the rewrite back.
    sid = scene["id"]
    saved = {**scene["config"], "photo": None, "total_seconds": 30}
    r = auth_client.patch(f"/api/ambient/{sid}", json={"config": saved})
    assert r.status_code == 200, r.text
    assert len(rewrites) == 1 and r.json()["config"]["photo"]["scene"] == photo["scene"]

    # Editing the text makes a new one.
    r = auth_client.patch(f"/api/ambient/{sid}", json={"config": {**r.json()["config"],
                                                                   "scene_prompt": "a magical misty fjord"}})
    assert len(rewrites) == 2 and "magical" not in r.json()["config"]["photo"]["scene"].lower()


def test_no_rewrite_when_off_stylized_or_a_preset(rewrites):
    from app.services import ambient

    for cfg in (_cfg(scene_preset="custom", scene_prompt=HYPE, rewrite_prompt=False),
                _cfg(scene_preset="custom", scene_prompt=HYPE, look="watercolor"),
                _cfg()):
        assert ambient.ensure_photo_rewrite(cfg).photo is None
    assert rewrites == []


def test_a_stale_or_disabled_rewrite_is_not_used():
    from app.schemas.ambient import PhotoRewrite
    from app.services import ambient

    good = ambient.ensure_photo_rewrite(_cfg(scene_preset="custom", scene_prompt=HYPE))
    assert ambient.active_rewrite(good) is not None
    # Text edited after the rewrite was made: the typed text is used until a new one.
    edited = good.model_copy(update={"scene_prompt": HYPE + " at dusk"})
    assert ambient.active_rewrite(edited) is None and "8k" in ambient.still_prompt(edited)
    assert ambient.active_rewrite(good.model_copy(update={"rewrite_prompt": False})) is None
    assert ambient.active_rewrite(good.model_copy(update={"look": "anime"})) is None
    forged = good.model_copy(update={"photo": PhotoRewrite(source="nope", scene="x")})
    assert ambient.active_rewrite(forged) is None


def test_a_failed_rewrite_falls_back_to_the_typed_text(monkeypatch):
    from app.providers.mock_chat import MockChatProvider
    from app.services import ambient

    def boom(self, text):
        raise RuntimeError("planner down")

    monkeypatch.setattr(MockChatProvider, "rewrite_scene", boom)
    cfg = ambient.ensure_photo_rewrite(_cfg(scene_preset="custom", scene_prompt=HYPE))
    assert cfg.photo is None and "8k ultra realistic" in ambient.still_prompt(cfg)


def test_a_chosen_time_of_day_replaces_the_rewrites_light():
    from app.services import ambient

    cfg = ambient.ensure_photo_rewrite(_cfg(scene_preset="custom", scene_prompt=HYPE))
    assert cfg.photo.light in ambient.still_prompt(cfg)
    night = ambient.still_prompt(cfg.model_copy(update={"time_of_day": "night"}))
    assert cfg.photo.light not in night and "At night" in night and cfg.photo.camera in night


def test_turning_the_rewrite_off_needs_a_new_loop(auth_client):
    cfg = {**SHORT, "scene_preset": "custom", "scene_prompt": HYPE}
    sid = auth_client.post("/api/ambient", json={"config": cfg, "generate": True}).json()["id"]
    assert auth_client.get(f"/api/ambient/{sid}").json()["loop_status"] == "completed"
    saved = auth_client.get(f"/api/ambient/{sid}").json()["config"]
    r = auth_client.patch(f"/api/ambient/{sid}?discard_loop=true",
                          json={"config": {**saved, "rewrite_prompt": False}})
    assert r.status_code == 200 and r.json()["loop_status"] != "completed"


@respx.mock
def test_openrouter_rewrite_request(monkeypatch):
    monkeypatch.setenv("MOCK_PROVIDERS", "false")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    from app import config
    from app.providers.openrouter_chat import SCENE_REWRITE_SYSTEM, OpenRouterChatProvider

    config.get_settings.cache_clear()
    try:
        reply = {"scene": "A wide beach of pale sand.", "light": "Soft daylight.", "camera": "35 mm, f/8."}
        route = respx.post("https://openrouter.ai/api/v1/chat/completions").mock(return_value=httpx.Response(
            200, json={"id": "gen-1", "model": "anthropic/claude-haiku-5.5", "usage": {"cost": 0.0008},
                       "choices": [{"message": {"content": "```json\n" + json.dumps(reply) + "\n```"}}]}))
        out = OpenRouterChatProvider(model="anthropic/claude-haiku-5.5", json_mode=False).rewrite_scene(HYPE)
        assert out == reply
        body = json.loads(route.calls[0].request.content)
        assert body["messages"][0] == {"role": "system", "content": SCENE_REWRITE_SYSTEM}
        assert body["messages"][1] == {"role": "user", "content": HYPE}
    finally:
        config.get_settings.cache_clear()


# ---------- tuned wording ----------


def test_tuned_presets_avoid_what_the_models_got_wrong():
    from app.services import ambient

    fire = ambient.still_prompt(_cfg(scene_preset="fireplace"))
    assert "firebox" in fire and "poker" not in fire and "grate" not in fire
    candles = ambient.still_prompt(_cfg(scene_preset="candles"))
    assert "Two white pillar candles" in candles and "orchid" not in candles
    rain = ambient.still_prompt(_cfg(scene_preset="rainforest"))
    assert "banana leaf" in rain and "macro" in rain


def test_tuned_times_of_day():
    from app.services.ambient import TIMES_OF_DAY

    assert "pink" not in TIMES_OF_DAY["dawn"][1] and "before sunrise" in TIMES_OF_DAY["dawn"][1]
    assert "no visible rays" in TIMES_OF_DAY["golden_hour"][1]
    assert "after sunset" in TIMES_OF_DAY["dusk"][1]
    assert "moonlight" in TIMES_OF_DAY["night"][1]  # night unchanged: neither wording passed


# ---------- review follow-ups ----------


def test_no_rewrite_for_a_scene_that_starts_from_its_own_picture(rewrites):
    from app.services import ambient

    for origin in ("upload", "studio", "asset"):
        cfg = _cfg(scene_preset="custom", scene_prompt="gentle ripples, clouds drifting", picture_origin=origin)
        assert ambient.ensure_photo_rewrite(cfg).photo is None
    assert rewrites == []
    # A generated preview picture came from the (rewritten) description: the rewrite still applies.
    gen = ambient.ensure_photo_rewrite(_cfg(scene_preset="custom", scene_prompt=HYPE, picture_origin="generated"))
    assert ambient.active_rewrite(gen) is not None


def _failing(monkeypatch):
    from app.providers.mock_chat import MockChatProvider

    real = MockChatProvider.rewrite_scene

    def boom(self, text):
        raise RuntimeError("planner down")

    monkeypatch.setattr(MockChatProvider, "rewrite_scene", boom)
    return lambda: monkeypatch.setattr(MockChatProvider, "rewrite_scene", real)


def test_an_unrelated_save_never_swaps_the_prompt_behind_a_clip(auth_client, monkeypatch, rewrites):
    restore = _failing(monkeypatch)
    cfg = {**SHORT, "scene_preset": "custom", "scene_prompt": HYPE}
    sid = auth_client.post("/api/ambient", json={"config": cfg, "generate": False}).json()["id"]
    restore()  # the planner works again from here on
    # Built from the typed text (the generate path would add the rewrite: see below).
    scene = auth_client.get(f"/api/ambient/{sid}").json()
    assert scene["config"]["photo"] is None
    saved = scene["config"]
    r = auth_client.patch(f"/api/ambient/{sid}", json={"config": {**saved, "music_volume": 0.3}})
    assert r.status_code == 200 and r.json()["config"]["photo"] is None
    assert "8k" in r.json()["loop_shot"]["prompt"]  # unchanged prompt
    assert rewrites == []


def test_generating_makes_a_missing_rewrite_first(auth_client, monkeypatch):
    restore = _failing(monkeypatch)
    cfg = {**SHORT, "scene_preset": "custom", "scene_prompt": HYPE}
    sid = auth_client.post("/api/ambient", json={"config": cfg, "generate": False}).json()["id"]
    assert auth_client.get(f"/api/ambient/{sid}").json()["config"]["photo"] is None
    restore()
    assert auth_client.post(f"/api/ambient/{sid}/generate-loop").status_code == 202
    scene = auth_client.get(f"/api/ambient/{sid}").json()
    assert scene["config"]["photo"]["scene"] and "8k" not in scene["loop_shot"]["prompt"]
    assert scene["loop_status"] == "completed"


def test_toggling_with_no_rewrite_keeps_the_loop(auth_client, monkeypatch):
    _failing(monkeypatch)
    cfg = {**SHORT, "scene_preset": "custom", "scene_prompt": HYPE}
    sid = auth_client.post("/api/ambient", json={"config": cfg, "generate": True}).json()["id"]
    scene = auth_client.get(f"/api/ambient/{sid}").json()
    assert scene["loop_status"] == "completed" and scene["config"]["photo"] is None
    r = auth_client.patch(f"/api/ambient/{sid}", json={"config": {**scene["config"], "rewrite_prompt": False}})
    assert r.json()["loop_status"] == "completed"  # the prompt didn't change


def test_indoor_typed_text_keeps_its_own_light_under_a_time_of_day():
    from app.services import ambient

    cfg = ambient.ensure_photo_rewrite(_cfg(scene_preset="fireplace", scene_prompt="a magical cabin fireplace"))
    night = ambient.still_prompt(cfg.model_copy(update={"time_of_day": "night"}))
    assert cfg.photo.light in night and "It is night outside." in night

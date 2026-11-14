"""ADULT_FEATURES=false: a clean deployment. Adult mode is locked off, the
API refuses to turn it on, every content filter is on and uncensored models
are hidden and refused. ADULT_FEATURES unset (true) changes nothing."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from .test_content_mode import SOGNI_WF, _maya, _tick_now
from .test_models_catalog import _mock_sources, _real
from .test_runware import _CORE, _IMG, _fake_model, _ref

ADULT_ONLY_SOGNI = ("sogni/seedream-5-0-pro-uncensored", "sogni/seedream-5-5-uncensored", "sogni/z-turbo",
                    "sogni/moss-image")
ADULT_ONLY_VIDEO = ("sogni/seedance2-5-uncensored", "sogni/seedance2-mini-uncensored", "sogni/wan3.0-spicy-video")


def _reset():
    from app import config
    from app.services import provider_keys

    config.get_settings.cache_clear()
    provider_keys.invalidate()


def _clean(monkeypatch):
    monkeypatch.setenv("ADULT_FEATURES", "false")
    _reset()


def _started(bodies):
    def started(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"status": "success", "data": {"workflow": {"workflowId": "wf", "status": "queued"}}})
    return started


# ---------- default: as today ----------


def test_unset_behaves_as_today(auth_client, monkeypatch):
    from app.providers import sogni
    from app.services import content_mode

    monkeypatch.delenv("ADULT_FEATURES", raising=False)
    _reset()
    assert content_mode.features() is True
    r = auth_client.get("/api/settings/content").json()
    assert r["available"] is True and r["text_model"] == "deepseek/deepseek-v4-flash"
    assert auth_client.put("/api/settings/content", json={"adult": True}).json()["adult"] is True
    assert content_mode.text_model() == "deepseek/deepseek-v4-flash"
    avatar_id, _ = _maya()
    r = auth_client.put(f"/api/settings/content/avatars/{avatar_id}", json={"adult_ok": True})
    assert r.status_code == 200 and r.json()["adult_ok_avatars"] == [avatar_id]
    images = {m.selector for m in sogni.image_models()}
    assert {"seedream-5-0-pro-uncensored", "seedream-5-5-uncensored", "z-turbo"} <= images
    assert "seedance2-5-uncensored" in {m.selector for m in sogni.video_models()}
    assert "seedream-5-0-pro-uncensored" in {e.selector for e in sogni.edit_models()}
    assert not sogni.hidden("sogni/seedream-5-0-pro-uncensored")


# ---------- locked off ----------


def test_adult_mode_is_locked_off_whatever_env_or_switch_say(auth_client, monkeypatch):
    from app.services import content_mode

    monkeypatch.setenv("ADULT_MODE", "true")
    _reset()
    content_mode.set_adult_mode(True)  # a row saved before the deployment was made clean
    assert content_mode.adult_mode() is True
    _clean(monkeypatch)
    assert content_mode.adult_mode() is False
    assert content_mode.text_model() == ""
    assert content_mode.chat_model() == content_mode.status()["planner"]


def test_the_api_refuses_to_turn_it_on_and_reports_it_unavailable(auth_client, monkeypatch):
    _clean(monkeypatch)
    r = auth_client.put("/api/settings/content", json={"adult": True})
    assert r.status_code == 403 and r.json()["detail"] == "Adult content is turned off for this deployment."
    assert auth_client.put("/api/settings/content", json={"adult": False}).status_code == 200
    assert auth_client.put("/api/settings/content", json={"adult": None}).status_code == 200
    got = auth_client.get("/api/settings/content").json()
    assert got["available"] is False and got["adult"] is False and got["default"] is False
    assert got["text_model"] == "" and got["adult_ok_avatars"] == []
    assert got["adult_ok_unchecked"] == {} and got["adult_ok_declined"] == {} and got["adult_ok_notices"] == {}
    assert got["adult_ok_checking"] == [] and got["adult_ok_stale"] == []
    assert "model" in got["safety_check"] and got["safety_check"]["note"]  # the minors check still reports
    avatar_id, _ = _maya()
    r = auth_client.put(f"/api/settings/content/avatars/{avatar_id}", json={"adult_ok": True})
    assert r.status_code == 403
    assert auth_client.put("/api/settings/content/avatars/99999", json={"adult_ok": True}).status_code == 403
    assert auth_client.put(f"/api/settings/content/avatars/{avatar_id}", json={"adult_ok": False}).status_code == 200
    # Clip check doesn't say adult mode is on.
    assert auth_client.get("/api/settings/clip-check").json()["adult_mode"] is False


def test_existing_marks_are_ignored(auth_client, monkeypatch):
    from app.db import SessionLocal
    from app.services import content_mode
    from app.services.pipeline import _asset_refs

    avatar_id, asset_id = _maya()
    _tick_now(avatar_id)  # a tick that holds, saved before the deployment was made clean
    content_mode.set_adult_mode(True)
    assert avatar_id in content_mode.adult_ok_avatars() and content_mode.holds(avatar_id)
    _clean(monkeypatch)
    assert content_mode.adult_ok_avatars() == {}
    assert not content_mode.holds(avatar_id) and content_mode.tick_row(avatar_id) is None
    db = SessionLocal()
    try:
        (ref,) = _asset_refs(db, [asset_id])
    finally:
        db.close()
    assert ref.adult_ok is False and content_mode.exempt(ref) is False and content_mode.forces_filter(ref)


# ---------- filters ----------


def test_runware_check_is_on_for_every_request(client, monkeypatch):
    from app.providers import runware
    from app.services import content_mode, provider_keys

    monkeypatch.setenv("MOCK_PROVIDERS", "false")
    monkeypatch.setenv("RUNWARE_API_KEY", "rw-test-abcdefgh1234")
    monkeypatch.setenv("RUNWARE_CHECK_CONTENT", "false")
    monkeypatch.setenv("ADULT_MODE", "true")
    _reset()
    model = _fake_model(monkeypatch, "test:clean@1", {
        "type": "object", "additionalProperties": False, "required": ["taskType", "taskUUID", "model"],
        "properties": {**_CORE,
                       "safety": {"type": "object", "properties": {"checkContent": {"type": "boolean"}}},
                       "providerSettings": {"type": "object", "properties": {"bfl": {"type": "object", "properties": {
                           "safetyTolerance": {"type": "integer", "minimum": 0, "maximum": 6}}}}},
                       "inputs": {"type": "object", "properties": {"referenceImages": {"type": "array", "items": _IMG}}}}},
        modes=[{"id": "text"}], caps=["text-to-image"])
    provider_keys.set_option("runware", "check_content", False)
    assert runware.build_image_task(model, "a quiet street at dusk", [])["safety"]["checkContent"] is False
    _clean(monkeypatch)
    assert content_mode.adult_mode() is False
    task = runware.build_image_task(model, "a quiet street at dusk", [])
    assert task["safety"]["checkContent"] is True and "providerSettings" not in task
    # Even a caller that asks for it off.
    assert runware.build_image_task(model, "a quiet street", [], check_content=False)["safety"]["checkContent"] is True
    assert runware._pictures_need_check([_ref()], "a nude portrait") is True
    # Settings → API keys shows no switch and refuses turning the check off.
    assert provider_keys.option("runware", "check_content") is True
    row = provider_keys._row("runware")
    assert "options" not in row and "uncensored" not in row["powers"]
    # Runware's open models aren't advertised as uncensored.
    assert not any(e.get("uncensored") for e in runware.catalog_entries(True))


def test_runware_options_endpoint_refuses_turning_the_check_off(auth_client, monkeypatch):
    _clean(monkeypatch)
    r = auth_client.put("/api/settings/keys/runware/options", json={"check_content": False})
    assert r.status_code == 403
    assert auth_client.put("/api/settings/keys/runware/options", json={"check_content": True}).status_code == 200
    rows = auth_client.get("/api/settings/keys").json()["keys"]
    rw = next(k for k in rows if k["name"] == "runware")
    assert "options" not in rw and "uncensored" not in rw["powers"]


def test_sogni_safe_filter_is_always_on(auth_client, monkeypatch):
    from app.providers.sogni import SogniClient

    monkeypatch.setenv("SOGNI_API_KEY", "sk-test-123456789")
    monkeypatch.setenv("SOGNI_SAFE_CONTENT_FILTER", "false")
    monkeypatch.setenv("ADULT_MODE", "true")
    _reset()
    avatar_id, _ = _maya()
    _tick_now(avatar_id)
    _clean(monkeypatch)
    bodies: list = []
    with respx.mock() as router:
        router.post(SOGNI_WF).mock(side_effect=_started(bodies))
        SogniClient().start(title="t", tool="generate_image", arguments={"prompt": "a lake"})
        SogniClient().start(title="t", tool="edit_image", arguments={"prompt": "a nude portrait"},
                            media_references=[{"kind": "image", "url": "https://x.test/maya.png"}])
    assert [b["safe_content_filter"] for b in bodies] == [True, True]


# ---------- uncensored models ----------


def test_uncensored_sogni_models_are_hidden(auth_client, monkeypatch):
    from app.providers import sogni

    _clean(monkeypatch)
    images = {m.selector for m in sogni.image_models()}
    videos = {m.selector for m in sogni.video_models()}
    edits = {e.selector for e in sogni.edit_models()}
    assert not any(m.uncensored for m in sogni.image_models() + sogni.video_models())
    assert not {s.split("/", 1)[1] for s in ADULT_ONLY_SOGNI} & images
    assert not {s.split("/", 1)[1] for s in ADULT_ONLY_VIDEO} & videos
    assert "seedream-5-0-pro-uncensored" not in edits and "dark-beast-krea2-identity-edit" not in edits
    assert "seedream-5-5-uncensored" not in edits
    assert "flux-3-klein" in edits and "krea-2-turbo" in images  # the rest stay
    # A picked model never routes to an uncensored editor.
    for model in ("sogni/krea-2-turbo", "sogni/flux-3-klein"):
        for intent in ("identity", "guide"):
            e = sogni.editor_for(model, intent)
            assert e is None or not sogni.adult_only(e.selector)


@respx.mock
def test_uncensored_models_are_absent_from_lists_and_refused(auth_client, monkeypatch):
    from app.services import model_registry
    from app.services.provider_keys import ProviderUnavailable

    _real(monkeypatch, SOGNI_API_KEY="sk-test-123456789")
    _mock_sources()
    auth_client.post("/api/models/refresh")  # stored while adult features were on
    before = {e["id"] for e in auth_client.get("/api/models/catalog").json()["entries"]}
    assert "sogni/seedream-5-0-pro-uncensored" in before
    monkeypatch.setenv("ADULT_FEATURES", "false")
    _reset()
    snap = auth_client.get("/api/models/catalog").json()
    ids = {e["id"] for e in snap["entries"]}
    assert not set(ADULT_ONLY_SOGNI + ADULT_ONLY_VIDEO) & ids
    assert not any(e.get("uncensored") for e in snap["entries"])
    assert "sogni/krea-2-turbo" in ids
    imgs = auth_client.get("/api/providers/openrouter/image-models").json()
    vids = auth_client.get("/api/providers/openrouter/video-models").json()
    listed = {m["id"] for m in imgs} | {m["id"] for m in vids}
    assert not set(ADULT_ONLY_SOGNI + ADULT_ONLY_VIDEO) & listed
    assert not any("uncensored" in (m.get("name") or "").lower() for m in imgs + vids)
    for m in imgs:
        for editor in (m.get("with_pictures") or {}).values():
            assert editor is None or "uncensored" not in editor["id"]
    # Asked for directly: refused, even as an unlisted id.
    for kind, model in (("image", ADULT_ONLY_SOGNI[0]), ("video", ADULT_ONLY_VIDEO[0])):
        r = auth_client.put("/api/models/active", json={"kind": kind, "model": model})
        assert r.status_code in (409, 422), r.text
        r = auth_client.put("/api/models/active", json={"kind": kind, "model": model, "allow_unlisted": True})
        assert r.status_code in (409, 422), r.text
        with pytest.raises(ProviderUnavailable, match="turned off for this deployment"):
            model_registry.require(kind, model)


_CHAT = {"architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
         "supported_parameters": ["response_format", "temperature"]}
ADULT_CHAT = (
    {"id": "cognitivecomputations/dolphin-mistral-24b-venice-edition", "name": "Venice: Uncensored",
     "description": "Venice Uncensored Dolphin Mistral 24B Venice Edition.", **_CHAT},
    {"id": "thedrummer/cydonia-24b-v4.1", "name": "TheDrummer: Cydonia 24B V4.1",
     "description": "Uncensored and creative writing model based on Mistral Small.", **_CHAT},
)


@respx.mock
def test_uncensored_chat_models_are_hidden_and_refused(auth_client, monkeypatch):
    """Other providers too: an OpenRouter chat model that says, in its name
    or description, it is uncensored is hidden and can't be the planner."""
    from app.services import model_registry
    from app.services.provider_keys import ProviderUnavailable

    from .test_models_catalog import MODELS

    _real(monkeypatch)
    _mock_sources(models={"data": MODELS["data"] + list(ADULT_CHAT)})
    auth_client.post("/api/models/refresh")
    adult = {m["id"] for m in ADULT_CHAT}
    chat = {e["id"] for e in auth_client.get("/api/models/catalog?kind=chat").json()["entries"]}
    assert adult <= chat  # adult features on: listed, as today
    assert auth_client.put("/api/models/active", json={"kind": "chat", "model": ADULT_CHAT[1]["id"]}).status_code == 200
    _clean(monkeypatch)
    entries = auth_client.get("/api/models/catalog?kind=chat").json()["entries"]
    chat = {e["id"] for e in entries}
    assert not adult & chat and "openai/gpt-4o-mini" in chat
    # The saved pick falls back to the default planner.
    assert model_registry.active_model("chat") == model_registry.default_model("chat")
    for model in adult:
        for body in ({}, {"allow_unlisted": True}):
            r = auth_client.put("/api/models/active", json={"kind": "chat", "model": model, **body})
            assert r.status_code in (409, 422), r.text
        with pytest.raises(ProviderUnavailable, match="turned off for this deployment"):
            model_registry.require("chat", model)
    # Not in the stored catalog, but its id says so: refused too.
    with pytest.raises(ProviderUnavailable, match="turned off for this deployment"):
        model_registry.require("chat", "someone/llama-uncensored-8b")
    assert auth_client.put("/api/models/active", json={"kind": "chat", "model": "openai/gpt-4o-mini"}).status_code == 200


def test_refused_in_mock_mode_too_and_old_picks_fall_back(auth_client, monkeypatch):
    from app.providers import get_image
    from app.services import model_registry
    from app.services.provider_keys import ProviderUnavailable

    model_registry.set_active("image", "sogni/seedream-5-0-pro-uncensored")
    _clean(monkeypatch)
    assert model_registry.active_model("image") == model_registry.default_model("image")
    with pytest.raises(ProviderUnavailable):
        get_image("sogni/seedream-5-0-pro-uncensored")


# ---------- minors checks unchanged ----------


def test_minors_checks_are_unchanged(auth_client, monkeypatch):
    from app.providers.sogni import SogniClient
    from app.services.safety import UnsafePromptError, check_prompt

    monkeypatch.setenv("SOGNI_API_KEY", "sk-test-123456789")
    _clean(monkeypatch)
    with pytest.raises(UnsafePromptError):
        check_prompt("naked teenage girl")
    bodies: list = []
    with respx.mock() as router:
        router.post(SOGNI_WF).mock(side_effect=_started(bodies))
        with pytest.raises(UnsafePromptError):
            SogniClient().start(title="t", tool="generate_image", arguments={"prompt": "naked teenage girl"})
        assert not bodies  # never sent
        SogniClient().start(title="t", tool="generate_image", arguments={"prompt": "kids at a beach"})
    assert [b["safe_content_filter"] for b in bodies] == [True]
    # A character described as a minor is still recognised as one.
    from app import models
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        av = models.Avatar(name="Kid", description="a 12-year-old boy")
        db.add(av)
        db.commit()
        from app.services import content_mode

        assert content_mode.describes_minor(av) is True
    finally:
        db.close()


# ---------- what the safety fixes added (Cast ticks, Sogni switch, notes) ----------


def test_old_ticks_queue_no_picture_check_and_edits_skip_the_tick_check(auth_client, monkeypatch):
    """A tick saved before the deployment was made clean: a new picture on
    the card queues no check, the job finds nothing ticked, and editing the
    card neither runs the tick check nor moves the tick."""
    from app.providers import mock_chat
    from app.services import cast_pictures, content_mode

    from .test_character_check import _upload

    avatar_id, _ = _maya()
    _tick_now(avatar_id)
    _clean(monkeypatch)
    queued: list[int] = []
    monkeypatch.setattr(cast_pictures, "queue_check", lambda aid: queued.append(aid) or "job")
    _upload(auth_client, avatar_id, "later.png")
    assert queued == []
    assert cast_pictures.run(avatar_id) == "unticked"
    mock_chat.MINOR_CALLS.clear()
    r = auth_client.patch(f"/api/avatars/{avatar_id}", json={"description": "Maya, 30, a painter"})
    assert r.status_code == 200 and r.json().get("adult_ok_notice") is None
    assert not content_mode.move_tick(avatar_id, "a", "b")
    assert not content_mode.tick_if_unchanged(avatar_id, None, card="x")
    assert content_mode.tick_row(avatar_id) is None
    # Still a clean deployment's answer after the edit: no tick anywhere.
    got = auth_client.get("/api/settings/content").json()
    assert got["adult_ok_avatars"] == [] and got["adult_ok_stale"] == []


def test_sogni_switch_is_hidden_locked_on_and_refused(auth_client, monkeypatch):
    from app.providers import sogni
    from app.services import provider_keys

    monkeypatch.setenv("SOGNI_SAFE_CONTENT_FILTER", "false")
    monkeypatch.setenv("ADULT_MODE", "true")
    _reset()
    provider_keys.set_option("sogni", "safe_filter", False)
    assert sogni.safe_filter_default() is False  # adult features on: the switch counts
    _clean(monkeypatch)
    assert provider_keys.option("sogni", "safe_filter") is True
    assert sogni.safe_filter_default() is True
    r = auth_client.put("/api/settings/keys/sogni/options", json={"safe_filter": False})
    assert r.status_code == 403 and r.json()["detail"] == "Adult content is turned off for this deployment."
    assert auth_client.put("/api/settings/keys/sogni/options", json={"safe_filter": True}).status_code == 200
    rows = auth_client.get("/api/settings/keys").json()["keys"]
    assert "options" not in next(k for k in rows if k["name"] == "sogni")


def test_safety_check_messages_name_no_adult_mode(auth_client, monkeypatch):
    from app.providers import mock_chat
    from app.services import minor_check, safety

    from .test_minor_check import ADULT_SEXUAL

    _clean(monkeypatch)
    assert "adult" not in minor_check._note(False).lower()
    assert "adult" not in minor_check._note(True).lower()
    mock_chat.MINOR_REPLY = RuntimeError("timeout")
    try:
        with pytest.raises(safety.UnsafePromptError) as e:
            minor_check.check_generation(ADULT_SEXUAL, kind="image")
    finally:
        mock_chat.MINOR_REPLY = None
    assert str(e.value) == minor_check.UNAVAILABLE_CLEAN and "adult" not in str(e.value).lower()
    # With adult features on, the full message (as before).
    monkeypatch.delenv("ADULT_FEATURES", raising=False)
    _reset()
    assert minor_check._unavailable() == minor_check.UNAVAILABLE


def test_hook_lab_decline_names_no_adult_mode(monkeypatch):
    from app.services import hook_lab

    assert "Adult mode" in str(hook_lab._planner_refused("declined"))
    _clean(monkeypatch)
    assert "adult" not in str(hook_lab._planner_refused("declined")).lower()
    assert "minor" in str(hook_lab._planner_refused("minor"))


def test_safety_memory_stays_on_a_clean_deployment(auth_client, monkeypatch):
    """The minors check's remembered refusals can still be seen and cleared."""
    _clean(monkeypatch)
    got = auth_client.get("/api/settings/content/safety-memory")
    assert got.status_code == 200 and "refusals" in got.json()
    cleared = auth_client.post("/api/settings/content/safety-memory/clear-refusals")
    assert cleared.status_code == 200 and "cleared" in cleared.json()


def test_live_provider_model_lists_hide_adult_models_on_a_clean_deployment(auth_client, monkeypatch):
    """The Studio pickers merge a provider's live list with the catalog; on a
    clean deployment the live part is filtered too."""
    from app.api import providers as api_providers
    from app.providers.base import ModelInfo

    live = [ModelInfo(id="vendor/plain-image", name="Plain", modality="image"),
            ModelInfo(id="vendor/uncensored-image", name="Uncensored image", modality="image")]

    class _Live:
        def list_models(self):
            return live

    monkeypatch.setattr(api_providers, "get_image", lambda: _Live())
    ids = {m["id"] for m in auth_client.get("/api/providers/openrouter/image-models").json()}
    assert {"vendor/plain-image", "vendor/uncensored-image"} <= ids  # default: unchanged
    _clean(monkeypatch)
    try:
        ids = {m["id"] for m in auth_client.get("/api/providers/openrouter/image-models").json()}
        assert "vendor/plain-image" in ids and "vendor/uncensored-image" not in ids
    finally:
        monkeypatch.delenv("ADULT_FEATURES")
        _reset()


_PERMISSIVE = ("adult and mature themes", "do not\n   refuse, soften or censor", "soften or censor")


def _sent_storyboard_system(monkeypatch) -> str:
    """The system prompt the real planner sends, captured from the request."""
    from app.providers.base import CastContext
    from app.providers.mock_chat import MockChatProvider
    from app.providers.openrouter_chat import OpenRouterChatProvider

    from .test_models_catalog import OR

    _real(monkeypatch)
    plan = MockChatProvider().generate_storyboard(
        project={"title": "t", "mode": "reel_montage", "original_script": "Hello world.",
                 "target_duration_seconds": 10}, cast=CastContext()).model_dump(mode="json")
    with respx.mock:
        route = respx.post(f"{OR}/chat/completions").mock(return_value=httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(plan)}}]}))
        OpenRouterChatProvider(model="vendor/plain-chat", json_mode=False).generate_storyboard(
            project={"title": "t"}, cast=CastContext())
    return json.loads(route.calls[0].request.content)["messages"][0]["content"]


def test_storyboard_planner_gets_a_clean_content_rule_on_a_clean_deployment(monkeypatch):
    """The planner's narration and captions reach the voice and the video
    without passing a picture filter, so a clean deployment must not tell it
    adult themes are allowed."""
    from app.providers import openrouter_chat

    monkeypatch.delenv("ADULT_FEATURES", raising=False)
    _reset()
    default = _sent_storyboard_system(monkeypatch)
    raw = (openrouter_chat.Path(openrouter_chat.__file__).parent.parent / "prompts"
           / "storyboard_system.md").read_text(encoding="utf-8")
    assert default == raw  # default: unchanged
    assert "adult and mature themes between adults are allowed" in default

    _clean(monkeypatch)
    clean = _sent_storyboard_system(monkeypatch)
    assert not any(p in clean for p in _PERMISSIVE)
    assert "general audience" in clean and "non-explicit" in clean
    assert "content_warning_notes" in clean and "sexual content involving minors" in clean
    # Only rule 7 changes; the rules around it are kept.
    assert clean.count("7. **Content**") == 1 and "8. **Duration**" in clean
    assert clean.replace(openrouter_chat.CLEAN_CONTENT_RULE, "") == openrouter_chat._CONTENT_RULE.sub("", raw)


def test_clean_content_rule_still_applies_if_the_prompt_file_changes_shape(monkeypatch):
    from app.providers import openrouter_chat

    _clean(monkeypatch)
    monkeypatch.setattr(openrouter_chat.Path, "read_text", lambda self, encoding=None: "Plan it.\n")
    out = openrouter_chat._load_system_prompt()
    assert out.startswith("Plan it.") and openrouter_chat.CLEAN_CONTENT_RULE in out


def test_other_chat_prompts_carry_no_adult_permissive_rule():
    """Enhance, hook ideas and scene rewrites never told the model adult
    content is allowed; keep it that way (the minors classifier's scope line
    is a classifier instruction, not a writing rule)."""
    from app.providers import openrouter_chat as oc

    texts = [oc.HOOK_IDEAS_SYSTEM, oc.SCENE_REWRITE_SYSTEM, oc._ENHANCE_COMMON, *oc.ENHANCE_GUIDES.values()]
    for t in texts:
        low = t.lower()
        assert "are allowed" not in low and "is allowed" not in low and "censor" not in low


def _no_check_model(monkeypatch, air_id: str):
    return _fake_model(monkeypatch, air_id, {
        "type": "object", "additionalProperties": False, "required": ["taskType", "taskUUID", "model"],
        "properties": {**_CORE, "duration": {"type": "integer", "enum": [5]}}},
        modes=[{"id": "text"}], caps=["text-to-image", "text-to-video"])


def test_runware_model_without_a_content_check_is_refused_and_hidden(client, monkeypatch):
    import copy

    from app.providers import runware
    from app.providers.runware import RunwareError, RunwareImageProvider

    monkeypatch.setenv("MOCK_PROVIDERS", "false")
    monkeypatch.setenv("RUNWARE_API_KEY", "rw-test-abcdefgh1234")
    monkeypatch.delenv("ADULT_FEATURES", raising=False)
    _reset()
    model = _no_check_model(monkeypatch, "test:nocheck@1")
    # A curated model whose schema has no content check.
    b = runware.bundle()
    stripped = copy.deepcopy(b["schemas"]["runware:107@1"])
    stripped["request"]["properties"].pop("safety", None)
    monkeypatch.setitem(b["schemas"], "runware:107@1", stripped)
    # Default: as before (a text-only request goes out without the field).
    assert "safety" not in runware.build_image_task(model, "a quiet street at dusk", [])
    assert "safety" not in runware.build_video_task(model, "a quiet street at dusk", [])[0]
    assert "runware/runware:107@1" in {e["id"] for e in runware.catalog_entries(True)}
    _clean(monkeypatch)
    for build in (lambda: runware.build_image_task(model, "a quiet street at dusk", []),
                  lambda: runware.build_video_task(model, "a quiet street at dusk", [])):
        with pytest.raises(RunwareError) as e:
            build()
        assert e.value.refused and e.value.final and "no content check" in str(e.value)
    # Nothing is sent (or paid for).
    with respx.mock(assert_all_called=False) as router:
        post = router.post(url__regex=r".*").mock(return_value=httpx.Response(500))
        with pytest.raises(RunwareError):
            RunwareImageProvider().generate(model=model, prompt="a quiet street at dusk", references=[])
    assert not post.called
    # Left out of the pickers; models with the check and picture tools stay.
    ids = {e["id"] for e in runware.catalog_entries(True)}
    assert "runware/runware:107@1" not in ids
    assert {"runware/runware:106@1", "runware/runware:112@5"} <= ids


# ---------- review follow-ups ----------


@respx.mock
def test_catalog_flagged_chat_models_are_refused_with_a_variant_suffix_or_capitals(auth_client, monkeypatch):
    """A chat model the catalog says is uncensored is refused however its id
    is written: an OpenRouter variant suffix or other capitals reach the
    same model, so it can't be the planner (nor so the minors judge)."""
    from app.services import content_mode, minor_check, model_registry

    from .test_models_catalog import MODELS

    _real(monkeypatch)
    _mock_sources(models={"data": MODELS["data"] + list(ADULT_CHAT)})
    auth_client.post("/api/models/refresh")
    variants = ("cognitivecomputations/dolphin-mistral-24b-venice-edition:nitro",
                "thedrummer/cydonia-24b-v4.1:floor", "TheDrummer/Cydonia-24B-V4.1")
    for model in variants:  # adult features on: as today
        assert not model_registry.adult_only(model, "chat")
    model_registry.set_active("chat", variants[1])
    _clean(monkeypatch)
    for model in variants:
        assert model_registry.adult_only(model) and model_registry.adult_only(model, "chat"), model
        r = auth_client.put("/api/models/active", json={"kind": "chat", "model": model, "allow_unlisted": True})
        assert r.status_code in (409, 422), (model, r.text)
    assert model_registry.active_model("chat") == model_registry.default_model("chat")
    assert minor_check.check_model() not in variants
    assert not model_registry.adult_only("openai/gpt-4o-mini:nitro", "chat")
    # Runware's ids have a ":" of their own: not a variant suffix.
    assert content_mode._base_id("runware/runware:107@1") == "runware/runware:107@1"


def test_the_adult_text_family_cant_write_text_but_the_picture_check_still_runs(auth_client, monkeypatch):
    """On a clean deployment every model of the adult text models' family is
    refused for text work (another DeepSeek version too), even with the
    settings blanked; the Cast picture check's model, of the same family,
    still judges pictures."""
    from app.services import minor_check, model_registry
    from app.services.provider_keys import ProviderUnavailable

    _real(monkeypatch)
    others = ("deepseek/deepseek-v4.1-flash", "deepseek/deepseek-chat:nitro", "DeepSeek/DeepSeek-R1")
    for model in others:  # adult features on: as today
        assert model_registry.route_info("chat", model)[0] == "openrouter"
    assert auth_client.put("/api/models/active", json={
        "kind": "chat", "model": others[0], "allow_unlisted": True}).status_code == 200
    _clean(monkeypatch)
    assert model_registry.active_model("chat") == model_registry.default_model("chat")
    for model in others:
        r = auth_client.put("/api/models/active", json={"kind": "chat", "model": model, "allow_unlisted": True})
        assert r.status_code in (409, 422), (model, r.text)
        with pytest.raises(ProviderUnavailable, match="turned off for this deployment"):
            model_registry.require("chat", model)
    # Blanking the settings doesn't bring the built-in defaults back.
    monkeypatch.setenv("ADULT_TEXT_MODEL", "")
    monkeypatch.setenv("ADULT_TEXT_FALLBACK_MODEL", "")
    _reset()
    for model in ("deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-pro", others[0]):
        with pytest.raises(ProviderUnavailable, match="turned off for this deployment"):
            model_registry.require("chat", model)
    # Other families and other kinds are untouched.
    assert model_registry.route_info("chat", "openai/gpt-4o-mini")[0] == "openrouter"
    assert not model_registry.adult_only(others[0], "image")
    # The picture check's model (DeepSeek V4.1 Flash by default) still runs.
    assert minor_check.picture_model() == "deepseek/deepseek-v4.1-flash"
    assert minor_check._picture_route() == ("openrouter", "deepseek/deepseek-v4.1-flash")
    assert auth_client.get("/api/settings/content").json()["safety_check"]["picture_available"] is True
    assert minor_check._picture_judge()[1] == "deepseek/deepseek-v4.1-flash"


@pytest.mark.parametrize("clean", [False, True])
def test_switches_take_only_real_true_false_or_null(auth_client, monkeypatch, clean):
    """0 and 1 are not booleans: refused (422) in both modes, so a 1 can never
    slip past a lock that checks for a real true."""
    from app.services import content_mode, provider_keys

    if clean:
        _clean(monkeypatch)
    else:
        monkeypatch.delenv("ADULT_FEATURES", raising=False)
        _reset()
    avatar_id, _ = _maya()
    for bad in (1, 0, 1.0, "true", [], {}):
        r = auth_client.put("/api/settings/content", json={"adult": bad})
        assert r.status_code == 422, (bad, r.text)
        r = auth_client.put(f"/api/settings/content/avatars/{avatar_id}", json={"adult_ok": bad})
        assert r.status_code == 422, (bad, r.text)
        for name, opt in (("runware", "check_content"), ("sogni", "safe_filter")):
            r = auth_client.put(f"/api/settings/keys/{name}/options", json={opt: bad})
            assert r.status_code == 422, (name, bad, r.text)
        r = auth_client.put("/api/settings/keys/runware/enabled", json={"enabled": bad})
        assert r.status_code == 422, (bad, r.text)
    from app.services.model_registry import get_setting

    assert content_mode.explicit() is None and not content_mode.adult_ok_avatars()
    assert provider_keys.explicit_enabled("runware") is None
    for name in ("runware", "sogni"):
        assert not get_setting(provider_keys.OPTIONS_PREFIX + name)  # nothing saved
    # The real values still work.
    assert auth_client.put("/api/settings/content", json={"adult": False}).status_code == 200
    assert auth_client.put("/api/settings/content", json={"adult": None}).status_code == 200
    for v in (True, None):
        assert auth_client.put("/api/settings/keys/runware/options", json={"check_content": v}).status_code == 200
        assert auth_client.put("/api/settings/keys/sogni/options", json={"safe_filter": v}).status_code == 200


ADULT_TEXT = (
    {"id": "deepseek/deepseek-v4-flash", "name": "DeepSeek: V4 Flash", "description": "A fast model.", **_CHAT},
    {"id": "deepseek/deepseek-v4-pro", "name": "DeepSeek: V4 Pro", "description": "A strong model.", **_CHAT},
)


@respx.mock
def test_adult_text_models_cant_be_the_planner_on_a_clean_deployment(auth_client, monkeypatch):
    """ADULT_TEXT_MODEL and ADULT_TEXT_FALLBACK_MODEL don't say "uncensored",
    but on a clean deployment they are adult-only all the same."""
    from app.services import model_registry
    from app.services.provider_keys import ProviderUnavailable

    from .test_models_catalog import MODELS

    _real(monkeypatch)
    _mock_sources(models={"data": MODELS["data"] + list(ADULT_TEXT)})
    auth_client.post("/api/models/refresh")
    ids = {m["id"] for m in ADULT_TEXT}
    chat = {e["id"] for e in auth_client.get("/api/models/catalog?kind=chat").json()["entries"]}
    assert ids <= chat  # adult features on: listed and pickable, as today
    assert auth_client.put("/api/models/active", json={"kind": "chat", "model": ADULT_TEXT[0]["id"]}).status_code == 200
    assert model_registry.active_model("chat") == ADULT_TEXT[0]["id"]
    _clean(monkeypatch)
    chat = {e["id"] for e in auth_client.get("/api/models/catalog?kind=chat").json()["entries"]}
    assert not ids & chat and "openai/gpt-4o-mini" in chat
    assert model_registry.active_model("chat") == model_registry.default_model("chat")
    for model in ids:
        for body in ({}, {"allow_unlisted": True}):
            r = auth_client.put("/api/models/active", json={"kind": "chat", "model": model, **body})
            assert r.status_code in (409, 422), r.text
        with pytest.raises(ProviderUnavailable, match="turned off for this deployment"):
            model_registry.require("chat", model)
    # The same model with an OpenRouter variant suffix or other capitals
    # routes to it too, so it is refused the same way.
    for model in ("deepseek/deepseek-v4-flash:nitro", "deepseek/deepseek-v4-pro:floor",
                  "DeepSeek/DeepSeek-V4-Flash"):
        r = auth_client.put("/api/models/active", json={"kind": "chat", "model": model, "allow_unlisted": True})
        assert r.status_code in (409, 422), (model, r.text)
        assert model_registry.adult_only(model)
    assert not model_registry.adult_only("openai/gpt-4o-mini:nitro")
    # Empty settings name no model.
    monkeypatch.setenv("ADULT_TEXT_FALLBACK_MODEL", "")
    _reset()
    from app.services import content_mode

    assert content_mode.adult_text_ids() == {"deepseek/deepseek-v4-flash"}
    assert not model_registry.adult_only("")


@pytest.mark.parametrize("clean", [False, True])
def test_startup_warns_when_a_clean_deployment_reuses_an_adult_database(auth_client, monkeypatch, caplog, clean):
    from app import main
    from app.services import content_mode

    if clean:
        _clean(monkeypatch)
    caplog.set_level("WARNING", logger="avs.app")
    main._warn_adult_leftovers()
    assert "empty database" not in caplog.text  # nothing saved yet: no warning
    monkeypatch.delenv("ADULT_FEATURES", raising=False)
    _reset()
    content_mode.set_adult_mode(True)
    if clean:
        _clean(monkeypatch)
    main._warn_adult_leftovers()
    assert ("empty database" in caplog.text) is clean
    assert ("Adult content switch in Settings is on" in caplog.text) is clean
    # A broken database is a warning too, never a crash.
    monkeypatch.setattr(main, "SessionLocal", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    main._warn_adult_leftovers()


def _leftover_warning(monkeypatch, caplog) -> str:
    from app import main

    _clean(monkeypatch)
    caplog.clear()
    caplog.set_level("WARNING", logger="avs.app")
    main._warn_adult_leftovers()
    return "\n".join(r.getMessage() for r in caplog.records if "empty database" in r.getMessage())


def test_startup_warning_skips_a_saved_off_switch_and_ticks_that_are_off(auth_client, monkeypatch, caplog):
    from app.services import content_mode

    # A fresh clean database where someone pressed "off": nothing to warn about.
    _clean(monkeypatch)
    assert auth_client.put("/api/settings/content", json={"adult": False}).status_code == 200
    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        assert db.get(models.AppSetting, content_mode.KEY) is not None
    aid, _ = _maya()
    monkeypatch.delenv("ADULT_FEATURES", raising=False)
    _reset()
    content_mode.set_avatar_adult_ok(aid, False)
    assert _leftover_warning(monkeypatch, caplog) == ""


def test_startup_warning_names_adult_leftovers_without_a_saved_switch(auth_client, monkeypatch, caplog):
    from app.services import content_mode, model_registry, provider_keys

    # Used with ADULT_MODE from the environment only: no content.adult_mode row.
    aid, _ = _maya()
    _tick_now(aid)
    model_registry.set_active("image", ADULT_ONLY_SOGNI[0])
    model_registry.set_active("video", ADULT_ONLY_VIDEO[0])
    provider_keys.set_option("runware", "check_content", False)
    provider_keys.set_option("sogni", "safe_filter", False)
    assert content_mode.explicit() is None
    msg = _leftover_warning(monkeypatch, caplog)
    assert f"character Maya (id {aid}) is ticked" in msg
    assert f"saved image model {ADULT_ONLY_SOGNI[0]} is adult-only" in msg
    assert f"saved video model {ADULT_ONLY_VIDEO[0]} is adult-only" in msg
    assert "Runware content check is saved as off" in msg
    assert "Sogni safe filter is saved as off" in msg
    assert "Adult content switch" not in msg


def test_startup_warning_names_adult_mode_set_in_the_environment(auth_client, monkeypatch, caplog):
    monkeypatch.setenv("ADULT_MODE", "true")
    assert "ADULT_MODE is set in the environment" in _leftover_warning(monkeypatch, caplog)


def test_startup_warning_skips_ticks_that_no_longer_hold_and_ordinary_picks(auth_client, monkeypatch, caplog):
    from app import models
    from app.db import SessionLocal
    from app.services import model_registry

    aid, _ = _maya()
    _tick_now(aid)
    with SessionLocal() as db:  # the card changed since the tick: it no longer holds
        db.get(models.Avatar, aid).name = "Maya B"
        db.commit()
    model_registry.set_active("image", model_registry.default_model("image"))
    assert _leftover_warning(monkeypatch, caplog) == ""


def test_startup_warning_never_crashes_when_a_part_fails(auth_client, monkeypatch, caplog):
    from app import main
    from app.services import content_mode, model_registry

    model_registry.set_active("image", ADULT_ONLY_SOGNI[0])
    aid, _ = _maya()
    _tick_now(aid)
    monkeypatch.setattr(content_mode, "_holding", lambda rows: (_ for _ in ()).throw(RuntimeError("boom")))
    _clean(monkeypatch)
    caplog.set_level("WARNING", logger="avs.app")
    main._warn_adult_leftovers()  # no exception
    assert "leftover check skipped: character ticks" in caplog.text
    # The other parts still report what they found.
    assert f"saved image model {ADULT_ONLY_SOGNI[0]} is adult-only" in caplog.text


# ---------- clean prompts: enhance, hook ideas, clip check ----------


_ADULT_RULE = "Everyone in anything sexual or suggestive is an adult"


def _sent_systems(monkeypatch) -> dict:
    """The system prompts the real enhance and hook ideas calls send."""
    from app.providers.openrouter_chat import OpenRouterChatProvider

    from .test_models_catalog import OR

    _real(monkeypatch)
    out = {}
    with respx.mock:
        route = respx.post(f"{OR}/chat/completions").mock(return_value=httpx.Response(200, json={
            "choices": [{"message": {"content": "A cat on a sofa."}}]}))
        chat = OpenRouterChatProvider(model="vendor/plain-chat", json_mode=False)
        for kind in ("image", "video", "script", "scene"):
            chat.enhance_prompt("a cat on a sofa", kind, "")
            out[kind] = json.loads(route.calls[-1].request.content)["messages"][0]["content"]
        chat.hook_ideas(script="Cats sleep a lot. Here is why.", hook="Cats sleep a lot.", n=1,
                        styles=["question"])
        out["hooks"] = json.loads(route.calls[-1].request.content)["messages"][0]["content"]
    return out


def test_enhance_and_hook_ideas_get_a_general_audience_rule_on_a_clean_deployment(auth_client, monkeypatch):
    """Their output reaches pictures, the voice and the video, so a clean
    deployment tells the model to keep it non-sexual and to decline sexual or
    explicit text (DECLINED: the user sees a plain message) instead of
    writing it; the minors instructions stay."""
    from app.providers import openrouter_chat as oc

    monkeypatch.delenv("ADULT_FEATURES", raising=False)
    _reset()
    default = _sent_systems(monkeypatch)
    for kind, guide in oc.ENHANCE_GUIDES.items():  # default: unchanged
        assert default[kind] == f"{guide}\n{oc._ENHANCE_COMMON}"
    assert default["hooks"] == oc.HOOK_IDEAS_SYSTEM
    assert all(_ADULT_RULE in t and "never a partial, safer" in t for t in default.values())

    _clean(monkeypatch)
    clean = _sent_systems(monkeypatch)
    for kind, text in clean.items():
        assert _ADULT_RULE not in text and "safer" not in text, kind
        assert "general audience" in text and "non-explicit" in text, kind
        assert "REFUSED_MINOR" in text and "DECLINED" in text and "under 18" in text, kind
    for kind in oc.ENHANCE_GUIDES:
        assert "If the text asks for sexual, nude or explicit content, don't rewrite it: reply with " \
               "exactly DECLINED." in clean[kind]
    assert "If the script is sexual or explicit, don't write openings for it: reply with exactly " \
           "DECLINED." in clean["hooks"]
    # Only the content rules change.
    assert clean["hooks"].split("Rules:")[0] == oc.HOOK_IDEAS_SYSTEM.split("Rules:")[0]


def test_a_clean_enhance_decline_is_a_plain_message(auth_client, monkeypatch):
    from .test_models_catalog import OR
    from .test_prompting import _live, _reply

    _live(monkeypatch)
    _clean(monkeypatch)
    with respx.mock:
        route = respx.post(f"{OR}/chat/completions").mock(return_value=_reply("DECLINED"))
        r = auth_client.post("/api/prompts/enhance", json={"text": "a couple on a beach", "kind": "image"})
    assert r.status_code == 422 and len(route.calls) == 1
    detail = r.json()["detail"]
    assert "declined" in detail and "unchanged" in detail and "adult" not in detail.lower()
    system = json.loads(route.calls[0].request.content)["messages"][0]["content"]
    assert "general audience" in system and _ADULT_RULE not in system


def test_clip_check_reports_nudity_on_a_clean_deployment(monkeypatch):
    """Default: the clip check is told adult content is allowed and drops
    nudity "issues". Clean: it is told the opposite and keeps them."""
    from app.services import inspector

    nude = json.dumps({"verdict": "problem", "summary": "The clip shows nudity.",
                       "issues": [{"kind": "other", "detail": "the woman is shown nude"}]})
    monkeypatch.delenv("ADULT_FEATURES", raising=False)
    _reset()
    assert inspector.system_prompt() == inspector.SYSTEM and "never an issue" in inspector.SYSTEM
    assert inspector.parse_reply(nude)["verdict"] == "unsure"  # default: unchanged

    _clean(monkeypatch)
    system = inspector.system_prompt()
    assert system == inspector.SYSTEM_CLEAN != inspector.SYSTEM
    assert "allowed" not in system and "never an issue" not in system
    assert "general audience" in system and "nudity, sexual or explicit content is always an issue" in system
    got = inspector.parse_reply(nude)
    assert got["verdict"] == "problem" and got["summary"] == "The clip shows nudity."
    assert got["issues"] == [{"kind": "other", "detail": "the woman is shown nude"}]
    mixed = inspector.parse_reply(json.dumps({
        "verdict": "problem", "summary": "Explicit content; hands broken.",
        "issues": [{"kind": "other", "detail": "sexually explicit content"},
                   {"kind": "anatomy", "detail": "six fingers on the hand"}]}))
    assert [i["detail"] for i in mixed["issues"]] == ["sexually explicit content", "six fingers on the hand"]
    assert mixed["summary"] == "Explicit content; hands broken."


def test_the_clip_check_sends_the_clean_prompt_on_a_clean_deployment(auth_client, monkeypatch):
    from app.providers import mock_chat
    from app.services import inspector

    from .test_shot_inspector import _body, _rendered

    sent: list[str] = []

    def capture(self, *, system, text, images, note="clip check", estimate=0.0):
        sent.append(system)
        return json.dumps({"verdict": "problem", "summary": "The clip shows nudity.",
                           "issues": [{"kind": "other", "detail": "a person is shown naked"}]})

    _clean(monkeypatch)
    monkeypatch.setattr(mock_chat.MockChatProvider, "inspect_images", capture)
    pid = _rendered(auth_client)
    shots = _body(auth_client, pid)
    assert sent and set(sent) == {inspector.SYSTEM_CLEAN}
    ins = shots[0]["inspection"]
    assert ins["verdict"] == "problem" and ins["issues"] == [{"kind": "other", "detail": "a person is shown naked"}]


# ---------- clean: the spoken words get an explicit-content check ----------


_EXPLICIT = "Tonight they have sex on the bed and she takes off her lingerie."
_PLAIN = "Tonight we cook a quick pasta dinner together."


def _calls():
    from app.providers import mock_chat

    return mock_chat.EXPLICIT_CALLS


def test_default_never_runs_the_explicit_check(auth_client, monkeypatch):
    """ADULT_FEATURES unset: an adult script is saved, enhanced and voiced
    as today, and the content check is never asked."""
    from app.providers import get_tts, mock_chat

    from .test_plan_edit import _planned

    monkeypatch.delenv("ADULT_FEATURES", raising=False)
    _reset()
    mock_chat.EXPLICIT_REPLY = RuntimeError("must not be asked")
    pid = _planned(auth_client)
    r = auth_client.patch(f"/api/projects/{pid}/plan",
                          json={"cleaned_voice_script": _EXPLICIT, "end_card_text": "Sexy night"})
    assert r.status_code == 200, r.text
    assert auth_client.post("/api/prompts/enhance", json={"text": _EXPLICIT, "kind": "script"}).status_code == 200
    get_tts().synthesize(text=_EXPLICIT, voice_id="v")
    assert _calls() == []


def test_clean_plan_edit_refuses_an_explicit_script_end_card_and_caption(auth_client, monkeypatch):
    from app.services import explicit_check

    from .test_plan_edit import _planned

    _clean(monkeypatch)
    pid = _planned(auth_client)
    before = auth_client.get(f"/api/projects/{pid}").json()["generated_plan_json"]
    for body in ({"cleaned_voice_script": _EXPLICIT}, {"hook": "They have sex tonight."},
                 {"end_card_text": "Nude pics inside"}):
        r = auth_client.patch(f"/api/projects/{pid}/plan", json=body)
        assert r.status_code == 422, body
        assert r.json()["detail"] == explicit_check.REFUSED
        assert "adult" not in r.json()["detail"].lower()
    assert auth_client.get(f"/api/projects/{pid}").json()["generated_plan_json"] == before
    # A plain script is still saved.
    r = auth_client.patch(f"/api/projects/{pid}/plan", json={"cleaned_voice_script": _PLAIN})
    assert r.status_code == 200 and r.json()["generated_plan_json"]["cleaned_voice_script"] == _PLAIN
    # A shot's caption.
    shot = auth_client.get(f"/api/projects/{pid}").json()["shots"][0]
    r = auth_client.patch(f"/api/projects/{pid}/shots/{shot['id']}", json={"caption_text": "naked and horny"})
    assert r.status_code == 422 and r.json()["detail"] == explicit_check.REFUSED
    r = auth_client.patch(f"/api/projects/{pid}/shots/{shot['id']}", json={"caption_text": "Dinner time"})
    assert r.status_code == 200


def test_clean_check_fails_closed_and_remembers_only_oks(auth_client, monkeypatch):
    from app.providers import mock_chat
    from app.services import explicit_check, minor_check

    from .test_plan_edit import _planned

    _clean(monkeypatch)
    pid = _planned(auth_client)
    mock_chat.EXPLICIT_REPLY = RuntimeError("planner down")
    r = auth_client.patch(f"/api/projects/{pid}/plan", json={"cleaned_voice_script": _PLAIN})
    assert r.status_code == 422 and r.json()["detail"] == explicit_check.UNAVAILABLE
    mock_chat.EXPLICIT_REPLY = {"maybe": True}  # unreadable
    r = auth_client.patch(f"/api/projects/{pid}/plan", json={"cleaned_voice_script": _PLAIN})
    assert r.status_code == 422 and r.json()["detail"] == explicit_check.UNAVAILABLE
    from app.providers.openrouter_chat import ModelDeclined

    mock_chat.EXPLICIT_REPLY = ModelDeclined("no")
    r = auth_client.patch(f"/api/projects/{pid}/plan", json={"cleaned_voice_script": _PLAIN})
    assert r.status_code == 422 and r.json()["detail"] == explicit_check.DECLINED
    # A planner with no key outside mock mode is no check: refused.
    mock_chat.EXPLICIT_REPLY = None
    monkeypatch.setenv("MOCK_PROVIDERS", "false")
    _reset()
    with pytest.raises(explicit_check.ExplicitContentError) as e:
        explicit_check.check(_PLAIN)
    assert str(e.value) == explicit_check.UNAVAILABLE
    monkeypatch.setenv("MOCK_PROVIDERS", "true")
    _reset()
    # An "ok" is remembered (the same cache as the minors check), a refusal isn't.
    _calls().clear()
    minor_check.reset_cache()
    explicit_check.check(_PLAIN)
    explicit_check.check(_PLAIN)
    assert _calls() == [_PLAIN]
    for _ in range(2):
        with pytest.raises(explicit_check.ExplicitContentError):
            explicit_check.check(_EXPLICIT)
    assert _calls().count(_EXPLICIT) == 2


def test_clean_check_reads_long_scripts_in_overlapping_parts(monkeypatch):
    from app.services import explicit_check

    _clean(monkeypatch)
    words = " ".join(f"word{i}" for i in range(3000))
    parts = explicit_check.parts(words + " she is naked")
    assert len(parts) > 1 and all(len(p) <= explicit_check.PART for p in parts)
    assert " ".join(parts).count("word1500") >= 1 and parts[-1].endswith("she is naked")
    for a, b in zip(parts, parts[1:], strict=False):  # each part starts inside the one before
        assert b.split()[0] in a.split()
        # ...about OVERLAP characters back, not just the last word.
        shared = a[a.index(" " + b.split()[0] + " ") + 1:]
        assert len(shared) >= explicit_check.OVERLAP - 20, len(shared)
    with pytest.raises(explicit_check.ExplicitContentError):
        explicit_check.check(words + " she is naked")


def test_clean_enhance_refuses_an_explicit_script_rewrite(auth_client, monkeypatch):
    _clean(monkeypatch)
    r = auth_client.post("/api/prompts/enhance", json={"text": _EXPLICIT, "kind": "script"})
    assert r.status_code == 422
    assert "general audience" in r.json()["detail"] and "unchanged" in r.json()["detail"]
    r = auth_client.post("/api/prompts/enhance", json={"text": _PLAIN, "kind": "script"})
    assert r.status_code == 200
    # A picture or clip rewrite is checked too: it is sent to a picture or
    # video model (tests/test_clean_mode_gaps.py has the rest).
    _calls().clear()
    assert auth_client.post("/api/prompts/enhance", json={"text": "a cat", "kind": "image"}).status_code == 200
    assert _calls()


def test_clean_plan_refuses_an_explicit_original_script_and_planner_output(auth_client, monkeypatch):
    from app.schemas.storyboard import StoryboardPlan
    from app.services import explicit_check, pipeline

    _clean(monkeypatch)
    pid = auth_client.post("/api/projects", json={"title": "T", "original_script": _EXPLICIT,
                                                  "cta_text": "Go"}).json()["id"]
    r = auth_client.post(f"/api/projects/{pid}/generate-plan")
    assert r.status_code == 422 and r.json()["detail"] == explicit_check.REFUSED
    # What the planner writes is checked too: script, end card, captions.
    base = {"title": "T", "cleaned_voice_script": _PLAIN, "estimated_duration_seconds": 5}
    pipeline._check_plan(StoryboardPlan(**base))
    for extra in ({"cleaned_voice_script": _EXPLICIT}, {"end_card_text": "Nude pics inside"},
                  {"caption_chunks": [{"text": "they have sex"}]},
                  {"shots": [{"order": 1, "visual_prompt": "a kitchen", "caption_text": "porn"}]}):
        with pytest.raises(explicit_check.ExplicitContentError):
            pipeline._check_plan(StoryboardPlan(**{**base, **extra}))


def test_clean_keep_my_edits_replan_checks_the_kept_words(auth_client, monkeypatch):
    """A script saved before the deployment was made clean is kept by "Keep
    my edits": the replan fails instead of voicing it."""
    from app import models
    from app.db import SessionLocal

    from .test_plan_edit import _planned

    pid = _planned(auth_client)  # default mode
    assert auth_client.patch(f"/api/projects/{pid}/plan",
                             json={"cleaned_voice_script": _EXPLICIT}).status_code == 200
    _clean(monkeypatch)
    auth_client.post(f"/api/projects/{pid}/generate-plan?preserve_edits=true")
    db = SessionLocal()
    try:
        p = db.get(models.VideoProject, pid)
        assert p.status == "failed"
        assert p.generated_plan_json["cleaned_voice_script"] == _EXPLICIT  # not replanned over
    finally:
        db.close()
    # A kept plain script replans as before.
    db = SessionLocal()
    try:
        p = db.get(models.VideoProject, pid)
        p.generated_plan_json = {**p.generated_plan_json, "cleaned_voice_script": _PLAIN, "hook": ""}
        p.status = "planned"
        db.commit()
    finally:
        db.close()
    auth_client.post(f"/api/projects/{pid}/generate-plan?preserve_edits=true")
    db = SessionLocal()
    try:
        p = db.get(models.VideoProject, pid)
        assert p.status == "planned" and p.generated_plan_json["cleaned_voice_script"] == _PLAIN
    finally:
        db.close()


def test_clean_voice_and_lipsync_refuse_explicit_words(auth_client, monkeypatch):
    from app.providers import get_tts
    from app.providers.mock_avatar import MockAvatarProvider
    from app.providers.registry import GuardedAvatarProvider
    from app.services import explicit_check

    _clean(monkeypatch)
    with pytest.raises(explicit_check.ExplicitContentError):
        get_tts().synthesize(text=_EXPLICIT, voice_id="v")
    get_tts().synthesize(text=_PLAIN, voice_id="v")
    with pytest.raises(explicit_check.ExplicitContentError):
        GuardedAvatarProvider(MockAvatarProvider(model="mock")).submit_avatar(
            script=_EXPLICIT, motion_prompt="talks to camera", image_path="x", audio_path="y")


def test_clean_hook_lab_refuses_an_explicit_opening(auth_client, monkeypatch):
    from .test_hook_lab import _base, _create

    _clean(monkeypatch)
    pid, base_id = _base(auth_client)
    r = auth_client.post(f"/api/projects/{pid}/hook-lab/variants", json={
        "base_render_id": base_id, "window_seconds": 2.5,
        "variants": [{"hook_text": "Watch her strip naked now.", "style": "custom"}]})
    assert r.status_code == 422 and "general audience" in r.json()["detail"]
    made = _create(auth_client, pid, base_id, [{"hook_text": "Why keep scrolling?", "style": "question"}])
    vid = made["variants"][0]["id"]
    r = auth_client.patch(f"/api/hook-variants/{vid}", json={"hook_text": "They have sex tonight."})
    assert r.status_code == 422 and "general audience" in r.json()["detail"]


def test_clean_explicit_check_sends_its_own_question(monkeypatch):
    from app.providers.openrouter_chat import EXPLICIT_CHECK_SYSTEM, OpenRouterChatProvider

    from .test_models_catalog import OR

    _real(monkeypatch)
    with respx.mock:
        route = respx.post(f"{OR}/chat/completions").mock(return_value=httpx.Response(200, json={
            "choices": [{"message": {"content": '{"explicit": true, "reason": "sexual acts"}'}}]}))
        out = OpenRouterChatProvider(model="vendor/plain-chat", json_mode=False).classify_explicit(_EXPLICIT)
    assert out == {"explicit": True, "reason": "sexual acts"}
    sent = json.loads(route.calls[0].request.content)["messages"]
    assert sent[0]["content"] == EXPLICIT_CHECK_SYSTEM and sent[1]["content"] == _EXPLICIT
    assert "sexually explicit or pornographic" in EXPLICIT_CHECK_SYSTEM


def test_clean_project_edit_refuses_explicit_end_card_disclosure_and_posted_plan(auth_client, monkeypatch):
    """PATCH /projects/{id} writes the end card, the disclosure and a whole
    storyboard, all shown in the video: a changed one is checked too."""
    from app.services import explicit_check

    from .test_plan_edit import _planned

    _clean(monkeypatch)
    pid = _planned(auth_client)
    before = auth_client.get(f"/api/projects/{pid}").json()
    plan = before["generated_plan_json"]
    for body in ({"cta_text": "Nude pics inside"}, {"disclosure_text": "Porn inside"},
                 {"generated_plan_json": {**plan, "caption_chunks": [{"text": _EXPLICIT}]}},
                 {"generated_plan_json": {**plan, "end_card_text": "Nude pics inside"}},
                 {"generated_plan_json": {**plan, "disclosure_text": "Porn inside"}}):
        r = auth_client.patch(f"/api/projects/{pid}", json=body)
        assert r.status_code == 422, body
        assert r.json()["detail"] == explicit_check.REFUSED
    after = auth_client.get(f"/api/projects/{pid}").json()
    assert after["cta_text"] == before["cta_text"] and after["generated_plan_json"] == plan
    # The disclosure on the Voiceover & captions card.
    r = auth_client.patch(f"/api/projects/{pid}/plan", json={"disclosure_text": "Porn inside"})
    assert r.status_code == 422 and r.json()["detail"] == explicit_check.REFUSED
    # Plain words still save; unchanged ones aren't asked about again.
    r = auth_client.patch(f"/api/projects/{pid}", json={"cta_text": "Follow for more", "title": "New"})
    assert r.status_code == 200 and r.json()["cta_text"] == "Follow for more"
    _calls().clear()
    assert auth_client.patch(f"/api/projects/{pid}", json={"title": "Newer", "cta_text": "Follow for more",
                                                           "generated_plan_json": plan}).status_code == 200
    assert _calls() == []


def test_clean_render_checks_the_shown_words_before_drawing(auth_client, monkeypatch):
    """A last check when the video is put together: an end card, disclosure,
    opening or captions saved before the deployment was made clean (or by
    any other way) fail the render instead of being drawn."""
    from types import SimpleNamespace

    from app.services import explicit_check, pipeline
    from app.services.captions import CaptionTiming

    look = {"cta_text": "Go", "disclosure_text": "Made with AI"}
    plain = [CaptionTiming(0.0, 1.0, _PLAIN)]
    project = SimpleNamespace(id=1)

    def compose(look, timed, hook_text=None):
        pipeline._compose(None, project, None, [], None, timed, plan=None, settings=look, hook_text=hook_text)

    _clean(monkeypatch)
    for bad_look, timed, hook in (({**look, "cta_text": "Nude pics inside"}, plain, None),
                                  ({**look, "disclosure_text": "Porn inside"}, plain, None),
                                  (look, [CaptionTiming(0.0, 1.0, _EXPLICIT)], None),
                                  (look, plain, "Watch her strip naked now.")):
        with pytest.raises(explicit_check.ExplicitContentError):
            compose(bad_look, timed, hook)
    # Plain words pass the check (and go on to the composing, which these
    # stand-ins can't do).
    with pytest.raises(Exception) as e:
        compose(look, plain)
    assert not isinstance(e.value, explicit_check.ExplicitContentError)
    # Default mode: never asked.
    monkeypatch.delenv("ADULT_FEATURES", raising=False)
    _reset()
    _calls().clear()
    with pytest.raises(Exception) as e:
        compose({**look, "cta_text": "Nude pics inside"}, plain)
    assert not isinstance(e.value, explicit_check.ExplicitContentError)
    assert _calls() == []


def test_clean_full_render_draws_plain_words_and_fails_on_an_old_explicit_end_card(auth_client, monkeypatch):
    from .test_plan_edit import _planned

    _clean(monkeypatch)
    pid = _planned(auth_client)
    assert auth_client.post(f"/api/projects/{pid}/generate-video").status_code == 202
    status = auth_client.get(f"/api/projects/{pid}/status").json()
    assert status["latest_render"]["status"] == "completed"
    # An end card saved while the deployment wasn't clean.
    monkeypatch.delenv("ADULT_FEATURES", raising=False)
    _reset()
    assert auth_client.patch(f"/api/projects/{pid}", json={"cta_text": "Nude pics inside"}).status_code == 200
    _clean(monkeypatch)
    auth_client.post(f"/api/projects/{pid}/generate-video")
    render = auth_client.get(f"/api/projects/{pid}/status").json()["latest_render"]
    assert render["status"] == "failed" and "general audience" in (render.get("error") or "")


# ---------- the Adult content setting's readers in the UI ----------

_FRONTEND = __import__("pathlib").Path(__file__).resolve().parents[2] / "frontend"


def _ts_function_body(src: str, start: str) -> str:
    """The text of one function or component, from `start` to the next
    top-level `function`/`export function`."""
    i = src.index(start)
    rest = src[i + len(start):]
    ends = [rest.find(m) for m in ("\nfunction ", "\nexport function ") if rest.find(m) != -1]
    return rest[: min(ends)] if ends else rest


def test_settings_adult_card_reuses_the_page_read():
    """The Adult content card on Settings appears once the page has read the
    setting; it starts from that answer instead of sending a second read and
    showing only its heading until that one comes back."""
    page = (_FRONTEND / "src/app/settings/page.tsx").read_text()
    assert "<AdultModePanel initial={content.mode} />" in page
    panel = _ts_function_body(
        (_FRONTEND / "src/components/AdultModePanel.tsx").read_text(), "export function AdultModePanel("
    )
    assert "useState<ContentMode | null>(initial)" in panel
    # It skips its own first read when given one, and still follows changes.
    assert "if (!hadInitial.current) read(false);" in panel
    assert 'addEventListener("content-mode-changed"' in panel


def test_cast_adult_tick_card_retry_asks_every_reader_again():
    """After a failed read the Cast card's Retry re-reads for every reader on
    the page (the card text editor's tick hint included), not just itself."""
    card = _ts_function_body((_FRONTEND / "src/app/cast/page.tsx").read_text(), "function AdultOkCard(")
    i = card.index("<ContentLoadError")
    retry = card[i: card.index("/>", i)]
    assert 'onRetry={() => window.dispatchEvent(new Event("content-mode-changed"))}' in retry
    assert "onRetry={reload}" not in card


def test_content_mode_reads_drop_an_older_success_after_a_newer_failure(tmp_path):
    """A slow older read that succeeds after a newer read failed is stale:
    the newer failure stands. Runs the real ordering code under Node."""
    import shutil
    import subprocess

    node = shutil.which("node")
    ts = _FRONTEND / "node_modules" / "typescript"
    if not node or not ts.exists():
        pytest.skip("needs Node and the frontend's TypeScript")
    script = tmp_path / "check.cjs"
    script.write_text(
        """
const fs = require("fs"), path = require("path");
const ts = require(process.argv[2]);
const src = fs.readFileSync(process.argv[3], "utf8");
const out = ts.transpileModule(src, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } });
const m = { exports: {} };
new Function("module", "exports", out.outputText)(m, m.exports);
const { createLoadOrder } = m.exports;
const res = {};
let o = createLoadOrder();
let a = o.next(), b = o.next();
res.newerFails = o.acceptFailure(b);
res.olderSuccessAfter = o.acceptSuccess(a);
o = createLoadOrder();
a = o.next(); b = o.next();
res.newerSucceeds = o.acceptSuccess(b);
res.olderSuccessAfterSuccess = o.acceptSuccess(a);
res.olderFailureAfterSuccess = o.acceptFailure(a);
res.nextAfterFailureCounts = (() => { const p = createLoadOrder(); const x = p.next(); p.acceptFailure(x); return p.acceptSuccess(p.next()); })();
res.inOrder = (() => { const p = createLoadOrder(); const x = p.next(), y = p.next(); return p.acceptSuccess(x) && p.acceptSuccess(y); })();
console.log(JSON.stringify(res));
"""
    )
    out = subprocess.run(
        [node, str(script), str(ts), str(_FRONTEND / "src/lib/loadOrder.ts")],
        capture_output=True, text=True, timeout=60, check=True,
    )
    assert json.loads(out.stdout) == {
        "newerFails": True,
        "olderSuccessAfter": False,
        "newerSucceeds": True,
        "olderSuccessAfterSuccess": False,
        "olderFailureAfterSuccess": False,
        "nextAfterFailureCounts": True,
        "inOrder": True,
    }
    # The panels' loader uses it.
    loader = _ts_function_body(
        (_FRONTEND / "src/components/AdultModePanel.tsx").read_text(), "function useContentModeLoader("
    )
    assert "acceptSuccess(n)" in loader and "acceptFailure(n)" in loader


def test_clean_recompose_checks_the_words_the_reused_voiceover_says(auth_client, monkeypatch):
    """A Re-compose reuses an older voiceover (no new voice is made, so the
    voice check never runs): the last check before drawing also reads what
    that audio says, not only the captions."""
    from app import models
    from app.db import SessionLocal
    from app.services import pipeline

    from .test_render_fixes import _rendered

    pid = _rendered(auth_client)
    _clean(monkeypatch)
    monkeypatch.setattr(pipeline, "_spoken_texts", lambda *a, **k: {"script": _EXPLICIT, "hook_text": ""})
    db = SessionLocal()
    try:
        with pytest.raises(Exception, match="general audience|explicit"):
            pipeline.recompose_project(db, pid)
    finally:
        db.close()
    db = SessionLocal()
    try:
        last = (db.query(models.Render).filter(models.Render.project_id == pid)
                .order_by(models.Render.id.desc()).first())
        assert last.status != "completed" or not last.final_video_path
    finally:
        db.close()

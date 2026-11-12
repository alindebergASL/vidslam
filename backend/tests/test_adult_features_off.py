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

ADULT_ONLY_SOGNI = ("sogni/seedream-5-0-pro-uncensored", "sogni/z-turbo", "sogni/moss-image")
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
    assert {"seedream-5-0-pro-uncensored", "z-turbo"} <= images
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
    # A broken database is a warning too, never a crash.
    monkeypatch.setattr(main, "SessionLocal", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    main._warn_adult_leftovers()

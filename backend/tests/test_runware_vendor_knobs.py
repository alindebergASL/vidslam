"""Runware's vendor safety settings in Adult mode (runware._PERMISSIVE):
loosened only where a model's published schema offers them. None of the
curated generation models documents any (Runware's docs, checked
2026-11-12), so they never get them; models that do (BFL's own, OpenAI's)
get the loosest value their schema allows, and only in Adult mode."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from .test_runware import _CORE, _IMG, _fake_model, _ref, live  # noqa: F401 — the fixture

BUNDLE = Path(__file__).resolve().parents[1] / "app" / "providers" / "runware_models.json"


def _bundle_generation_models() -> list[str]:
    """The curated image and video models (not remove background or
    upscale), read from the bundle without importing the app."""
    b = json.loads(BUNDLE.read_text())
    gen = {"text-to-image", "edit", "text-to-video", "image-to-video"}
    return [a for a in b["curated"] if set(b["schemas"][a].get("capabilities") or []) & gen]


def _has(task: dict, path: tuple[str, ...]) -> bool:
    node = task
    for name in path:
        if not isinstance(node, dict) or name not in node:
            return False
        node = node[name]
    return True


def test_no_curated_generation_model_documents_a_vendor_safety_setting():
    """If a new snapshot adds one, check it against Runware's docs and the
    wording in Settings (AdultModePanel), then update this test."""
    from app.providers import runware

    models = _bundle_generation_models()
    assert len(models) == 19
    for a in models:
        props = runware._props(runware.bundle()["schemas"][a])
        offered = [p for p, _ in runware._PERMISSIVE if runware._sub(props, *p) is not None]
        assert offered == [], f"{a} now documents {offered}"


@pytest.mark.parametrize("model_air", _bundle_generation_models())
def test_curated_models_get_none_in_adult_mode_and_still_build(client, live, model_air):  # noqa: F811
    from app.providers import runware
    from app.services import content_mode

    content_mode.set_adult_mode(True)
    model = runware.PREFIX + model_air
    pic = _ref(frame_type="first_frame")
    if runware.kind_of(runware.schema(model).get("capabilities")) == "image":
        caps = runware.schema(model).get("capabilities") or []
        task = runware.build_image_task(model, "a quiet street at dusk", [] if "text-to-image" in caps else [pic])
    else:
        task, _ = runware.build_video_task(model, "rain falls on a quiet street", [pic],
                                           reference_strategy="frame_images", duration_seconds=5)
    assert not [p for p, _ in runware._PERMISSIVE if _has(task, p)]
    assert task["safety"]["checkContent"] is False  # Adult mode, box unticked: the request still goes out


def _openai_like(monkeypatch):
    """settings.moderation as on https://runware.ai/docs/models/openai-gpt-image-2-6-sunburst."""
    return _fake_model(monkeypatch, "test:moderation@1", {
        "type": "object", "additionalProperties": False, "required": ["taskType", "taskUUID", "model"],
        "properties": {**_CORE,
                       "settings": {"type": "object", "additionalProperties": False, "properties": {
                           "moderation": {"type": "string", "default": "auto",
                                          "oneOf": [{"const": "auto"}, {"const": "low"}]}}},
                       "inputs": {"type": "object", "properties": {"referenceImages": {"type": "array",
                                                                                       "items": _IMG}}}}},
        modes=[{"id": "text"}], caps=["text-to-image"])


def _nano_banana_like(monkeypatch):
    """settings.safetyTolerance as words, as on https://runware.ai/docs/models/google-nano-banana-2-1."""
    return _fake_model(monkeypatch, "test:words@1", {
        "type": "object", "additionalProperties": False, "required": ["taskType", "taskUUID", "model"],
        "properties": {**_CORE,
                       "settings": {"type": "object", "additionalProperties": False, "properties": {
                           "safetyTolerance": {"type": "string", "default": "none",
                                               "enum": ["high", "medium", "low", "none", "off"]}}}}},
        modes=[{"id": "text"}], caps=["text-to-image"])


def test_a_documented_moderation_setting_is_loosened_only_in_adult_mode(client, live, monkeypatch):  # noqa: F811
    from app.providers import runware
    from app.services import content_mode

    model = _openai_like(monkeypatch)
    content_mode.set_adult_mode(False)
    assert "settings" not in runware.build_image_task(model, "a quiet street at dusk", [])
    content_mode.set_adult_mode(True)
    assert runware.build_image_task(model, "a quiet street at dusk", [])["settings"] == {"moderation": "low"}


def test_a_setting_given_in_words_is_left_at_the_vendor_default(client, live, monkeypatch):  # noqa: F811
    from app.providers import runware
    from app.services import content_mode

    content_mode.set_adult_mode(True)
    task = runware.build_image_task(_nano_banana_like(monkeypatch), "a quiet street at dusk", [])
    assert "settings" not in task  # "max" doesn't apply to words: nothing is guessed

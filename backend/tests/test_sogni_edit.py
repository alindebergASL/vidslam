"""Reference pictures on Sogni picture models go to Sogni's edit_image tool
(its generate_image ignores them, tested live Nov 2026): which editor, the
request, spend, Studio's references, and ambient's "same place" stills."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from .test_sogni import ART_IMG, PNG, WF, _wf, live_sogni  # noqa: F401 — fixture


def _ref(role="scene", name="", url="https://x/a.png"):
    from app.providers.base import ImageRef

    return ImageRef(url=url, role=role, name=name)


@pytest.fixture
def edit_env(monkeypatch):
    """Set Sogni edit settings for one test."""
    from app import config

    def set_env(**kw):
        for k, v in kw.items():
            monkeypatch.setenv(k, v)
        config.get_settings.cache_clear()

    yield set_env
    config.get_settings.cache_clear()


# ---------- which editor ----------


def test_the_editor_follows_the_picked_model_and_what_the_pictures_are_for(edit_env):
    from app.providers import sogni

    def route(model, refs, intent=""):
        r = sogni.edit_route(model, refs, intent)
        return r and (r.model.selector, r.intent, r.send_aspect, [x.role for x in r.refs])

    # A character: Krea's identity editor (the picked Krea's own).
    assert route("sogni/krea-2-turbo", [_ref("character")]) == ("krea-identity-edit", "identity", True, ["character"])
    assert route("sogni/dark-beast-krea2", [_ref("character")])[0] == "dark-beast-krea2-identity-edit"
    # A place in other light keeps its shape; an identity editor isn't for places.
    assert route("sogni/krea-2-turbo", [_ref()], "same_place") == ("flux-3-klein", "same_place", False, ["scene"])
    # No edit version of its own: the defaults.
    assert route("sogni/moss-image", [_ref("style")])[:2] == ("flux-3-klein", "guide")
    assert route("sogni/moss-image", [_ref("character"), _ref("character")])[0] == "krea-identity-edit"
    # Its own edit version where it has one.
    assert route("sogni/qwen-2512-lightning", [_ref()])[0] == "qwen-lightning"
    assert route("sogni/gpt-image-2.6-sunburst", [_ref()])[0] == "gpt-image-2.6-sunburst"
    # More pictures than Krea takes (2): Klein (10). A place first, then the people, then details.
    many = [_ref("character", "A"), _ref("object", "Lamp"), _ref("scene", "Cafe"), _ref("character", "B")]
    assert route("sogni/krea-2-turbo", many) == ("flux-3-klein", "identity", True,
                                                  ["scene", "character", "character", "object"])
    assert len(sogni.edit_route("sogni/moss-image", [_ref()] * 12).refs) == 10
    # Over the limit, the people are kept first.
    crowd = [_ref("character", "A"), *[_ref("object", f"o{i}") for i in range(10)], _ref("character", "B")]
    kept = sogni.edit_route("sogni/moss-image", crowd).refs
    assert len(kept) == 10 and [r.name for r in kept if r.role == "character"] == ["A", "B"]
    # Settings: another default editor (with or without the sogni/ prefix), or no edits at all.
    edit_env(SOGNI_SCENE_EDIT_MODEL="qwen")
    assert route("sogni/moss-image", [_ref()])[0] == "qwen"
    edit_env(SOGNI_SCENE_EDIT_MODEL="sogni/qwen-lightning")
    assert route("sogni/moss-image", [_ref()])[0] == "qwen-lightning"
    edit_env(SOGNI_EDIT_REFERENCES="false")
    assert sogni.edit_route("sogni/moss-image", [_ref()]) is None and not sogni.can_use_references("sogni/moss-image")
    assert sogni.edit_route("sogni/moss-image", []) is None


def test_editors_come_from_sognis_list(monkeypatch):
    from app import config
    from app.providers import sogni

    assert "flux-3-klein" in sogni.listed()["tools"]["edit_image"]  # bundled
    doc = {"tools": [{"name": "edit_image", "parameters": {"properties": {"model": {"enum": ["qwen", "new-editor"]}}}},
                     {"name": "generate_image", "parameters": {"properties": {"model": {"enum": ["krea-2-turbo"]}}}}]}
    assert sogni.parse_manifest(doc)["edit_image"] == ["qwen", "new-editor"]
    # A list without Klein: the next default that is listed.
    monkeypatch.setenv("SOGNI_TOOLS_MANIFEST_URL", "")
    config.get_settings.cache_clear()
    monkeypatch.setattr(sogni, "_listed", {"tools": {**sogni.listed()["tools"], "edit_image": ["qwen", "new-editor"]}})
    monkeypatch.setattr(sogni, "_listed_key", None)
    assert sogni.edit_route("sogni/moss-image", [_ref()]).model.selector == "qwen"
    # A listing without edit_image is still valid (and uses our table).
    tools = {k: v for k, v in sogni.listed()["tools"].items() if k != "edit_image"}
    assert sogni._valid_listing({"tools": tools})
    config.get_settings.cache_clear()


# ---------- the request ----------


def _generate(model, refs, **kw):
    from app.providers.sogni import SogniImageProvider

    done = _wf("completed", steps=[{"id": "s1", "artifacts": [{"id": "a", "kind": "image", "url": ART_IMG}]}])
    provider = SogniImageProvider()
    with respx.mock() as router:
        start = router.post(WF).mock(return_value=httpx.Response(201, json=done))
        router.get(ART_IMG).mock(return_value=httpx.Response(200, content=PNG))
        assert provider.generate(model=model, references=refs, **kw) == PNG
    return json.loads(start.calls[0].request.content), provider


def test_a_character_picture_makes_an_identity_edit(client, live_sogni):  # noqa: F811
    from app import models
    from app.db import SessionLocal

    refs = [_ref("object", "Lamp", "https://x/lamp.png"), _ref("character", "Mia", "https://x/mia.png")]
    body, provider = _generate("sogni/krea-2-turbo", refs, prompt="Mia reading by the lamp.",
                               negative_prompt="text", settings={"aspect_ratio": "9:16"})
    step = body["input"]["steps"][0]
    args = step["arguments"]
    assert step["toolName"] == "edit_image" and args["model"] == "krea-identity-edit"
    assert args["sourceImageIndex"] == -1 and args["aspectRatio"] == "9:16" and "negativePrompt" not in args
    # The person before a detail picture; the prompt names each with Krea's literal tokens.
    assert body["media_references"] == [{"kind": "image", "url": "https://x/mia.png"},
                                        {"kind": "image", "url": "https://x/lamp.png"}]
    assert "context_image_0 shows Mia: keep their exact likeness" in args["prompt"]
    assert "context_image_1 is Lamp to include as it is." in args["prompt"]
    # No place picture as the base: a new picture, not an edit of the first one.
    assert args["prompt"].endswith("Mia reading by the lamp. The picture itself is new; use the pictures only "
                                   "as described.")
    assert provider.last_route.model.selector == "krea-identity-edit"
    db = SessionLocal()
    try:
        row = db.query(models.SpendEntry).one()
        # Free Spark editor: $0 (PR #19's rule), with the list price noted.
        assert (row.model, row.cost_usd) == ("sogni/krea-identity-edit (edit)", 0.0)
        assert "edit_image" in row.note and "not counted" in row.note and "sogni/krea-2-turbo" in row.note
    finally:
        db.close()


def test_a_same_place_edit_keeps_the_pictures_shape_and_prompt(client, live_sogni):  # noqa: F811
    body, _ = _generate("sogni/moss-image", [_ref()], prompt="Keep the place; make it dusk.",
                        settings={"aspect_ratio": "16:9", "reference_intent": "same_place"})
    args = body["input"]["steps"][0]["arguments"]
    assert args["model"] == "flux-3-klein" and "aspectRatio" not in args
    assert args["prompt"] == "Keep the place; make it dusk."


def test_a_premium_editor_counts_toward_spend(client, live_sogni):  # noqa: F811
    from app import models
    from app.db import SessionLocal
    from app.providers import sogni

    _generate("sogni/gpt-image-2.6-sunburst", [_ref(), _ref()], prompt="p")
    db = SessionLocal()
    try:
        row = db.query(models.SpendEntry).one()
        assert row.model == "sogni/gpt-image-2.6-sunburst (edit)"
        assert row.cost_usd == pytest.approx(sogni.edit_cost_usd("gpt-image-2.6-sunburst", 2)) and row.cost_usd > 0
    finally:
        db.close()


def test_a_failed_edit_is_an_error_not_a_picture_without_the_references(client, live_sogni):  # noqa: F811
    from app.providers.sogni import SogniError, SogniImageProvider

    with respx.mock() as router:
        router.post(WF).mock(return_value=httpx.Response(201, json=_wf("failed", error={"message": "nope"})))
        with pytest.raises(SogniError):
            SogniImageProvider().generate(model="sogni/moss-image", prompt="p", references=[_ref("character")])


# ---------- Studio ----------


def test_studio_sends_references_in_the_picked_order_with_roles_and_names(client):
    from app import models
    from app.db import SessionLocal
    from app.services import pipeline

    db = SessionLocal()
    try:
        av = models.Avatar(name="Mia")
        ing = models.Ingredient(name="Brass lamp", kind="prop")
        style = models.Ingredient(name="Film look", kind="style")
        db.add_all([av, ing, style])
        db.flush()
        assets = [models.Asset(owner_kind="avatar", avatar_id=av.id, file_path="/x/a.png"),
                  models.Asset(owner_kind="ingredient", ingredient_id=ing.id, file_path="/x/b.png"),
                  models.Asset(owner_kind="ingredient", ingredient_id=style.id, file_path="/x/c.png")]
        db.add_all(assets)
        db.commit()
        picked = [assets[2].id, assets[0].id, assets[1].id]  # not database order
        refs = pipeline._asset_refs(db, picked)
        assert [(r.role, r.name) for r in refs] == [("style", "Film look"), ("character", "Mia"),
                                                     ("object", "Brass lamp")]
    finally:
        db.close()


# ---------- ambient ----------


def test_same_place_prompt_changes_only_the_light():
    from app.schemas.ambient import AmbientConfig
    from app.services import ambient

    from .test_ambient import SHORT

    for tod in ("dusk", "night"):
        text = ambient.same_place_prompt(AmbientConfig(**{**SHORT, "time_of_day": tod}))
        assert text.startswith("Keep exactly the same place") and ambient.TIMES_OF_DAY[tod][1] in text
    # Indoors the fire or candles stay; it's night outside.
    fire = ambient.same_place_prompt(AmbientConfig(**{**SHORT, "scene_preset": "fireplace", "time_of_day": "night"}))
    assert "It is night outside." in fire


def test_day_to_night_on_a_sogni_picture_model_asks_for_the_same_place(auth_client, monkeypatch):
    from app.services import ambient

    from .test_ambient import SHORT

    calls: list[dict] = []
    real = ambient.generate_still

    def spy(db, cfg, **kw):
        calls.append(kw)
        return real(db, cfg, **kw)

    monkeypatch.setattr(ambient, "generate_still", spy)
    sid = auth_client.post("/api/ambient", json={"config": {
        **SHORT, "source": "image_drift", "music_mood": "none", "variety": "day_night", "variations": 2,
        "still_model": "sogni/krea-2-turbo"}, "generate": True}).json()["id"]
    assert auth_client.get(f"/api/ambient/{sid}").json()["loop_status"] == "completed"
    first, later = calls[0], calls[1]
    assert not first.get("references") and not first.get("edit_prompt")
    assert later["references"] and later["edit_prompt"].startswith("Keep exactly the same place")
    assert ambient._SAME_PLACE not in later["prompt"]  # the full prompt is the text-only last resort


# ---------- review follow-ups ----------


def test_qwen_names_pictures_and_keeps_a_place(client, live_sogni):  # noqa: F811
    refs = [_ref("scene", "Cafe", "https://x/cafe.png"), _ref("character", "Ann", "https://x/ann.png")]
    body, _ = _generate("sogni/qwen-2512", refs, prompt="Ann at the counter.")
    args = body["input"]["steps"][0]["arguments"]
    assert args["model"] == "qwen"
    assert args["prompt"] == ("Picture 1 is the place (Cafe). Picture 2 shows Ann: keep their exact likeness, face, "
                              "hair and build. Ann at the counter. Preserve all unmentioned details.")


def test_a_new_listed_editor_can_be_the_picked_models_own(monkeypatch):
    from app import config
    from app.providers import sogni

    monkeypatch.setenv("SOGNI_TOOLS_MANIFEST_URL", "")
    config.get_settings.cache_clear()
    tools = dict(sogni.listed()["tools"])
    tools["generate_image"] = [*tools["generate_image"], "gpt-image-2.7-flare"]
    tools["edit_image"] = [*tools["edit_image"], "gpt-image-2.7-flare"]
    monkeypatch.setattr(sogni, "_listed", {"tools": tools})
    monkeypatch.setattr(sogni, "_listed_key", None)
    route = sogni.edit_route("sogni/gpt-image-2.7-flare", [_ref()])
    assert route.model.selector == "gpt-image-2.7-flare" and route.model.premium and route.model.max_refs == 16
    assert sogni.edit_spend_usd("gpt-image-2.7-flare") > 0
    config.get_settings.cache_clear()


def test_same_place_attempts_dont_repeat_an_edit_and_respect_the_scenes_choice(monkeypatch):
    from app import config
    from app.schemas.ambient import AmbientConfig
    from app.services import ambient

    from .test_ambient import SHORT

    monkeypatch.setenv("AMBIENT_STILL_FALLBACK_MODEL", "sogni/z-turbo")
    config.get_settings.cache_clear()
    both = AmbientConfig(**{**SHORT, "still_model": "sogni/krea-2-turbo"})
    # Both Sogni models edit with FLUX.3 Klein: one edit, then each from text.
    assert ambient.reference_still_attempts(both) == [("sogni/krea-2-turbo", True), ("sogni/krea-2-turbo", False),
                                                      ("sogni/z-turbo", False)]
    # Sogni first only on a scene whose own Picture model is a Sogni one.
    monkeypatch.setenv("SOGNI_SAME_PLACE_FIRST", "true")
    config.get_settings.cache_clear()
    gpt = AmbientConfig(**{**SHORT, "still_model": "openai/gpt-image-2.6-sunburst"})
    assert ambient.reference_still_attempts(gpt)[0] == ("openai/gpt-image-2.6-sunburst", True)
    config.get_settings.cache_clear()


def test_a_stylised_look_isnt_told_it_is_a_photograph():
    from app.schemas.ambient import AmbientConfig
    from app.services import ambient

    from .test_ambient import SHORT

    text = ambient.same_place_prompt(AmbientConfig(**{**SHORT, "look": "watercolor", "time_of_day": "dusk"}))
    assert "real, unedited photograph" not in text and "Keep its look unchanged" in text


def test_shot_references_keep_asset_order_and_accept_string_ids(client):
    from app import models
    from app.db import SessionLocal
    from app.services import pipeline

    db = SessionLocal()
    try:
        av = models.Avatar(name="Mia")
        db.add(av)
        db.flush()
        a1 = models.Asset(owner_kind="avatar", avatar_id=av.id, file_path="/x/a.png")
        a2 = models.Asset(owner_kind="avatar", avatar_id=av.id, file_path="/x/logo.png", asset_type="logo")
        db.add_all([a1, a2])
        db.commit()
        refs = pipeline._asset_refs(db, [str(a2.id), a1.id])  # Studio: picked order, string ids too
        assert [r.role for r in refs] == ["object", "character"]  # a character's logo is an object
        project = models.VideoProject(title="t")
        db.add(project)
        db.flush()
        shot = models.VideoShot(project_id=project.id, shot_order=0, prompt="p",
                                reference_asset_ids_json=[a2.id, a1.id])
        db.add(shot)
        db.commit()
        shot_refs = pipeline._resolve_references_for_shot(db, project, shot)
        # Asset order, as before (the URL carries ?ref=1, the scaled-down reference copy).
        assert [r.url.split("?")[0].endswith(a1.public_token) for r in shot_refs] == [True, False]
    finally:
        db.close()


def test_review_round_two_edges(client, monkeypatch):
    from app import config, models
    from app.db import SessionLocal
    from app.providers import sogni
    from app.services import pipeline

    # Guide edits: a place picture is the base, whatever the pick order.
    route = sogni.edit_route("sogni/moss-image", [_ref("style", "Film"), _ref("scene", "Cafe")])
    assert [r.role for r in route.refs] == ["scene", "style"]
    # Klein names all ten pictures the documented way.
    many = sogni.edit_route("sogni/moss-image", [_ref("object", f"o{i}") for i in range(10)])
    assert "The tenth image is o9" in sogni.edit_prompt(many, "p")
    # A newly listed vendor editor counts toward spend.
    monkeypatch.setenv("SOGNI_TOOLS_MANIFEST_URL", "")
    config.get_settings.cache_clear()
    tools = {**sogni.listed()["tools"], "edit_image": [*sogni.listed()["tools"]["edit_image"], "nano-banana-pro"]}
    monkeypatch.setattr(sogni, "_listed", {"tools": tools})
    monkeypatch.setattr(sogni, "_listed_key", None)
    assert sogni.edit_spend_usd("nano-banana-pro") > 0
    config.get_settings.cache_clear()
    # Odd stored ids are skipped, not a crash; a logo is named as a logo.
    assert pipeline._asset_ids(["12", 3, "\u00b2", None, "-1", True]) == [12, 3]
    db = SessionLocal()
    try:
        av = models.Avatar(name="Mia")
        db.add(av)
        db.flush()
        logo = models.Asset(owner_kind="avatar", avatar_id=av.id, file_path="/x/l.png", asset_type="logo")
        db.add(logo)
        db.commit()
        assert [(r.role, r.name) for r in pipeline._asset_refs(db, [logo.id])] == [("object", "Mia's logo")]
    finally:
        db.close()

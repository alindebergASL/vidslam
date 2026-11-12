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


# ---------- which editor really runs: shown in Studio ----------


def test_each_picture_model_says_which_editor_it_runs_as(edit_env):
    from app.providers import sogni

    def ed(model, intent, n=1):
        e = sogni.editor_for(model, intent, n)
        return e and e.selector

    # A Cast character: Krea's identity editor, unless the model has an edit version of its own.
    for m in ("dark-beast-z-turbo", "chroma-v46-flash", "krea-2-turbo", "moss-image"):
        assert ed(f"sogni/{m}", "identity") == "krea-identity-edit", m
    # Seedream edits with its own pictures (Sogni takes it on edit_image though
    # its published list leaves it out), for a likeness or a place, up to 10.
    for intent in ("identity", "guide"):
        assert ed("sogni/seedream-5-0-pro-uncensored", intent) == "seedream-5-0-pro-uncensored"
    assert ed("sogni/seedream-5-0-pro-uncensored", "identity", 10) == "seedream-5-0-pro-uncensored"
    assert ed("sogni/dark-beast-krea2", "identity") == "dark-beast-krea2-identity-edit"
    assert ed("sogni/flux-3-klein", "identity") == "flux-3-klein"
    assert ed("sogni/qwen-2512", "identity") == "qwen"
    assert ed("sogni/qwen-2512-lightning", "identity") == "qwen-lightning"
    assert ed("sogni/gpt-image-2.6-flare", "identity") == "gpt-image-2.6-flare"
    # Other pictures: an identity editor gives way to FLUX.3 Klein.
    for m in ("krea-2-turbo", "dark-beast-krea2", "chroma-v46-flash"):
        assert ed(f"sogni/{m}", "guide") == "flux-3-klein", m
    assert ed("sogni/qwen-2512", "guide") == "qwen"
    # More pictures than Krea takes (2): Klein.
    assert ed("sogni/krea-2-turbo", "identity", 3) == "flux-3-klein"
    # Not a Sogni model, or no pictures: no editor.
    assert ed("openai/gpt-image-2", "identity") is None and ed("sogni/krea-2-turbo", "identity", 0) is None

    # What the pickers get.
    chroma = sogni.with_pictures("sogni/chroma-v46-flash")
    ident = chroma["identity"]
    assert (ident["id"], ident["name"], ident["own"], ident["max_refs"]) == (
        "krea-identity-edit", "Krea 2 Identity Edit", False, 2)
    assert ident["price_text"] == "about $0.03 per image" and ident["usd"] == pytest.approx(6.49 * 0.005)
    assert ident["over_max"]["id"] == "flux-3-klein" and ident["over_max"]["own"] is False
    assert chroma["guide"]["id"] == "flux-3-klein" and chroma["guide"]["over_max"] is None
    seedream = sogni.with_pictures("sogni/seedream-5-0-pro-uncensored")
    for intent in ("identity", "guide"):
        s = seedream[intent]
        assert (s["id"], s["name"], s["own"], s["max_refs"], s["over_max"]) == (
            "seedream-5-0-pro-uncensored", "Seedream 5.0 Pro (uncensored) edit", True, 10, None)
        assert s["usd"] == pytest.approx(13.5 * 0.005)
    beast = sogni.with_pictures("sogni/dark-beast-krea2")["identity"]
    assert (beast["name"], beast["own"]) == ("Dark Beast Krea 2 Identity Edit", True)
    assert sogni.with_pictures("sogni/flux-3-klein")["guide"]["own"] is True
    assert sogni.with_pictures("sogni/krea-2-turbo")["identity"]["own"] is True
    # Edits switched off: the pictures are ignored, so no editor.
    edit_env(SOGNI_EDIT_REFERENCES="false")
    assert sogni.with_pictures("sogni/krea-2-turbo") is None


def test_a_missing_default_editor_is_logged_for_pictures_not_for_the_pickers(edit_env, caplog):
    from app.providers import sogni

    edit_env(SOGNI_IDENTITY_EDIT_MODEL="no-such-editor")
    with caplog.at_level("WARNING", logger=sogni.log.name):
        assert sogni.with_pictures("sogni/chroma-v46-flash")["identity"] is not None
        assert sogni.editor_for("sogni/moss-image", "identity") is not None
    assert "isn't in Sogni's edit list" not in caplog.text
    # A real picture still says so.
    with caplog.at_level("WARNING", logger=sogni.log.name):
        sogni.edit_route("sogni/moss-image", [_ref("character")])
    assert "'no-such-editor' isn't in Sogni's edit list" in caplog.text


def test_the_studio_image_model_list_says_which_editor_runs(auth_client, monkeypatch):
    from app import config
    from app.services import model_catalog, provider_keys
    from app.services import model_registry as reg

    monkeypatch.setenv("SOGNI_API_KEY", "sk-test-123456789")
    config.get_settings.cache_clear()
    provider_keys.invalidate()
    reg.put_setting(model_catalog.CATALOG_KEY, {"fetched_at": "2026-11-12T00:00:00+00:00", "errors": {},
                                                "entries": []})
    models = {m["id"]: m for m in auth_client.get("/api/providers/openrouter/image-models").json()}
    chroma = models["sogni/chroma-v46-flash"]
    assert chroma["with_pictures"]["identity"]["name"] == "Krea 2 Identity Edit"
    assert chroma["with_pictures"]["guide"]["name"] == "FLUX.3 Klein edit"
    assert models["sogni/dark-beast-krea2"]["with_pictures"]["identity"]["id"] == "dark-beast-krea2-identity-edit"
    seedream = models["sogni/seedream-5-0-pro-uncensored"]["with_pictures"]
    assert seedream["identity"]["id"] == seedream["guide"]["id"] == "seedream-5-0-pro-uncensored"
    # The other fields are as before; other providers' models get no editor.
    assert {"id", "name", "description", "price_text"} <= set(chroma)
    assert all("with_pictures" not in m for i, m in models.items() if not i.startswith("sogni/"))
    config.get_settings.cache_clear()
    provider_keys.invalidate()


def _studio_job(db, model, *, kind="avatar", options=None):
    from app import models

    if kind == "avatar":
        owner = models.Avatar(name="Mia")
        db.add(owner)
        db.flush()
        asset = models.Asset(owner_kind="avatar", avatar_id=owner.id, file_path="/x/mia.png")
    else:
        owner = models.Ingredient(name="Cafe", kind="scene")
        db.add(owner)
        db.flush()
        asset = models.Asset(owner_kind="ingredient", ingredient_id=owner.id, file_path="/x/cafe.png")
    db.add(asset)
    db.flush()
    job = models.AssetGenerationJob(owner_kind=kind, owner_id=owner.id, output_kind="image",
                                    prompt="Reading by the window.", reference_asset_ids_json=[asset.id],
                                    provider_model=model, options_json=options, status="pending")
    db.add(job)
    db.commit()
    return job.id


def test_a_studio_picture_records_the_editor_that_really_ran(auth_client, live_sogni):  # noqa: F811
    from app.db import SessionLocal
    from app.services import pipeline

    done = _wf("completed", steps=[{"id": "s1", "artifacts": [{"id": "a", "kind": "image", "url": ART_IMG}]}])
    with SessionLocal() as db:
        jid = _studio_job(db, "sogni/chroma-v46-flash", options={"note": "kept"})
        with respx.mock() as router:
            start = router.post(WF).mock(return_value=httpx.Response(201, json=done))
            router.get(ART_IMG).mock(return_value=httpx.Response(200, content=PNG))
            pipeline.run_studio_job(db, jid)
    args = json.loads(start.calls[0].request.content)["input"]["steps"][0]
    assert (args["toolName"], args["arguments"]["model"]) == ("edit_image", "krea-identity-edit")
    job = auth_client.get(f"/api/studio/jobs/{jid}").json()
    assert job["status"] == "completed" and job["provider_model"] == "sogni/chroma-v46-flash"
    assert (job["ran_on_name"], job["ran_on_editor"], job["ref_intent"]) == (
        "Krea 2 Identity Edit", "krea-identity-edit", "identity")
    # Merged into the job's options; nothing else there is lost.
    assert job["options"] == {"note": "kept", "ran_on": {"provider": "sogni", "tool": "edit_image",
                                                         "model": "krea-identity-edit",
                                                         "name": "Krea 2 Identity Edit"}}

    # A failed edit says which editor it was tried on.
    from app.providers.sogni import SogniError

    with SessionLocal() as db:
        jid = _studio_job(db, "sogni/krea-2-turbo", kind="ingredient")
        with respx.mock() as router:
            router.post(WF).mock(return_value=httpx.Response(201, json=_wf("failed", error={"message": "nope"})))
            with pytest.raises(SogniError):
                pipeline.run_studio_job(db, jid)
    job = auth_client.get(f"/api/studio/jobs/{jid}").json()
    assert job["status"] == "failed" and job["ref_intent"] == "guide"
    assert (job["ran_on_name"], job["ran_on_editor"]) == ("FLUX.3 Klein edit", "flux-3-klein")

    # A start Sogni refused (or never reached) ran no editor: the failed
    # tile names none, and the provider says no edit was used.
    from app.providers.sogni import SogniImageProvider

    refused = httpx.Response(400, json={"status": "error", "errorCode": 4103, "message": "not allowed"})
    for answer in (refused, httpx.ConnectError("down")):
        with SessionLocal() as db:
            jid = _studio_job(db, "sogni/chroma-v46-flash", options={"note": "kept"})
            with respx.mock() as router:
                router.post(WF).mock(side_effect=[answer] * 3)
                with pytest.raises(SogniError):
                    pipeline.run_studio_job(db, jid)
        job = auth_client.get(f"/api/studio/jobs/{jid}").json()
        assert job["status"] == "failed" and job["ref_intent"] == "identity"
        assert (job["ran_on_name"], job["ran_on_editor"], job["options"]) == (None, None, {"note": "kept"})
    provider = SogniImageProvider()
    with respx.mock() as router:
        router.post(WF).mock(return_value=refused)
        with pytest.raises(SogniError):
            provider.generate(model="sogni/krea-2-turbo", prompt="Mia by the lamp.", references=[_ref("character")])
    assert provider.last_route is None


def test_a_picture_without_an_edit_records_nothing(auth_client):
    from app.services import pipeline

    # Mock pictures (and every non-Sogni provider) have no editor; a stale
    # "ran_on" from an earlier run is dropped, an upscale's factor stays.
    assert pipeline._options_with_ran_on({"factor": 2, "ran_on": {"name": "x"}}, None) == {"factor": 2}
    assert pipeline._options_with_ran_on(None, {"name": "y"}) == {"ran_on": {"name": "y"}}
    aid = auth_client.post("/api/avatars", json={"name": "N"}).json()["id"]
    job = auth_client.post("/api/studio/generate-image", json={
        "owner_kind": "avatar", "owner_id": aid, "prompt": "N on a rooftop", "reference_asset_ids": []}).json()
    job = auth_client.get(f"/api/studio/jobs/{job['id']}").json()
    assert job["status"] == "completed" and job["options"] == {}
    assert (job["ran_on_name"], job["ran_on_editor"], job["ref_intent"]) == (None, None, None)


def test_seedream_edits_with_the_pictures_itself(client, live_sogni, monkeypatch):  # noqa: F811
    """Sogni takes seedream-5-0-pro-uncensored on edit_image (live 2026-11-12)
    although its published edit list leaves it out: picked with pictures,
    Seedream itself makes the picture, naming them "Image 1", "Image 2"."""
    from app.providers import sogni

    assert "seedream-5-0-pro-uncensored" not in sogni.listed()["tools"]["edit_image"]
    assert "seedream-5-0-pro-uncensored" in [e.selector for e in sogni.edit_models()]
    # Also when Sogni's list is short.
    monkeypatch.setattr(sogni, "_listed", {"tools": {**sogni.listed()["tools"], "edit_image": ["qwen"]}})
    monkeypatch.setattr(sogni, "_listed_key", None)
    assert sogni.edit_route("sogni/seedream-5-0-pro-uncensored", [_ref("character")]).model.selector == \
        "seedream-5-0-pro-uncensored"
    monkeypatch.undo()

    refs = [_ref("character", "Maya"), _ref("scene", "Beach"), _ref("character", "Ana")]
    body, provider = _generate("sogni/seedream-5-0-pro-uncensored", refs, prompt="They walk at dusk",
                               settings={"aspect_ratio": "9:16"})
    step = body["input"]["steps"][0]
    assert step["toolName"] == "edit_image"
    args = step["arguments"]
    assert args["model"] == "seedream-5-0-pro-uncensored" and args["sourceImageIndex"] == -1
    assert args["aspectRatio"] == "9:16"
    assert len(body["media_references"]) == 3  # premium: no switch to another editor over a limit
    # The place first, then the people (Sogni: name them "Image N" in the order sent).
    assert args["prompt"].startswith("Image 1 is the place (Beach). Image 2 shows Maya")
    assert "Image 3 shows Ana" in args["prompt"]
    assert provider.last_route.model.name == "Seedream 5.0 Pro (uncensored) edit"
    assert sogni.edit_cost_usd("seedream-5-0-pro-uncensored", 3) == pytest.approx((13.5 + 2 * 0.9) * 0.005)

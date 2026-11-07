"""Sogni's full model list: synced from its published tool schemas, with our
notes and prices, closed loops on first + last frame models, and clear errors
for models that need a picture."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from .test_sogni import WF, _started, live_sogni  # noqa: F401 — fixture

MANIFEST = "https://cdn.example.net/sogni/generation-tools.json"


def _manifest(image=(), video=(), animate=()) -> dict:
    def tool(name, key, enum):
        return {"name": name, "parameters": {"type": "object", "properties": {key: {"type": "string",
                                                                                 "enum": list(enum)}}}}
    return {"version": "2099-01-01.1", "tools": [tool("generate_image", "model", image),
                                           tool("generate_video", "videoModel", video),
                                           tool("animate_photo", "videoModel", animate),
                                           tool("generate_music", "model", ["music3"])]}


# ---------- the bundled list ----------


def test_bundled_list_has_every_next_gen_model_first():
    from app.providers import sogni

    images = sogni.image_models()
    sels = [m.selector for m in images]
    next_gen = {"krea-2-turbo", "moss-image", "moss-image-8step", "dark-beast-krea2", "dark-beast-z-turbo",
                "z-turbo", "z-image", "chroma-detail", "chroma1-hd", "chroma-v46-flash", "qwen-2512",
                "qwen-2512-lightning"}
    assert next_gen <= set(sels)
    assert {m.family for m in images[:len(next_gen)]} == {sogni.NEXT_GEN}
    assert len(sels) >= 33 and len(set(sels)) == len(sels)
    # Listed by Sogni but not in our notes yet: still offered, with sensible defaults.
    xl = sogni.image_model("sogni/art-universe-xl")
    assert xl and xl.family == sogni.COMMUNITY and xl.name == "Art Universe XL"
    # Not in the published schema yet, but known to work: kept.
    assert sogni.image_model("sogni/seedream-5-0-pro-uncensored").premium


def test_video_capabilities_follow_the_tool_that_lists_the_model():
    from app.providers import sogni

    ltx = sogni.video_model("sogni/ltx25")
    assert ltx.animate and ltx.text and ltx.last_frame  # first + last frame: closed loops
    flf = sogni.video_model("sogni/minimax-h3-fasth3-flf2v-turbo")
    assert flf.animate and not flf.text and flf.flf_only and flf.last_frame and not flf.premium
    i2v = sogni.video_model("sogni/minimax-h3-i2v")
    assert i2v.animate and not i2v.text and not i2v.last_frame
    t2v = sogni.video_model("sogni/minimax-h3-t2v")
    assert t2v.text and not t2v.animate
    caps = sogni.capabilities_for("sogni/minimax-h3-fasth3-flf2v-turbo")
    assert caps.first_frame and caps.last_frame and (min(caps.durations), caps.max_seconds) == (6, 15)
    assert sogni.video_model("sogni/seedance2-5-uncensored").premium
    assert sogni.video_model("sogni/wan3.0-spicy-video").animate


def test_display_names_say_what_a_model_is():
    from app.providers import sogni

    assert sogni.display_name(sogni.image_model("sogni/moss-image")) == "MOSS-Image (Sogni · Next-Gen · uncensored)"
    assert "Premium Spark" in sogni.display_name(sogni.image_model("sogni/gpt-image-2.6-sunburst"))
    name = sogni.display_name(sogni.video_model("sogni/minimax-h3-fasth3-flf2v-turbo"))
    assert "first + last frame" in name and "6–15s" in name


# ---------- syncing ----------


@respx.mock
def test_refresh_picks_up_new_models_and_keeps_the_last_good_list(monkeypatch):
    from app import config
    from app.providers import sogni

    monkeypatch.setenv("SOGNI_TOOLS_MANIFEST_URL", MANIFEST)
    config.get_settings.cache_clear()
    sogni._reset_listed()
    route = respx.get(MANIFEST).mock(return_value=httpx.Response(200, json=_manifest(
        image=["krea-2-turbo", "brand-new-model", "shiny-xl"], video=["ltx25", "newvid-t2v"],
        animate=["ltx25", "minimax-h3-flf2v-next"])))
    data = sogni.refresh_listed()
    assert data["tools"]["generate_image"] == ["krea-2-turbo", "brand-new-model", "shiny-xl"]
    assert "generate_music" not in data["tools"]
    sels = [m.selector for m in sogni.image_models()]
    assert {"brand-new-model", "shiny-xl", "seedream-5-0-pro-uncensored"} <= set(sels)
    assert "moss-image" not in sels  # Sogni no longer lists it
    assert sogni.image_model("brand-new-model").family == sogni.OPEN  # unverified: not filed as Next-Gen
    nxt = sogni.video_model("minimax-h3-flf2v-next")
    assert nxt.flf_only and nxt.last_frame and not nxt.text
    # Kept on disk: a fresh process reads the synced list, not the bundled one.
    sogni._reset_listed()
    assert "brand-new-model" in [m.selector for m in sogni.image_models()]

    # A broken manifest raises and leaves the list alone.
    route.mock(return_value=httpx.Response(200, json={"tools": []}))
    with pytest.raises(ValueError):
        sogni.refresh_listed()
    route.mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        sogni.refresh_listed()
    assert "brand-new-model" in [m.selector for m in sogni.image_models()]
    config.get_settings.cache_clear()


def test_catalog_refresh_syncs_sogni_and_reports_a_failure(client, monkeypatch, live_sogni):  # noqa: F811
    from app import config
    from app.providers import sogni
    from app.services import model_catalog

    monkeypatch.setenv("SOGNI_TOOLS_MANIFEST_URL", MANIFEST)
    config.get_settings.cache_clear()
    sogni._reset_listed()
    ok = {"ok": True}

    def handler(request):
        if str(request.url) == MANIFEST:
            if not ok["ok"]:
                return httpx.Response(500)
            return httpx.Response(200, json=_manifest(image=["krea-2-turbo", "moss-image", "late-addition"],
                                                      video=["ltx25"], animate=["ltx25"]))
        return httpx.Response(200, json={"data": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        snap = model_catalog.refresh(client=c)
    assert "sogni_tools" not in snap["errors"]
    ids = {e["id"] for e in snap["entries"] if e["provider"] == "sogni"}
    assert {"sogni/moss-image", "sogni/late-addition", "sogni/ltx25"} <= ids
    ok["ok"] = False
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        snap = model_catalog.refresh(client=c)
    assert "sogni_tools" in snap["errors"]
    assert "sogni/late-addition" in {e["id"] for e in snap["entries"]}  # last good list kept
    config.get_settings.cache_clear()


def test_stored_snapshot_serves_the_current_sogni_list(client):
    from app.services import model_catalog
    from app.services import model_registry as reg

    reg.put_setting(model_catalog.CATALOG_KEY, {"fetched_at": "2026-11-01T00:00:00+00:00", "errors": {},
                                                "entries": [{"id": "sogni/krea-2-turbo", "kind": "image",
                                                             "provider": "sogni", "name": "old"}]})
    snap = model_catalog.get_snapshot()
    sogni = {e["id"]: e for e in snap["entries"] if e["provider"] == "sogni"}
    assert sogni["sogni/krea-2-turbo"]["name"].startswith("Krea 2 Turbo (Sogni")
    assert sogni["sogni/moss-image"]["new"] is True and sogni["sogni/krea-2-turbo"]["new"] is False


# ---------- submitting ----------


def _submit(model, refs, strategy="frame_images"):
    from app.providers.sogni import SogniVideoProvider

    with respx.mock() as router:
        start = router.post(WF).mock(side_effect=_started)
        SogniVideoProvider().submit(model=model, prompt="water", references=refs, duration_seconds=8,
                                    reference_strategy=strategy)
    return json.loads(start.calls[0].request.content)


def test_closed_loop_pins_both_ends(client, live_sogni):  # noqa: F811
    from app.providers.base import ImageRef

    same = [ImageRef(url="https://x/a.png", frame_type="first_frame"),
            ImageRef(url="https://x/a.png", frame_type="last_frame")]
    body = _submit("sogni/ltx25", same)
    args = body["input"]["steps"][0]["arguments"]
    assert body["input"]["steps"][0]["toolName"] == "animate_photo"
    assert (args["frameRole"], args["sourceImageIndex"], args["endImageIndex"]) == ("both", -1, -1)
    assert body["media_references"] == [{"kind": "image", "url": "https://x/a.png"}]
    # A first + last frame model given only a first frame closes on it.
    args = _submit("sogni/minimax-h3-fasth3-flf2v-turbo", same[:1])["input"]["steps"][0]["arguments"]
    assert (args["frameRole"], args["endImageIndex"]) == ("both", -1)
    # Different first and last frames.
    two = [ImageRef(url="https://x/a.png", frame_type="first_frame"),
           ImageRef(url="https://x/b.png", frame_type="last_frame")]
    body = _submit("sogni/minimax-h3-flf2v", two)
    assert body["input"]["steps"][0]["arguments"]["endImageIndex"] == -2 and len(body["media_references"]) == 2


def test_picture_only_models_use_a_loose_reference_as_the_first_frame(client, live_sogni):  # noqa: F811
    from app.providers.base import ImageRef

    body = _submit("sogni/minimax-h3-i2v", [ImageRef(url="https://x/a.png")], strategy="input_references")
    step = body["input"]["steps"][0]
    assert step["toolName"] == "animate_photo" and step["arguments"]["frameRole"] == "start"


def test_models_that_need_a_picture_fail_before_calling_sogni(client, live_sogni):  # noqa: F811
    from app.providers.base import ImageRef
    from app.providers.sogni import SogniError, SogniVideoProvider

    with respx.mock() as router:
        start = router.post(WF).mock(side_effect=_started)
        with pytest.raises(SogniError, match="needs a starting picture"):
            SogniVideoProvider().submit(model="sogni/minimax-h3-i2v", prompt="w", references=[])
        with pytest.raises(SogniError, match="needs at least one reference picture"):
            SogniVideoProvider().submit(model="sogni/minimax-h3-r2v", prompt="w", references=[])
        assert start.call_count == 0
        SogniVideoProvider().submit(model="sogni/minimax-h3-r2v", prompt="w",
                                    references=[ImageRef(url="https://x/a.png")])
        assert start.call_count == 1


def test_new_next_gen_images_are_priced_from_the_table(client):
    from app.providers import sogni

    assert sogni.image_cost_usd("sogni/moss-image") == pytest.approx(8.86 * 0.005)
    assert sogni.image_cost_usd("sogni/dark-beast-z-turbo") == pytest.approx(1.60 * 0.005)
    assert sogni.video_rate_usd("sogni/minimax-h3-fasth3-flf2v-turbo") == pytest.approx(4.0 * 0.005)


# ---------- review follow-ups ----------


@respx.mock
def test_every_process_sees_a_sync_and_a_partial_manifest_is_refused(monkeypatch):
    from app import config
    from app.providers import sogni

    monkeypatch.setenv("SOGNI_TOOLS_MANIFEST_URL", MANIFEST)
    config.get_settings.cache_clear()
    sogni._reset_listed()
    assert "moss-image" in [m.selector for m in sogni.image_models()]  # loaded (bundled)
    # Another worker syncs: this one reads the new file on its next lookup.
    other = {"source": MANIFEST, "fetched_at": "2099-01-01T00:00:00Z",
             "tools": {"generate_image": ["krea-2-turbo", "from-elsewhere"], "generate_video": ["ltx25"],
                       "animate_photo": ["ltx25"]}}
    sogni._cache_path().write_text(json.dumps(other), encoding="utf-8")
    assert sogni.image_model("sogni/from-elsewhere") is not None
    assert sogni.image_model("sogni/moss-image") is None
    # One tool's list missing (an unparsed schema change): refused, nothing kept.
    respx.get(MANIFEST).mock(return_value=httpx.Response(200, json=_manifest(
        image=["krea-2-turbo"], video=["ltx25"], animate=[])))
    with pytest.raises(ValueError, match="animate_photo 0"):
        sogni.refresh_listed()
    assert sogni.video_model("sogni/ltx25").animate and sogni.image_model("sogni/from-elsewhere")
    # Syncing off: only the bundled copy, whatever was synced before.
    monkeypatch.setenv("SOGNI_TOOLS_MANIFEST_URL", "")
    config.get_settings.cache_clear()
    assert sogni.image_model("sogni/from-elsewhere") is None and sogni.image_model("sogni/moss-image")
    config.get_settings.cache_clear()


def test_a_refresh_keeps_new_flags_on_sogni_models(client, monkeypatch, live_sogni):  # noqa: F811
    from app import config
    from app.providers import sogni
    from app.services import model_catalog

    monkeypatch.setenv("SOGNI_TOOLS_MANIFEST_URL", MANIFEST)
    config.get_settings.cache_clear()
    sogni._reset_listed()
    image = ["krea-2-turbo"]

    def handler(request):
        if str(request.url) == MANIFEST:
            return httpx.Response(200, json=_manifest(image=image, video=["ltx25"], animate=["ltx25"]))
        return httpx.Response(200, json={"data": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        model_catalog.refresh(client=c)
        image.append("late-addition")
        model_catalog.refresh(client=c)
    snap = model_catalog.get_snapshot()
    by_id = {e["id"]: e for e in snap["entries"]}
    assert by_id["sogni/late-addition"]["new"] is True and by_id["sogni/krea-2-turbo"]["new"] is False
    assert snap["new_count"] == sum(1 for e in snap["entries"] if e.get("new")) >= 1
    assert snap["counts"]["image"] == sum(1 for e in snap["entries"] if e["kind"] == "image")
    config.get_settings.cache_clear()


def _plan(model, **cfg):
    from app.providers import sogni
    from app.schemas.ambient import AmbientConfig
    from app.services import ambient

    from .test_ambient import SHORT

    c = AmbientConfig(**{**SHORT, "model_override": model, **cfg})
    return c, ambient.plan_loop(c, sogni.capabilities_for(model))


def test_ambient_makes_a_starting_still_for_picture_only_models():
    from app.services import ambient

    cfg, plan = _plan("sogni/minimax-h3-fasth3-i2v-turbo")
    assert plan.first_frame_still and not plan.anchored and plan.provider_calls == 2
    assert any("needs a starting picture" in n for n in plan.notes)
    items = [line["item"] for line in ambient.estimate_ambient_cost(cfg, plan)["lines"]]
    assert "Starting still (image)" in items
    # A model that also works from text needs no still.
    assert not _plan("sogni/ltx23")[1].first_frame_still


def test_ambient_gives_reference_models_a_reference_still():
    cfg, plan = _plan("sogni/minimax-h3-r2v-turbo")
    assert plan.first_frame_still and not plan.anchored and plan.strategy == "single"
    assert any("reference picture" in n for n in plan.notes)
    own = _plan("sogni/minimax-h3-r2v-turbo", picture_origin="upload")[1]
    assert own.first_frame_still and not any("isn't used" in n for n in own.notes)


def test_first_and_last_frame_models_never_chain_open_clips():
    plan = _plan("sogni/minimax-h3-fasth3-flf2v-turbo", loop_seconds=30)[1]
    assert plan.segments == [15] and plan.anchored and plan.closed
    assert any("can't be chained" in n for n in plan.notes)
    assert len(_plan("sogni/ltx25", loop_seconds=30)[1].segments) == 2  # LTX still chains


def test_fields_go_only_to_models_that_take_them(client, live_sogni):  # noqa: F811
    from app.providers.base import ImageRef
    from app.providers.sogni import SogniVideoProvider

    def args_for(model, refs=(), strategy="frame_images"):
        with respx.mock() as router:
            start = router.post(WF).mock(side_effect=_started)
            SogniVideoProvider().submit(model=model, prompt="water", references=list(refs), duration_seconds=5,
                                        negative_prompt="text", reference_strategy=strategy)
        body = json.loads(start.calls[0].request.content)
        return body["input"]["steps"][0]["arguments"]

    pic = [ImageRef(url="https://x/a.png", frame_type="first_frame")]
    wan3 = args_for("sogni/wan3.0-video", pic)
    assert "negativePrompt" not in wan3 and wan3["generateAudio"] is False
    assert "generateAudio" not in args_for("sogni/happyhorse-1.1-t2v")
    assert args_for("sogni/ltx25")["negativePrompt"] == "text"
    # An ambient reference still on H3 r2v is a loose reference, named its way.
    r2v = args_for("sogni/minimax-h3-r2v-turbo", pic)
    assert r2v["referenceImageIndices"] == [-1] and r2v["prompt"].startswith("Use <Picture 1>")
    assert "negativePrompt" not in r2v


def test_text_only_image_models_get_no_source_image(client, live_sogni):  # noqa: F811
    from app.providers.base import ImageRef
    from app.providers.sogni import SogniImageProvider

    from .test_sogni import ART_IMG, PNG, _wf

    done = _wf("completed", steps=[{"id": "s1", "artifacts": [{"id": "a", "kind": "image", "url": ART_IMG}]}])
    with respx.mock() as router:
        start = router.post(WF).mock(return_value=httpx.Response(201, json=done))
        router.get(ART_IMG).mock(return_value=httpx.Response(200, content=PNG))
        SogniImageProvider().generate(model="sogni/moss-image", prompt="the same place at night",
                                      references=[ImageRef(url="https://x/first.png", role="scene")])
    body = json.loads(start.calls[0].request.content)
    args = body["input"]["steps"][0]["arguments"]
    assert not body.get("media_references") and "sourceImageIndex" not in args
    assert "starting_image_strength" not in args


def test_picker_names_say_frames_once(client):
    from app.api.providers import _catalog_entries
    from app.services import model_catalog
    from app.services import model_registry as reg

    reg.put_setting(model_catalog.CATALOG_KEY, {"fetched_at": "2026-11-01T00:00:00+00:00", "errors": {},
                                                "entries": []})
    names = {e["id"]: e["name"] for e in _catalog_entries("video")}
    name = names["sogni/minimax-h3-fasth3-flf2v-turbo"]
    assert name.count("first + last frame") == 1 and "first+last" not in name and "up to" not in name


# ---------- second review ----------


def test_happyhorse_picture_modes_need_their_pictures(client, live_sogni):  # noqa: F811
    from app.providers import sogni
    from app.providers.base import ImageRef

    i2v = sogni.capabilities_for("sogni/happyhorse-1.1-i2v")
    assert i2v.first_frame and i2v.needs_first_frame  # listed under generate_video too, but needs a frame
    r2v = sogni.capabilities_for("sogni/happyhorse-1.1-r2v")
    assert r2v.needs_reference and not r2v.first_frame  # loose references, not frames
    assert "reference" in sogni.video_frames(sogni.video_model("sogni/happyhorse-1.1-r2v"))
    assert sogni.capabilities_for("sogni/happyhorse-1.1-t2v").needs_first_frame is False
    body = _submit("sogni/happyhorse-1.1-r2v", [ImageRef(url="https://x/a.png", frame_type="first_frame")])
    step = body["input"]["steps"][0]
    assert step["toolName"] == "generate_video" and step["arguments"]["prompt"].startswith("Use [Image 1]")
    assert "generateAudio" not in step["arguments"]
    assert "generateAudio" not in _submit("sogni/wan22", [])["input"]["steps"][0]["arguments"]


def test_a_sync_that_cant_be_stored_changes_nothing(monkeypatch, tmp_path):
    from app import config
    from app.providers import sogni

    monkeypatch.setenv("SOGNI_TOOLS_MANIFEST_URL", MANIFEST)
    config.get_settings.cache_clear()
    sogni._reset_listed()
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setattr(sogni, "_cache_path", lambda: blocker / "sogni_tools.json")
    with respx.mock() as router:
        router.get(MANIFEST).mock(return_value=httpx.Response(200, json=_manifest(
            image=["krea-2-turbo", "brand-new-model"], video=["ltx25"], animate=["ltx25"])))
        with pytest.raises(OSError, match="couldn't store"):
            sogni.refresh_listed()
    assert sogni.image_model("sogni/brand-new-model") is None  # this process too
    assert not list(tmp_path.rglob(".sogni_tools.json.*"))  # no temp file left
    config.get_settings.cache_clear()


def test_a_lagging_sync_doesnt_replace_a_newer_bundled_list(monkeypatch):
    from app import config
    from app.providers import sogni

    monkeypatch.setenv("SOGNI_TOOLS_MANIFEST_URL", MANIFEST)
    config.get_settings.cache_clear()
    sogni._reset_listed()
    old = {"version": "2026-01-01.1", "fetched_at": "2099-01-01T00:00:00Z",
           "tools": {"generate_image": ["krea-2-turbo", "old-one"], "generate_video": ["ltx25"],
                     "animate_photo": ["ltx25"]}}
    sogni._cache_path().parent.mkdir(parents=True, exist_ok=True)
    sogni._cache_path().write_text(json.dumps(old), encoding="utf-8")
    assert sogni.image_model("sogni/old-one") is None and sogni.image_model("sogni/moss-image")
    config.get_settings.cache_clear()


def test_a_model_sogni_stops_listing_keeps_its_price(monkeypatch):
    from app import config
    from app.providers import sogni

    monkeypatch.setenv("SOGNI_TOOLS_MANIFEST_URL", MANIFEST)
    config.get_settings.cache_clear()
    sogni._reset_listed()
    with respx.mock() as router:
        router.get(MANIFEST).mock(return_value=httpx.Response(200, json=_manifest(
            image=["krea-2-turbo"], video=["ltx25"], animate=["ltx25"])))
        sogni.refresh_listed()
    assert sogni.image_model("sogni/moss-image") is None
    assert sogni.image_cost_usd("sogni/moss-image") == pytest.approx(8.86 * 0.005)
    assert sogni.video_rate_usd("sogni/minimax-h3-i2v") == pytest.approx(16.0 * 0.005)
    config.get_settings.cache_clear()


def test_same_place_stills_prefer_a_model_that_takes_the_reference(client, monkeypatch):
    from app.db import SessionLocal
    from app.providers.base import ImageRef
    from app.schemas.ambient import AmbientConfig
    from app.services import ambient

    from .test_ambient import SHORT

    calls: list[tuple[str, str]] = []
    failing: set[str] = set()

    class Stub:
        def generate(self, *, model, prompt, references, negative_prompt="", settings=None):
            calls.append((model, prompt))
            if model in failing:
                raise RuntimeError("down")
            return b"png"

    monkeypatch.setattr(ambient, "get_image", lambda model=None: Stub())
    monkeypatch.setattr(ambient, "routes_per_model", lambda kind, model: True)
    cfg = AmbientConfig(**{**SHORT, "still_model": "sogni/krea-2-turbo"})
    fallback = ambient.still_model_chain(cfg)[1]
    ref = [ImageRef(url="https://x/a.png", role="scene")]
    same = "night." + ambient._SAME_PLACE
    db = SessionLocal()
    try:
        assert ambient.generate_still(db, cfg, prompt="p", aspect_ratio="16:9")[1] == "sogni/krea-2-turbo"
        # A "same place" still: the model that applies the reference first.
        assert ambient.generate_still(db, cfg, prompt=same, aspect_ratio="16:9", references=ref)[1] == fallback
        # It fails: the scene's Sogni model still makes it (from the text alone).
        failing.add(fallback)
        assert ambient.generate_still(db, cfg, prompt=same, aspect_ratio="16:9", references=ref)[1] == \
            "sogni/krea-2-turbo"
    finally:
        db.close()
    assert [m for m, _ in calls] == ["sogni/krea-2-turbo", fallback, fallback, "sogni/krea-2-turbo"]
    assert calls[1][1] == same and calls[3][1] == "night."  # no talk of a reference it never sees
    assert ambient.reference_still_cost(cfg) != ambient.still_cost(cfg)  # priced at the model that makes it


def test_a_failed_sogni_sync_doesnt_mute_model_gone_alerts(client, monkeypatch):
    from app.services import alerts, model_catalog
    from app.services import model_registry as reg

    sent: list[str] = []
    monkeypatch.setattr(alerts, "send", lambda key, *a, **k: sent.append(key))
    monkeypatch.setattr(reg, "selected", lambda kind: "openai/gone-model" if kind == "chat" else "")
    prev = [{"kind": "chat", "id": "openai/gone-model", "provider": "openrouter"}]
    model_catalog._alert_missing_active({"entries": [], "errors": {"sogni_tools": "boom"}}, prev)
    assert sent == ["model-gone:chat:openai/gone-model"]
    model_catalog._alert_missing_active({"entries": [], "errors": {"openrouter_models": "down"}}, prev)
    assert len(sent) == 1  # an OpenRouter outage still mutes it
    # ...but not a Sogni model's alert: OpenRouter doesn't list Sogni models.
    monkeypatch.setattr(reg, "selected", lambda kind: "sogni/moss-image" if kind == "image" else "")
    gone = [{"kind": "image", "id": "sogni/moss-image", "provider": "sogni"}]
    model_catalog._alert_missing_active({"entries": [], "errors": {"openrouter_models": "down"}}, gone)
    assert sent[-1] == "model-gone:image:sogni/moss-image"


def test_new_picture_models_need_their_picture_and_a_newer_release_wins(monkeypatch):
    from app import config
    from app.providers import sogni

    monkeypatch.setenv("SOGNI_TOOLS_MANIFEST_URL", MANIFEST)
    config.get_settings.cache_clear()
    sogni._reset_listed()
    with respx.mock() as router:
        router.get(MANIFEST).mock(return_value=httpx.Response(200, headers={"x-jsd-version": "1.0.0-alpha.60"},
                                                              json=_manifest(
            image=["krea-2-turbo"], video=["ltx25", "kling-3-i2v"], animate=["ltx25", "kling-3-i2v"])))
        data = sogni.refresh_listed()
    assert data["package"] == "1.0.0-alpha.60"
    caps = sogni.capabilities_for("sogni/kling-3-i2v")  # listed under both tools, like HappyHorse i2v
    assert caps.first_frame and caps.needs_first_frame
    # Same manifest version, older release than the bundled one: the bundled list wins.
    lagging = {**data, "package": "1.0.0-alpha.58", "version": "2026-07-18.1"}
    assert sogni._newer(lagging, sogni._read_listing(sogni._BUNDLED))["package"] == "1.0.0-alpha.59"
    assert sogni._newer({**lagging, "package": "1.0.0-alpha.60"},
                        sogni._read_listing(sogni._BUNDLED))["package"] == "1.0.0-alpha.60"
    config.get_settings.cache_clear()

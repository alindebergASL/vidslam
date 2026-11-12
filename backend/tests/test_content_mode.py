"""Adult mode (Settings → Adult content): one switch for the text model
(planning, Enhance, scene rewrites) and the picture/video filters."""
from __future__ import annotations

import json

import httpx
import respx

from .test_runware import _CORE, _IMG, _fake_model, _ref

SOGNI_WF = "https://api.sogni.ai/v1/creative-agent/workflows"


def _reset():
    from app import config
    from app.services import provider_keys

    config.get_settings.cache_clear()
    provider_keys.invalidate()


def test_the_switch_defaults_off_and_round_trips(auth_client):
    r = auth_client.get("/api/settings/content").json()
    assert r["adult"] is False and r["default"] is False and r["explicit"] is False
    assert r["text_model"] == "deepseek/deepseek-v4-flash"
    assert auth_client.put("/api/settings/content", json={"adult": True}).json()["adult"] is True
    assert auth_client.get("/api/settings/content").json()["explicit"] is True
    back = auth_client.put("/api/settings/content", json={"adult": None}).json()
    assert back["adult"] is False and back["explicit"] is False
    for bad in ({}, {"adult": "yes"}, {"enabled": True}, []):
        assert auth_client.put("/api/settings/content", json=bad).status_code == 422


def test_the_env_default_applies_until_the_switch_is_set(auth_client, monkeypatch):
    monkeypatch.setenv("ADULT_MODE", "true")
    _reset()
    assert auth_client.get("/api/settings/content").json()["adult"] is True
    auth_client.put("/api/settings/content", json={"adult": False})
    assert auth_client.get("/api/settings/content").json()["adult"] is False
    _reset()


def test_planning_follows_adult_mode(auth_client, monkeypatch):
    from app.providers import get_chat
    from app.services import content_mode

    monkeypatch.setenv("MOCK_PROVIDERS", "false")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-abcdefgh1234")
    monkeypatch.setenv("OPENROUTER_CHAT_MODEL", "anthropic/claude-haiku-5.5")
    _reset()
    assert get_chat().model == "anthropic/claude-haiku-5.5"
    content_mode.set_adult_mode(True)
    assert get_chat().model == "deepseek/deepseek-v4-flash"  # storyboards and scene rewrites too
    content_mode.set_adult_mode(False)
    assert get_chat().model == "anthropic/claude-haiku-5.5"
    _reset()


def test_sogni_safe_filter_is_on_unless_adult_mode(auth_client, monkeypatch):
    from app.providers.sogni import SogniClient
    from app.services import content_mode

    monkeypatch.setenv("SOGNI_API_KEY", "sk-test-123456789")
    _reset()
    bodies = []

    def started(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"status": "success", "data": {"workflow": {"workflowId": "wf", "status": "queued"}}})

    with respx.mock() as router:
        router.post(SOGNI_WF).mock(side_effect=started)
        SogniClient().start(title="t", tool="generate_image", arguments={"prompt": "a lake"})
        content_mode.set_adult_mode(True)
        SogniClient().start(title="t", tool="generate_image", arguments={"prompt": "a lake"})
    assert [b["safe_content_filter"] for b in bodies] == [True, False]
    _reset()


def test_runware_check_and_vendor_knobs_follow_adult_mode(client, monkeypatch):
    from app.providers import runware
    from app.services import content_mode, provider_keys

    monkeypatch.setenv("MOCK_PROVIDERS", "false")
    monkeypatch.setenv("RUNWARE_API_KEY", "rw-test-abcdefgh1234")
    _reset()
    model = _fake_model(monkeypatch, "test:knobs@1", {
        "type": "object", "additionalProperties": False, "required": ["taskType", "taskUUID", "model"],
        "properties": {**_CORE,
                       "safety": {"type": "object", "properties": {"checkContent": {"type": "boolean"}}},
                       "providerSettings": {"type": "object", "properties": {"bfl": {"type": "object", "properties": {
                           "safetyTolerance": {"type": "integer", "minimum": 0, "maximum": 6}}}}},
                       "inputs": {"type": "object", "properties": {"referenceImages": {"type": "array", "items": _IMG}}}}},
        modes=[{"id": "text"}], caps=["text-to-image"])
    off = runware.build_image_task(model, "a quiet street at dusk", [])
    assert off["safety"]["checkContent"] is True and "providerSettings" not in off
    provider_keys.set_option("runware", "check_content", False)  # ignored while adult mode is off
    assert runware.build_image_task(model, "a quiet street at dusk", [])["safety"]["checkContent"] is True
    content_mode.set_adult_mode(True)
    on = runware.build_image_task(model, "a quiet street at dusk", [])
    assert on["safety"]["checkContent"] is False and on["providerSettings"]["bfl"]["safetyTolerance"] == 6
    # A Cast picture is checked in adult mode too.
    cast = runware.build_image_task(model, "her portrait in the street", [_ref(role="character", name="Naina")])
    assert cast["safety"]["checkContent"] is True
    _reset()


def test_sogni_refuses_minors_and_guards_cast_pictures_in_adult_mode(auth_client, monkeypatch):
    import pytest

    from app.providers.sogni import SogniClient
    from app.services import content_mode
    from app.services.safety import UnsafePromptError

    monkeypatch.setenv("SOGNI_API_KEY", "sk-test-123456789")
    _reset()
    content_mode.set_adult_mode(True)
    bodies = []

    def started(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"status": "success", "data": {"workflow": {"workflowId": "wf", "status": "queued"}}})

    with respx.mock() as router:
        router.post(SOGNI_WF).mock(side_effect=started)
        with pytest.raises(UnsafePromptError):
            SogniClient().start(title="t", tool="generate_image", arguments={"prompt": "naked teenage girl"})
        assert not bodies  # never sent
        SogniClient().start(title="t", tool="edit_image", arguments={"prompt": "her on a beach"}, cast=True)
        SogniClient().start(title="t", tool="generate_image", arguments={"prompt": "kids at a beach"})
        SogniClient().start(title="t", tool="generate_image", arguments={"prompt": "a lake at dawn"})
    assert [b["safe_content_filter"] for b in bodies] == [True, True, False]
    _reset()


def test_an_unreadable_setting_counts_as_off(monkeypatch):
    from sqlalchemy.exc import OperationalError

    from app.services import content_mode

    monkeypatch.setenv("ADULT_MODE", "true")
    _reset()

    class Broken:
        def get(self, *a, **k):
            raise OperationalError("select", {}, Exception("db down"))

        def close(self):
            pass

    import app.db as db_mod

    monkeypatch.setattr(db_mod, "SessionLocal", lambda: Broken())
    assert content_mode.adult_mode() is False  # filters on, never off by accident
    _reset()


def test_number_words_are_not_ages():
    import pytest

    from app.services.safety import UnsafePromptError, check_prompt

    for fine in ("a nude couple celebrating ten years of marriage", "after five years apart, they undress each other",
                 "one person in frame, nude", "two models in lingerie", "12 girls on a runway in lingerie"):
        check_prompt(fine)
    for bad in ("nude 14-year-old girl", "13 girl nude", "fifteen years old, lingerie", "13 years of age, nude",
                "girl, 13, topless", "a teen girl stripped naked"):
        with pytest.raises(UnsafePromptError):
            check_prompt(bad)


def test_planning_checks_its_input_and_its_output(auth_client, monkeypatch):
    import pytest

    from app import models
    from app.db import SessionLocal
    from app.schemas.storyboard import ShotPlan, StoryboardPlan
    from app.services.pipeline import _check_plan
    from app.services.safety import UnsafePromptError

    db = SessionLocal()
    try:
        p = models.VideoProject(title="Summer", original_script="A day at the lake.",
                                creative_direction="she is 14; nude scenes")
        db.add(p)
        db.commit()
        pid = p.id
    finally:
        db.close()
    r = auth_client.post(f"/api/projects/{pid}/generate-plan")
    assert r.status_code == 422 and "refused" in r.json()["detail"]

    def plan(prompt, negative=""):
        return StoryboardPlan(title="t", cleaned_voice_script="Hello.", estimated_duration_seconds=5,
                              shots=[ShotPlan(order=0, visual_prompt=prompt, negative_prompt=negative)])

    _check_plan(plan("a couple on a beach at dusk"))
    with pytest.raises(UnsafePromptError):
        _check_plan(plan("a naked teenage girl on a beach"))
    with pytest.raises(UnsafePromptError):
        _check_plan(plan("a nude figure on a beach", negative="adult, mature, woman"))


def test_openrouter_images_refuse_minors_before_any_call(monkeypatch):
    import pytest

    from app.providers.openrouter_image import OpenRouterImageProvider
    from app.services.safety import UnsafePromptError

    monkeypatch.setenv("MOCK_PROVIDERS", "false")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-abcdefgh1234")
    _reset()
    with respx.mock() as router:
        with pytest.raises(UnsafePromptError):
            OpenRouterImageProvider(model="google/nano").generate(model="google/nano", prompt="naked teenage girl",
                                                                  references=[])
        assert not router.calls
    _reset()


def test_the_running_planner_is_what_gates_and_labels_show(auth_client, monkeypatch):
    from app.services import content_mode

    monkeypatch.setenv("OPENROUTER_CHAT_MODEL", "anthropic/claude-haiku-5.5")
    _reset()
    assert content_mode.chat_model() == "anthropic/claude-haiku-5.5"
    content_mode.set_adult_mode(True)
    assert content_mode.chat_model() == "deepseek/deepseek-v4-flash"
    assert auth_client.get("/api/system/info").json()["models"]["chat"] == "deepseek/deepseek-v4-flash"
    _reset()


# ---------- Cast characters marked "adult content allowed" ----------


def _maya(db_add=True):
    from app import models
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        av = models.Avatar(name="Maya")
        db.add(av)
        db.commit()
        a = models.Asset(owner_kind="avatar", avatar_id=av.id, asset_type="hero", source="generated",
                         file_path="/tmp/x.png", public_token=f"tok-maya-{av.id}", mime_type="image/png")
        db.add(a)
        db.commit()
        return av.id, a.id
    finally:
        db.close()


def test_marking_a_character_round_trips_and_validates(auth_client):
    avatar_id, _ = _maya()
    assert auth_client.get("/api/settings/content").json()["adult_ok_avatars"] == []
    r = auth_client.put(f"/api/settings/content/avatars/{avatar_id}", json={"adult_ok": True})
    assert r.status_code == 200 and r.json()["adult_ok_avatars"] == [avatar_id]
    for bad in ({}, {"adult_ok": "yes"}, {"adult_ok": None}):
        assert auth_client.put(f"/api/settings/content/avatars/{avatar_id}", json=bad).status_code == 422
    assert auth_client.put("/api/settings/content/avatars/99999", json={"adult_ok": True}).status_code == 404
    r = auth_client.put(f"/api/settings/content/avatars/{avatar_id}", json={"adult_ok": False})
    assert r.json()["adult_ok_avatars"] == []


def test_refs_carry_the_mark_and_only_adult_mode_honours_it(auth_client):
    from app.db import SessionLocal
    from app.services import content_mode
    from app.services.pipeline import _asset_refs

    avatar_id, asset_id = _maya()
    db = SessionLocal()
    try:
        (ref,) = _asset_refs(db, [asset_id])
        assert ref.role == "character" and ref.adult_ok is False
        _tick_now(avatar_id)
        (ref,) = _asset_refs(db, [asset_id])
        assert ref.adult_ok is True
    finally:
        db.close()
    assert content_mode.exempt(ref) is False  # adult mode is off
    content_mode.set_adult_mode(True)
    assert content_mode.exempt(ref) is True


def test_sogni_filter_for_marked_and_unmarked_cast_pictures(auth_client, monkeypatch):
    import pytest

    from app.providers.sogni import SogniClient
    from app.services import content_mode
    from app.services.safety import UnsafePromptError

    monkeypatch.setenv("SOGNI_API_KEY", "sk-test-123456789")
    _reset()
    content_mode.set_adult_mode(True)
    bodies = []

    def started(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"status": "success", "data": {"workflow": {"workflowId": "wf", "status": "queued"}}})

    media = [{"kind": "image", "url": "https://x.test/maya.png"}]
    nude = {"prompt": "a nude portrait by the window"}

    def run_all(start):
        start(title="t", tool="edit_image", arguments=nude, media_references=media)  # marked character
        start(title="t", tool="edit_image", arguments=nude, media_references=media, cast=True)  # unmarked
        start(title="t", tool="edit_image", arguments=nude, media_references=media)  # a scene or Studio result
        start(title="t", tool="edit_image", arguments={"prompt": "her portrait with the kids at the beach"},
              media_references=media)  # a child mentioned: on, marked or not
        with pytest.raises(UnsafePromptError):  # minors: refused, marked or not
            start(title="t", tool="edit_image", arguments={"prompt": "a nude teenage girl"},
                  media_references=media)

    with respx.mock() as router:
        router.post(SOGNI_WF).mock(side_effect=started)
        run_all(SogniClient().start)
        content_mode.set_adult_mode(False)
        run_all(SogniClient().start)
    assert [b["safe_content_filter"] for b in bodies] == [False, True, False, True] + [True] * 4
    _reset()


def test_runware_check_for_marked_and_unmarked_cast_pictures(client, monkeypatch):
    from app.providers import runware
    from app.providers.base import ImageRef
    from app.services import content_mode

    monkeypatch.setenv("MOCK_PROVIDERS", "false")
    monkeypatch.setenv("RUNWARE_API_KEY", "rw-test-abcdefgh1234")
    _reset()
    model = _fake_model(monkeypatch, "test:castok@1", {
        "type": "object", "additionalProperties": False, "required": ["taskType", "taskUUID", "model"],
        "properties": {**_CORE,
                       "safety": {"type": "object", "properties": {"checkContent": {"type": "boolean"}}},
                       "inputs": {"type": "object", "properties": {"referenceImages": {"type": "array", "items": _IMG}}}}},
        modes=[{"id": "text"}, {"id": "reference-images", "inputs": ["referenceImages"]}], caps=["text-to-image", "edit"])
    marked = ImageRef(url="https://x.test/maya.png", role="character", name="Maya", adult_ok=True)
    unmarked = ImageRef(url="https://x.test/real.png", role="character", name="Sam")
    prompt = "a nude portrait by the window"

    def checked(refs, text=prompt):
        return runware.build_image_task(model, text, refs)["safety"]["checkContent"]

    cases = ([marked], [unmarked], [marked, unmarked], [marked, _ref()], [_ref()])
    # Adult mode off: always checked.
    assert [checked(refs) for refs in cases] == [True] * 5
    content_mode.set_adult_mode(True)
    # On: only a picture of an unmarked character turns it on; a scene
    # picture with a sexual prompt no longer does.
    assert [checked(refs) for refs in cases] == [False, True, True, False, False]
    assert checked([marked], "her portrait with the kids in the park") is True  # a child mentioned
    assert runware._pictures_need_check([_ref()], prompt) is False
    content_mode.set_adult_mode(False)
    assert runware._pictures_need_check([_ref()], prompt) is True
    _reset()


# ---------- review follow-ups ----------


def _avatar(name="Maya", **text):
    from app import models
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        av = models.Avatar(name=name, **text)
        db.add(av)
        db.commit()
        return av.id
    finally:
        db.close()


def _tick_now(avatar_id):
    """A tick as the API leaves it once its checks pass: the card as it is
    now, and every picture on it now (pictures added later aren't covered)."""
    from app import models
    from app.db import SessionLocal
    from app.services import content_mode

    with SessionLocal() as db:
        a = db.get(models.Avatar, avatar_id)
        card, pictures = content_mode.card_hash(a), [x.id for x in content_mode.tick_pictures(a)]
    content_mode.set_avatar_adult_ok(avatar_id, True, card=card, assets=pictures)


def _asset(avatar_id, *, source="upload", job=None, asset_type="hero"):
    from app import models
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        a = models.Asset(owner_kind="avatar", avatar_id=avatar_id, asset_type=asset_type, source=source,
                         generation_job_id=job, file_path="/tmp/x.png", mime_type="image/png")
        db.add(a)
        db.commit()
        return a.id
    finally:
        db.close()


def _job(avatar_id, refs=(), first_frame_asset_id=None):
    from app import models
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        j = models.AssetGenerationJob(owner_kind="avatar", owner_id=avatar_id, output_kind="image", prompt="p",
                                      status="saved", reference_asset_ids_json=list(refs),
                                      first_frame_asset_id=first_frame_asset_id)
        db.add(j)
        db.commit()
        return j.id
    finally:
        db.close()


def _exempt_ids(asset_ids):
    from app.db import SessionLocal
    from app.services.pipeline import _asset_refs

    db = SessionLocal()
    try:
        return [r.adult_ok for r in _asset_refs(db, asset_ids)]
    finally:
        db.close()


def test_a_character_described_as_a_minor_cannot_be_marked(auth_client):
    kid = _avatar("Lily", description="a nine-year-old girl")
    r = auth_client.put(f"/api/settings/content/avatars/{kid}", json={"adult_ok": True})
    assert r.status_code == 422 and "age under 18" in r.json()["detail"]
    maya = _avatar("Maya", description="a painter in her thirties")
    pic = _asset(maya)
    assert auth_client.put(f"/api/settings/content/avatars/{maya}", json={"adult_ok": True}).status_code == 200
    assert _exempt_ids([pic]) == [True]
    r = auth_client.patch(f"/api/avatars/{maya}", json={"description": "a teenage girl"})
    assert r.status_code == 200, r.text  # not sexual: saved, but the mark goes
    assert maya not in auth_client.get("/api/settings/content").json()["adult_ok_avatars"]
    assert _exempt_ids([pic]) == [False]


def test_deleting_a_character_clears_its_mark(auth_client):
    maya = _avatar("Maya")
    auth_client.put(f"/api/settings/content/avatars/{maya}", json={"adult_ok": True})
    assert auth_client.delete(f"/api/avatars/{maya}").status_code == 204
    assert auth_client.get("/api/settings/content").json()["adult_ok_avatars"] == []
    sam = _avatar("Sam")  # SQLite may hand out the same id again
    assert sam not in auth_client.get("/api/settings/content").json()["adult_ok_avatars"]
    # Clearing a mark never 404s.
    assert auth_client.put("/api/settings/content/avatars/987654", json={"adult_ok": False}).status_code == 200


def test_every_checked_picture_on_a_marked_card_counts_however_it_was_made(auth_client):
    """The tick covers the pictures on the card the check looked at:
    uploaded, made from the character's own pictures, from someone else's,
    from a picture deleted since, or from a Studio result. Logos, other
    characters' pictures and a picture added after the tick never count."""
    from app import models
    from app.db import SessionLocal

    maya, sam = _avatar("Maya"), _avatar("Sam")
    uploaded = _asset(maya)
    hero = _asset(maya, source="generated", job=_job(maya))  # text only
    chained = _asset(maya, source="generated", job=_job(maya, refs=[hero]))
    sam_pic = _asset(sam)
    from_sam = _asset(maya, source="generated", job=_job(maya, refs=[sam_pic]))  # another character's picture
    gone = _asset(maya)
    from_gone = _asset(maya, source="generated", job=_job(maya, refs=[gone]))
    db = SessionLocal()
    try:
        studio_job = db.get(models.AssetGenerationJob, _job(maya))
        studio_job.first_frame_job_id = _job(sam)  # made from a Studio result
        db.delete(db.get(models.Asset, gone))  # the source picture was deleted
        db.commit()
        from_studio = _asset(maya, source="generated", job=studio_job.id)
    finally:
        db.close()
    logo = _asset(maya, asset_type="logo")
    _tick_now(maya)
    assert _exempt_ids([uploaded, hero, chained, from_sam, from_gone, from_studio]) == [True] * 6
    assert _exempt_ids([sam_pic, logo]) == [False, False]
    later = _asset(maya, source="generated", job=_job(maya))
    assert _exempt_ids([later]) == [False]  # not checked yet


def test_a_marked_character_whose_text_now_names_a_minor_never_counts(auth_client):
    """Even if the mark survives an edit made outside the API, a character
    whose own text names a minor gets no exemption."""
    from app import models
    from app.db import SessionLocal
    from app.services import content_mode

    maya = _avatar("Maya", description="a painter in her thirties")
    pic = _asset(maya)
    _tick_now(maya)
    assert _exempt_ids([pic]) == [True]
    db = SessionLocal()
    try:
        db.get(models.Avatar, maya).description = "a 15-year-old girl"
        db.commit()
    finally:
        db.close()
    assert maya not in content_mode.adult_ok_avatars()  # the row is still there, but it doesn't hold
    assert maya in content_mode.status()["adult_ok_stale"]
    assert _exempt_ids([pic]) == [False]


def test_animating_a_cast_picture_keeps_it_a_cast_picture(auth_client):
    """"Animate this" on a Cast picture: the first frame is that
    character's likeness, so an unmarked character keeps the filter."""
    from app import models
    from app.db import SessionLocal
    from app.services.pipeline import studio_first_frame

    maya, sam = _avatar("Maya"), _avatar("Sam")
    cases = [(owner, role, want, _asset(owner, asset_type=kind))
             for owner, kind, role, want in ((maya, "hero", "character", True), (sam, "hero", "character", False),
                                             (maya, "logo", "scene", False))]
    _tick_now(maya)
    db = SessionLocal()
    try:
        for owner, role, want, pic in cases:
            job = db.get(models.AssetGenerationJob, _job(owner, first_frame_asset_id=pic))
            ref = studio_first_frame(db, job)
            assert (ref.frame_type, ref.role, ref.adult_ok) == ("first_frame", role, want)
    finally:
        db.close()


def test_sogni_video_filter_follows_the_cast_pictures_only(auth_client, monkeypatch):
    from app import models
    from app.db import SessionLocal
    from app.providers.base import ImageRef
    from app.providers.sogni import SogniVideoProvider
    from app.services import content_mode
    from app.services.pipeline import studio_first_frame

    monkeypatch.setenv("SOGNI_API_KEY", "sk-test-123456789")
    monkeypatch.setenv("MOCK_PROVIDERS", "false")
    _reset()
    bodies = []

    def started(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"status": "success", "data": {"workflow": {"workflowId": "wf", "status": "queued"}}})

    maya, sam = _avatar("Maya"), _avatar("Sam")
    pics = {owner: _asset(owner) for owner in (maya, sam)}
    _tick_now(maya)
    db = SessionLocal()
    try:
        first = {}
        for owner in (maya, sam):
            job = db.get(models.AssetGenerationJob, _job(owner, first_frame_asset_id=pics[owner]))
            first[owner] = studio_first_frame(db, job)
    finally:
        db.close()
    marked = ImageRef(url="https://x.test/maya.png", role="character", name="Maya", adult_ok=True)
    unmarked = ImageRef(url="https://x.test/sam.png", role="character", name="Sam")
    scene = ImageRef(url="https://x.test/room.png", role="scene", name="room")
    studio_result = ImageRef(url="https://x.test/result.png", role="scene", name="first frame",
                             frame_type="first_frame")
    loose = ([marked], [marked, scene], [scene], [unmarked], [marked, unmarked])
    frames = ([first[maya]], [first[sam]], [studio_result])
    nude = "a nude portrait by the window"

    def submit_all():
        for refs in loose:
            SogniVideoProvider().submit(model="sogni/minimax-h3-r2v", prompt=nude, references=refs,
                                        duration_seconds=5)
        for refs in frames:
            SogniVideoProvider().submit(model="sogni/wan3.0-video", prompt=nude, references=refs,
                                        duration_seconds=10, reference_strategy="frame_images")
        SogniVideoProvider().submit(model="sogni/wan3.0-video", prompt="she waves at the kids", references=frames[0],
                                    duration_seconds=10, reference_strategy="frame_images")

    with respx.mock() as router:
        router.post(SOGNI_WF).mock(side_effect=started)
        content_mode.set_adult_mode(True)
        submit_all()
        content_mode.set_adult_mode(False)
        submit_all()
    assert all(b.get("media_references") for b in bodies)
    on = [b["safe_content_filter"] for b in bodies[:9]]
    assert on == [False, False, False, True, True,  # loose: only an unmarked character turns it on
                  False, True, False,  # first frames: marked Cast, unmarked Cast, a Studio result
                  True]  # a child mentioned
    assert [b["safe_content_filter"] for b in bodies[9:]] == [True] * 9  # adult mode off
    _reset()


def test_a_cutout_or_upscale_on_a_marked_card_counts_whatever_its_source(auth_client):
    """Remove background / upscale results saved to a marked character's
    card count like any other picture there, whatever they were made from."""
    from app import models
    from app.db import SessionLocal

    maya, sam = _avatar("Maya"), _avatar("Sam")
    own, sam_pic = _asset(maya), _asset(sam)

    def op_job(owner=maya, **source):
        db = SessionLocal()
        try:
            j = models.AssetGenerationJob(owner_kind="avatar", owner_id=owner, output_kind="image", prompt="",
                                          status="saved", operation="upscale", options_json={"factor": 2},
                                          **source)
            db.add(j)
            db.commit()
            return j.id
        finally:
            db.close()

    from_own = _asset(maya, source="generated", job=op_job(source_asset_id=own))
    from_sam = _asset(maya, source="generated", job=op_job(source_asset_id=sam_pic))
    from_studio = _asset(maya, source="generated", job=op_job(source_job_id=_job(sam)))
    no_source = _asset(maya, source="generated", job=op_job())
    on_sams_card = _asset(sam, source="generated", job=op_job(sam, source_asset_id=own))
    _tick_now(maya)
    assert _exempt_ids([from_own, from_sam, from_studio, no_source]) == [True] * 4
    assert _exempt_ids([on_sams_card]) == [False]  # Sam isn't marked


# ---------- second review ----------


def _op_job(owner, tmp_path, *, operation="upscale", **source):
    """A finished cutout or upscale (or, with operation="", a picture made
    in Studio) with a real result file."""
    from app import models
    from app.db import SessionLocal

    from .conftest import make_png_bytes

    path = tmp_path / f"op-{owner}-{len(list(tmp_path.iterdir()))}.png"
    path.write_bytes(make_png_bytes((1024, 1024)))
    db = SessionLocal()
    try:
        j = models.AssetGenerationJob(owner_kind="avatar", owner_id=owner, output_kind="image", prompt="p",
                                      status="completed", operation=operation, result_path=str(path),
                                      reference_asset_ids_json=[], **source)
        db.add(j)
        db.commit()
        return j.id
    finally:
        db.close()


def _sogni_video_filters(monkeypatch, frames, prompt="a nude portrait by the window"):
    """safe_content_filter of a Sogni clip started from each set of frames."""
    from app.providers.sogni import SogniVideoProvider

    monkeypatch.setenv("SOGNI_API_KEY", "sk-test-123456789")
    monkeypatch.setenv("MOCK_PROVIDERS", "false")
    _reset()
    bodies = []

    def started(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"status": "success", "data": {"workflow": {"workflowId": "wf", "status": "queued"}}})

    with respx.mock() as router:
        router.post(SOGNI_WF).mock(side_effect=started)
        for refs in frames:
            SogniVideoProvider().submit(model="sogni/wan3.0-video", prompt=prompt, references=refs,
                                        duration_seconds=10, reference_strategy="frame_images")
    assert all(b.get("media_references") for b in bodies)
    _reset()
    return [b["safe_content_filter"] for b in bodies]


def test_animating_a_cutout_or_upscale_of_a_cast_picture_keeps_it_theirs(auth_client, monkeypatch, tmp_path):
    """"Animate this" on an upscale or cutout of a Cast picture (even of
    another cutout, or of a picture deleted since) is still that
    character's picture: an unticked character keeps the filter. A picture
    newly made in Studio is not a Cast picture."""
    from app import models
    from app.db import SessionLocal
    from app.services import content_mode
    from app.services.pipeline import studio_first_frame

    maya, sam = _avatar("Maya"), _avatar("Sam")
    maya_pic = _asset(maya)
    _tick_now(maya)
    up_sam = _op_job(sam, tmp_path, source_asset_id=_asset(sam))
    cut_of_up = _op_job(sam, tmp_path, operation="remove_background", source_job_id=up_sam)
    up_maya = _op_job(maya, tmp_path, source_asset_id=maya_pic)
    up_unchecked = _op_job(maya, tmp_path, source_asset_id=_asset(maya))  # a picture added after the tick
    source_gone = _op_job(sam, tmp_path)  # its Cast picture was deleted since
    logo = _op_job(sam, tmp_path, source_asset_id=_asset(sam, asset_type="logo"))
    made = _op_job(sam, tmp_path, operation="")  # a new Studio picture
    cases = ((up_sam, "character", False), (cut_of_up, "character", False), (up_maya, "character", True),
             (source_gone, "character", False), (logo, "scene", False), (made, "scene", False),
             (up_unchecked, "character", False))
    db = SessionLocal()
    try:
        frames = []
        for src, role, ok in cases:
            job = models.AssetGenerationJob(owner_kind="avatar", owner_id=sam, output_kind="video_clip",
                                            prompt="p", status="pending", first_frame_job_id=src)
            ref = studio_first_frame(db, job)
            assert (ref.frame_type, ref.role, ref.adult_ok) == ("first_frame", role, ok), src
            frames.append([ref])
    finally:
        db.close()
    content_mode.set_adult_mode(True)
    assert _sogni_video_filters(monkeypatch, frames) == [True, True, False, True, False, False, True]


def test_an_ambient_loop_made_from_a_cast_picture_keeps_that_characters_filter(auth_client, monkeypatch, tmp_path):
    """Ambient: a Cast character's picture chosen as the scene's picture
    (or an upscale of one) counts as that character's picture on every
    frame and still made from it, so an unticked character keeps the
    filter. The tick is read when each clip is sent."""
    from app import models
    from app.db import SessionLocal
    from app.providers import mock_image, mock_video
    from app.services import ambient, content_mode

    from .conftest import make_png_bytes
    from .test_ambient import SHORT

    maya, sam = _avatar("Maya"), _avatar("Sam")

    def cast_picture(avatar_id):
        up = auth_client.post(f"/api/avatars/{avatar_id}/assets",
                              files={"file": ("h.png", make_png_bytes((1024, 1024)), "image/png")},
                              data={"asset_type": "hero", "rights_confirmed": "true"}).json()
        return up["id"] if "id" in up else up["assets"][0]["id"]

    sam_pic, maya_pic = cast_picture(sam), cast_picture(maya)
    _tick_now(maya)
    sent: list = []  # the reference pictures of every clip and still
    for cls, name in ((mock_video.MockVideoProvider, "submit"), (mock_image.MockImageProvider, "generate")):
        def spy(self, real=getattr(cls, name), **kw):
            sent.append(list(kw["references"]))
            return real(self, **kw)

        monkeypatch.setattr(cls, name, spy)

    def loop(picture: dict, **cfg) -> tuple[int, list]:
        sid = auth_client.post("/api/ambient", json={
            "config": {**SHORT, "music_mood": "none", "variety": "day_night", "variations": 2, **cfg},
            "generate": False}).json()["id"]
        if picture:
            assert auth_client.post(f"/api/ambient/{sid}/picture/from", json=picture).status_code == 200
        else:
            auth_client.post(f"/api/ambient/{sid}/picture",
                             files={"file": ("p.png", make_png_bytes((1920, 1080)), "image/png")})
        sent.clear()
        auth_client.post(f"/api/ambient/{sid}/generate-loop")
        assert auth_client.get(f"/api/ambient/{sid}").json()["loop_status"] == "completed"
        refs = [r for refs in sent for r in refs]
        assert refs and all(r.role == "scene" for r in refs)  # still sent as the scene and its frames
        return sid, refs

    sid, sam_refs = loop({"asset_id": sam_pic})
    assert any(r.frame_type == "first_frame" for r in sam_refs) and any(r.frame_type is None for r in sam_refs)
    assert all(r.cast_picture and not r.adult_ok for r in sam_refs)
    _, maya_refs = loop({"asset_id": maya_pic})
    assert all(r.cast_picture and r.adult_ok for r in maya_refs)
    _, drift_refs = loop({"asset_id": sam_pic}, source="image_drift")  # the night still, from the picture
    assert len(drift_refs) == 1 and drift_refs[0].cast_picture and not drift_refs[0].adult_ok
    _, up_refs = loop({"studio_job_id": _op_job(sam, tmp_path, source_asset_id=sam_pic)})
    assert all(r.cast_picture and not r.adult_ok for r in up_refs)
    for picture in ({}, {"studio_job_id": _op_job(sam, tmp_path, operation="")}):  # upload, a new Studio picture
        _, refs = loop(picture)
        assert not any(r.cast_picture or r.adult_ok for r in refs)

    # Ticking Sam afterwards applies to the next clip sent.
    _tick_now(sam)
    db = SessionLocal()
    try:
        project = db.get(models.VideoProject, sid)
        assert ambient._picture_mark(db, project) == {"cast_picture": True, "adult_ok": True}
        # A record from before ticks covered pictures one by one: the filter stays on.
        project.ambient_config_json = {**project.ambient_config_json, "_picture_cast": {"avatar_id": sam}}
        assert ambient._picture_mark(db, project) == {"cast_picture": True, "adult_ok": False}
    finally:
        db.close()
    content_mode.set_avatar_adult_ok(sam, False)

    content_mode.set_adult_mode(True)
    loops = [[r for r in refs if r.frame_type] for refs in (sam_refs, maya_refs)]
    assert _sogni_video_filters(monkeypatch, loops) == [True, False]

"""A clean deployment (ADULT_FEATURES=false) keeps explicit content out of
three more places, and a deployment with adult features (ADULT_FEATURES
unset) behaves exactly as before, with no new check ever asked:

1. the planner can't be a model of the adult text models' family published
   under another vendor ("tngtech/deepseek-..."), while the Cast picture
   check keeps its model;
2. every prompt sent to a picture or video model gets the explicit-content
   check (one place: minor_check.check_generation, run at the provider and
   in the early API checks), a 422 before anything is paid for;
3. recordings of the owner's own (a voiceover, a music bed, an uploaded
   video's sound) aren't taken, since what they say can't be checked there.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from .test_adult_features_off import _clean, _real, _reset
from .test_image_ops import _avatar
from .test_image_ops import _upload as _add_picture

_EXPLICIT = "a couple having sex on the bed, fully naked"
_PLAIN = "a slow push-in on a quiet rooftop at golden hour"


def _default(monkeypatch):
    monkeypatch.delenv("ADULT_FEATURES", raising=False)
    _reset()


@pytest.fixture
def spied(monkeypatch):
    """Every way into the new prompt check, counted (check_prompt and the
    API helper), and the content check's model call itself (_check_part)."""
    from app.services import explicit_check

    calls: list[str] = []
    for name in ("check_prompt", "check_prompts_or_422", "_check_part"):
        real = getattr(explicit_check, name)

        def spy(*a, _real=real, _name=name, **kw):
            calls.append(_name)
            return _real(*a, **kw)

        monkeypatch.setattr(explicit_check, name, spy)
    return calls


@pytest.fixture
def paid(monkeypatch):
    """The provider calls that cost money (mock adapters), counted."""
    from app.providers import mock_image, mock_video

    calls: list[str] = []
    real_image = mock_image.MockImageProvider.generate
    real_video = type(mock_video.get_singleton()).submit

    def image(self, *a, **kw):
        calls.append("image")
        return real_image(self, *a, **kw)

    def video(self, *a, **kw):
        calls.append("video")
        return real_video(self, *a, **kw)

    monkeypatch.setattr(mock_image.MockImageProvider, "generate", image)
    monkeypatch.setattr(type(mock_video.get_singleton()), "submit", video)
    return calls


def _jobs() -> int:
    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        return db.query(models.AssetGenerationJob).count()


def _set(model_name: str, row_id: int, **values) -> None:
    """Write a row directly (as data saved before the switch would be)."""
    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        row = db.get(getattr(models, model_name), row_id)
        for k, v in values.items():
            setattr(row, k, v)
        db.commit()


def _refused(r) -> None:
    from app.services import explicit_check

    assert r.status_code == 422, r.text
    assert r.json()["detail"].startswith(explicit_check.PROMPT_REFUSED), r.json()["detail"]


# ---------- 1. the adult text family under any vendor ----------


@pytest.mark.parametrize("model", [
    "tngtech/deepseek-r1t2-chimera",
    "tngtech/DeepSeek-R1T2-Chimera:free",
    "nousresearch/deepseek-r1-distill-llama-70b",
    "~deepseek/deepseek-chat",
    "someone/qwen-deepseek-merge:nitro",
    "deepseek/deepseek-v4.1-flash",
])
def test_clean_planner_refuses_the_adult_family_under_any_vendor(monkeypatch, model):
    from app.services import model_registry
    from app.services.provider_keys import ProviderUnavailable

    _real(monkeypatch)
    assert not model_registry.adult_only(model, "chat")  # adult features on: as today
    assert model_registry.route_info("chat", model)[0] == "openrouter"
    _clean(monkeypatch)
    assert model_registry.adult_only(model, "chat")
    with pytest.raises(ProviderUnavailable, match="turned off for this deployment"):
        model_registry.require("chat", model)
    assert not model_registry.adult_only(model, "image")  # text work only


@pytest.mark.parametrize("model", ["openai/gpt-4o-mini", "meta-llama/llama-3.1-70b-instruct:nitro",
                                   "anthropic/some-chat", "google/gemini-2.5-flash"])
def test_clean_planner_keeps_other_families(monkeypatch, model):
    from app.services import model_registry

    _real(monkeypatch)
    _clean(monkeypatch)
    assert not model_registry.adult_only(model, "chat")
    assert model_registry.route_info("chat", model)[0] == "openrouter"


def test_clean_family_follows_the_configured_adult_models(monkeypatch):
    """Another adult text family named in the settings is matched the same way."""
    from app.services import content_mode, model_registry

    _real(monkeypatch)
    monkeypatch.setenv("ADULT_TEXT_FALLBACK_MODEL", "mistralai/mistral-large")
    _clean(monkeypatch)
    assert {"deepseek", "mistral"} <= content_mode.adult_text_family_names()
    for model in ("thedrummer/mistral-nemo-finetune", "MistralAI/Mistral-Small:free", "tngtech/deepseek-x"):
        assert model_registry.adult_only(model, "chat"), model
    assert not model_registry.adult_only("openai/gpt-4o-mini", "chat")


def test_clean_picture_check_keeps_its_model(auth_client, monkeypatch):
    from app.services import minor_check

    _real(monkeypatch)
    _clean(monkeypatch)
    assert minor_check.picture_model() == "deepseek/deepseek-v4.1-flash"
    assert minor_check._picture_route() == ("openrouter", "deepseek/deepseek-v4.1-flash")
    assert auth_client.get("/api/settings/content").json()["safety_check"]["picture_available"] is True


# ---------- 2. picture and video prompts ----------


def test_clean_guarded_providers_refuse_an_explicit_prompt_before_the_call(monkeypatch, paid):
    from app.providers import get_image, get_video
    from app.services import explicit_check
    from app.services.safety import UnsafePromptError

    _clean(monkeypatch)
    with pytest.raises(explicit_check.ExplicitPromptError) as e:
        get_image().generate(model="mock", prompt=_EXPLICIT, references=[])
    assert isinstance(e.value, UnsafePromptError) and str(e.value) == explicit_check.PROMPT_REFUSED
    with pytest.raises(explicit_check.ExplicitPromptError):
        get_video().submit(model="mock", prompt=_EXPLICIT, references=[])
    assert paid == []
    # The avoid list names what to keep out: never judged.
    get_image().generate(model="mock", prompt=_PLAIN, references=[], negative_prompt="nudity, sex, naked")
    assert paid == ["image"]


def test_clean_check_fails_closed_for_prompts(auth_client, monkeypatch, paid):
    from app.providers import mock_chat
    from app.services import explicit_check

    _clean(monkeypatch)
    aid = _avatar(auth_client)
    mock_chat.EXPLICIT_REPLY = RuntimeError("down")
    r = auth_client.post("/api/studio/generate-image", json={"owner_kind": "avatar", "owner_id": aid,
                                                             "prompt": _PLAIN})
    assert r.status_code == 422 and r.json()["detail"] == explicit_check.PROMPT_UNAVAILABLE
    assert paid == []


def test_clean_studio_refuses_explicit_prompts(auth_client, monkeypatch, paid):
    _clean(monkeypatch)
    aid = _avatar(auth_client)
    before = _jobs()
    for path in ("generate-image", "generate-clip"):
        _refused(auth_client.post(f"/api/studio/{path}", json={"owner_kind": "avatar", "owner_id": aid,
                                                               "prompt": _EXPLICIT}))
    # Character sheet: its style words go into every view's prompt.
    _add_picture(auth_client, aid)
    _refused(auth_client.post("/api/studio/character-sheet",
                              json={"avatar_id": aid, "views": ["front"], "style": "fully naked, having sex"}))
    assert _jobs() == before and paid == []
    # A plain prompt is made as usual.
    r = auth_client.post("/api/studio/generate-image", json={"owner_kind": "avatar", "owner_id": aid,
                                                             "prompt": _PLAIN})
    assert r.status_code == 202 and paid == ["image"]


def test_clean_retry_and_continue_refuse_an_explicit_prompt(auth_client, monkeypatch, paid):
    from .test_continue_clip import _clip

    _clean(monkeypatch)
    aid = _avatar(auth_client)
    clip = _clip(auth_client, aid, prompt=_PLAIN)
    paid.clear()
    before = _jobs()
    _refused(auth_client.post(f"/api/studio/jobs/{clip['id']}/retry", json={"prompt": _EXPLICIT}))
    _refused(auth_client.post(f"/api/studio/jobs/{clip['id']}/continue", json={"prompt": _EXPLICIT}))
    assert _jobs() == before and paid == []  # not even the continuation's frame is kept


def test_clean_storyboard_refuses_explicit_shot_prompts(auth_client, monkeypatch, paid):
    from .test_plan_edit import _planned

    _clean(monkeypatch)
    pid = _planned(auth_client)
    shots = [s for s in auth_client.get(f"/api/projects/{pid}").json()["shots"] if s["shot_type"] != "end_card"]
    sid = shots[0]["id"]
    _refused(auth_client.patch(f"/api/projects/{pid}/shots/{sid}", json={"prompt": _EXPLICIT}))
    _refused(auth_client.post(f"/api/projects/{pid}/shots", json={"prompt": _EXPLICIT, "shot_type": "b_roll",
                                                                  "duration_seconds": 3}))
    # One saved before the switch: refused at render and re-roll, before
    # the voiceover or any clip is paid for.
    _set("VideoShot", sid, prompt=_EXPLICIT)
    paid.clear()
    _refused(auth_client.post(f"/api/projects/{pid}/generate-video", params={"force": True}))
    _refused(auth_client.post(f"/api/projects/{pid}/shots/{sid}/regenerate"))
    _refused(auth_client.post(f"/api/projects/{pid}/shots/regenerate-bulk", json={"shot_ids": [sid]}))
    assert paid == []
    status = auth_client.get(f"/api/projects/{pid}").json()["status"]
    assert status != "generating"


def test_clean_storyboard_plan_with_an_explicit_shot_prompt_is_refused(auth_client, monkeypatch):
    """The planner's shot prompts are checked when the plan comes back."""
    from app.schemas.storyboard import StoryboardPlan
    from app.services import explicit_check, pipeline

    from .test_plan_edit import _planned

    _clean(monkeypatch)
    data = auth_client.get(f"/api/projects/{_planned(auth_client)}").json()["generated_plan_json"]
    pipeline._check_plan(StoryboardPlan.model_validate(data))
    data["shots"][0]["visual_prompt"] = _EXPLICIT
    with pytest.raises(explicit_check.ExplicitPromptError):
        pipeline._check_plan(StoryboardPlan.model_validate(data))


def test_clean_ambient_refuses_explicit_scene_text(auth_client, monkeypatch, paid):
    from .test_ambient import SHORT

    _clean(monkeypatch)
    _refused(auth_client.post("/api/ambient", json={"config": {**SHORT, "scene_prompt": _EXPLICIT},
                                                    "generate": False}))
    sid = auth_client.post("/api/ambient", json={"config": SHORT, "generate": False}).json()["id"]
    _refused(auth_client.patch(f"/api/ambient/{sid}", json={"config": {**SHORT, "scene_prompt": _EXPLICIT}}))
    # Saved before the switch: refused before the picture or loop is made.
    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        p = db.get(models.VideoProject, sid)
        p.ambient_config_json = {**(p.ambient_config_json or {}), "scene_prompt": _EXPLICIT}
        db.commit()
    _refused(auth_client.post(f"/api/ambient/{sid}/picture/generate"))
    _refused(auth_client.post(f"/api/ambient/{sid}/generate-loop"))
    assert paid == []


def test_clean_hook_lab_refuses_an_explicit_shot_one(auth_client, monkeypatch, paid):
    from .test_hook_lab import _base, _create

    _clean(monkeypatch)
    pid, base_id = _base(auth_client)
    paid.clear()
    r = auth_client.post(f"/api/projects/{pid}/hook-lab/variants", json={
        "base_render_id": base_id, "window_seconds": 2.5,
        "variants": [{"hook_text": "Why keep scrolling?", "style": "question", "shot1_source": "new",
                      "shot1_prompt": _EXPLICIT}]})
    _refused(r)
    made = _create(auth_client, pid, base_id, [{"hook_text": "Why keep scrolling?", "style": "question"}])
    vid = made["variants"][0]["id"]
    _refused(auth_client.patch(f"/api/hook-variants/{vid}", json={"shot1_source": "new",
                                                                  "shot1_prompt": _EXPLICIT}))
    # Saved before the switch: refused by Make, before anything is paid for.
    _set("HookVariant", vid, shot1_source="new", shot1_prompt=_EXPLICIT)
    _refused(auth_client.post(f"/api/projects/{pid}/hook-lab/make", json={"variant_ids": [vid]}))
    assert paid == []


@pytest.mark.parametrize("kind", ["image", "video", "scene"])
def test_clean_enhance_refuses_an_explicit_picture_or_clip_rewrite(auth_client, monkeypatch, kind):
    from app.providers.mock_chat import MockChatProvider

    _clean(monkeypatch)
    monkeypatch.setattr(MockChatProvider, "enhance_prompt", lambda self, text, kind, model="": _EXPLICIT)
    r = auth_client.post("/api/prompts/enhance", json={"text": "two people at home", "kind": kind})
    assert r.status_code == 422, r.text
    assert "general audience" in r.json()["detail"] and "unchanged" in r.json()["detail"]


# ---------- 2. default: nothing new is asked ----------


def test_default_never_runs_the_prompt_check(auth_client, monkeypatch, spied):
    """ADULT_FEATURES unset: the same explicit prompts are made as before,
    and no way into the explicit-content check is ever taken."""
    from app.providers import get_image, get_video, mock_chat
    from app.providers.mock_chat import MockChatProvider

    from .test_ambient import SHORT
    from .test_continue_clip import _clip
    from .test_plan_edit import _planned

    _default(monkeypatch)
    mock_chat.EXPLICIT_REPLY = RuntimeError("must not be asked")
    aid = _avatar(auth_client)
    get_image().generate(model="mock", prompt=_EXPLICIT, references=[])
    get_video().submit(model="mock", prompt=_EXPLICIT, references=[])
    for path in ("generate-image", "generate-clip"):
        r = auth_client.post(f"/api/studio/{path}", json={"owner_kind": "avatar", "owner_id": aid,
                                                          "prompt": _EXPLICIT})
        assert r.status_code == 202, r.text
    _add_picture(auth_client, aid)
    r = auth_client.post("/api/studio/character-sheet",
                         json={"avatar_id": aid, "views": ["front"], "style": "fully naked"})
    assert r.status_code == 202, r.text
    clip = _clip(auth_client, aid, prompt=_PLAIN)
    assert auth_client.post(f"/api/studio/jobs/{clip['id']}/retry", json={"prompt": _EXPLICIT}).status_code == 202
    assert auth_client.post(f"/api/studio/jobs/{clip['id']}/continue",
                            json={"prompt": _EXPLICIT}).status_code == 202
    pid = _planned(auth_client)
    sid = [s for s in auth_client.get(f"/api/projects/{pid}").json()["shots"] if s["shot_type"] != "end_card"][0]["id"]
    assert auth_client.patch(f"/api/projects/{pid}/shots/{sid}", json={"prompt": _EXPLICIT}).status_code == 200
    assert auth_client.post(f"/api/projects/{pid}/shots/{sid}/regenerate").status_code == 202
    assert auth_client.post(f"/api/projects/{pid}/generate-video", params={"force": True}).status_code == 202
    r = auth_client.post("/api/ambient", json={"config": {**SHORT, "scene_prompt": _EXPLICIT}, "generate": False})
    assert r.status_code == 201, r.text
    assert auth_client.post(f"/api/ambient/{r.json()['id']}/generate-loop").status_code == 202
    monkeypatch.setattr(MockChatProvider, "enhance_prompt", lambda self, text, kind, model="": _EXPLICIT)
    assert auth_client.post("/api/prompts/enhance", json={"text": "two people", "kind": "image"}).status_code == 200
    assert spied == [] and mock_chat.EXPLICIT_CALLS == []


def test_default_hook_lab_shot_one_never_runs_the_prompt_check(auth_client, monkeypatch, spied):
    from app.providers import mock_chat

    from .test_hook_lab import _base, _create

    _default(monkeypatch)
    mock_chat.EXPLICIT_REPLY = RuntimeError("must not be asked")
    pid, base_id = _base(auth_client)
    made = _create(auth_client, pid, base_id, [{"hook_text": "Why keep scrolling?", "style": "question",
                                                "shot1_source": "new", "shot1_prompt": _EXPLICIT}])
    vid = made["variants"][0]["id"]
    assert auth_client.patch(f"/api/hook-variants/{vid}", json={"shot1_prompt": _EXPLICIT}).status_code == 200
    assert spied == [] and mock_chat.EXPLICIT_CALLS == []


# ---------- 3. recordings of the owner's own ----------


def _wav() -> bytes:
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\0\0" * 8000)
    return buf.getvalue()


def _project(auth_client) -> int:
    return auth_client.post("/api/projects", json={"title": "T", "original_script": "one two three"}).json()["id"]


def test_clean_refuses_own_voiceover_and_music(auth_client, monkeypatch, spied):
    from app.services import content_mode

    _clean(monkeypatch)
    pid = _project(auth_client)
    r = auth_client.post(f"/api/projects/{pid}/voiceover", files={"file": ("v.wav", _wav(), "audio/wav")})
    assert r.status_code == 422 and r.json()["detail"] == content_mode.VOICEOVER_UPLOAD_OFF
    assert r.json()["detail"] == "Uploading your own voiceover is off on this server; use a generated voice."
    r = auth_client.post(f"/api/projects/{pid}/music", files={"file": ("m.wav", _wav(), "audio/wav")})
    assert r.status_code == 422 and r.json()["detail"] == content_mode.MUSIC_UPLOAD_OFF
    r = auth_client.patch(f"/api/projects/{pid}", json={"voiceover_source": "upload"})
    assert r.status_code == 422 and r.json()["detail"] == content_mode.VOICEOVER_UPLOAD_OFF
    p = auth_client.get(f"/api/projects/{pid}").json()
    assert p["voiceover_upload_path"] == "" and p["music_upload_path"] == "" and p["voiceover_source"] == "tts"
    assert spied == []  # nothing to transcribe or ask: refused outright
    # Generated music is still made (its prompt checked).
    r = auth_client.post(f"/api/projects/{pid}/music/generate", json={"prompt": "soft piano", "duration_seconds": 5})
    assert r.status_code == 200 and r.json()["music_upload_path"]
    assert "check_prompt" in spied


def test_clean_refuses_a_voiceover_uploaded_before_the_switch(auth_client, monkeypatch):
    from app import models
    from app.db import SessionLocal
    from app.schemas.storyboard import StoryboardPlan
    from app.services import content_mode, pipeline

    from .test_plan_edit import _planned

    _default(monkeypatch)
    pid = _planned(auth_client)
    r = auth_client.post(f"/api/projects/{pid}/voiceover", files={"file": ("v.wav", _wav(), "audio/wav")})
    assert r.status_code == 200 and r.json()["voiceover_source"] == "upload"
    _clean(monkeypatch)
    r = auth_client.post(f"/api/projects/{pid}/generate-video", params={"force": True})
    assert r.status_code == 422 and r.json()["detail"] == content_mode.VOICEOVER_UPLOAD_OFF
    with SessionLocal() as db:
        project = db.get(models.VideoProject, pid)
        plan = StoryboardPlan.model_validate(project.generated_plan_json)
        with pytest.raises(pipeline.VoiceoverFailed, match="off on this server"):
            pipeline.generate_audio(db, project, plan=plan)
    # Removing it (back to a generated voice) is allowed.
    assert auth_client.delete(f"/api/projects/{pid}/voiceover").status_code == 200


def test_default_keeps_own_voiceover_and_music(auth_client, monkeypatch, spied):
    _default(monkeypatch)
    pid = _project(auth_client)
    r = auth_client.post(f"/api/projects/{pid}/voiceover", files={"file": ("v.wav", _wav(), "audio/wav")})
    assert r.status_code == 200 and r.json()["voiceover_source"] == "upload"
    r = auth_client.post(f"/api/projects/{pid}/music", files={"file": ("m.wav", _wav(), "audio/wav")})
    assert r.status_code == 200 and r.json()["music_upload_path"]
    assert spied == []


@pytest.mark.parametrize("clean", [False, True])
def test_uploaded_video_keeps_its_sound_only_with_adult_features(auth_client, monkeypatch, tmp_path: Path,
                                                                 spied, clean):
    from .test_upload_video import _codecs, _upload, _video

    if clean:
        _clean(monkeypatch)
    else:
        _default(monkeypatch)
    aid = _avatar(auth_client)
    r = _upload(auth_client, _video(tmp_path / "talk.mp4", audio=True), aid)
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["options"]["has_audio"] is (not clean)
    assert ("audio" in _codecs(job["result_path"])) is (not clean)
    if not clean:
        assert spied == []

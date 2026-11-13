"""Studio clips ask for sound: the choice on the request, kept on the job
(Retry / Try another model reuse it), sent to each provider the way it
takes it, priced with sound where that costs more. Storyboard shots,
ambient segments and hook-lab clips stay silent (the renderer lays in
voice and music)."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from .conftest import make_png_bytes
from .test_runware import live  # noqa: F401 — fixture
from .test_sogni import WF, _started, live_sogni  # noqa: F401 — fixture

OR_BASE = "https://openrouter.ai/api/v1"


def _avatar(client) -> int:
    return client.post("/api/avatars", json={"name": "Mia"}).json()["id"]


def _clip_job(db, model: str, options=None) -> int:
    from app import models

    owner = models.Avatar(name="Mia")
    db.add(owner)
    db.flush()
    job = models.AssetGenerationJob(owner_kind="avatar", owner_id=owner.id, output_kind="video_clip",
                                    prompt="She laughs at the waves.", provider_model=model,
                                    duration_seconds=5.0, options_json=options, status="pending")
    db.add(job)
    db.commit()
    return job.id


# ---------- the choice on the job ----------


def test_a_clip_asks_for_sound_unless_turned_off(auth_client, monkeypatch):
    from app.api import studio as studio_api

    monkeypatch.setattr(studio_api, "enqueue", lambda *a, **k: "q")
    aid = _avatar(auth_client)
    body = {"owner_kind": "avatar", "owner_id": aid, "prompt": "a walk on the beach"}
    on = auth_client.post("/api/studio/generate-clip", json=body).json()
    assert (on["options"], on["audio"]) == ({"audio": True}, True)
    off = auth_client.post("/api/studio/generate-clip", json={**body, "audio": False}).json()
    assert (off["options"], off["audio"]) == ({"audio": False}, False)
    assert auth_client.get(f"/api/studio/jobs/{off['id']}").json()["audio"] is False
    # Pictures have no sound: nothing stored.
    pic = auth_client.post("/api/studio/generate-image", json={**body, "audio": False}).json()
    assert (pic["options"], pic["audio"]) == ({}, None)


def test_retry_and_try_another_model_keep_the_choice(auth_client, monkeypatch):
    from app import models
    from app.api import studio as studio_api
    from app.db import SessionLocal

    monkeypatch.setattr(studio_api, "enqueue", lambda *a, **k: "q")
    with SessionLocal() as db:
        silent = _clip_job(db, "mock/video-default", {"audio": False, "note": "kept"})
        old = _clip_job(db, "mock/video-default")  # made before the choice existed
    again = auth_client.post(f"/api/studio/jobs/{silent}/retry", json={}).json()
    assert again["options"] == {"audio": False}
    other = auth_client.post(f"/api/studio/jobs/{silent}/retry", json={"model": "sogni/ltx25"}).json()
    assert (other["provider_model"], other["audio"]) == ("sogni/ltx25", False)
    loud = auth_client.post(f"/api/studio/jobs/{silent}/retry", json={"audio": True}).json()
    assert loud["audio"] is True
    assert auth_client.post(f"/api/studio/jobs/{old}/retry", json={}).json()["audio"] is True
    with SessionLocal() as db:  # the source job is untouched
        assert db.get(models.AssetGenerationJob, silent).options_json == {"audio": False, "note": "kept"}


def test_the_choice_merges_into_the_jobs_options():
    from types import SimpleNamespace

    from app.services import pipeline

    assert pipeline.options_with_audio({"ran_on": {"model": "x"}}, False) == {"ran_on": {"model": "x"},
                                                                              "audio": False}
    assert pipeline.options_with_audio(None, True) == {"audio": True}
    assert pipeline.studio_clip_audio(SimpleNamespace(options_json=None)) is True
    assert pipeline.studio_clip_audio(SimpleNamespace(options_json={"audio": False})) is False
    assert pipeline.studio_clip_audio(SimpleNamespace(options_json={"audio": "no"})) is True


def test_studio_clip_sends_the_choice_to_the_provider(auth_client, monkeypatch):
    from app import models
    from app.db import SessionLocal
    from app.providers import mock_video

    calls = []
    real = mock_video.MockVideoProvider.submit
    monkeypatch.setattr(mock_video.MockVideoProvider, "submit", lambda self, **kw: calls.append(kw) or real(self, **kw))
    aid = _avatar(auth_client)
    body = {"owner_kind": "avatar", "owner_id": aid, "prompt": "a walk on the beach"}
    on = auth_client.post("/api/studio/generate-clip", json=body).json()["id"]
    off = auth_client.post("/api/studio/generate-clip", json={**body, "audio": False}).json()["id"]
    assert [c["settings"]["generate_audio"] for c in calls] == [True, False]
    with SessionLocal() as db:
        ctx = {j.ref_id: j.context_json for j in db.query(models.VideoJob).filter_by(purpose="studio")}
    assert ctx[on]["audio"] is True and ctx[off]["audio"] is False


# ---------- Sogni ----------


def test_sogni_studio_clip_sends_generate_audio(auth_client, live_sogni):  # noqa: F811
    from app.db import SessionLocal
    from app.services import pipeline

    def args_for(model, options=None):
        with SessionLocal() as db:
            jid = _clip_job(db, model, options)
            with respx.mock() as router:
                start = router.post(WF).mock(side_effect=_started)
                pipeline.run_studio_job(db, jid)
        return json.loads(start.calls[0].request.content)["input"]["steps"][0]["arguments"]

    assert args_for("sogni/wan3.0-video")["generateAudio"] is True  # on by default
    assert args_for("sogni/wan3.0-video", {"audio": False})["generateAudio"] is False
    assert args_for("sogni/ltx25", {"audio": True})["generateAudio"] is True
    assert args_for("sogni/wan3.0-spicy-video")["generateAudio"] is True  # the clip that had no sound
    # Models that take no generateAudio get none, whatever the choice.
    assert "generateAudio" not in args_for("sogni/happyhorse-1.1-t2v")
    assert "generateAudio" not in args_for("sogni/wan22", {"audio": False})


# ---------- OpenRouter ----------

MODELS = {"data": [
    {"id": "google/veo-3.1", "supported_durations": [4, 6, 8], "generate_audio": True},
    {"id": "google/veo-3.1-fast", "supported_durations": [4, 6, 8], "generate_audio": True},
    {"id": "runway/gen-4.5", "supported_durations": [5, 10], "generate_audio": False},
    {"id": "alibaba/wan-3.0", "supported_durations": list(range(2, 31))},
]}


@pytest.fixture
def openrouter(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    from app import config
    from app.providers import openrouter_video as ov

    config.get_settings.cache_clear()
    ov._reset_models_cache()
    yield ov.OpenRouterVideoProvider(api_key="sk-test", model="google/veo-3.1")
    ov._reset_models_cache()


def _sent(provider, model, audio, *, reject=False):
    """The bodies POSTed for one submit with generate_audio=`audio`."""
    bodies = []

    def answer(request):
        body = json.loads(request.content)
        bodies.append(body)
        if reject and "generate_audio" in body:
            return httpx.Response(400, json={"error": {"message": "generate_audio is not supported"}})
        return httpx.Response(202, json={"id": "j1"})

    with respx.mock(assert_all_called=False) as router:  # the model list is cached
        router.get(f"{OR_BASE}/videos/models").mock(return_value=httpx.Response(200, json=MODELS))
        router.post(f"{OR_BASE}/videos").mock(side_effect=answer)
        provider.submit(model=model, prompt="waves", references=[], duration_seconds=5,
                        settings={"aspect_ratio": "9:16", "generate_audio": audio})
    return bodies


def test_openrouter_sends_sound_only_to_models_that_make_it(openrouter):
    assert _sent(openrouter, "google/veo-3.1", True)[0]["generate_audio"] is True
    assert _sent(openrouter, "google/veo-3.1", False)[0]["generate_audio"] is False
    # The list says it makes no sound: never sent.
    assert "generate_audio" not in _sent(openrouter, "runway/gen-4.5", True)[0]
    assert "generate_audio" not in _sent(openrouter, "runway/gen-4.5", False)[0]
    # Not said: sound on is the model's own default (not asked); off is asked, as before.
    assert "generate_audio" not in _sent(openrouter, "alibaba/wan-3.0", True)[0]
    assert _sent(openrouter, "alibaba/wan-3.0", False)[0]["generate_audio"] is False


def test_openrouter_resends_without_a_refused_sound_field(openrouter):
    from app.providers import openrouter_video as ov

    bodies = _sent(openrouter, "google/veo-3.1-fast", True, reject=True)
    assert bodies[0]["generate_audio"] is True and "generate_audio" not in bodies[1]
    # A refused "on" doesn't mark the model as always making sound.
    assert "google/veo-3.1-fast" not in ov._ALWAYS_AUDIO
    assert _sent(openrouter, "google/veo-3.1-fast", False)[0]["generate_audio"] is False


@pytest.mark.parametrize("audio", [True, False])
def test_openrouter_submit_reads_an_unreadable_model_list_once(openrouter, audio):
    # A failed read isn't cached: a submit must not pay its timeout twice
    # (once for the clip's capabilities, again for the sound flag).
    bodies = []
    with respx.mock() as router:
        models = router.get(f"{OR_BASE}/videos/models").mock(return_value=httpx.Response(503))
        router.post(f"{OR_BASE}/videos").mock(
            side_effect=lambda req: bodies.append(json.loads(req.content)) or httpx.Response(202, json={"id": "j1"}))
        openrouter.submit(model="google/veo-3.1", prompt="waves", references=[], duration_seconds=5,
                          settings={"aspect_ratio": "9:16", "generate_audio": audio})
    assert models.call_count == 1
    # Unknown: sound on is the model's own default (not asked); off is still asked.
    if audio:
        assert "generate_audio" not in bodies[0]
    else:
        assert bodies[0]["generate_audio"] is False


def test_openrouter_audio_flag_from_its_listing():
    from app.providers.openrouter_video import audio_support

    assert audio_support({"generate_audio": True}) is True
    assert audio_support({"generate_audio": False}) is False
    assert audio_support({"generate_audio": None}) is None and audio_support(None) is None


# ---------- Runware ----------


def test_runware_sound_field_and_price(client, live):  # noqa: F811
    from app.providers import runware
    from app.providers.base import ImageRef

    veo, kling = "runware/google:veo@3.1-lite", "runware/klingai:6@1"
    assert runware.makes_audio(veo) and runware.makes_audio("runware/alibaba:wan@2.7")
    assert not runware.makes_audio(kling)
    pic = [ImageRef(url="https://x/a.png", frame_type="first_frame")]

    def task(audio):
        t, _ = runware.build_video_task(veo, "waves", pic, reference_strategy="frame_images",
                                        duration_seconds=8, settings={"aspect_ratio": "16:9",
                                                                      "generate_audio": audio})
        return t["providerSettings"]["google"]["generateAudio"]

    assert task(True) is True and task(False) is False
    t, _ = runware.build_video_task(kling, "waves", pic, reference_strategy="frame_images", duration_seconds=5,
                                    settings={"generate_audio": True})
    assert "generateAudio" not in json.dumps(t) and '"audio"' not in json.dumps(t)
    # Veo prices sound apart; the others don't.
    assert runware.video_rate_usd(veo, "720p") == 0.03 and runware.video_rate_usd(veo, "720p", audio=True) == 0.05
    assert runware.video_rate_usd(veo) == 0.05 and runware.video_rate_usd(veo, audio=True) == 0.08
    assert runware.video_cost_usd(veo, 8, audio=True) == 0.64
    assert runware.video_rate_usd(kling, audio=True) == runware.video_rate_usd(kling)
    entries = {e["id"]: e for e in runware.catalog_entries(True)}
    assert entries[veo]["generate_audio"] is True and entries[veo]["audio_price_per_second"] == 0.08
    assert entries[kling]["generate_audio"] is False and "audio_price_per_second" not in entries[kling]


def test_runware_clip_spend_estimate_counts_sound(auth_client):
    from app import models
    from app.db import SessionLocal
    from app.services import spend

    with SessionLocal() as db:
        for i, audio in enumerate((True, False, None)):
            ctx = {"duration": 8, **({} if audio is None else {"audio": audio})}
            job = models.VideoJob(purpose="studio", ref_id=i + 1, provider="runware",
                                  model="runware/google:veo@3.1-lite", context_json=ctx, status="completed")
            db.add(job)
            db.commit()
            spend.record_video_job(db, job)
        costs = [round(e.cost_usd, 4) for e in db.query(models.SpendEntry).order_by(models.SpendEntry.id)]
    assert costs == [0.64, 0.4, 0.4]


# ---------- prices ----------


def test_price_text_picks_the_sku_for_the_choice():
    from app.services.prices import price_text

    both = {"provider": "openrouter", "pricing_skus": {"duration_seconds_with_audio": "0.4",
                                                       "duration_seconds_without_audio": "0.2"}}
    assert price_text(both, "video", audio=True) == "about $2.00 per 5 s"
    assert price_text(both, "video", audio=False) == "about $1.00 per 5 s"
    assert price_text(both, "video") == "about $1.00 per 5 s"  # the lowest, as before
    extra = {"provider": "openrouter", "pricing_skus": {"duration_seconds": "0.1", "duration_seconds_audio": "0.15"}}
    assert price_text(extra, "video", audio=True) == "about $0.75 per 5 s"
    assert price_text(extra, "video", audio=False) == "about $0.50 per 5 s"
    plain = {"provider": "openrouter", "pricing_skus": {"duration_seconds": "0.1"}}
    assert price_text(plain, "video", audio=True) == price_text(plain, "video") == "about $0.50 per 5 s"
    rw = {"provider": "runware", "price_per_second": 0.05, "audio_price_per_second": 0.08}
    assert (price_text(rw, "video"), price_text(rw, "video", audio=True)) == ("about $0.25 per 5 s",
                                                                              "about $0.40 per 5 s")


def test_video_model_list_says_which_models_make_sound(auth_client):
    from app.services import model_catalog

    listing = {"data": [{"id": "google/veo-3.1", "supported_durations": [4, 8], "generate_audio": True,
                         "pricing_skus": {"duration_seconds_without_audio": "0.2",
                                          "duration_seconds_with_audio": "0.4"}},
                        {"id": "runway/gen-4.5", "supported_durations": [5, 10], "generate_audio": False}]}

    def handler(request):
        return httpx.Response(200, json=listing if request.url.path.endswith("/videos/models") else {"data": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        model_catalog.refresh(client=c)
    rows = {m["id"]: m for m in auth_client.get("/api/providers/openrouter/video-models").json()}
    veo = rows["google/veo-3.1"]
    assert veo["supports_audio"] is True
    assert (veo["price_text"], veo["audio_price_text"]) == ("about $1.00 per 5 s", "about $2.00 per 5 s")
    assert rows["runway/gen-4.5"]["supports_audio"] is False and "audio_price_text" not in rows["runway/gen-4.5"]
    wan3 = rows["sogni/wan3.0-video"]
    assert wan3["supports_audio"] is True and wan3["audio_price_text"] == wan3["price_text"]  # same price
    assert rows["sogni/wan22"]["supports_audio"] is False
    assert rows["sogni/happyhorse-1.1-t2v"]["supports_audio"] is False
    assert model_catalog.find("video", "sogni/wan3.0-video")["generate_audio"] is True


# ---------- everything else stays silent ----------


def test_storyboard_shots_stay_silent(auth_client, monkeypatch):
    from app.providers import mock_video

    calls = []
    real = mock_video.MockVideoProvider.submit
    monkeypatch.setattr(mock_video.MockVideoProvider, "submit", lambda self, **kw: calls.append(kw) or real(self, **kw))
    aid = auth_client.post("/api/avatars", json={"name": "X"}).json()["id"]
    auth_client.post(f"/api/avatars/{aid}/assets", files={"file": ("h.png", make_png_bytes(), "image/png")},
                     data={"asset_type": "hero", "rights_confirmed": "true"})
    pid = auth_client.post("/api/projects", json={
        "title": "T", "original_script": "one two three four five", "cta_text": "Go",
        "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}]}).json()["id"]
    auth_client.post(f"/api/projects/{pid}/generate-plan")
    auth_client.post(f"/api/projects/{pid}/generate-video")
    assert calls and all(c["settings"]["generate_audio"] is False for c in calls)


def test_hook_lab_clips_stay_silent():
    from types import SimpleNamespace

    from app.services import hook_lab

    out = hook_lab._video_settings(SimpleNamespace(aspect_ratio="9:16"), "https://cb")
    assert out == {"aspect_ratio": "9:16", "generate_audio": False, "callback_url": "https://cb"}

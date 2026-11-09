"""Runware image + video provider (respx-mocked, offline): request bodies
built from the bundled model schemas, never resending a task that may have
run, error and refusal messages, spend, polling, routing, catalog, probe."""
from __future__ import annotations

import json
import uuid

import httpx
import pytest
import respx

API = "https://api.runware.ai/v1"
KEY = "rw-test-abcdefgh1234"
IMG_URL = "https://im.runware.ai/image/out.jpg"
VID_URL = "https://vm.runware.ai/video/out.mp4"
JPEG = b"\xff\xd8\xff\xe0fake-runware"
MP4 = b"\x00\x00\x00\x18ftypmp42fake"
PIC = "https://vid.ai-lab1.com/public/a/scene.jpg"
FACE = "https://vid.ai-lab1.com/public/a/face.jpg"


def _reset():
    from app import config
    from app.services import provider_keys

    config.get_settings.cache_clear()
    provider_keys.invalidate()


@pytest.fixture
def live(monkeypatch):
    """Providers live with a fake Runware key; nothing sleeps."""
    monkeypatch.setenv("MOCK_PROVIDERS", "false")
    monkeypatch.setenv("RUNWARE_API_KEY", KEY)
    from app.providers import runware

    monkeypatch.setattr(runware, "_sleep", lambda s: None)
    _reset()
    yield
    _reset()


def _tasks(call) -> list[dict]:
    return json.loads(call.request.content)


def _inference_posts(route, task_type: str) -> list[dict]:
    return [t for c in route.calls for t in _tasks(c) if t["taskType"] == task_type]


def _answer(*rows, status=200, **extra):
    return httpx.Response(status, json={"data": list(rows), **extra})


def _respond(handler):
    """A side effect answering each task in the posted array via handler(task)."""
    def side(request):
        tasks = json.loads(request.content)
        return handler(tasks[0])
    return side


def _ref(url=PIC, role="scene", frame_type=None, name=""):
    from app.providers.base import ImageRef

    return ImageRef(url=url, role=role, frame_type=frame_type, name=name)


# ---------- images ----------


def test_image_request_spend_and_download(client, live):
    from app import models
    from app.db import SessionLocal
    from app.providers.runware import RunwareImageProvider

    with respx.mock() as router:
        post = router.post(API).mock(side_effect=_respond(lambda t: _answer(
            {"taskType": "imageInference", "taskUUID": t["taskUUID"], "imageURL": IMG_URL,
             "cost": 0.0213, "NSFWContent": False})))
        dl = router.get(IMG_URL).mock(return_value=httpx.Response(200, content=JPEG))
        out = RunwareImageProvider().generate(model="runware/runware:107@1", prompt="a cabin at dawn",
                                              references=[], negative_prompt="cgi, blurry",
                                              settings={"aspect_ratio": "16:9"})
    assert out == JPEG
    req = post.calls[0].request
    assert req.headers["Authorization"] == f"Bearer {KEY}"
    assert "authorization" not in {k.lower() for k in dl.calls[0].request.headers}
    (task,) = _tasks(post.calls[0])
    assert uuid.UUID(task["taskUUID"]).version == 4
    assert task["model"] == "runware:107@1" and task["deliveryMethod"] == "sync"
    assert task["includeCost"] is True and task["safety"] == {"checkContent": False}
    assert (task["width"], task["height"]) == (1920, 1088)  # FLUX: multiples of 64
    assert task["negativePrompt"] == "cgi, blurry"
    db = SessionLocal()
    try:
        row = db.query(models.SpendEntry).one()
        assert (row.provider, row.kind, row.cost_usd, row.is_estimate) == ("runware", "image", 0.0213, False)
        assert row.generation_id == task["taskUUID"]
    finally:
        db.close()


def test_content_check_setting_and_cast_pictures(client, live):
    from app.providers import runware
    from app.services import provider_keys

    t = runware.build_image_task("runware/runware:z-image@turbo", "a quiet street", [])
    assert t["safety"]["checkContent"] is False  # off by default: uncensored open models
    provider_keys.set_option("runware", "check_content", True)
    assert runware.build_image_task("runware/runware:z-image@turbo", "a quiet street", [])["safety"]["checkContent"] is True
    provider_keys.set_option("runware", "check_content", None)
    # A Cast member's picture always goes through Runware's check.
    t = runware.build_image_task("runware/alibaba:qwen-image-edit@2511", "her sitting in the bar",
                                 [_ref(FACE, "character", name="Naina"), _ref()])
    assert t["safety"]["checkContent"] is True
    assert t["inputs"]["referenceImages"] == [FACE, PIC]  # the user's order
    assert "image 1 is Naina" in t["positivePrompt"] and "image 2 is the scene" in t["positivePrompt"]


def test_references_follow_each_models_inputs(client, live):
    from app.providers import runware

    refs = [_ref(f"https://x.test/{i}.jpg", "character") for i in range(5)]
    t = runware.build_image_task("runware/alibaba:qwen-image-edit@2511", "same place, other light", refs)
    assert len(t["inputs"]["referenceImages"]) == 3  # the schema's maxItems
    assert (t["width"], t["height"]) == (1920, 1080)  # any integer size on Qwen edit
    t = runware.build_image_task("runware/runware:107@1", "same place, other light", [_ref(frame_type="first_frame")],
                                 negative_prompt=" ")
    assert t["inputs"] == {"seedImage": PIC} and t["strength"] == 0.4
    assert "negativePrompt" not in t  # empty after trimming: omitted, not sent short
    # A character picture on an image-to-image-only model is left out.
    t = runware.build_image_task("runware/runware:107@1", "same place, other light", [_ref(FACE, "character")])
    assert "inputs" not in t and t["safety"]["checkContent"] is False


def test_processing_row_is_polled_then_missing_cost_is_estimated(client, live):
    from app import models
    from app.db import SessionLocal
    from app.providers.runware import RunwareImageProvider, image_cost_usd

    def answer(t):
        if t["taskType"] == "imageInference":
            return _answer({"taskType": "imageInference", "taskUUID": t["taskUUID"], "status": "processing"})
        return _answer({"taskType": "imageInference", "taskUUID": t["taskUUID"], "status": "success",
                        "imageURL": IMG_URL})

    with respx.mock() as router:
        post = router.post(API).mock(side_effect=_respond(answer))
        router.get(IMG_URL).mock(return_value=httpx.Response(200, content=JPEG))
        RunwareImageProvider().generate(model="runware/runware:z-image@turbo", prompt="a quiet street", references=[])
    assert len(_inference_posts(post, "imageInference")) == 1 and _inference_posts(post, "getResponse")
    db = SessionLocal()
    try:
        row = db.query(models.SpendEntry).one()
        assert row.is_estimate and row.source == "rate_card"
        assert row.cost_usd == pytest.approx(image_cost_usd("runware/runware:z-image@turbo"))
    finally:
        db.close()


@pytest.mark.parametrize("failure", ["500", "timeout"])
def test_a_task_that_may_have_run_is_never_resent(client, live, failure):
    from app.providers.runware import RunwareImageProvider

    def answer(t):
        if t["taskType"] == "imageInference":
            if failure == "timeout":
                raise httpx.ReadTimeout("slow")
            return httpx.Response(500, json={"errors": [{"code": "internalError", "message": "oops"}]})
        return _answer({"taskType": "imageInference", "taskUUID": t["taskUUID"], "imageURL": IMG_URL,
                        "cost": 0.01})

    with respx.mock() as router:
        post = router.post(API).mock(side_effect=_respond(answer))
        router.get(IMG_URL).mock(return_value=httpx.Response(200, content=JPEG))
        assert RunwareImageProvider().generate(model="runware/runware:z-image@turbo", prompt="a quiet street",
                                               references=[]) == JPEG
    assert len(_inference_posts(post, "imageInference")) == 1  # exactly one paid request
    asked = _inference_posts(post, "getResponse")
    assert asked and asked[0]["taskUUID"] == _inference_posts(post, "imageInference")[0]["taskUUID"]


def test_overloaded_is_retried(client, live):
    from app.providers.runware import RunwareImageProvider

    answers = iter([httpx.Response(429, json={"errors": [{"code": "serviceOverloaded", "retryAfter": 1}]})])

    def answer(t):
        nxt = next(answers, None)
        return nxt or _answer({"taskType": "imageInference", "taskUUID": t["taskUUID"], "imageURL": IMG_URL,
                               "cost": 0.01})

    with respx.mock() as router:
        post = router.post(API).mock(side_effect=_respond(answer))
        router.get(IMG_URL).mock(return_value=httpx.Response(200, content=JPEG))
        RunwareImageProvider().generate(model="runware/runware:z-image@turbo", prompt="a quiet street", references=[])
    assert post.call_count == 2


def test_errors_are_readable_refusals_named_and_the_key_never_shown(client, live):
    from app.providers import runware

    c = runware.RunwareClient()
    e = c.error_for(400, {"errors": [{"code": "providerContentPolicyViolation",
                                      "message": f"blocked for {KEY}"}]})
    assert e.refused and e.final and "refused this request for its content" in str(e)
    assert KEY not in str(e)
    e = c.error_for(400, {"errors": [{"error": {"code": "unknownParameter", "message": "foo is not allowed"}}]})
    assert not e.refused and "unknownParameter" in str(e) and "foo is not allowed" in str(e)
    e = c.error_for(401, {"error": "bad key"})
    assert e.final and "rejected the API key" in str(e)
    assert "out of credit" in str(c.error_for(402, {"errors": [{"code": "insufficientCredits", "message": "x"}]}))


def test_minors_prompt_refused_before_any_request(client, live):
    from app.providers.runware import RunwareImageProvider, RunwareVideoProvider
    from app.services.safety import UnsafePromptError

    with respx.mock() as router:  # no routes: any request would fail the test
        with pytest.raises(UnsafePromptError):
            RunwareImageProvider().generate(model="runware/runware:107@1",
                                            prompt="nude photo of a\nteen girl", references=[])
        with pytest.raises(UnsafePromptError):
            RunwareVideoProvider().submit(model="runware/lightricks:ltx@2.3-fast",
                                          prompt="sexual scene with kids", references=[])
        assert not router.calls


# ---------- video ----------


def test_video_payloads_per_model(client, live):
    from app.providers import runware

    loop = [_ref(frame_type="first_frame"), _ref(frame_type="last_frame")]

    def build(model, secs=8.0, refs=loop):
        return runware.build_video_task(f"runware/{model}", "mist drifts, static camera", refs,
                                        reference_strategy="frame_images", duration_seconds=secs,
                                        settings={"aspect_ratio": "16:9", "generate_audio": False})

    t, d = build("lightricks:ltx@2.3-fast")
    assert (t["width"], t["height"], d, t["deliveryMethod"]) == (1920, 1088, 8, "async")
    assert t["inputs"]["frameImages"] == [{"image": PIC, "frame": "first"}, {"image": PIC, "frame": "last"}]
    assert t["settings"] == {"enhancePrompt": False}
    t, _ = build("alibaba:wan@2.7")
    assert t["resolution"] == "1080p" and "width" not in t  # with frames: the image sets the aspect
    assert t["settings"] == {"audio": False, "promptExtend": False}
    t, d = build("google:veo@3.1-lite", secs=7)
    assert d == 8 and t["providerSettings"]["google"]["generateAudio"] is False
    t, d = build("luma:ray@3.2")
    assert d == 5  # Luma takes 5 s only when frames are given
    t, _ = build("bytedance:2@1")
    assert t["providerSettings"]["bytedance"]["cameraFixed"] is True  # a closed loop: camera locked
    t, d = build("google:veo@3.1-lite", secs=5, refs=[])
    assert (t["width"], t["height"], d) == (1920, 1080, 6) and "inputs" not in t


def test_capabilities_from_the_schemas(client, live):
    from app.providers.runware import capabilities_for

    veo = capabilities_for("runware/google:veo@3.1-lite")
    assert veo.durations == [4, 6, 8] and veo.first_frame and veo.last_frame and not veo.needs_first_frame
    assert capabilities_for("runware/klingai:6@1").durations == [5, 10]
    assert capabilities_for("runware/lightricks:ltx@2.3-fast").durations == list(range(1, 21))


def test_poll_states(client, live):
    from app.providers.base import SubmittedJob
    from app.providers.runware import RunwareError, RunwareVideoProvider

    job = SubmittedJob(provider="runware", model="runware/lightricks:ltx@2.3-fast", job_id="t-1")
    rows = {
        "processing": {"taskUUID": "t-1", "status": "processing", "progress": 40},
        "success": {"taskUUID": "t-1", "status": "success", "videoURL": VID_URL, "cost": 0.48},
    }
    p = RunwareVideoProvider()
    with respx.mock() as router:
        route = router.post(API)
        route.mock(return_value=_answer(rows["processing"]))
        st = p.poll(job)
        assert st.state == "running" and st.progress == pytest.approx(0.4)
        route.mock(return_value=_answer(rows["success"]))
        st = p.poll(job)
        assert (st.state, st.result_url, st.cost) == ("succeeded", VID_URL, 0.48)
        route.mock(return_value=httpx.Response(400, json={"errors": [{"code": "taskNotFound", "message": "no"}]}))
        assert p.poll(job).state == "failed"
        # Failing to *ask* isn't the task failing: the sweeper asks again
        # (a clip Runware is still making, and billing, isn't thrown away).
        for bad in (httpx.Response(500, text="<html>oops</html>"), httpx.Response(502, text="bad gateway"),
                    httpx.Response(429, json={"errors": [{"code": "serviceOverloaded"}]})):
            route.mock(return_value=bad)
            with pytest.raises(RunwareError) as e:
                p.poll(job)
            assert e.value.transient
        route.mock(return_value=_answer({"taskUUID": "t-1", "status": "error",
                                         "error": {"code": "providerError", "message": "boom"}}))
        st = p.poll(job)
        assert st.state == "failed" and "boom" in st.error


def test_storyboard_shot_end_to_end(client, live, monkeypatch):
    """A shot on a runware/ model: submitted async with no callback, polled
    via getResponse, completed, and billed at the reported cost."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-1234")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://studio.example.com")
    monkeypatch.setenv("VIDEO_WEBHOOKS", "on")
    _reset()
    from app import models
    from app.db import SessionLocal
    from app.providers.runware import RunwareVideoProvider
    from app.services import model_registry as reg
    from app.services import pipeline, video_jobs

    reg.set_active("video", "alibaba/wan-3.0")
    db = SessionLocal()
    try:
        project = models.VideoProject(title="t")
        db.add(project)
        db.commit()
        shot = models.VideoShot(project_id=project.id, shot_order=0, prompt="waves", duration_seconds=5.0,
                                model_override="runware/lightricks:ltx@2.3-fast")
        db.add(shot)
        db.commit()

        def answer(t):
            if t["taskType"] == "videoInference":
                return _answer({"taskType": "videoInference", "taskUUID": t["taskUUID"]})
            return _answer({"taskType": "videoInference", "taskUUID": t["taskUUID"], "status": "success",
                            "videoURL": VID_URL, "cost": 0.3})

        with respx.mock() as router:
            post = router.post(API).mock(side_effect=_respond(answer))
            job = pipeline.submit_shot_clip(db, project, shot, after="none", references=[])
            assert (job.provider, job.uses_webhook) == ("runware", False)
            assert shot.provider == "runware" and shot.provider_model == "runware/lightricks:ltx@2.3-fast"
            assert "callback" not in post.calls[0].request.content.decode().lower()
            assert (job.context_json or {}).get("duration") == 5
            assert isinstance(video_jobs.provider_for(job), RunwareVideoProvider)
            router.get(VID_URL).mock(return_value=httpx.Response(200, content=MP4))
            assert video_jobs.check(db, job.id) == "completed"
        db.refresh(shot)
        assert shot.status == "completed"
        row = db.query(models.SpendEntry).filter_by(ref=f"video_job:{job.id}").one()
        assert (row.provider, row.cost_usd, row.is_estimate) == ("runware", 0.3, False)
    finally:
        db.close()


def test_every_curated_model_builds_a_valid_request(client, live):
    """Contract: for each bundled model, the requests the builder makes
    (text only, with pictures, a closed loop) satisfy its published schema."""
    from app.providers import runware

    b = runware.bundle()
    assert len(b["curated"]) >= 15
    loop = [_ref(frame_type="first_frame"), _ref(frame_type="last_frame")]
    for a in b["curated"]:
        sch = b["schemas"][a]
        model = f"runware/{a}"
        caps = set(sch["capabilities"])
        if runware.kind_of(caps) == "image":
            tasks = [runware.build_image_task(model, "a quiet street", [_ref(), _ref(FACE, "character")])]
            if "text-to-image" in caps:
                tasks.append(runware.build_image_task(model, "a quiet street", [], negative_prompt="cgi"))
        else:
            tasks = [runware.build_video_task(model, "rain falls", loop, reference_strategy="frame_images",
                                              duration_seconds=8)[0]]
            if not runware.capabilities_for(model).needs_first_frame:
                tasks.append(runware.build_video_task(model, "rain falls", [], duration_seconds=5)[0])
        for t in tasks:
            assert runware.schema_error(sch, t) is None, (a, t)


# ---------- routing, catalog, settings ----------


def test_routing(client, live, monkeypatch):
    from app.providers import get_image, get_video, uses_webhooks
    from app.providers.runware import RunwareImageProvider, RunwareVideoProvider
    from app.services import model_registry as reg
    from app.services import provider_keys

    assert reg.provider_for("video", "runware/lightricks:ltx@2.3-fast") == "runware"
    assert isinstance(get_video("runware/lightricks:ltx@2.3-fast"), RunwareVideoProvider)
    assert isinstance(get_image("runware/runware:107@1"), RunwareImageProvider)
    assert not uses_webhooks("runware/lightricks:ltx@2.3-fast") and uses_webhooks("alibaba/wan-3.0")
    provider_keys.set_enabled("runware", False)
    assert reg.route_info("image", "runware/runware:107@1") == ("unavailable", "runware")
    provider_keys.set_enabled("runware", None)
    # Without its key a runware/ model is refused (not set up), never faked.
    from app.services.provider_keys import ProviderUnavailable

    monkeypatch.setenv("RUNWARE_API_KEY", "")
    _reset()
    assert reg.route_info("video", "runware/lightricks:ltx@2.3-fast") == ("unavailable", "runware")
    with pytest.raises(ProviderUnavailable, match="isn't set up"):
        get_video("runware/lightricks:ltx@2.3-fast")
    monkeypatch.setenv("MOCK_PROVIDERS", "true")
    _reset()
    assert reg.provider_for("image", "runware/runware:107@1") == "mock"


def test_switch_follows_the_key_and_pickers_follow_the_switch(client, auth_client, monkeypatch):
    from app.services import provider_keys

    monkeypatch.setenv("MOCK_PROVIDERS", "false")
    monkeypatch.setenv("RUNWARE_API_KEY", "")
    _reset()

    def ids():
        return [m["id"] for m in auth_client.get("/api/providers/openrouter/image-models").json()]

    from app.services import model_catalog
    from app.services import model_registry as reg

    reg.put_setting(model_catalog.CATALOG_KEY, {"fetched_at": "2026-11-09T00:00:00+00:00", "errors": {},
                                                "entries": []})
    assert provider_keys.enabled("runware") is False  # no key: off, so nothing shows
    assert not any(i.startswith("runware/") for i in ids())
    provider_keys.set_key("runware", KEY)
    assert provider_keys.enabled("runware") is True
    got = ids()
    assert "runware/runware:107@1" in got and "runware/alibaba:qwen-image-edit@2511" in got
    provider_keys.set_enabled("runware", False)
    assert not any(i.startswith("runware/") for i in ids())


def test_catalog_entries(client, live):
    from app.providers import runware

    entries = runware.catalog_entries(True)
    by = {e["id"]: e for e in entries}
    krea = by["runware/runware:107@1"]
    assert krea["kind"] == "image" and krea["uncensored"] and krea["suggested"] and "open" in krea["name"]
    veo = by["runware/google:veo@3.1-lite"]
    assert veo["kind"] == "video" and not veo["uncensored"] and veo["frame_images"] == ["first_frame", "last_frame"]
    assert veo["durations"] == [4, 6, 8] and veo["price_per_second"] == pytest.approx(0.05)
    assert entries[0]["suggested"]  # curated first
    assert len(entries) > 100  # plus the rest of the index
    assert not any(e["id"].startswith("runware/") and "lipsync" in e["id"] for e in entries)


def test_index_sync_uses_etags_and_keeps_the_last_good_list(client, live):
    from app.providers import runware

    rows = [{"air": "runware:1@1", "id": "m1", "name": "M1", "status": "live", "capabilities": ["text-to-image"]},
            {"air": "runware:2@1", "id": "m2", "name": "Old", "status": "deprecated", "capabilities": ["text-to-image"]}]
    with respx.mock() as router:
        route = router.get("https://runware.ai/docs/models/index.json")
        route.mock(return_value=httpx.Response(200, json=rows, headers={"etag": '"v1"'}))
        assert runware.refresh_index() == 1  # the deprecated one is dropped
        assert [r["air"] for r in runware.index_rows()] == ["runware:1@1"]
        route.mock(return_value=httpx.Response(304))
        assert runware.refresh_index() == 1
        assert route.calls[-1].request.headers["If-None-Match"] == '"v1"'
        route.mock(return_value=httpx.Response(500))
        with pytest.raises(httpx.HTTPStatusError):
            runware.refresh_index()
    assert [r["air"] for r in runware.index_rows()] == ["runware:1@1"]


def test_catalog_poll_skips_runware_without_a_key_or_in_mock_mode(client, monkeypatch):
    from app.providers import runware
    from app.services import model_catalog

    called = []
    monkeypatch.setattr(runware, "refresh_index", lambda client=None: called.append(1) or 0)
    with respx.mock(assert_all_called=False) as router:
        router.route().mock(return_value=httpx.Response(200, json={"data": []}))
        model_catalog.refresh()
    assert called == []  # mock mode (the test default)


def test_probe_posts_account_details(client, auth_client, live):
    with respx.mock() as router:
        route = router.post(API).mock(return_value=_answer(
            {"taskType": "accountManagement", "teamName": "Studio", "balance": 12.5}))
        r = auth_client.post("/api/settings/keys/runware/test").json()
        (task,) = _tasks(route.calls[0])
        assert task["taskType"] == "accountManagement" and task["operation"] == "getDetails"
        assert r["ok"] and "Studio" in r["detail"] and "$12.50" in r["detail"]
        route.mock(return_value=httpx.Response(401, json={"errors": [{"code": "invalidApiKey",
                                                                      "message": "Invalid API key"}]}))
        r = auth_client.post("/api/settings/keys/runware/test").json()
        assert not r["ok"] and "Invalid API key" in r["detail"] and KEY not in r["detail"]


def test_options_endpoint(client, auth_client, live):
    r = auth_client.put("/api/settings/keys/runware/options", json={"check_content": True})
    assert r.status_code == 200 and r.json()["options"] == {"check_content": True}
    assert auth_client.put("/api/settings/keys/runware/options", json={"nope": True}).status_code == 422
    assert auth_client.put("/api/settings/keys/sogni/options", json={"check_content": True}).status_code == 422
    r = auth_client.put("/api/settings/keys/runware/options", json={"check_content": None})
    assert r.json()["options"] == {"check_content": False}


def test_cost_estimates(client, live):
    from app.providers import runware

    assert runware.video_rate_usd("runware/lightricks:ltx@2.3-fast") == pytest.approx(0.06)
    assert runware.video_rate_usd("runware/lightricks:ltx@2.3-fast", "720p") == pytest.approx(0.03)
    assert runware.video_rate_usd("runware/google:veo@3.1-lite", "720p") == pytest.approx(0.03)  # no audio
    assert runware.image_cost_usd("runware/alibaba:qwen-image-edit@2511") == pytest.approx(0.0122)
    assert 0.002 < runware.image_cost_usd("runware/runware:z-image@turbo") < 0.02


def test_family_hint_reads_the_model_name(client):
    from app.providers.openrouter_chat import model_family_hint

    assert "diffusion" in model_family_hint("image", "runware/runware:107@1")  # FLUX.1 Krea
    assert "camera" in model_family_hint("video", "runware/lightricks:ltx@2.3-fast")


def test_budget_cap_stops_requests_before_they_are_sent(client, live):
    from app.db import SessionLocal
    from app.providers.runware import RunwareError, RunwareImageProvider
    from app.services import spend

    db = SessionLocal()
    try:
        spend.set_budget(db, monthly_usd=1.0, hard_stop=True, alert_pcts=[])
    finally:
        db.close()
    spend.record(provider="runware", kind="image", model="runware/runware:107@1", cost=1.5)
    with respx.mock() as router:  # no routes: nothing may be sent
        with pytest.raises(RunwareError, match="budget"):
            RunwareImageProvider().generate(model="runware/runware:107@1", prompt="a quiet street",
                                            references=[])
        assert not router.calls



# ---------- review follow-ups ----------


def test_waiting_survives_a_failed_question_and_overload_is_not_waited_on(client, live):
    from app.providers.runware import RunwareError, RunwareImageProvider

    answers = {"get": iter([httpx.Response(500, json={"errors": [{"code": "internalError", "message": "x"}]})])}

    def answer(t):
        if t["taskType"] == "imageInference":
            raise httpx.ReadTimeout("slow")
        nxt = next(answers["get"], None)
        return nxt or _answer({"taskType": "imageInference", "taskUUID": t["taskUUID"], "imageURL": IMG_URL,
                               "cost": 0.01})

    with respx.mock() as router:
        post = router.post(API).mock(side_effect=_respond(answer))
        router.get(IMG_URL).mock(return_value=httpx.Response(200, content=JPEG))
        assert RunwareImageProvider().generate(model="runware/runware:z-image@turbo", prompt="a quiet street",
                                               references=[]) == JPEG
        assert len(_inference_posts(post, "imageInference")) == 1
    # Still overloaded after the retries: it didn't run, so no waiting for it.
    with respx.mock() as router:
        post = router.post(API).mock(return_value=httpx.Response(503, json={"errors": [{"code": "serviceOverloaded"}]}))
        with pytest.raises(RunwareError):
            RunwareImageProvider().generate(model="runware/runware:z-image@turbo", prompt="a quiet street",
                                            references=[])
        assert not _inference_posts(post, "getResponse")


def test_a_billed_image_is_recorded_even_if_its_download_fails(client, live):
    from app import models
    from app.db import SessionLocal
    from app.providers.runware import RunwareImageProvider

    with respx.mock() as router:
        router.post(API).mock(side_effect=_respond(lambda t: _answer(
            {"taskType": "imageInference", "taskUUID": t["taskUUID"], "imageURL": IMG_URL, "cost": 0.02})))
        router.get(IMG_URL).mock(return_value=httpx.Response(503))
        with pytest.raises(httpx.HTTPStatusError):
            RunwareImageProvider().generate(model="runware/runware:z-image@turbo", prompt="a quiet street",
                                            references=[])
    db = SessionLocal()
    try:
        row = db.query(models.SpendEntry).one()
        assert (row.cost_usd, row.is_estimate) == (0.02, False)
    finally:
        db.close()


def test_durations_nested_in_alternatives_and_rules():
    from app.providers.runware import _durations

    assert _durations({"properties": {"duration": {"oneOf": [{"type": "integer", "enum": [6, 10]},
                                                             {"const": "auto"}]}}}) == [6, 10]
    assert _durations({"properties": {"duration": {"type": "integer"}},
                       "allOf": [{"if": {}, "then": {"properties": {"duration": {"enum": [5]}}}}]}) == [5]
    assert _durations({"properties": {"duration": {"type": "number", "default": 7}}}) == [7]


def _fake_model(monkeypatch, air_id: str, request: dict, modes=None, caps=None):
    from app.providers import runware

    monkeypatch.setitem(runware._schemas, air_id, {
        "id": air_id, "name": air_id, "capabilities": caps or ["text-to-video", "image-to-video"],
        "modes": modes or [{"id": "text"}, {"id": "first-frame", "inputs": ["frameImages"], "frames": ["first"]}],
        "pricing": {}, "request": request})
    return f"runware/{air_id}"


_IMG = {"type": "string"}
_CORE = {"taskType": {"type": "string"}, "taskUUID": {"type": "string"}, "model": {"type": "string"},
         "positivePrompt": {"type": "string", "minLength": 2}}


def test_a_permissive_knob_a_rule_forbids_is_dropped_not_fatal(client, live, monkeypatch):
    from app.providers import runware

    model = _fake_model(monkeypatch, "test:knob@1", {
        "type": "object", "additionalProperties": False, "required": ["taskType", "taskUUID", "model"],
        "properties": {**_CORE, "duration": {"type": "integer", "enum": [5]},
                       "settings": {"type": "object", "additionalProperties": False,
                                    "properties": {"safetyTolerance": {"type": "integer", "maximum": 4},
                                                   "audio": {"type": "boolean"}}},
                       "inputs": {"type": "object", "properties": {"frameImages": {"type": "array", "items": _IMG}}}},
        "allOf": [{"if": {"properties": {"inputs": {"required": ["frameImages"]}}, "required": ["inputs"]},
                   "then": {"properties": {"settings": {"properties": {"safetyTolerance": {"maximum": 2}}}}}}]})
    t, _ = runware.build_video_task(model, "rain on a street", [_ref(frame_type="first_frame")],
                                    reference_strategy="frame_images")
    assert t["inputs"]["frameImages"] and "safetyTolerance" not in t.get("settings", {})
    assert t["settings"]["audio"] is False  # the quiet defaults stay
    t, _ = runware.build_video_task(model, "rain on a street", [])
    assert t["settings"]["safetyTolerance"] == 4


def test_cast_pictures_are_kept_and_never_sent_unchecked(client, live, monkeypatch):
    from app.providers import runware

    naina, raj = _ref(FACE, "character", name="Naina"), _ref("https://x.test/raj.jpg", "character", name="Raj")
    bar, street = _ref("https://x.test/bar.jpg"), _ref("https://x.test/street.jpg")
    t = runware.build_image_task("runware/alibaba:qwen-image-edit@2511", "the two of them at the bar",
                                 [naina, raj, bar, street])
    assert t["inputs"]["referenceImages"] == [FACE, "https://x.test/raj.jpg", "https://x.test/bar.jpg"]
    t, _ = runware.build_video_task("runware/alibaba:wan@2.7", "she walks into the bar", [naina, bar])
    assert t["inputs"]["referenceImages"] == [FACE] and t["safety"]["checkContent"] is True
    model = _fake_model(monkeypatch, "test:nocheck@1", {
        "type": "object", "additionalProperties": False, "required": ["taskType", "taskUUID", "model"],
        "properties": {**_CORE, "inputs": {"type": "object", "properties": {
            "referenceImages": {"type": "array", "items": _IMG, "maxItems": 2}}}}},
        modes=[{"id": "reference-images", "inputs": ["referenceImages"]}], caps=["edit"])
    with pytest.raises(runware.RunwareError, match="no content check"):
        runware.build_image_task(model, "her portrait", [naina])
    runware.build_image_task(model, "the street at night", [street])  # no Cast picture: fine


def test_long_prompts_fit_the_model(client, live):
    from app.providers import runware

    negative = ", ".join(f"thing number {i}" for i in range(60))  # ~900 characters
    t, _ = runware.build_video_task("runware/alibaba:wan@2.7", "rain on a street", [], negative_prompt=negative)
    assert 0 < len(t["negativePrompt"]) <= 500 and t["negativePrompt"].endswith(tuple("0123456789"))
    t, _ = runware.build_video_task("runware/vidu:4@2", "slow rain " * 400, [])
    assert len(t["positivePrompt"]) <= 2000


def test_minors_wording_is_refused_and_any_child_turns_the_check_on(client, live):
    from app.providers import runware
    from app.services.safety import UnsafePromptError, check_prompt

    for bad in ("naked teenage girl", "sexy teenager in lingerie", "nude 14-year-old girl", "topless schoolgirl",
                "erotic photo of a young boy", "nsfw pic of a 12 year old", "a nude toddler",
                "explicit photo of a little girl", "a 15yo in lingerie", "loli art"):
        with pytest.raises(UnsafePromptError):
            check_prompt(bad)
    for fine in ("a young boy fishing at dawn", "nude woman on a beach at sunset", "sexy woman in lingerie",
                 "a kid at a strip mall", "an 18 year old student, nude portrait", "comic strip of a cat"):
        check_prompt(fine)
    t = runware.build_image_task("runware/runware:z-image@turbo", "children playing in a park", [])
    assert t["safety"]["checkContent"] is True  # innocent, but checked anyway
    t = runware.build_image_task("runware/runware:z-image@turbo", "a quiet park at dusk", [])
    assert t["safety"]["checkContent"] is False


def test_models_the_app_cannot_drive_are_not_offered(client, live):
    from app.providers import runware

    b = runware.bundle()
    assert b["unusable"]  # e.g. video editors that need a video, try-on, avatars
    ids = {e["id"] for e in runware.catalog_entries(True)}
    assert not ids & {f"runware/{a}" for a in b["unusable"]}
    assert all(f"runware/{a}" in ids for a in b["curated"])

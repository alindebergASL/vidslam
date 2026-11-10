"""Review fixes for the Runware provider:

- The minors filter catches ages in words and numbers, "y.o.", school
  years, look-alike letters and hidden characters; "CP" alone isn't refused.
- The exact text sent (reference names included) is what's checked, and an
  "avoid" list that strips signs of adulthood from a sexual prompt turns
  Runware's content check on.
- Sizes keep their shape when a model caps width or height.
- An edit model nobody has used yet still gets its reference picture.
- Spend: an image that times out, a finished video whose download fails, a
  video that times out, and videos still being made (the monthly cap).
- Models added to the live index are offered only once the app can drive them.
"""
from __future__ import annotations

import httpx
import pytest

from .test_runware import API, IMG_URL, KEY, _answer, _respond, live  # noqa: F401 — fixture

EDIT = "runware/alibaba:qwen-image-edit@2511"
PIC = "https://vid.ai-lab1.com/public/a/scene.jpg"
FACE = "https://vid.ai-lab1.com/public/a/face.jpg"


@pytest.mark.parametrize("bad", [
    "nude fourteen-year-old girl", "nude 13 y.o.", "naked girl, 13", "naked girl age thirteen",
    "nude high-schooler", "sexy middle schooler", "nude 7th grader", "topless girl (14)",
    "lingerie, aged 15", "nude girl, age: 12", "nude 13yo", "nude 16 years old",
    "nude ｔｅｅｎ",  # fullwidth "teen"
    "na​ked 12 year old",  # a hidden character inside a word
    "child porn", "csam",
])
def test_minors_wording_is_refused(bad):
    from app.services.safety import UnsafePromptError, check_prompt

    with pytest.raises(UnsafePromptError):
        check_prompt(bad)


@pytest.mark.parametrize("fine", [
    "CP violation notice on a door", "nude woman, 25 years old", "sexy model at 10 pm",
    "a girl, 20, in a bikini", "nude portrait, 35mm lens, f/1.8", "children playing in a park",
    "high school reunion of 40 year olds", "babydoll lingerie on a woman", "a kid at a strip mall",
])
def test_ordinary_prompts_pass(fine):
    from app.services.safety import check_prompt

    check_prompt(fine)


def test_reference_names_are_checked_with_the_prompt(client, live):  # noqa: F811
    from app.providers import runware
    from app.providers.base import ImageRef
    from app.services.safety import UnsafePromptError

    refs = [ImageRef(url=PIC, role="scene", name="the bedroom"), ImageRef(url=FACE, role="object", name="Lily, 13 years old")]
    with pytest.raises(UnsafePromptError):
        runware.build_image_task(EDIT, "nude, same pose", refs)
    t = runware.build_image_task(EDIT, "the two together in a garden", refs)
    assert "Lily, 13 years old" in t["positivePrompt"] and t["safety"]["checkContent"] is True


def test_an_avoid_list_that_strips_adulthood_turns_the_check_on(client, live):  # noqa: F811
    from app.providers import runware

    t = runware.build_image_task("runware/runware:z-image@turbo", "nude figure, petite", [],
                                 negative_prompt="adult, mature, woman")
    assert t["safety"]["checkContent"] is True
    t = runware.build_image_task("runware/runware:z-image@turbo", "nude figure on a beach", [],
                                 negative_prompt="blurry, watermark")
    assert t["safety"]["checkContent"] is False
    t = runware.build_image_task("runware/runware:z-image@turbo", "a mountain lake", [],
                                 negative_prompt="old, wrinkled photo")
    assert t["safety"]["checkContent"] is False  # not sexual: nothing to steer


def test_capped_sizes_keep_their_shape():
    from app.providers.runware import _size_variants

    sch = {"request": {"properties": {
        "width": {"type": "integer", "minimum": 256, "maximum": 1440, "multipleOf": 32},
        "height": {"type": "integer", "minimum": 256, "maximum": 1440, "multipleOf": 32}}}}
    first = next(v for v in _size_variants(sch, "16:9", 1920, [], frames=False) if v)
    assert first["width"] == 1440 and abs(first["width"] / first["height"] - 16 / 9) < 0.03


def test_an_unused_edit_model_still_takes_its_reference(monkeypatch):
    from app.providers import runware

    seen = []

    def fake_schema(model, *, fetch=True):
        seen.append(fetch)
        return {"request": {"properties": {"inputs": {"properties": {"referenceImages": {}}}}}} if fetch else None

    monkeypatch.setattr(runware, "schema", fake_schema)
    assert runware.takes_reference("runware/someone:new-edit@1") is True and seen == [True]


def _spend_rows(**kw):
    from app import models
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        return [(r.provider, r.kind, round(r.cost_usd, 4), r.is_estimate, r.note)
                for r in db.query(models.SpendEntry).filter_by(**kw)]
    finally:
        db.close()


def test_an_image_that_times_out_is_counted(client, live, monkeypatch):  # noqa: F811
    from app.providers import runware

    def slow(self, task, *, timeout):
        raise runware.RunwareError("didn't finish in time", pending=True)

    monkeypatch.setattr(runware.RunwareClient, "run", slow)
    with pytest.raises(runware.RunwareError):
        runware.RunwareImageProvider().generate(model="runware/runware:z-image@turbo", prompt="a quiet street",
                                                references=[])
    ((provider, kind, cost, est, note),) = _spend_rows(provider="runware")
    assert (provider, kind, est) == ("runware", "image", True) and cost > 0 and "may still bill" in note


def _video_job(**kw):
    from app import models
    from app.db import SessionLocal
    from app.services import video_jobs

    db = SessionLocal()
    try:
        job = models.VideoJob(purpose="studio", ref_id=1, provider="runware", model="runware/klingai:6@1",
                              provider_job_id="t-1", status="running", context_json={"duration": 5},
                              deadline_at=video_jobs._now() + video_jobs.timedelta(minutes=30), **kw)
        db.add(job)
        db.commit()
        return job.id
    finally:
        db.close()


def test_a_finished_video_whose_download_fails_is_still_counted(client, live, monkeypatch):  # noqa: F811
    from app.db import SessionLocal
    from app.services import video_jobs

    class Broken:
        def download(self, job, result_url=None):
            raise httpx.ConnectError("cdn down")

    monkeypatch.setattr(video_jobs, "provider_for", lambda job: Broken())
    jid = _video_job()
    db = SessionLocal()
    try:
        assert video_jobs.complete(db, jid, cost=0.7, result_url="https://vm.runware.ai/v.mp4") == "failed"
    finally:
        db.close()
    assert [r[:4] for r in _spend_rows(provider="runware")] == [("runware", "video", 0.7, False)]


def test_a_runware_video_that_times_out_is_counted_at_its_own_price(client, live, monkeypatch):  # noqa: F811
    from app import models
    from app.db import SessionLocal
    from app.providers import runware
    from app.services import video_jobs

    class Slow:
        def poll(self, job):
            from app.providers.base import JobStatus
            return JobStatus(state="running")

    monkeypatch.setattr(video_jobs, "provider_for", lambda job: Slow())
    jid = _video_job()
    db = SessionLocal()
    try:
        db.get(models.VideoJob, jid).deadline_at = video_jobs._now() - video_jobs.timedelta(minutes=1)
        db.commit()
        assert video_jobs.check(db, jid) == "failed"
    finally:
        db.close()
    ((_, kind, cost, est, _),) = _spend_rows(provider="runware")
    assert kind == "video" and est is True
    assert cost == pytest.approx(runware.video_cost_usd("runware/klingai:6@1", 5), abs=1e-4)


def test_the_monthly_cap_counts_videos_still_being_made(client, live):  # noqa: F811
    from app.db import SessionLocal
    from app.providers import runware
    from app.services import spend

    per_clip = runware.video_cost_usd("runware/klingai:6@1", 5)
    db = SessionLocal()
    try:
        spend.set_budget(db, monthly_usd=per_clip * 2.5, hard_stop=True, alert_pcts=[])
    finally:
        db.close()
    runware._guard("rain on a street")  # nothing in flight: fine
    _video_job()
    _video_job()
    runware._guard("rain on a street")  # two in flight: still under the cap
    _video_job()
    with pytest.raises(runware.RunwareError) as e:
        runware._guard("rain on a street")  # three in flight: over it
    assert e.value.status == 402 and "still being made" in str(e.value)


def test_new_index_models_are_offered_only_once_checked(client, live, monkeypatch):  # noqa: F811
    from app.providers import runware

    rows = list(runware.index_rows()) + [
        {"air": "newco:good@1", "id": "newco-good", "name": "Good", "caps": ["text-to-image"]},
        {"air": "newco:mask@1", "id": "newco-mask", "name": "Mask", "caps": ["text-to-image"]},
    ]
    monkeypatch.setattr(runware, "index_rows", lambda: rows)
    ids = lambda: {e["id"] for e in runware.catalog_entries(True)}  # noqa: E731
    assert not {"runware/newco:good@1", "runware/newco:mask@1"} & ids()  # not checked yet
    monkeypatch.setattr(runware, "smoke_build", lambda m: None if "good" in m else "needs a mask")
    assert runware.check_new_models() == 2
    got = ids()
    assert "runware/newco:good@1" in got and "runware/newco:mask@1" not in got
    assert runware.check_new_models() == 0  # remembered

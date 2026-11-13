"""Continue this clip: a new clip that starts from a finished Studio clip's
last frame (paid like any clip), joined after the original once it is
finished when asked (on by default), exactly once."""
from __future__ import annotations

from pathlib import Path

import pytest

from .test_image_ops import _avatar, _picture
from .test_join_clips import _clip_job, _join_row, _video


def _clip(client, aid: int, prompt: str = "a slow push-in") -> dict:
    r = client.post("/api/studio/generate-clip", json={"owner_kind": "avatar", "owner_id": aid, "prompt": prompt})
    assert r.status_code == 202, r.text
    job = client.get(f"/api/studio/jobs/{r.json()['id']}").json()
    assert job["status"] == "completed", job
    return job


def _job(client, jid: int) -> dict:
    r = client.get(f"/api/studio/jobs/{jid}")
    assert r.status_code == 200, r.text
    return r.json()


def _continue(client, jid: int, **body):
    return client.post(f"/api/studio/jobs/{jid}/continue", json={"prompt": "she turns and walks away", **body})


def _rows(**filters) -> list:
    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        q = db.query(models.AssetGenerationJob)
        for k, v in filters.items():
            q = q.filter(getattr(models.AssetGenerationJob, k) == v)
        return [(j.id, j.status, dict(j.options_json or {})) for j in q.order_by(models.AssetGenerationJob.id)]


def test_continue_makes_a_frame_then_a_clip_from_it_and_joins_them_once(auth_client):
    aid = _avatar(auth_client)
    original = _clip(auth_client, aid, "a woman at a window")
    info = auth_client.get(f"/api/studio/jobs/{original['id']}/continue").json()
    assert info["prompt"] == "a woman at a window"
    assert info["model"] == original["provider_model"]  # its own model can start from a picture

    r = _continue(auth_client, original["id"], duration_seconds=5)
    assert r.status_code == 202, r.text
    out = r.json()
    assert out["batch_ids"] == [out["id"]]
    cont = _job(auth_client, out["id"])
    # The last frame was saved as a Studio picture of the clip, visible in the library.
    frame = _job(auth_client, cont["first_frame_job_id"])
    assert (frame["operation"], frame["source_job_id"], frame["output_kind"]) == (
        "extract_frame", original["id"], "image")
    assert frame["options"]["at"] == "last" and frame["status"] == "completed"
    # A paid generation like any clip, from the new prompt, in the clip's aspect.
    assert cont["operation"] == "" and cont["prompt"] == "she turns and walks away"
    assert cont["status"] == "completed" and cont["provider_model"] == original["provider_model"]
    assert (cont["owner_kind"], cont["owner_id"]) == ("avatar", aid)
    assert cont["options"]["continues_job_id"] == original["id"] and cont["options"]["auto_join"] is True
    # Tests run the queue inline: the join with the original is made too.
    join_id = cont["options"]["join_job_id"]
    joined = _job(auth_client, join_id)
    assert joined["operation"] == "join_clips" and joined["status"] == "completed", joined
    assert joined["options"]["job_ids"] == [original["id"], cont["id"]]
    assert joined["options"]["transition"] == "cut" and joined["options"]["auto_join_for"] == cont["id"]
    assert joined["label"] == "Joined 2 clips"
    assert joined["result_duration_seconds"] == pytest.approx(
        original["result_duration_seconds"] + cont["result_duration_seconds"], abs=0.15)
    assert len(_rows(operation="join_clips")) == 1


def test_switch_off_means_no_join(auth_client):
    aid = _avatar(auth_client)
    original = _clip(auth_client, aid)
    cont = _job(auth_client, _continue(auth_client, original["id"], auto_join=False).json()["id"])
    assert cont["status"] == "completed"
    assert cont["options"] == {"continues_job_id": original["id"], "auto_join": False}
    assert _rows(operation="join_clips") == []


def test_model_without_a_first_frame_is_refused_before_anything_is_made(auth_client, monkeypatch):
    from app import providers
    from app.providers.base import VideoCapabilities

    aid = _avatar(auth_client)
    original = _clip(auth_client, aid)
    frames = len(_rows(operation="extract_frame"))
    monkeypatch.setattr(providers, "video_capabilities", lambda model: VideoCapabilities(durations=[5]))
    r = _continue(auth_client, original["id"], model="mock/video-default")
    assert r.status_code == 422 and "can't start from a picture" in r.json()["detail"]
    # No model picked and neither the clip's nor the default can: a plain 422 too.
    r = _continue(auth_client, original["id"])
    assert r.status_code == 422 and "pick a model" in r.json()["detail"]
    assert auth_client.get(f"/api/studio/jobs/{original['id']}/continue").json()["model"] == ""
    assert len(_rows(operation="extract_frame")) == frames


def test_what_can_be_continued(auth_client, tmp_path):
    aid = _avatar(auth_client)
    pic = _picture(auth_client, aid)
    assert _continue(auth_client, pic["id"]).status_code == 422
    assert _continue(auth_client, 999999).status_code == 404
    running = _clip_job(_video(tmp_path / "r.mp4"), aid, status="polling")
    assert _continue(auth_client, running).status_code == 409
    assert auth_client.post(f"/api/studio/jobs/{running}/continue", json={"prompt": ""}).status_code == 422
    # A joined clip: the prompt of its last clip, its own aspect.
    a = _clip(auth_client, aid, "first part")
    b = _clip(auth_client, aid, "second part")
    joined = auth_client.post("/api/studio/join-clips", json={"job_ids": [a["id"], b["id"]]}).json()
    info = auth_client.get(f"/api/studio/jobs/{joined['id']}/continue").json()
    assert info["prompt"] == "second part" and info["model"] == b["provider_model"]
    r = _continue(auth_client, joined["id"])
    assert r.status_code == 202, r.text
    cont = _job(auth_client, r.json()["id"])
    assert cont["status"] == "completed"
    assert _job(auth_client, cont["options"]["join_job_id"])["options"]["job_ids"] == [joined["id"], cont["id"]]


def test_prompt_goes_through_the_safety_check_and_the_budget_and_rate_limit_apply(auth_client, monkeypatch):
    from fastapi import HTTPException

    from app.api import studio
    from app.services import minor_check

    aid = _avatar(auth_client)
    original = _clip(auth_client, aid)
    frames = len(_rows(operation="extract_frame"))
    seen: list[tuple] = []
    real = minor_check.check_or_422

    def spy(prompt, negative="", *, kind):
        seen.append((prompt, kind))
        if "forbidden" in prompt:
            raise HTTPException(422, "refused by the check")
        return real(prompt, negative, kind=kind)

    monkeypatch.setattr(minor_check, "check_or_422", spy)
    r = _continue(auth_client, original["id"], prompt="forbidden words")
    assert r.status_code == 422 and seen == [("forbidden words", "video")]
    assert len(_rows(operation="extract_frame")) == frames  # refused before the frame was made

    # The budget guard is a dependency of the route (as for generate-clip).
    deps = {d.call for route in studio.router.routes if getattr(route, "path", "") == "/jobs/{job_id}/continue"
            and "POST" in route.methods for d in route.dependant.dependencies}
    assert studio.budget_guard in deps


def test_retry_of_a_failed_continuation_keeps_the_join_intent(auth_client):
    from app import models
    from app.db import SessionLocal

    aid = _avatar(auth_client)
    original = _clip(auth_client, aid)
    cont = _job(auth_client, _continue(auth_client, original["id"]).json()["id"])
    with SessionLocal() as db:  # as if the provider had failed it
        row = db.get(models.AssetGenerationJob, cont["id"])
        row.status, row.error = "failed", "provider error"
        row.options_json = {"continues_job_id": original["id"], "auto_join": True}
        db.commit()
    joins = len(_rows(operation="join_clips"))
    r = auth_client.post(f"/api/studio/jobs/{cont['id']}/retry")
    assert r.status_code == 202, r.text
    again = _job(auth_client, r.json()["id"])
    assert again["first_frame_job_id"] == cont["first_frame_job_id"] and again["status"] == "completed"
    assert again["options"]["continues_job_id"] == original["id"] and again["options"]["auto_join"] is True
    join = _job(auth_client, again["options"]["join_job_id"])
    assert join["options"]["job_ids"] == [original["id"], again["id"]]
    assert len(_rows(operation="join_clips")) == joins + 1
    # Try another model keeps it too (only models that start from a picture).
    r = auth_client.post(f"/api/studio/jobs/{cont['id']}/retry", json={"model": "mock/video-default"})
    assert r.status_code == 202, r.text
    assert _job(auth_client, r.json()["id"])["options"]["continues_job_id"] == original["id"]


def test_retry_of_a_failed_auto_join_joins_again(auth_client):
    from app import models
    from app.db import SessionLocal

    aid = _avatar(auth_client)
    original = _clip(auth_client, aid)
    cont = _job(auth_client, _continue(auth_client, original["id"]).json()["id"])
    join_id = cont["options"]["join_job_id"]
    with SessionLocal() as db:
        row = db.get(models.AssetGenerationJob, join_id)
        row.status, row.error = "failed", "Stopped unexpectedly"
        db.commit()
    r = auth_client.post(f"/api/studio/jobs/{join_id}/retry")
    assert r.status_code == 202, r.text
    again = _job(auth_client, r.json()["id"])
    assert again["status"] == "completed" and again["options"]["job_ids"] == [original["id"], cont["id"]]
    # The new join is the continuation's join from now on.
    assert again["options"]["auto_join_for"] == cont["id"]
    assert _job(auth_client, cont["id"])["options"]["join_job_id"] == again["id"]
    # Running a finished (not failed) join again doesn't move the link.
    r = auth_client.post(f"/api/studio/jobs/{again['id']}/retry")
    assert r.status_code == 202, r.text
    assert "auto_join_for" not in _job(auth_client, r.json()["id"])["options"]
    assert _job(auth_client, cont["id"])["options"]["join_job_id"] == again["id"]


def test_continue_falls_back_to_the_default_when_the_clips_own_provider_is_off(auth_client, monkeypatch):
    from app import models, providers
    from app.db import SessionLocal
    from app.providers.base import VideoCapabilities
    from app.services import provider_gate
    from app.services.provider_keys import ProviderUnavailable

    aid = _avatar(auth_client)
    original = _clip(auth_client, aid)
    default = original["provider_model"]
    with SessionLocal() as db:  # made with another provider's model
        db.get(models.AssetGenerationJob, original["id"]).provider_model = "openrouter/some-video"
        db.commit()
    monkeypatch.setattr(providers, "video_capabilities",
                        lambda model: VideoCapabilities(durations=[5], first_frame=True))
    real = provider_gate.require_model

    def gate(kind, model=None, db=None):
        if model == "openrouter/some-video":
            raise ProviderUnavailable("openrouter", kind=kind, model=model)
        return real(kind, model, db)

    monkeypatch.setattr(provider_gate, "require_model", gate)
    assert auth_client.get(f"/api/studio/jobs/{original['id']}/continue").json()["model"] == default
    r = _continue(auth_client, original["id"])
    assert r.status_code == 202, r.text
    assert _job(auth_client, r.json()["id"])["provider_model"] == default
    # Picked on purpose, the switched-off model is still a plain 409.
    assert _continue(auth_client, original["id"], model="openrouter/some-video").status_code == 409


def test_a_continue_that_fails_after_the_frame_leaves_no_frame_behind(auth_client, monkeypatch):
    from app.api import studio
    from app.workers import queue

    aid = _avatar(auth_client)
    original = _clip(auth_client, aid)
    frames = len([f for f in _rows(operation="extract_frame") if f[1] != "deleted"])
    clips = len(_rows(output_kind="video_clip"))

    def down(*a, **kw):
        raise queue.QueueUnavailable("down")

    monkeypatch.setattr(queue, "enqueue", down)
    monkeypatch.setattr(studio, "enqueue", down)
    for _ in range(2):
        r = _continue(auth_client, original["id"])
        assert r.status_code >= 500, r.text  # the queue is down
    listed = auth_client.get("/api/studio/jobs", params={"limit": 200}).json()
    assert [j for j in listed if j["operation"] == "extract_frame"] == []
    assert len([j for j in listed if j["output_kind"] == "video_clip"]) == 1  # only the original
    assert len([f for f in _rows(operation="extract_frame") if f[1] != "deleted"]) == frames
    assert len(_rows(output_kind="video_clip")) == clips + 2  # tombstoned, never shown


def test_a_failed_join_insert_rolls_back_only_the_join(auth_client, tmp_path, monkeypatch):
    """The join row can't be inserted (a constraint, a lost serialization):
    only the join is rolled back, and the paid clip still completes."""
    from app.services import video_ops

    aid = _avatar(auth_client)
    original = _clip_job(_video(tmp_path / "o.mp4"), aid)
    jid, vid = _polling_continuation(auth_client, aid, original, tmp_path)
    real = video_ops.new_join_job

    def broken(sources, transition, crossfade):
        join = real(sources, transition, crossfade)
        join.result_asset_id = 999999  # no such asset: the flush fails in the database (FK)
        return join

    monkeypatch.setattr(video_ops, "new_join_job", broken)
    _apply(vid)
    cont = _job(auth_client, jid)
    assert cont["status"] == "completed", cont
    assert cont["options"]["join_note"] == "Not joined: something went wrong"
    assert "join_job_id" not in cont["options"]
    assert _rows(operation="join_clips") == []


# ---------- the join, exactly once ----------


def _polling_continuation(client, aid: int, original: int, tmp_path: Path, *, auto_join: bool = True) -> tuple:
    """A continuation clip at the provider ('polling') and its finished
    video job, ready to be applied (as a webhook or a poll would)."""
    from app import models
    from app.db import SessionLocal

    path = _video(tmp_path / f"c{original}.mp4", seconds=1.0)
    with SessionLocal() as db:
        job = models.AssetGenerationJob(
            owner_kind="avatar", owner_id=aid, output_kind="video_clip", prompt="next", negative_prompt="",
            reference_asset_ids_json=[], provider="mock", provider_model="mock/video-default", status="polling",
            duration_seconds=1.0, options_json={"continues_job_id": original, "auto_join": auto_join})
        db.add(job)
        db.commit()
        vjob = models.VideoJob(purpose="studio", ref_id=job.id, provider="mock", provider_job_id="x",
                               status="completed", result_path=str(path))
        db.add(vjob)
        db.commit()
        return job.id, vjob.id


def _apply(vjob_id: int) -> None:
    from app import models
    from app.db import SessionLocal
    from app.services import pipeline

    with SessionLocal() as db:
        pipeline.on_studio_clip_ready(db, db.get(models.VideoJob, vjob_id))


def test_duplicate_completion_joins_once(auth_client, tmp_path):
    """The same finished clip applied again (a redelivered webhook, the
    sweeper's retry): the clip is saved once and joined once."""
    aid = _avatar(auth_client)
    original = _clip_job(_video(tmp_path / "o.mp4"), aid)
    jid, vid = _polling_continuation(auth_client, aid, original, tmp_path)
    _apply(vid)
    cont = _job(auth_client, jid)
    assert cont["status"] == "completed" and cont["options"]["join_job_id"]
    # A second copy of the provider's file arrives: applied again.
    _video(tmp_path / f"c{original}.mp4", seconds=1.0)
    _apply(vid)
    _apply(vid)
    joins = _rows(operation="join_clips")
    assert [j[0] for j in joins] == [cont["options"]["join_job_id"]]
    assert _job(auth_client, jid)["options"] == cont["options"]


def test_plan_auto_join_never_makes_a_second_join(auth_client, tmp_path):
    from app import models
    from app.db import SessionLocal
    from app.services import video_ops

    aid = _avatar(auth_client)
    original = _clip_job(_video(tmp_path / "o.mp4"), aid)
    jid, vid = _polling_continuation(auth_client, aid, original, tmp_path)
    _apply(vid)
    with SessionLocal() as db:
        job = db.get(models.AssetGenerationJob, jid)
        assert video_ops.plan_auto_join(db, job) is None
        db.commit()
    assert len(_rows(operation="join_clips")) == 1


def test_racing_completions_make_one_join(auth_client, tmp_path, monkeypatch):
    """Two handlers apply the same clip at once: only the one whose
    "polling → completed" write wins adds the join (same transaction)."""
    from app import models
    from app.db import SessionLocal
    from app.services import pipeline

    aid = _avatar(auth_client)
    original = _clip_job(_video(tmp_path / "o.mp4"), aid)
    jid, vid = _polling_continuation(auth_client, aid, original, tmp_path)
    real = pipeline._plan_auto_join
    once: list = []

    def other_first(db, job):
        # Another handler completes the clip (and joins it) first... but
        # this one already won its claim, so the other must have lost.
        once.append(job.id)
        return real(db, job)

    monkeypatch.setattr(pipeline, "_plan_auto_join", other_first)
    with SessionLocal() as db1, SessionLocal() as db2:
        v1, v2 = db1.get(models.VideoJob, vid), db2.get(models.VideoJob, vid)
        pipeline.on_studio_clip_ready(db1, v1)
        _video(tmp_path / f"c{original}.mp4", seconds=1.0)
        pipeline.on_studio_clip_ready(db2, v2)  # the stale handler: status isn't polling any more
    assert once == [jid]
    assert len(_rows(operation="join_clips")) == 1


def test_original_deleted_records_a_plain_note(auth_client, tmp_path):
    aid = _avatar(auth_client)
    original = _clip_job(_video(tmp_path / "o.mp4"), aid)
    jid, vid = _polling_continuation(auth_client, aid, original, tmp_path)
    assert auth_client.delete(f"/api/studio/jobs/{original}").status_code == 204
    _apply(vid)
    cont = _job(auth_client, jid)
    assert cont["status"] == "completed"
    assert cont["options"]["join_note"] == "Not joined: the original was deleted"
    assert "join_job_id" not in cont["options"]
    assert _rows(operation="join_clips") == []


def test_join_that_cant_start_is_noted_and_the_clip_still_completes(auth_client, tmp_path):
    aid = _avatar(auth_client)
    long = _clip_job(_video(tmp_path / "o.mp4"), aid, seconds=599.5)
    jid, vid = _polling_continuation(auth_client, aid, long, tmp_path)
    _apply(vid)
    cont = _job(auth_client, jid)
    assert cont["status"] == "completed"
    assert cont["options"]["join_note"].startswith("Not joined: those clips are")
    assert "10 minutes" in cont["options"]["join_note"]


def test_a_fault_in_planning_the_join_never_costs_the_clip(auth_client, tmp_path, monkeypatch):
    from app.services import video_ops

    aid = _avatar(auth_client)
    original = _clip_job(_video(tmp_path / "o.mp4"), aid)
    jid, vid = _polling_continuation(auth_client, aid, original, tmp_path)

    def boom(db, job):
        raise RuntimeError("bug")

    monkeypatch.setattr(video_ops, "plan_auto_join", boom)
    _apply(vid)
    cont = _job(auth_client, jid)
    assert cont["status"] == "completed" and cont["options"]["join_note"] == "Not joined: something went wrong"


def test_join_cap_full_queues_the_auto_join_anyway(auth_client, tmp_path):
    """Auto-joins count toward the 2-join cap (a manual join then waits)
    but are queued even when it is full: one per paid clip."""
    from app.db import SessionLocal
    from app.services import video_ops

    aid = _avatar(auth_client)
    original = _clip_job(_video(tmp_path / "o.mp4"), aid)
    _join_row(aid, original)
    _join_row(aid, original, status="submitted")
    with SessionLocal() as db:
        assert video_ops.join_busy(db)
    jid, vid = _polling_continuation(auth_client, aid, original, tmp_path)
    _apply(vid)
    cont = _job(auth_client, jid)
    join = _job(auth_client, cont["options"]["join_job_id"])
    assert join["status"] == "completed", join  # queued (inline here) and made
    assert "join_note" not in cont["options"]
    # While an auto-join waits it holds a place like any join.
    _join_row(aid, original, status="pending")
    assert auth_client.post("/api/studio/join-clips", json={"job_ids": [original, original]}).status_code == 429


def test_auto_join_that_cant_be_queued_is_failed_for_retry(auth_client, tmp_path, monkeypatch):
    from app.workers import queue

    aid = _avatar(auth_client)
    original = _clip_job(_video(tmp_path / "o.mp4"), aid)
    jid, vid = _polling_continuation(auth_client, aid, original, tmp_path)

    def down(*a, **kw):
        raise queue.QueueUnavailable("down")

    monkeypatch.setattr(queue, "enqueue", down)
    _apply(vid)
    cont = _job(auth_client, jid)
    assert cont["status"] == "completed"
    join = _job(auth_client, cont["options"]["join_job_id"])
    assert join["status"] == "failed" and "retry" in join["error"]


def test_failed_continuation_makes_no_join(auth_client, tmp_path):
    from app import models
    from app.db import SessionLocal
    from app.services import pipeline

    aid = _avatar(auth_client)
    original = _clip_job(_video(tmp_path / "o.mp4"), aid)
    jid, vid = _polling_continuation(auth_client, aid, original, tmp_path)
    with SessionLocal() as db:
        v = db.get(models.VideoJob, vid)
        v.status, v.error = "failed", "provider error"
        db.commit()
        pipeline.on_studio_clip_failed(db, v)
    assert _job(auth_client, jid)["status"] == "failed"
    assert _rows(operation="join_clips") == []


# ---------- no Cast exemption ----------


def test_continuation_frame_and_join_are_never_cast_exempt(auth_client):
    from app import models
    from app.db import SessionLocal
    from app.services import pipeline

    aid = _avatar(auth_client)
    original = _clip(auth_client, aid)
    cont = _job(auth_client, _continue(auth_client, original["id"]).json()["id"])
    marked = {aid: frozenset(range(1, 50))}
    with SessionLocal() as db:
        for jid in (cont["first_frame_job_id"], cont["id"], cont["options"]["join_job_id"]):
            job = db.get(models.AssetGenerationJob, jid)
            avatar_id, source = pipeline.studio_cast_source(db, job)
            assert (avatar_id, source) == (None, None)
            assert pipeline.avatar_adult_ok(avatar_id, source, marked) is False
        ref = pipeline.studio_first_frame(db, db.get(models.AssetGenerationJob, cont["id"]))
        assert ref is not None and ref.adult_ok is False and ref.role == "scene"


def test_two_retries_of_a_failed_auto_join_link_only_one(auth_client):
    """Two Retries of the same failed auto-join at once: each read the
    continuation still pointing at the failed join before either stored its
    own; only the first to store is linked, the other stays a plain join."""
    from app import models
    from app.db import SessionLocal
    from app.services import video_ops

    aid = _avatar(auth_client)
    original = _clip(auth_client, aid)
    cont = _job(auth_client, _continue(auth_client, original["id"]).json()["id"])
    old_id = cont["options"]["join_job_id"]
    with SessionLocal() as db:
        row = db.get(models.AssetGenerationJob, old_id)
        row.status, row.error = "failed", "Stopped unexpectedly"
        db.commit()
    a, b = _join_row(aid, original["id"]), _join_row(aid, original["id"])
    with SessionLocal() as one, SessionLocal() as two:
        # Both requests have read the old join and the continuation.
        old1, old2 = one.get(models.AssetGenerationJob, old_id), two.get(models.AssetGenerationJob, old_id)
        held = [s.get(models.AssetGenerationJob, cont["id"]) for s in (one, two)]
        assert all(h.options_json["join_job_id"] == old_id for h in held)
        video_ops.relink_auto_join(one, old1, one.get(models.AssetGenerationJob, a))
        one.commit()
        video_ops.relink_auto_join(two, old2, two.get(models.AssetGenerationJob, b))
        two.commit()
    assert _job(auth_client, cont["id"])["options"]["join_job_id"] == a
    assert _job(auth_client, a)["options"].get("auto_join_for") == cont["id"]
    assert "auto_join_for" not in _job(auth_client, b)["options"]

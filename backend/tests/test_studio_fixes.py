"""Studio fixes: batched polling by ids, preview media types, a Studio clip
as a storyboard take, "Animate this" (first frame), cancel, enqueue
failures, the Studio sweeper, real clip size/length, deleted references."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from .conftest import make_png_bytes


def _avatar(client, name: str = "Mia") -> int:
    return client.post("/api/avatars", json={"name": name}).json()["id"]


def _upload(client, aid: int) -> int:
    r = client.post(f"/api/avatars/{aid}/assets", files={"file": ("h.png", make_png_bytes(), "image/png")},
                    data={"asset_type": "hero", "rights_confirmed": "true"})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _job(**kw) -> int:
    from app import models
    from app.db import SessionLocal

    fields = {"owner_kind": "avatar", "owner_id": 1, "output_kind": "image", "prompt": "p",
              "status": "completed", **kw}
    with SessionLocal() as db:
        job = models.AssetGenerationJob(**fields)
        db.add(job)
        db.commit()
        return job.id


def _get_job(job_id: int):
    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        j = db.get(models.AssetGenerationJob, job_id)
        db.expunge(j)
        return j


def _clip(client, aid: int, **extra) -> dict:
    r = client.post("/api/studio/generate-clip", json={
        "owner_kind": "avatar", "owner_id": aid, "prompt": "a walk on the beach", **extra})
    assert r.status_code == 202, r.text
    return client.get(f"/api/studio/jobs/{r.json()['id']}").json()


def _image(client, aid: int) -> dict:
    r = client.post("/api/studio/generate-image", json={
        "owner_kind": "avatar", "owner_id": aid, "prompt": "a portrait"})
    assert r.status_code == 202, r.text
    return client.get(f"/api/studio/jobs/{r.json()['id']}").json()


def _events(kind: str | None = None) -> list:
    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        q = db.query(models.Event)
        if kind:
            q = q.filter(models.Event.kind == kind)
        rows = q.order_by(models.Event.id).all()
        for r in rows:
            db.expunge(r)
        return rows


# ---------- ids filter ----------


def test_ids_filter_returns_just_those_jobs_whatever_the_filters(auth_client):
    a = _job(status="pending")
    b = _job(status="completed")
    c = _job(status="failed")
    gone = _job(status="deleted")
    r = auth_client.get("/api/studio/jobs", params={"ids": f"{a},{c},{gone}", "status": "completed",
                                                     "output_kind": "video_clip"})
    assert r.status_code == 200, r.text
    assert [j["id"] for j in r.json()] == [c, a]
    assert b not in [j["id"] for j in r.json()]


def test_ids_filter_limits(auth_client):
    many = ",".join(str(i) for i in range(1, 202))
    assert auth_client.get("/api/studio/jobs", params={"ids": many}).status_code == 422
    assert auth_client.get("/api/studio/jobs", params={"ids": "1,x"}).status_code == 422
    assert auth_client.get("/api/studio/jobs", params={"ids": ""}).json() == []


# ---------- preview media type ----------


@pytest.mark.parametrize("suffix,mime", [(".jpg", "image/jpeg"), (".webp", "image/webp"), (".png", "image/png")])
def test_preview_uses_the_real_file_type(auth_client, tmp_path, suffix, mime):
    f = tmp_path / f"pic{suffix}"
    f.write_bytes(b"x" * 10)
    jid = _job(result_path=str(f))
    r = auth_client.get(f"/api/studio/jobs/{jid}/preview")
    assert r.status_code == 200
    assert r.headers["content-type"] == mime


# ---------- real size / duration, missing references ----------


def test_finished_clip_stores_real_size_and_length(auth_client):
    aid = _avatar(auth_client)
    job = _clip(auth_client, aid, duration_seconds=5)
    assert job["status"] == "completed"
    assert (job["result_width"], job["result_height"]) == (720, 1280)
    assert job["result_duration_seconds"] == pytest.approx(5.0, abs=0.2)
    clip_events = [e for e in _events("studio_job") if e.entity_id == job["id"]]
    assert [e.status for e in clip_events] == ["completed"]
    assert clip_events[0].url == "/studio"


def test_missing_reference_ids_are_listed(auth_client):
    aid = _avatar(auth_client)
    asset = _upload(auth_client, aid)
    jid = _job(owner_id=aid, reference_asset_ids_json=[asset, 99999])
    out = auth_client.get(f"/api/studio/jobs/{jid}").json()
    assert out["missing_reference_ids"] == [99999]
    assert out["reference_asset_ids"] == [asset, 99999]


# ---------- Animate this ----------


@pytest.fixture
def video_calls(monkeypatch):
    from app.providers import mock_video

    calls: list[dict] = []
    real = mock_video.MockVideoProvider.submit

    def spy(self, **kw):
        calls.append(kw)
        return real(self, **kw)

    monkeypatch.setattr(mock_video.MockVideoProvider, "submit", spy)
    return calls


def test_animate_this_sends_the_picture_as_first_frame(auth_client, video_calls):
    aid = _avatar(auth_client)
    pic = _image(auth_client, aid)
    assert pic["status"] == "completed"
    job = _clip(auth_client, aid, first_frame_job_id=pic["id"])
    assert job["first_frame_job_id"] == pic["id"]
    assert job["status"] == "completed"
    call = video_calls[-1]
    assert call["reference_strategy"] == "frame_images"
    assert len(call["references"]) == 1
    ref = call["references"][0]
    assert ref.frame_type == "first_frame"
    assert "/api/public-media/" in ref.url
    # Retry keeps the first frame.
    r = auth_client.post(f"/api/studio/jobs/{job['id']}/retry", json={})
    assert r.status_code == 202, r.text
    assert r.json()["first_frame_job_id"] == pic["id"]
    assert video_calls[-1]["reference_strategy"] == "frame_images"


def test_animate_this_from_a_cast_picture(auth_client, video_calls):
    aid = _avatar(auth_client)
    asset = _upload(auth_client, aid)
    job = _clip(auth_client, aid, first_frame_asset_id=asset)
    assert job["first_frame_asset_id"] == asset
    assert video_calls[-1]["references"][0].frame_type == "first_frame"


def test_animate_this_refused_for_models_without_first_frame(auth_client, monkeypatch, video_calls):
    from app.providers import mock_video
    from app.providers.base import VideoCapabilities

    aid = _avatar(auth_client)
    pic = _image(auth_client, aid)
    monkeypatch.setattr(mock_video.MockVideoProvider, "capabilities",
                        lambda self, model: VideoCapabilities(durations=[5], first_frame=False))
    r = auth_client.post("/api/studio/generate-clip", json={
        "owner_kind": "avatar", "owner_id": aid, "prompt": "go", "first_frame_job_id": pic["id"]})
    assert r.status_code == 422
    assert "can't start from a picture" in r.json()["detail"]
    assert video_calls == []


def test_animate_this_validation(auth_client):
    aid = _avatar(auth_client)
    clip_job = _job(owner_id=aid, output_kind="video_clip")
    body = {"owner_kind": "avatar", "owner_id": aid, "prompt": "go"}
    # A clip can't be a first frame; an image job can't take one; deleted → 404.
    assert auth_client.post("/api/studio/generate-clip",
                            json={**body, "first_frame_job_id": clip_job}).status_code == 422
    pic = _image(auth_client, aid)
    assert auth_client.post("/api/studio/generate-image",
                            json={**body, "first_frame_job_id": pic["id"]}).status_code == 422
    auth_client.delete(f"/api/studio/jobs/{pic['id']}")
    assert auth_client.post("/api/studio/generate-clip",
                            json={**body, "first_frame_job_id": pic["id"]}).status_code == 404


def test_video_model_list_says_which_models_take_a_first_frame(auth_client):
    r = auth_client.get("/api/providers/openrouter/video-models")
    assert r.status_code == 200, r.text
    assert r.json() and all("first_frame" in m for m in r.json())
    assert any(m["first_frame"] for m in r.json())


# ---------- cancel ----------


def test_cancel_running_job(auth_client):
    jid = _job(status="polling", output_kind="video_clip")
    r = auth_client.post(f"/api/studio/jobs/{jid}/cancel")
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["error"]) == ("failed", "Cancelled")
    assert [e.status for e in _events("studio_job")] == ["cancelled"]
    # Finished jobs are answered unchanged; deleted ones are 404.
    done = _job(status="completed")
    assert auth_client.post(f"/api/studio/jobs/{done}/cancel").json()["status"] == "completed"
    gone = _job(status="deleted")
    assert auth_client.post(f"/api/studio/jobs/{gone}/cancel").status_code == 404


def test_a_cancelled_clip_that_lands_later_stays_cancelled(auth_client, tmp_path):
    from app import models
    from app.db import SessionLocal
    from app.services import pipeline

    jid = _job(status="failed", error="Cancelled", output_kind="video_clip")
    f = tmp_path / "vjob.mp4"
    f.write_bytes(b"x")
    with SessionLocal() as db:
        v = models.VideoJob(purpose="studio", ref_id=jid, status="completed", result_path=str(f))
        db.add(v)
        db.commit()
        pipeline.on_studio_clip_ready(db, v)
    j = _get_job(jid)
    assert (j.status, j.error, j.result_path) == ("failed", "Cancelled", "")
    assert not f.exists()


# ---------- enqueue failure ----------


def test_enqueue_failure_marks_jobs_failed(auth_client, monkeypatch):
    from app.api import studio
    from app.workers.queue import QueueUnavailable

    def boom(*a, **k):
        raise QueueUnavailable("job queue (Redis) unavailable and QUEUE_MODE=required")

    monkeypatch.setattr(studio, "enqueue", boom)
    aid = _avatar(auth_client)
    r = auth_client.post("/api/studio/generate-image", json={
        "owner_kind": "avatar", "owner_id": aid, "prompt": "p", "variations": 3})
    assert r.status_code == 503
    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        rows = db.query(models.AssetGenerationJob).all()
        assert len(rows) == 3
        assert {(j.status, j.error) for j in rows} == {("failed", "Couldn't queue this job")}


# ---------- sweeper ----------


def _age(job_id: int, *, created: timedelta, updated: timedelta | None = None) -> None:
    from sqlalchemy import update

    from app import models
    from app.db import SessionLocal

    now = datetime.utcnow()
    with SessionLocal() as db:
        db.execute(update(models.AssetGenerationJob).where(models.AssetGenerationJob.id == job_id)
                   .values(created_at=now - created, updated_at=now - (updated if updated is not None else created)))
        db.commit()


def test_sweeper_requeues_a_lost_pending_job_once_then_fails_it(auth_client, monkeypatch):
    from app.db import SessionLocal
    from app.services import sweepers
    from app.workers import queue

    queued: list = []
    monkeypatch.setattr(queue, "enqueue", lambda fn, *a, **k: queued.append(a) or "rq:1")
    jid = _job(status="pending")
    _age(jid, created=timedelta(minutes=11))
    with SessionLocal() as db:
        assert sweepers.sweep_studio(db)["requeued"] == 1
        assert sweepers.sweep_studio(db)["requeued"] == 0  # once only
    assert queued == [(jid,)]
    assert _get_job(jid).status == "pending"
    with SessionLocal() as db:
        out = sweepers.sweep_studio(db, now=datetime.utcnow() + timedelta(minutes=25))
    assert out["failed"] == 1
    j = _get_job(jid)
    assert (j.status, j.error) == ("failed", "Couldn't start this job; try again")
    assert [e.status for e in _events("studio_job")] == ["failed"]


def test_sweeper_fails_a_submitted_job_past_the_queue_timeout(auth_client):
    from app.config import get_settings
    from app.db import SessionLocal
    from app.services import sweepers

    jid = _job(status="submitted")
    fresh = _job(status="submitted")
    _age(jid, created=timedelta(seconds=get_settings().queue_job_timeout_seconds + 6 * 60))
    with SessionLocal() as db:
        sweepers.sweep_studio(db)
    assert (_get_job(jid).status, _get_job(jid).error) == ("failed", "Stopped unexpectedly")
    assert _get_job(fresh).status == "submitted"


def test_sweeper_fails_polling_without_a_video_job(auth_client):
    from app.db import SessionLocal
    from app.services import sweepers

    old = _job(status="polling", output_kind="video_clip")
    young = _job(status="polling", output_kind="video_clip")
    _age(old, created=timedelta(minutes=11))
    _age(young, created=timedelta(minutes=2))
    with SessionLocal() as db:
        sweepers.sweep_studio(db)
    assert _get_job(old).status == "failed"
    assert _get_job(young).status == "polling"


def test_sweeper_applies_a_finished_clip_whose_handler_raised(auth_client, monkeypatch):
    from app import models
    from app.db import SessionLocal
    from app.providers.mock_video import _generate_clip
    from app.services import sweepers

    jid = _job(status="polling", output_kind="video_clip")
    _age(jid, created=timedelta(minutes=5))
    from app.config import get_settings

    clip = get_settings().data_path / "studio" / "clips" / "vjob_x.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(_generate_clip("p", 2.0, 0))
    with SessionLocal() as db:
        db.add(models.VideoJob(purpose="studio", ref_id=jid, status="completed", result_path=str(clip),
                               completed_at=datetime.utcnow() - timedelta(minutes=3)))
        db.commit()
        out = sweepers.sweep_studio(db)
    assert out["applied"] == 1
    j = _get_job(jid)
    assert j.status == "completed" and Path(j.result_path).is_file()
    assert j.result_width == 720


def test_sweeper_fails_when_applying_raises_again(auth_client, monkeypatch, tmp_path):
    from app import models
    from app.db import SessionLocal
    from app.services import pipeline, sweepers

    def boom(db, vjob):
        raise OSError("disk full")

    monkeypatch.setattr(pipeline, "on_studio_clip_ready", boom)
    jid = _job(status="polling", output_kind="video_clip")
    _age(jid, created=timedelta(minutes=5))
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    with SessionLocal() as db:
        db.add(models.VideoJob(purpose="studio", ref_id=jid, status="completed", result_path=str(f),
                               completed_at=datetime.utcnow() - timedelta(minutes=3)))
        db.commit()
        sweepers.sweep_studio(db)
    j = _get_job(jid)
    assert j.status == "failed" and "couldn't be saved" in j.error


# ---------- a Studio clip in a storyboard project ----------


def _planned_project(client, aid: int, aspect: str = "9:16") -> int:
    pid = client.post("/api/projects", json={
        "title": "Beach day", "original_script": "one two three four five", "cta_text": "Go",
        "aspect_ratio": aspect, "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
    }).json()["id"]
    r = client.post(f"/api/projects/{pid}/generate-plan")
    assert r.status_code == 202, r.text
    return pid


def _body_shot(client, pid: int, duration: float | None = None) -> int:
    shots = client.get(f"/api/projects/{pid}").json()["shots"]
    sid = [s for s in shots if s["shot_type"] not in ("end_card", "talking_head")][0]["id"]
    if duration is not None:
        from app import models
        from app.db import SessionLocal

        with SessionLocal() as db:
            db.get(models.VideoShot, sid).duration_seconds = duration
            db.commit()
    return sid


def test_studio_clip_becomes_the_current_take(auth_client):
    from app import models
    from app.db import SessionLocal
    from app.services import fingerprints
    from app.services.renderer import _probe_duration

    aid = _avatar(auth_client)
    pid = _planned_project(auth_client, aid)
    sid = _body_shot(auth_client, pid, duration=3.0)
    job = _clip(auth_client, aid, duration_seconds=5)
    r = auth_client.post(f"/api/projects/{pid}/shots/{sid}/takes/from-studio", json={"studio_job_id": job["id"]})
    assert r.status_code == 201, r.text
    take = r.json()
    assert take["current"] is True
    assert take["note"] == f"From Studio #{job['id']}"
    assert take["aspect_ratio"] == "9:16"
    assert take["warning"] == ""
    assert take["duration_seconds"] == pytest.approx(3.0)
    with SessionLocal() as db:
        shot = db.get(models.VideoShot, sid)
        row = db.get(models.ShotTake, take["id"])
        assert shot.clip_path == row.clip_path != job["result_path"]  # a copy
        assert Path(row.clip_path).is_file() and Path(job["result_path"]).is_file()
        assert _probe_duration(Path(row.clip_path)) == pytest.approx(3.0, abs=0.15)  # trimmed
        assert row.clip_fingerprint == fingerprints.video_fingerprint(shot.project, shot)
        assert row.video_job_id is not None
        assert shot.status == "completed" and not shot.clip_stale
    # Studio delete keeps the project's copy.
    auth_client.delete(f"/api/studio/jobs/{job['id']}")
    assert auth_client.get(take["url"]).status_code == 200


def test_studio_clip_shorter_than_the_shot_warns(auth_client):
    aid = _avatar(auth_client)
    pid = _planned_project(auth_client, aid)
    sid = _body_shot(auth_client, pid, duration=8.0)
    job = _clip(auth_client, aid, duration_seconds=5)
    r = auth_client.post(f"/api/projects/{pid}/shots/{sid}/takes/from-studio", json={"studio_job_id": job["id"]})
    assert r.status_code == 201, r.text
    assert r.json()["warning"] == "This clip is 5.0 s; the shot is 8.0 s, so the video will be 3.0 s shorter."


def test_studio_clip_aspect_mismatch_warns_and_is_not_remade(auth_client):
    from app import models
    from app.db import SessionLocal

    aid = _avatar(auth_client)
    pid = _planned_project(auth_client, aid, aspect="16:9")
    sid = _body_shot(auth_client, pid, duration=5.0)
    job = _clip(auth_client, aid, duration_seconds=5)
    r = auth_client.post(f"/api/projects/{pid}/shots/{sid}/takes/from-studio", json={"studio_job_id": job["id"]})
    assert r.status_code == 201, r.text
    # The take counts as the project's shape; its real shape is in the note.
    assert r.json()["aspect_ratio"] == "16:9"
    assert r.json()["note"] == f"From Studio #{job['id']} (9:16 clip)"
    assert "This clip is 9:16; the project is 16:9, so it will be cropped/letterboxed." in r.json()["warning"]
    with SessionLocal() as db:
        assert not db.get(models.VideoShot, sid).clip_stale
    # Using it again later (after picking another take) keeps it current.
    with SessionLocal() as db:
        shot = db.get(models.VideoShot, sid)
        shot.clip_aspect_ratio = "9:16"
        db.commit()
    u = auth_client.post(f"/api/projects/{pid}/shots/{sid}/takes/{r.json()['id']}/use")
    assert u.status_code in (200, 204), u.text
    with SessionLocal() as db:
        shot = db.get(models.VideoShot, sid)
        assert shot.clip_aspect_ratio == "16:9" and not shot.clip_stale


def test_from_studio_refusals(auth_client):
    from app import models
    from app.db import SessionLocal

    aid = _avatar(auth_client)
    pid = _planned_project(auth_client, aid)
    sid = _body_shot(auth_client, pid)
    url = f"/api/projects/{pid}/shots/{sid}/takes/from-studio"
    pic = _image(auth_client, aid)
    assert auth_client.post(url, json={"studio_job_id": pic["id"]}).status_code == 422
    assert auth_client.post(url, json={"studio_job_id": 9999}).status_code == 404
    running = _job(owner_id=aid, output_kind="video_clip", status="polling")
    assert auth_client.post(url, json={"studio_job_id": running}).status_code == 409
    job = _clip(auth_client, aid)
    # A clip for the shot still being made.
    with SessionLocal() as db:
        db.add(models.VideoJob(purpose="shot", ref_id=sid, project_id=pid, status="submitted"))
        db.commit()
    r = auth_client.post(url, json={"studio_job_id": job["id"]})
    assert r.status_code == 409 and "still being made" in r.json()["detail"]
    # The end card can't take one.
    shots = auth_client.get(f"/api/projects/{pid}").json()["shots"]
    end = [s for s in shots if s["shot_type"] == "end_card"]
    if end:
        r = auth_client.post(f"/api/projects/{pid}/shots/{end[0]['id']}/takes/from-studio",
                             json={"studio_job_id": job["id"]})
        assert r.status_code == 409
    # Deleted Studio job.
    auth_client.delete(f"/api/studio/jobs/{job['id']}")
    with SessionLocal() as db:
        db.query(models.VideoJob).filter(models.VideoJob.purpose == "shot").delete()
        db.commit()
    assert auth_client.post(url, json={"studio_job_id": job["id"]}).status_code == 404


# ---------- a busy queue, races, cancel ----------


def test_sweeper_leaves_a_pending_job_alone_while_its_queue_entry_waits(auth_client, monkeypatch):
    from app import models
    from app.db import SessionLocal
    from app.services import sweepers
    from app.workers import queue

    states = {"rq-busy": "alive", "rq-unknown": "unknown"}
    monkeypatch.setattr(queue, "queue_entry_state", lambda qid: states.get(qid, "gone"))
    monkeypatch.setattr(queue, "enqueue", lambda fn, *a, **k: "rq-new")
    waiting = _job(status="pending", queue_job_id="rq-busy")
    unknown = _job(status="pending", queue_job_id="rq-unknown")
    _age(waiting, created=timedelta(minutes=45))
    _age(unknown, created=timedelta(minutes=45))
    with SessionLocal() as db:
        out = sweepers.sweep_studio(db)
    assert out == {"requeued": 0, "failed": 0, "applied": 0}
    assert _get_job(waiting).status == "pending" and _get_job(unknown).status == "pending"
    # Its entry gone: re-enqueued once, and the new entry's id is kept.
    lost = _job(status="pending", queue_job_id="rq-lost")
    _age(lost, created=timedelta(minutes=11))
    with SessionLocal() as db:
        assert sweepers.sweep_studio(db)["requeued"] == 1
        assert db.get(models.AssetGenerationJob, lost).queue_job_id == "rq-new"


def test_studio_enqueue_keeps_the_queue_job_id(auth_client, monkeypatch):
    from app.api import studio

    monkeypatch.setattr(studio, "enqueue", lambda fn, *a, **k: "rq-abc")
    aid = _avatar(auth_client)
    r = auth_client.post("/api/studio/generate-image", json={"owner_kind": "avatar", "owner_id": aid, "prompt": "p"})
    assert r.status_code in (200, 201, 202), r.text
    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        assert {j.queue_job_id for j in db.query(models.AssetGenerationJob).all()} == {"rq-abc"}


def test_run_studio_job_never_undoes_a_cancel(auth_client):
    from app.db import SessionLocal
    from app.services import pipeline

    jid = _job(status="failed", error="Cancelled", output_kind="image")
    with SessionLocal() as db:
        pipeline.run_studio_job(db, jid)
    j = _get_job(jid)
    assert (j.status, j.error, j.result_path) == ("failed", "Cancelled", "")


def _finished_vjob(jid: int) -> tuple[int, Path]:
    from app import models
    from app.config import get_settings
    from app.db import SessionLocal
    from app.providers.mock_video import _generate_clip

    clip = get_settings().data_path / "studio" / "clips" / f"vjob_race_{jid}.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(_generate_clip("p", 2.0, 0))
    with SessionLocal() as db:
        v = models.VideoJob(purpose="studio", ref_id=jid, status="completed", result_path=str(clip))
        db.add(v)
        db.commit()
        return v.id, clip


def test_a_finished_clip_is_applied_once(auth_client, monkeypatch):
    from app import models
    from app.db import SessionLocal
    from app.services import pipeline, storage

    jid = _job(status="polling", output_kind="video_clip")
    vid, clip = _finished_vjob(jid)
    saved: list[Path] = []
    real_save = storage.save_generated_bytes

    def save(*a, **k):
        saved.append(Path(real_save(*a, **k)))
        return saved[-1]

    monkeypatch.setattr(storage, "save_generated_bytes", save)
    with SessionLocal() as a, SessionLocal() as b:
        va, vb = a.get(models.VideoJob, vid), b.get(models.VideoJob, vid)
        held = (a.get(models.AssetGenerationJob, jid), b.get(models.AssetGenerationJob, jid))  # both read "polling"
        assert all(h.status == "polling" for h in held)
        clip_bytes = clip.read_bytes()
        pipeline.on_studio_clip_ready(a, va)
        clip.write_bytes(clip_bytes)  # the second sweeper read it before the first removed it
        pipeline.on_studio_clip_ready(b, vb)
    j = _get_job(jid)
    assert j.status == "completed" and Path(j.result_path).is_file()
    assert [e.status for e in _events("studio_job")] == ["completed"]
    assert len(saved) == 2 and [p for p in saved if p.exists()] == [Path(j.result_path)]  # no orphan


def test_a_failed_save_keeps_the_provider_file_for_the_retry(auth_client, monkeypatch):
    from app import models
    from app.db import SessionLocal
    from app.services import events as events_svc
    from app.services import pipeline

    jid = _job(status="polling", output_kind="video_clip")
    vid, clip = _finished_vjob(jid)

    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(events_svc, "studio_event", boom)
    with SessionLocal() as db:
        with pytest.raises(RuntimeError):
            pipeline.on_studio_clip_ready(db, db.get(models.VideoJob, vid))
    assert clip.is_file()  # still there for the sweeper's retry
    assert _get_job(jid).status == "polling"
    monkeypatch.undo()
    with SessionLocal() as db:
        pipeline.on_studio_clip_ready(db, db.get(models.VideoJob, vid))
    assert _get_job(jid).status == "completed" and not clip.exists()

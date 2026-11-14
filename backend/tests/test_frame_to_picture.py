"""Save a frame of a video as a picture: a Studio clip's first, last or any
frame (free, finished at once), and a frame of an uploaded video, which
the picture safety check sees before it is stored."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from PIL import Image

from .test_image_ops import _avatar, _job_with_file, _open, _picture


def _clip(client, aid: int) -> dict:
    """A finished Studio clip (mock: a real short video)."""
    r = client.post("/api/studio/generate-clip", json={"owner_kind": "avatar", "owner_id": aid,
                                                      "prompt": "slow push-in"})
    assert r.status_code == 202, r.text
    job = client.get(f"/api/studio/jobs/{r.json()['id']}").json()
    assert job["status"] == "completed" and Path(job["result_path"]).exists(), job
    return job


def _video(tmp_path: Path, size=(96, 64), seconds: float = 2.0, name: str = "holiday.mp4") -> Path:
    """A small real video: the frame number drawn over a test pattern."""
    path = tmp_path / name
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    f"testsrc=size={size[0]}x{size[1]}:rate=10:duration={seconds}",
                    "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def _size_of_video(path: str) -> tuple[int, int]:
    from app.services.renderer import _probe_size

    size = _probe_size(Path(path))
    assert size is not None
    return size


def _upload(client, path: Path, aid: int, at="first", content_type: str = "video/mp4"):
    with open(path, "rb") as f:
        return client.post("/api/studio/frame-from-video",
                           files={"file": (path.name, f.read(), content_type)},
                           data={"owner_kind": "avatar", "owner_id": str(aid), "at": str(at)})


def _spend_rows() -> int:
    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        return db.query(models.SpendEntry).count()


def _frame_jobs() -> int:
    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        return db.query(models.AssetGenerationJob).filter(
            models.AssetGenerationJob.operation == "extract_frame").count()


# ---------- a Studio clip ----------


def test_renderer_extract_frame_at_keeps_the_time_inside_the_video(tmp_path):
    from app.services.renderer import extract_frame_at

    video = _video(tmp_path, seconds=2.0)
    out = tmp_path / "f.png"
    assert extract_frame_at(video, out, 1.0) == 1.0
    assert Image.open(out).size == (96, 64)
    assert extract_frame_at(video, out, -3) == 0.0
    last = extract_frame_at(video, out, 99)
    assert 1.0 < last <= 2.0 - 0.05 and Image.open(out).size == (96, 64)
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video")
    with pytest.raises(RuntimeError):
        extract_frame_at(bad, out, 0)


def test_last_frame_is_found_when_the_sound_outlasts_the_picture(tmp_path):
    """2 s of picture (red, then blue) with 4 s of sound: the last frame is
    the blue one near 2 s, not the first frame."""
    import subprocess

    from app.services.renderer import extract_frame_at

    video = tmp_path / "long_sound.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", "color=red:s=96x64:r=24:d=1",
                    "-f", "lavfi", "-i", "color=blue:s=96x64:r=24:d=1",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                    "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
                    "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(video)], check=True)
    out = tmp_path / "last.png"
    used = extract_frame_at(video, out, 99)
    assert 1.5 < used <= 2.0
    r, g, b = Image.open(out).convert("RGB").getpixel((48, 32))
    assert b > 150 and r < 100


def test_first_last_and_a_time_are_free_pictures_of_the_clips_size(auth_client):
    from app import models
    from app.db import SessionLocal

    aid = _avatar(auth_client)
    clip = _clip(auth_client, aid)
    size = _size_of_video(clip["result_path"])
    spent = _spend_rows()
    made = []
    for at in ("first", "last", 1.0):
        r = auth_client.post(f"/api/studio/jobs/{clip['id']}/frame-to-picture", json={"at": at})
        assert r.status_code == 201, r.text
        job = r.json()
        assert job["status"] == "completed" and job["batch_ids"] == [job["id"]]
        assert (job["output_kind"], job["operation"], job["source_job_id"]) == ("image", "extract_frame", clip["id"])
        assert (job["owner_kind"], job["owner_id"]) == ("avatar", aid)
        assert job["provider_model"] == "local/frame-grab" and job["provider"] == "local"
        assert job["options"]["at"] == at and "seconds" in job["options"]
        img = _open(job["result_path"])
        assert img.format == "PNG" and img.size == size
        assert (job["result_width"], job["result_height"]) == size
        assert job["label"].startswith(f"Frame from clip #{clip['id']} (0:0")
        assert job["cost_usd"] is None
        made.append(job)
    assert made[0]["options"]["seconds"] == 0.0 and made[0]["label"].endswith("(0:00.0)")
    assert made[2]["options"]["seconds"] == 1.0 and made[2]["label"].endswith("(0:01.0)")
    assert made[1]["options"]["seconds"] > 1.0
    assert _spend_rows() == spent  # nothing billed
    with SessionLocal() as db:
        done = db.query(models.Event).filter(models.Event.entity_type == "studio_job",
                                             models.Event.status == "completed",
                                             models.Event.entity_id.in_([j["id"] for j in made])).count()
    assert done == 3
    # The picture is an ordinary Studio picture: it can be saved to the cast,
    # upscaled and have its background removed.
    assert auth_client.post(f"/api/studio/jobs/{made[0]['id']}/save").status_code == 200
    assert auth_client.post(f"/api/studio/jobs/{made[1]['id']}/upscale", json={"factor": 2}).status_code == 202


def test_bad_times_unfinished_deleted_and_not_a_clip(auth_client, tmp_path):
    aid = _avatar(auth_client)
    clip = _clip(auth_client, aid)
    url = f"/api/studio/jobs/{clip['id']}/frame-to-picture"
    for bad in (-1, "middle", None):
        assert auth_client.post(url, json={"at": bad}).status_code == 422, bad
    running = _job_with_file(tmp_path, output_kind="video_clip", status="polling", owner_id=aid)
    r = auth_client.post(f"/api/studio/jobs/{running}/frame-to-picture", json={"at": "last"})
    assert r.status_code == 409 and r.json()["detail"] == "That clip isn't finished yet"
    picture = _picture(auth_client, aid)
    r = auth_client.post(f"/api/studio/jobs/{picture['id']}/frame-to-picture")
    assert r.status_code == 422
    assert auth_client.post("/api/studio/jobs/99999/frame-to-picture").status_code == 404
    assert auth_client.delete(f"/api/studio/jobs/{clip['id']}").status_code == 204
    r = auth_client.post(url, json={"at": "first"})
    assert r.status_code == 404 and r.json()["detail"] == "That clip was deleted"
    assert _frame_jobs() == 0


def test_remove_background_and_upscale_still_refuse_a_clip(auth_client):
    aid = _avatar(auth_client)
    clip = _clip(auth_client, aid)
    r = auth_client.post(f"/api/studio/jobs/{clip['id']}/remove-background")
    assert r.status_code == 422 and "not clips" in r.json()["detail"]
    r = auth_client.post(f"/api/studio/jobs/{clip['id']}/upscale", json={"factor": 2})
    assert r.status_code == 422 and "not clips" in r.json()["detail"]


def test_free_whatever_the_budget_and_never_asks_the_budget_guard(auth_client, monkeypatch):
    from app import models
    from app.api import studio
    from app.db import SessionLocal

    aid = _avatar(auth_client)
    clip = _clip(auth_client, aid)
    auth_client.put("/api/spend/budget", json={"monthly_usd": 1, "hard_stop": True, "alert_pcts": []})
    with SessionLocal() as db:
        db.add(models.SpendEntry(provider="runware", kind="image", model="m", cost_usd=2.0))
        db.commit()

    def guard():
        raise AssertionError("budget_guard called")

    monkeypatch.setattr(studio, "budget_guard", guard)
    r = auth_client.post(f"/api/studio/jobs/{clip['id']}/frame-to-picture", json={"at": "last"})
    assert r.status_code == 201, r.text
    r = auth_client.post(f"/api/studio/jobs/{r.json()['id']}/retry")
    assert r.status_code == 201, r.text


def test_retry_grabs_the_frame_again_and_fails_plainly_once_the_clip_is_gone(auth_client):
    aid = _avatar(auth_client)
    clip = _clip(auth_client, aid)
    first = auth_client.post(f"/api/studio/jobs/{clip['id']}/frame-to-picture", json={"at": 1.0}).json()
    r = auth_client.post(f"/api/studio/jobs/{first['id']}/retry", json={"model": "mock/anything", "prompt": "x"})
    assert r.status_code == 201, r.text
    again = r.json()
    assert again["id"] != first["id"] and again["status"] == "completed"
    assert (again["operation"], again["source_job_id"], again["options"]) == (
        "extract_frame", clip["id"], first["options"])
    assert again["label"] == first["label"] and again["provider_model"] == "local/frame-grab"
    assert _open(again["result_path"]).size == _open(first["result_path"]).size
    auth_client.delete(f"/api/studio/jobs/{clip['id']}")
    assert auth_client.post(f"/api/studio/jobs/{first['id']}/retry").status_code == 404


def test_without_ffmpeg_in_the_api_the_frame_is_queued_for_the_worker(auth_client, monkeypatch):
    from app.services import video_ops

    aid = _avatar(auth_client)
    clip = _clip(auth_client, aid)
    monkeypatch.setattr(video_ops, "ffmpeg_here", lambda: False)
    r = auth_client.post(f"/api/studio/jobs/{clip['id']}/frame-to-picture", json={"at": "first"})
    assert r.status_code == 202, r.text
    # Tests run the queue inline: the worker grabbed it.
    job = auth_client.get(f"/api/studio/jobs/{r.json()['id']}").json()
    assert job["status"] == "completed" and job["options"]["seconds"] == 0.0
    assert _open(job["result_path"]).size == _size_of_video(clip["result_path"])


def test_a_frame_of_a_clip_is_never_cast_exempt(auth_client):
    """Even a clip of a Cast character marked "adult content allowed": its
    frame is a new Studio picture, not a picture on the card."""
    from app.db import SessionLocal
    from app.services import pipeline

    aid = _avatar(auth_client)
    clip = _clip(auth_client, aid)
    frame = auth_client.post(f"/api/studio/jobs/{clip['id']}/frame-to-picture", json={"at": "last"}).json()
    up = auth_client.post(f"/api/studio/jobs/{frame['id']}/upscale", json={"factor": 2}).json()
    from app import models

    with SessionLocal() as db:
        for jid in (frame["id"], up["id"]):
            job = db.get(models.AssetGenerationJob, jid)
            assert pipeline.studio_cast_source(db, job) == (None, None)
            avatar_id, source = pipeline.studio_cast_source(db, job)
            assert pipeline.avatar_adult_ok(avatar_id, source, {aid: frozenset({1, 2, 3})}) is False


# ---------- an uploaded video ----------


def test_uploaded_video_frame_is_checked_then_stored_and_the_video_not_kept(auth_client, tmp_path):
    from app.providers import mock_chat

    aid = _avatar(auth_client)
    video = _video(tmp_path, size=(80, 48))
    spent = _spend_rows()
    r = _upload(auth_client, video, aid, at="1.0")
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["status"] == "completed" and job["operation"] == "extract_frame"
    assert (job["source_job_id"], job["source_asset_id"]) == (None, None)
    assert (job["owner_kind"], job["owner_id"]) == ("avatar", aid)
    assert job["options"] == {"at": 1.0, "upload": "holiday.mp4", "seconds": 1.0}
    assert job["label"] == "Frame from holiday.mp4 (0:01.0)"
    assert _open(job["result_path"]).size == (80, 48)
    assert mock_chat.PICTURE_CALLS == [0]  # the picture check saw it
    assert mock_chat.PICTURE_EXPLICIT_CALLS == []  # not a clean deployment
    assert _spend_rows() == spent
    # No copy of the video stays anywhere the app keeps files.
    from app.config import get_settings

    assert not [p for p in get_settings().data_path.rglob("*") if p.suffix in (".mp4", ".mov")
                and "holiday" in p.name]
    # Not kept, so it can't be grabbed again.
    r = auth_client.post(f"/api/studio/jobs/{job['id']}/retry")
    assert r.status_code == 409 and "upload it again" in r.json()["detail"]
    # It belongs to no Cast card: it never skips the filter.
    from app import models
    from app.db import SessionLocal
    from app.services import pipeline

    with SessionLocal() as db:
        avatar_id, source = pipeline.studio_cast_source(db, db.get(models.AssetGenerationJob, job["id"]))
    assert source is None and pipeline.avatar_adult_ok(avatar_id, source, {aid: frozenset({1})}) is False


def test_uploaded_frame_refused_when_the_judge_says_under_18(auth_client, tmp_path):
    from app.providers import mock_chat

    aid = _avatar(auth_client)
    mock_chat.PICTURE_REPLY = {"minor": True, "reason": "A child."}
    r = _upload(auth_client, _video(tmp_path), aid)
    assert r.status_code == 422 and "under 18" in r.json()["detail"]
    assert _frame_jobs() == 0


@pytest.mark.parametrize("reply", [RuntimeError("down"), {"minor": None, "reason": "no picture"}])
def test_uploaded_frame_fails_closed_when_the_check_cant_answer(auth_client, tmp_path, reply):
    from app.providers import mock_chat

    aid = _avatar(auth_client)
    mock_chat.PICTURE_REPLY = reply
    r = _upload(auth_client, _video(tmp_path), aid)
    assert r.status_code == 503 and "couldn't look" in r.json()["detail"]
    assert _frame_jobs() == 0


def test_uploaded_frame_fails_closed_without_a_picture_model(auth_client, tmp_path, monkeypatch):
    from app import config

    aid = _avatar(auth_client)
    monkeypatch.setenv("CHARACTER_PICTURE_CHECK_MODEL", "")
    config.get_settings.cache_clear()
    r = _upload(auth_client, _video(tmp_path), aid)
    assert r.status_code == 503
    assert _frame_jobs() == 0


def test_uploaded_frame_refused_when_the_judge_declines(auth_client, tmp_path):
    from app.providers import mock_chat
    from app.providers.openrouter_chat import ModelDeclined

    aid = _avatar(auth_client)
    mock_chat.PICTURE_REPLY = ModelDeclined("no")
    r = _upload(auth_client, _video(tmp_path), aid)
    assert r.status_code == 422 and "declined" in r.json()["detail"]
    assert _frame_jobs() == 0


def test_upload_must_be_a_readable_video_for_a_real_owner(auth_client, tmp_path):
    aid = _avatar(auth_client)
    fake = tmp_path / "notes.mp4"
    fake.write_bytes(b"hello")
    r = _upload(auth_client, fake, aid)
    assert r.status_code == 422 and "isn't a video" in r.json()["detail"]
    png = tmp_path / "p.png"
    Image.new("RGB", (10, 10)).save(png)
    assert _upload(auth_client, png, aid, content_type="image/png").status_code == 422
    assert _upload(auth_client, _video(tmp_path), 9999).status_code == 404
    assert _upload(auth_client, _video(tmp_path), aid, at="soon").status_code == 422
    assert _frame_jobs() == 0


def test_clean_deployment_also_asks_whether_the_frame_is_explicit(auth_client, tmp_path, monkeypatch):
    from app.providers import mock_chat

    from .test_adult_features_off import _clean

    aid = _avatar(auth_client)
    _clean(monkeypatch)
    video = _video(tmp_path)
    r = _upload(auth_client, video, aid)
    assert r.status_code == 201, r.text
    assert mock_chat.PICTURE_CALLS == [0] and len(mock_chat.PICTURE_EXPLICIT_CALLS) == 1
    # The same frame again: the "ok" is remembered.
    assert _upload(auth_client, video, aid).status_code == 201
    assert len(mock_chat.PICTURE_EXPLICIT_CALLS) == 1

    other = _video(tmp_path, size=(64, 64), name="b.mp4")
    mock_chat.PICTURE_EXPLICIT_REPLY = {"explicit": True, "reason": "Nudity."}
    r = _upload(auth_client, other, aid)
    assert r.status_code == 422 and "general audience" in r.json()["detail"]
    mock_chat.PICTURE_EXPLICIT_REPLY = RuntimeError("down")
    r = _upload(auth_client, other, aid)
    assert r.status_code == 503 and "content check couldn't look" in r.json()["detail"]
    mock_chat.PICTURE_EXPLICIT_REPLY = {"explicit": None}
    assert _upload(auth_client, other, aid).status_code == 503
    # The minors check still comes first, and still refuses.
    mock_chat.PICTURE_EXPLICIT_REPLY = None
    mock_chat.PICTURE_REPLY = {"minor": True, "reason": "A child."}
    r = _upload(auth_client, _video(tmp_path, size=(48, 48), name="c.mp4"), aid)
    assert r.status_code == 422 and "under 18" in r.json()["detail"]


def test_frames_of_app_clips_skip_the_picture_check(auth_client):
    from app.providers import mock_chat

    aid = _avatar(auth_client)
    clip = _clip(auth_client, aid)
    mock_chat.PICTURE_REPLY = RuntimeError("down")
    r = auth_client.post(f"/api/studio/jobs/{clip['id']}/frame-to-picture", json={"at": "first"})
    assert r.status_code == 201
    assert mock_chat.PICTURE_CALLS == []


def test_saved_upload_gets_a_sniffed_extension_never_the_uploaders(auth_client, tmp_path, monkeypatch):
    from app.services import video_ops

    mp4 = _video(tmp_path, name="a.mp4")
    webm = _video(tmp_path, name="b.webm")
    junk = tmp_path / "c.bin"
    junk.write_bytes(b"not a video at all")
    assert video_ops.sniffed_extension(mp4) == ".mp4"
    assert video_ops.sniffed_extension(webm) == ".mkv"
    assert video_ops.sniffed_extension(junk) == ""

    seen: list[str] = []
    real_probe = video_ops._probe_upload

    def spy(video, demuxer):
        seen.append(video.name)
        return real_probe(video, demuxer)

    monkeypatch.setattr(video_ops, "_probe_upload", spy)
    aid = _avatar(auth_client)
    # A real MP4 named with another (odd) extension: stored as .mp4, and still read.
    odd = tmp_path / "holiday.PHP"
    odd.write_bytes(mp4.read_bytes())
    r = _upload(auth_client, odd, aid)
    assert r.status_code == 201, r.text
    # WebM declared as a .mov: the content decides.
    mov = tmp_path / "clip.mov"
    mov.write_bytes(webm.read_bytes())
    assert _upload(auth_client, mov, aid, content_type="video/quicktime").status_code == 201
    assert seen == ["upload.mp4", "upload.mkv"]


@pytest.mark.parametrize("at", ["first", "last", "1.5"])
def test_a_live_webm_with_no_stated_length_gives_its_frames(auth_client, tmp_path, at):
    """A browser recording (MediaRecorder: live WebM) states no length; its
    frames are grabbed from the prepared copy, as "Upload a video" keeps it."""
    from app.services import video_ops

    aid = _avatar(auth_client)
    webm = tmp_path / "recording.webm"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x48:rate=10:duration=3",
                    "-c:v", "libvpx", "-f", "webm", "-live", "1", str(webm)], check=True)
    assert video_ops._probe_upload(webm, "matroska")["seconds"] == 0.0  # really no stated length
    r = _upload(auth_client, webm, aid, at=at, content_type="video/webm")
    assert r.status_code == 201, r.text
    job = r.json()
    used = job["options"]["seconds"]
    assert {"first": used == 0.0, "last": 2.5 <= used <= 3.0, "1.5": used == pytest.approx(1.5, abs=0.11)}[at], used
    assert _open(job["result_path"]).size == (64, 48)


def test_frame_from_a_video_goes_the_same_safe_way_as_upload_a_video(auth_client, tmp_path, monkeypatch):
    """Refused on its headers before decoding (a picture past 4K), and too
    long a video is refused once prepared (capped), as Upload a video."""
    from app.services import video_ops

    aid = _avatar(auth_client)
    calls: list = []
    real = video_ops.prepare_upload

    def spy(raw, out, step):
        calls.append(raw)
        return real(raw, out, step)

    monkeypatch.setattr(video_ops, "prepare_upload", spy)
    monkeypatch.setattr(video_ops, "UPLOAD_MAX_SIDE", 64)
    r = _upload(auth_client, _video(tmp_path, size=(96, 64)), aid)
    assert r.status_code == 422 and "96x64" in r.json()["detail"], r.text
    monkeypatch.setattr(video_ops, "UPLOAD_MAX_SIDE", 4096)
    monkeypatch.setattr(video_ops, "UPLOAD_MAX_SECONDS", 1.0)
    r = _upload(auth_client, _video(tmp_path, seconds=4.0, name="long.mp4"), aid, at="last")
    assert r.status_code == 422 and "long" in r.json()["detail"], r.text
    assert len(calls) == 2 and _frame_jobs() == 0

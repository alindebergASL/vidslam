"""Save a frame of a video as a picture: a Studio clip's first, last or any
frame (free, finished at once), and a frame of an uploaded video, which
the picture safety check sees before it is stored."""
from __future__ import annotations

import math
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

    def spy(raw, out, step, **kw):
        calls.append(raw)
        return real(raw, out, step, **kw)

    monkeypatch.setattr(video_ops, "prepare_upload", spy)
    monkeypatch.setattr(video_ops, "UPLOAD_MAX_SIDE", 64)
    r = _upload(auth_client, _video(tmp_path, size=(96, 64)), aid)
    assert r.status_code == 422 and "96x64" in r.json()["detail"], r.text
    monkeypatch.setattr(video_ops, "UPLOAD_MAX_SIDE", 4096)
    monkeypatch.setattr(video_ops, "UPLOAD_MAX_SECONDS", 1.0)
    r = _upload(auth_client, _video(tmp_path, seconds=4.0, name="long.mp4"), aid, at="last")
    assert r.status_code == 422 and "long" in r.json()["detail"], r.text
    assert len(calls) == 2 and _frame_jobs() == 0


def test_a_frame_near_the_start_prepares_only_up_to_it(auth_client, tmp_path, monkeypatch):
    """"first" or a time t re-encodes only up to t and a second (never the
    whole upload); "last" prepares the capped whole. The 10-minute rule
    applies only to a time past it."""
    import time

    from app.services import video_ops

    aid = _avatar(auth_client)
    long = tmp_path / "long.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x48:rate=5:duration=120",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(long)], check=True)
    caps: list[str] = []
    real_raw = video_ops._run_raw

    def spy(cmd, timeout):
        if cmd[0] == "ffmpeg":
            caps.append(cmd[cmd.index("-t") + 1])
        return real_raw(cmd, timeout)

    monkeypatch.setattr(video_ops, "_run_raw", spy)
    begun = time.monotonic()
    r = _upload(auth_client, long, aid, at="first")
    assert r.status_code == 201, r.text
    assert caps == ["1"] and r.json()["options"]["seconds"] == 0.0
    assert time.monotonic() - begun < 30
    r = _upload(auth_client, long, aid, at="30")
    assert r.status_code == 201, r.text
    assert caps[-1] == "31" and r.json()["options"]["seconds"] == pytest.approx(30.0, abs=0.25)
    r = _upload(auth_client, long, aid, at="last")
    assert r.status_code == 201, r.text
    assert caps[-1] == "601" and r.json()["options"]["seconds"] >= 119.0
    # Past the most: only a time past it (or "last") is refused for length.
    monkeypatch.setattr(video_ops, "UPLOAD_MAX_SECONDS", 60.0)
    caps.clear()
    r = _upload(auth_client, long, aid, at="first")
    assert r.status_code == 201, r.text
    r = _upload(auth_client, long, aid, at="45")
    assert r.status_code == 201, r.text
    assert caps == ["1", "46"]
    for at in ("90", "last"):
        r = _upload(auth_client, long, aid, at=at)
        assert r.status_code == 422 and "minutes" in r.json()["detail"], (at, r.text)
    assert caps == ["1", "46"]  # neither was prepared


# ---------- the frame a paused player shows ----------


def _coded_video(tmp_path: Path, fps: str, seconds: float = 3.0, name: str = "", extra=()) -> Path:
    """A video whose frame n is a flat colour coding n: red 40·(n mod 6),
    blue 40·(n div 6 mod 6)."""
    path = tmp_path / (name or f"coded_{fps.replace('/', '_')}.mp4")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    f"color=black:s=64x48:r={fps}:d={seconds},"
                    "geq=r='40*mod(N\\,6)':g='0':b='40*mod(floor(N/6)\\,6)'",
                    *(extra or ("-c:v", "libx264", "-crf", "1", "-pix_fmt", "yuv444p")), str(path)], check=True)
    return path


def _frame_number(png: Path) -> tuple[int, int]:
    r, _g, b = Image.open(png).convert("RGB").getpixel((32, 24))
    return round(r / 40), round(b / 40)


def _code(n: int) -> tuple[int, int]:
    return n % 6, (n // 6) % 6


def _png_depth(path) -> tuple[int, int]:
    """(bits per channel, PNG colour type: 2 RGB, 6 RGBA) from the header."""
    data = Path(path).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return data[24], data[25]


@pytest.mark.parametrize("fps", ["2", "25", "30", "30000/1001"])
def test_the_frame_saved_is_the_one_a_paused_player_shows(tmp_path, fps):
    """A player shows each frame until the next one starts: the frame at or
    before the time, never the next one (ffmpeg's -ss alone gives the next
    one whenever the time falls between two frames)."""
    from fractions import Fraction

    from app.services.renderer import extract_frame_at

    rate = Fraction(fps)
    video = _coded_video(tmp_path, fps)
    out = tmp_path / "f.png"
    last = math.ceil(3 * rate) - 1
    for n in (1, 5, last - 1):
        start = Fraction(n) / rate
        for t in (start + Fraction(1, 1000), start + Fraction(1, 2) / rate, start + 1 / rate - Fraction(1, 1000),
                  Fraction(math.ceil(start * 10**6), 10**6)):  # just after it starts, mid-frame, just before the next
            used = extract_frame_at(video, out, float(t))
            assert _frame_number(out) == _code(n), (fps, n, float(t))
            assert used == round(float(start), 3)
    # Just before a frame starts (to the microsecond): still the one before.
    t = math.floor(Fraction(5) / rate * 10**6) / 10**6 - 1e-6
    extract_frame_at(video, out, t)
    assert _frame_number(out) == _code(4)
    assert extract_frame_at(video, out, math.inf) == round(float(last / rate), 3)
    assert _frame_number(out) == _code(last)
    assert extract_frame_at(video, out, 0) == 0.0 and _frame_number(out) == _code(0)


def test_a_time_is_kept_to_the_microsecond_rounded_down():
    from app.services.video_ops import parse_at

    assert parse_at(0.4999996) == 0.499999  # rounding up would cross into the frame at 0.5
    assert parse_at("1.0") == 1.0 and parse_at(0.3) == 0.3 and parse_at(2) == 2.0
    assert parse_at("0.033366") == 0.033366


def test_save_this_frame_between_two_frames_saves_the_one_on_screen(auth_client, tmp_path):
    """2 fps (red at 0, green at 0.5): paused at 0.25 the player shows red,
    at 0.6 green."""
    aid = _avatar(auth_client)
    video = _coded_video(tmp_path, "2", seconds=2.0)
    for at, frame, seconds in (("0.25", 0, 0.0), ("0.6", 1, 0.5), ("0.999999", 1, 0.5)):
        r = _upload(auth_client, video, aid, at=at)
        assert r.status_code == 201, r.text
        assert r.json()["options"]["seconds"] == seconds
        assert _frame_number(Path(r.json()["result_path"])) == _code(frame)


def test_webm_without_a_stated_length_still_gives_any_frame(auth_client, tmp_path):
    """A browser recording (MediaRecorder: WebM with no duration and no
    index): the first frame, a time and the last frame are all found."""
    from app.services import renderer

    aid = _avatar(auth_client)
    video = _coded_video(tmp_path, "10", name="rec.webm",
                         extra=("-c:v", "libvpx", "-b:v", "1M", "-live", "1", "-f", "webm"))
    info = renderer._probe_video(video)
    assert info is not None and info.duration == 0 and info.end is None
    for at, frame, seconds in (("first", 0, 0.0), ("1.05", 10, 1.0), ("last", 29, 2.9)):
        r = _upload(auth_client, video, aid, at=at, content_type="video/webm")
        assert r.status_code == 201, (at, r.text)
        assert r.json()["options"]["seconds"] == seconds
        assert _frame_number(Path(r.json()["result_path"])) == _code(frame)


def test_ten_bit_and_hdr_frames_are_8_bit_pngs_and_hdr_is_tone_mapped(tmp_path, monkeypatch):
    from app.services import renderer

    ran: list[str] = []
    real_run = renderer._run

    def spy(cmd, timeout=None):
        ran.append(" ".join(cmd))
        return real_run(cmd, timeout=timeout)

    monkeypatch.setattr(renderer, "_run", spy)
    src = ["-f", "lavfi", "-i", "testsrc2=s=160x90:r=25:d=1"]
    x265 = "log-level=error:colorprim=bt2020:colormatrix=bt2020nc:transfer="
    videos = {
        "ten_bit": [*src, "-c:v", "libx264", "-pix_fmt", "yuv420p10le"],
        "pq": [*src, "-c:v", "libx265", "-pix_fmt", "yuv420p10le", "-x265-params", x265 + "smpte2084"],
        "hlg": [*src, "-c:v", "libx265", "-pix_fmt", "yuv420p10le", "-x265-params", x265 + "arib-std-b67"],
        "alpha": ["-f", "lavfi", "-i", "testsrc=size=64x48:rate=10:duration=1", "-vf", "format=yuva444p10le",
                  "-c:v", "prores_ks", "-profile:v", "4444"],
    }
    out = tmp_path / "f.png"
    for name, args in videos.items():
        path = tmp_path / (name + (".mov" if name == "alpha" else ".mp4"))
        subprocess.run(["ffmpeg", "-y", "-v", "error", *args, str(path)], check=True)
        ran.clear()
        assert renderer.extract_frame_at(path, out, 0.5) in (0.48, 0.5)
        assert _png_depth(out) == ((8, 6) if name == "alpha" else (8, 2)), name
        assert any("tonemap" in c for c in ran) == (name in ("pq", "hlg")), name
    # Without the tone-mapping filters (another ffmpeg build): still an 8-bit picture.
    monkeypatch.setattr(renderer, "TONEMAP", "no_such_filter")
    assert renderer.extract_frame_at(tmp_path / "pq.mp4", out, 0.5) == 0.48
    assert _png_depth(out) == (8, 2)


def test_a_tool_past_its_time_limit_is_stopped(tmp_path):
    import time

    from app.services import renderer

    t0 = time.monotonic()
    assert renderer._run(["sleep", "10"], timeout=0.3)[0] == -1  # killed, answered (as the joins expect)
    assert renderer._run_parts(["sleep", "10"], timeout=0.3)[0] == -1
    assert time.monotonic() - t0 < 5
    video = _video(tmp_path)
    with pytest.raises(renderer.FFmpegTimeout):
        renderer.extract_frame_at(video, tmp_path / "f.png", 1.0, timeout=0)
    # A run of a frame grab that is stopped is a timeout too, never "not a
    # video"; callers that catch either TimeoutError or RuntimeError see it.
    real = renderer._run

    def stuck(cmd, timeout=None, should_stop=None):
        return (-1, "timed out") if "-frames:v" in cmd else real(cmd, timeout=timeout, should_stop=should_stop)

    renderer._run = stuck
    try:
        with pytest.raises(renderer.FFmpegTimeout) as caught:
            renderer.extract_frame_at(video, tmp_path / "f.png", 1.0)
    finally:
        renderer._run = real
    assert isinstance(caught.value, TimeoutError) and isinstance(caught.value, RuntimeError)


def test_a_frame_that_takes_too_long_to_read_is_refused_and_nothing_is_stored(auth_client, tmp_path, monkeypatch):
    """A clip's frame past its time limit: 504. An uploaded video's frame,
    grabbed from its prepared copy within the upload's own limits: "took
    too long", 422, as Upload a video."""
    from app.providers import mock_chat
    from app.services import renderer, video_ops

    aid = _avatar(auth_client)
    clip = _clip(auth_client, aid)
    monkeypatch.setattr(renderer, "FRAME_TIMEOUT", 0.0)
    r = auth_client.post(f"/api/studio/jobs/{clip['id']}/frame-to-picture", json={"at": "last"})
    assert r.status_code == 504 and "took too long" in r.json()["detail"]
    monkeypatch.setattr(video_ops, "UPLOAD_STEP_TIMEOUT", 0.0)
    r = _upload(auth_client, _video(tmp_path), aid)
    assert r.status_code == 422 and "took too long" in r.json()["detail"], r.text
    assert _frame_jobs() == 0 and mock_chat.PICTURE_CALLS == []


def test_an_uploaded_frame_is_refused_past_the_budget_before_its_paid_check(auth_client, tmp_path, monkeypatch):
    """Its safety check is a (tiny) paid vision call: the budget's hard stop
    applies (mock mode never reaches it, so the guard is stood in for).
    Frames of Studio clips make no call and stay free (above)."""
    from fastapi import HTTPException

    from app.api import studio
    from app.providers import mock_chat

    aid = _avatar(auth_client)
    asked: list[bool] = []

    def guard():
        asked.append(True)
        raise HTTPException(402, "Monthly budget reached")

    monkeypatch.setattr(studio, "budget_guard", guard)
    r = _upload(auth_client, _video(tmp_path), aid)
    assert r.status_code == 402 and asked == [True]
    assert mock_chat.PICTURE_CALLS == [] and _frame_jobs() == 0


# ---------- damaged and cut-off files ----------


def _cut_videos(tmp_path: Path) -> dict[str, tuple[Path, str]]:
    """Coded 25 fps videos, 5 s long as made, then damaged: (path, the
    upload's content type)."""
    def cut(path: Path, keep: float, name: str, head: bool = False) -> Path:
        data = path.read_bytes()
        n = int(len(data) * keep)
        out = tmp_path / name
        # An MPEG-TS whose start is lost: whole 188-byte packets, from mid-GOP.
        out.write_bytes(data[len(data) - n // 188 * 188:] if head else data[:n])
        return out

    mp4 = _coded_video(tmp_path, "25", 5.0, "whole.mp4",
                       ("-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart"))
    webm = _coded_video(tmp_path, "25", 5.0, "whole.webm",
                        ("-c:v", "libvpx", "-b:v", "1M", "-live", "1", "-f", "webm"))
    ts = _coded_video(tmp_path, "25", 5.0, "whole.ts", ("-c:v", "libx264", "-g", "50", "-pix_fmt", "yuv420p"))
    return {
        # An interrupted recording or copy: the index says 5 s, the picture stops before.
        "mp4": (cut(mp4, 0.8, "cut.mp4"), "video/mp4"),
        # A browser recording (no stated length) cut off: "File ended prematurely".
        "webm": (cut(webm, 0.9, "cut.webm"), "video/webm"),
        # Every read logs "non-existing PPS" errors.
        "ts": (cut(ts, 0.8, "cut.ts", head=True), "video/mp2t"),
    }


def _check_frame(png: Path, seconds: float) -> int:
    """The saved picture is the frame at the time given (25 fps, timed from
    the file's start): how many frames the file's start is past frame 0 of
    the video as made (mod 36, the frame code's range; 0 unless its start
    was cut off)."""
    n = round(seconds * 25)
    assert abs(n / 25 - seconds) < 1e-6, seconds
    r, b = _frame_number(png)
    return (r + 6 * b - n) % 36


@pytest.mark.parametrize("kind", ["mp4", "webm", "ts"])
def test_a_cut_off_or_damaged_video_gives_the_frames_it_has(auth_client, tmp_path, kind):
    """ffprobe's error lines are never read as part of its listing: a file
    that logs errors is still read, and the last frame of a cut-off file
    (also for a time past where it stops) is the last one ffmpeg decodes.
    Uploaded, a cut-off MP4 or WebM gives the same frames (from its
    prepared copy); an MPEG-TS isn't a container uploads are read in
    (video_ops.UPLOAD_DEMUXERS), so only the frame grab itself reads it."""
    from app.services import renderer

    path, content_type = _cut_videos(tmp_path)[kind]
    _rc, out, err = renderer._run_parts(["ffprobe", "-v", "error", "-show_entries", "frame=pts", "-of", "json",
                                         str(path)])
    assert err.strip() and out.strip().startswith("{")  # errors logged next to the listing
    aid = _avatar(auth_client)
    seconds, offsets = {}, set()
    for at in ("first", "1.05", "last", "4.9"):
        if kind == "ts":
            r = _upload(auth_client, path, aid, at=at, content_type=content_type)
            assert r.status_code == 422 and "isn't a video" in r.json()["detail"], r.text
            png = tmp_path / f"f{at}.png"
            t = {"first": 0.0, "last": math.inf}.get(at, float(at) if at[0].isdigit() else 0.0)
            seconds[at] = renderer.extract_frame_at(path, png, t)
            offsets.add(_check_frame(png, seconds[at]))
            continue
        r = _upload(auth_client, path, aid, at=at, content_type=content_type)
        assert r.status_code == 201, (at, r.text)
        seconds[at] = r.json()["options"]["seconds"]
        offsets.add(_check_frame(Path(r.json()["result_path"]), seconds[at]))
    assert len(offsets) == 1 and (kind == "ts" or offsets == {0})  # each frame at the time it was saved with
    assert seconds["first"] < 0.2 and 1.01 <= seconds["1.05"] <= 1.05  # the frame on screen at 1.05 s
    assert seconds["last"] == seconds["4.9"] >= 3.0
    if kind != "ts":
        assert seconds["first"] == 0.0 and seconds["1.05"] == 1.04


def test_the_last_frame_is_still_found_when_ffprobe_cant_list_the_frames(tmp_path, monkeypatch):
    """The last resort: ffmpeg decodes from the start up to the time (to
    the end for the last frame) and the last frame that comes out is the
    one, for a cut-off MP4 and for a cut-off WebM with no stated length."""
    from app.services import renderer

    videos = _cut_videos(tmp_path)
    out = tmp_path / "f.png"
    expected = {kind: renderer.extract_frame_at(videos[kind][0], out, math.inf) for kind in ("mp4", "webm")}
    monkeypatch.setattr(renderer, "_frame_times", lambda *a, **k: [])
    for kind, last in expected.items():
        path = videos[kind][0]
        assert last >= 3.0
        for t, want in ((math.inf, last), (9.0, last), (1.05, 1.04), (0, 0.0)):
            # Past where it stops: the last frame; 1.05 s: the frame on
            # screen then, not the next one.
            assert renderer.extract_frame_at(path, out, t) == want, (kind, t)
            assert _check_frame(out, want) == 0, (kind, t)


# ---------- a frame of a clip made from a Cast picture ----------


def _clip_row(tmp_path: Path, owner: int, size=(96, 64), owner_kind="avatar", **fields) -> int:
    """A finished Studio clip (a real video file) made from whatever
    `fields` say (first_frame_asset_id, first_frame_job_id,
    reference_asset_ids_json)."""
    from app import models
    from app.db import SessionLocal

    video = _video(tmp_path, size=size, seconds=1.0, name=f"clip{len(list(tmp_path.glob('clip*.mp4')))}.mp4")
    with SessionLocal() as db:
        job = models.AssetGenerationJob(owner_kind=owner_kind, owner_id=owner, output_kind="video_clip", prompt="p",
                                        status="completed", result_path=str(video),
                                        **{"reference_asset_ids_json": [], **fields})
        db.add(job)
        db.commit()
        return job.id


def _frame_of(client, clip_id: int, at="first") -> int:
    r = client.post(f"/api/studio/jobs/{clip_id}/frame-to-picture", json={"at": at})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _source(job_id: int, marked=None) -> tuple:
    from app import models
    from app.db import SessionLocal
    from app.services import pipeline

    with SessionLocal() as db:
        return pipeline.studio_cast_source(db, db.get(models.AssetGenerationJob, job_id), marked)


def _animated_from(client, frame_id: int, owner: int, owner_kind="avatar"):
    """"Animate this" on a saved frame: the first-frame reference the video
    provider gets."""
    from app.providers import mock_video

    sent: list = []
    real = mock_video.MockVideoProvider.submit

    def spy(self, **kw):
        sent.append(kw["references"])
        return real(self, **kw)

    mock_video.MockVideoProvider.submit = spy
    try:
        r = client.post("/api/studio/generate-clip", json={"owner_kind": owner_kind, "owner_id": owner,
                                                           "prompt": "she turns", "first_frame_job_id": frame_id})
    finally:
        mock_video.MockVideoProvider.submit = real
    assert r.status_code == 202, r.text
    (ref,) = [x for refs in sent for x in refs if x.frame_type == "first_frame"]
    return ref


def _ambient_mark(client, frame: int) -> dict:
    """How the content filter sees an ambient scene whose picture is the
    saved frame `frame`."""
    from app import models
    from app.db import SessionLocal
    from app.services import ambient

    from .test_ambient import SHORT

    sid = client.post("/api/ambient", json={"config": SHORT, "generate": False}).json()["id"]
    r = client.post(f"/api/ambient/{sid}/picture/from", json={"studio_job_id": frame})
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        return ambient._picture_mark(db, db.get(models.VideoProject, sid))


def test_a_saved_frame_of_an_animated_cast_picture_is_still_that_characters_picture(auth_client, tmp_path):
    """Adult mode, Bob not ticked: the first frame of a clip animated from
    his Cast picture is (nearly) that picture, so animating the saved frame
    forces his content filter, as animating the picture itself does; so
    does an ambient scene made from the frame. Anna, ticked: her checked
    picture's frames skip it; one added since keeps it."""
    from app.services import content_mode

    from .test_content_mode import _asset, _tick_now
    from .test_content_mode import _avatar as _avatar_row

    bob, anna = _avatar_row("Bob"), _avatar_row("Anna")
    bob_pic, anna_pic = _asset(bob), _asset(anna)
    _tick_now(anna)
    anna_new = _asset(anna)  # added after the tick: not checked yet
    content_mode.set_adult_mode(True)

    big = (640, 480)  # big enough for an ambient scene's picture
    bob_frame = _frame_of(auth_client, _clip_row(tmp_path, bob, big, first_frame_asset_id=bob_pic))
    anna_frame = _frame_of(auth_client, _clip_row(tmp_path, anna, big, first_frame_asset_id=anna_pic), at="last")
    anna_new_frame = _frame_of(auth_client, _clip_row(tmp_path, anna, first_frame_asset_id=anna_new))
    assert _source(bob_frame) == (bob, bob_pic)
    assert _source(anna_frame) == (anna, anna_pic)

    for frame, owner, adult_ok in ((bob_frame, bob, False), (anna_frame, anna, True), (anna_new_frame, anna, False)):
        ref = _animated_from(auth_client, frame, owner)
        assert (ref.role, ref.adult_ok) == ("character", adult_ok), frame
        assert content_mode.forces_filter(ref) is not adult_ok

    # An ambient scene whose picture is the frame: still Bob's picture.
    assert _ambient_mark(auth_client, bob_frame) == {"cast_picture": True, "adult_ok": False}
    assert _ambient_mark(auth_client, anna_frame) == {"cast_picture": True, "adult_ok": True}


def test_the_cast_link_is_followed_through_every_copy_and_reference(auth_client, tmp_path):
    from app.services import content_mode

    from .test_content_mode import _asset, _job, _tick_now
    from .test_content_mode import _avatar as _avatar_row

    bob, anna, owner = _avatar_row("Bob"), _avatar_row("Anna"), _avatar_row("Owner")
    bob_pic, anna_pic, anna_pic2 = _asset(bob), _asset(anna), _asset(anna)
    logo = _asset(bob, asset_type="logo")
    _tick_now(anna)
    marked = content_mode.adult_ok_avatars()

    frame = _frame_of(auth_client, _clip_row(tmp_path, owner, first_frame_asset_id=bob_pic))
    # A clip animated from the frame, a frame of that, an upscale of that: all Bob's picture.
    frame2 = _frame_of(auth_client, _clip_row(tmp_path, owner, first_frame_job_id=frame))
    up = auth_client.post(f"/api/studio/jobs/{frame2}/upscale", json={"factor": 2}).json()["id"]
    assert _source(frame2) == _source(up) == (bob, bob_pic)
    # A clip made with Bob's picture as a reference.
    assert _source(_frame_of(auth_client, _clip_row(tmp_path, owner, reference_asset_ids_json=[bob_pic]))) == (
        bob, bob_pic)
    # Two of Anna's checked pictures as references: hers, covered by her tick
    # now; without the ticks to go by, never covered.
    two = _frame_of(auth_client, _clip_row(tmp_path, owner, reference_asset_ids_json=[anna_pic, anna_pic2]))
    assert _source(two, marked) == (anna, anna_pic) and _source(two) == (anna, None)
    # Anna's checked picture animated with Bob as a reference: Bob's filter wins.
    mixed = _frame_of(auth_client, _clip_row(tmp_path, owner, first_frame_asset_id=anna_pic,
                                             reference_asset_ids_json=[bob_pic]))
    assert _source(mixed, marked) == (bob, bob_pic) and _source(mixed) == (anna, None)
    # A Cast picture deleted since: the clip's owner, when a character, with no picture.
    gone = _frame_of(auth_client, _clip_row(tmp_path, bob, first_frame_asset_id=99999))
    assert _source(gone) == (bob, None)
    # Not a likeness: a logo, a new Studio picture, a prompt alone.
    for fields in ({"first_frame_asset_id": logo}, {"first_frame_job_id": _job(bob)}, {}):
        assert _source(_frame_of(auth_client, _clip_row(tmp_path, bob, **fields))) == (None, None), fields


def _ingredient_with_picture(name: str) -> tuple[int, int]:
    """(an ingredient, a picture on it)."""
    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        ing = models.Ingredient(name=name, kind="scene")
        db.add(ing)
        db.flush()
        pic = models.Asset(owner_kind="ingredient", ingredient_id=ing.id, asset_type="reference",
                           file_path="/tmp/x.png", mime_type="image/png")
        db.add(pic)
        db.commit()
        return ing.id, pic.id


def test_a_scenes_clip_keeps_the_cast_link_once_its_cast_picture_is_deleted(auth_client, tmp_path):
    """Adult mode, Bob not ticked. A scene's clip (owned by "Beach", not a
    character) animated from Bob's Cast picture, or made with his picture
    as a reference, gives frames that are his picture, also once that
    picture is deleted: the clip remembers whose it was, so animating a
    frame or making an ambient scene of it still forces his filter. Same
    when a ticked character is deleted with her pictures. A deleted
    picture that wasn't a Cast picture leaves a new picture; one gone with
    nothing remembered (deleted before clips recorded it) keeps the filter
    whoever owns the clip."""
    from app.services import content_mode, pipeline

    from .test_content_mode import _asset, _tick_now
    from .test_content_mode import _avatar as _avatar_row

    bob, anna = _avatar_row("Bob"), _avatar_row("Anna")
    bob_pic, bob_ref, anna_pic = _asset(bob), _asset(bob), _asset(anna)
    beach, beach_pic = _ingredient_with_picture("Beach")
    _tick_now(anna)
    content_mode.set_adult_mode(True)

    big = (640, 480)  # big enough for an ambient scene's picture

    def clip(**fields) -> int:
        return _clip_row(tmp_path, beach, big, owner_kind="ingredient", **fields)

    started, referenced = clip(first_frame_asset_id=bob_pic), clip(reference_asset_ids_json=[bob_ref])
    scenery, anna_clip = clip(first_frame_asset_id=beach_pic), clip(first_frame_asset_id=anna_pic)
    frames = {c: _frame_of(auth_client, c) for c in (started, referenced, scenery, anna_clip)}
    assert [_source(frames[c]) for c in (started, referenced, scenery, anna_clip)] == [
        (bob, bob_pic), (bob, bob_ref), (None, None), (anna, anna_pic)]
    assert _animated_from(auth_client, frames[anna_clip], beach, "ingredient").adult_ok is True

    for pic in (bob_pic, bob_ref, beach_pic):
        assert auth_client.delete(f"/api/assets/{pic}").status_code == 204
    assert auth_client.delete(f"/api/avatars/{anna}").status_code == 204
    later = _frame_of(auth_client, referenced, at="last")  # a frame saved after the delete
    assert [_source(f) for f in (frames[started], frames[referenced], later, frames[anna_clip])] == [
        (bob, None), (bob, None), (bob, None), (anna, None)]
    assert _source(frames[scenery]) == (None, None)  # a scene's own picture: still a new picture
    for frame in (frames[started], later, frames[anna_clip]):
        ref = _animated_from(auth_client, frame, beach, "ingredient")
        assert (ref.role, ref.adult_ok) == ("character", False) and content_mode.forces_filter(ref), frame
    assert _ambient_mark(auth_client, frames[started]) == {"cast_picture": True, "adult_ok": False}
    ref = _animated_from(auth_client, frames[scenery], beach, "ingredient")
    assert ref.role == "scene" and not content_mode.forces_filter(ref)

    # Gone with nothing remembered: whose it was isn't known, so the filter stays on.
    unknown = _frame_of(auth_client, clip(first_frame_asset_id=99999, reference_asset_ids_json=[99998]))
    assert _source(unknown) == (pipeline.UNKNOWN_CAST, None)
    ref = _animated_from(auth_client, unknown, beach, "ingredient")
    assert ref.role == "character" and content_mode.forces_filter(ref)
    assert _ambient_mark(auth_client, unknown) == {"cast_picture": True, "adult_ok": False}

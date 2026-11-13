"""Upload a video: a video from this device kept as a finished Studio clip
(free), once frames across it passed the picture checks, so it can be
continued, joined or have its frames saved."""
from __future__ import annotations

import json
import math
import subprocess
import tempfile
from pathlib import Path

import pytest

from .test_image_ops import _avatar


def _video(path: Path, *, seconds: float = 2.0, size=(96, 64), audio: bool = False, codec: str = "libx264",
           pix_fmt: str = "yuv420p") -> Path:
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
           f"testsrc=size={size[0]}x{size[1]}:rate=10:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-shortest"]
        cmd += ["-c:a", "libopus" if path.suffix == ".webm" else "aac"]
    cmd += ["-c:v", codec, "-pix_fmt", pix_fmt, str(path)]
    subprocess.run(cmd, check=True)
    return path


def _codecs(path: str) -> dict:
    out = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_entries",
                          "stream=codec_type,codec_name,pix_fmt", path], capture_output=True, text=True, check=True)
    return {s["codec_type"]: (s["codec_name"], s.get("pix_fmt")) for s in json.loads(out.stdout)["streams"]}


def _upload(client, path: Path, aid: int, content_type: str = "video/mp4", owner_kind: str = "avatar"):
    with open(path, "rb") as f:
        return client.post("/api/studio/upload-video", files={"file": (path.name, f.read(), content_type)},
                           data={"owner_kind": owner_kind, "owner_id": str(aid)})


def _uploads() -> int:
    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        return db.query(models.AssetGenerationJob).filter(
            models.AssetGenerationJob.operation == "upload_video").count()


def _clip_files() -> list[Path]:
    from app.config import get_settings

    return [p for p in (get_settings().data_path / "studio").rglob("*.mp4")]


@pytest.fixture
def temp_root(tmp_path, monkeypatch):
    """Temporary files go here, so a test can see none is left behind."""
    root = tmp_path / "tmp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


def _left(root: Path) -> list[str]:
    return [p.name for p in root.iterdir() if p.name.startswith("upload-video-")]


def test_frame_times_first_middle_last_about_one_per_10s_at_most_8():
    from app.services.video_ops import upload_frame_times

    assert upload_frame_times(2.0) == [0.0, 1.0, math.inf]
    assert upload_frame_times(25.0) == [0.0, pytest.approx(25 / 3, abs=1e-3), pytest.approx(50 / 3, abs=1e-3),
                                        math.inf]
    assert len(upload_frame_times(65.0)) == 8 and len(upload_frame_times(600.0)) == 8


def test_upload_is_checked_then_kept_as_a_finished_free_clip(auth_client, tmp_path, temp_root):
    from app import models
    from app.db import SessionLocal
    from app.providers import mock_chat
    from app.services import pipeline

    aid = _avatar(auth_client)
    video = _video(tmp_path / "holiday.mp4", seconds=12.0, size=(64, 96), audio=True)
    r = _upload(auth_client, video, aid)
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["batch_ids"] == [job["id"]]
    assert (job["status"], job["output_kind"], job["operation"]) == ("completed", "video_clip", "upload_video")
    assert (job["provider"], job["provider_model"], job["label"]) == ("local", "local/upload", "Uploaded video")
    assert (job["owner_kind"], job["owner_id"]) == ("avatar", aid)
    assert (job["result_width"], job["result_height"]) == (64, 96) and job["aspect_ratio"] == "9:16"
    assert job["result_duration_seconds"] == pytest.approx(12.0, abs=0.15)
    assert job["options"] == {"upload": "holiday.mp4", "has_audio": True, "reencoded": False}
    assert job["prompt"] == "" and job["cost_usd"] is None
    assert sorted(mock_chat.PICTURE_CALLS) == [0, 1, 2]  # first, middle and last frames
    assert mock_chat.PICTURE_EXPLICIT_CALLS == []  # not a clean deployment
    assert _codecs(job["result_path"]) == {"video": ("h264", "yuv420p"), "audio": ("aac", None)}
    assert auth_client.get(f"/api/studio/jobs/{job['id']}/preview").headers["content-type"] == "video/mp4"
    assert _left(temp_root) == []
    # Nothing to make again; never a Cast picture.
    r = auth_client.post(f"/api/studio/jobs/{job['id']}/retry")
    assert r.status_code == 409 and "upload it again" in r.json()["detail"]
    with SessionLocal() as db:
        row = db.get(models.AssetGenerationJob, job["id"])
        assert pipeline.studio_cast_source(db, row) == (None, None)


def test_uploaded_clip_can_be_continued_joined_and_framed(auth_client, tmp_path):
    from app.providers import mock_chat

    aid = _avatar(auth_client)
    up = _upload(auth_client, _video(tmp_path / "a.mp4", seconds=2.0), aid).json()
    info = auth_client.get(f"/api/studio/jobs/{up['id']}/continue").json()
    assert info["prompt"] == "" and info["aspect_ratio"] == "16:9" and info["model"]
    calls = len(mock_chat.PICTURE_CALLS)
    r = auth_client.post(f"/api/studio/jobs/{up['id']}/continue", json={"prompt": "the camera pulls back"})
    assert r.status_code == 202, r.text
    cont = auth_client.get(f"/api/studio/jobs/{r.json()['id']}").json()
    assert cont["status"] == "completed" and cont["aspect_ratio"] == "16:9"
    join = auth_client.get(f"/api/studio/jobs/{cont['options']['join_job_id']}").json()
    assert join["status"] == "completed" and join["options"]["job_ids"] == [up["id"], cont["id"]]
    # Its frames are frames of a Studio clip now: no new check.
    frame = auth_client.post(f"/api/studio/jobs/{up['id']}/frame-to-picture", json={"at": "first"})
    assert frame.status_code == 201
    assert len(mock_chat.PICTURE_CALLS) == calls


def test_other_formats_are_re_encoded_to_h264_aac_mp4(auth_client, tmp_path):
    aid = _avatar(auth_client)
    webm = _video(tmp_path / "clip.webm", codec="libvpx-vp9", audio=True)
    r = _upload(auth_client, webm, aid, content_type="video/webm")
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["options"]["reencoded"] is True and job["options"]["has_audio"] is True
    assert Path(job["result_path"]).suffix == ".mp4"
    assert _codecs(job["result_path"]) == {"video": ("h264", "yuv420p"), "audio": ("aac", None)}
    # An MP4 in 4:4:4 (many browsers can't play it) is re-encoded too.
    odd = _video(tmp_path / "odd.mp4", pix_fmt="yuv444p")
    job = _upload(auth_client, odd, aid).json()
    assert job["options"]["reencoded"] is True and _codecs(job["result_path"])["video"] == ("h264", "yuv420p")


def test_refused_when_any_frame_shows_a_minor(auth_client, tmp_path, temp_root):
    from app.providers import mock_chat

    aid = _avatar(auth_client)
    # Only the middle frame is judged under 18.
    mock_chat.PICTURE_REPLY = lambda i: {"minor": True, "reason": "A child."} if i == 1 else None
    r = _upload(auth_client, _video(tmp_path / "a.mp4", seconds=4.0), aid)
    assert r.status_code == 422 and "under 18" in r.json()["detail"], r.text
    assert _uploads() == 0 and _clip_files() == [] and _left(temp_root) == []


@pytest.mark.parametrize("reply,status,words", [
    (RuntimeError("down"), 503, "couldn't look"),
    ({"minor": None, "reason": "no picture"}, 503, "couldn't look"),
    ("declined", 422, "declined"),
])
def test_fails_closed_when_the_check_cant_answer(auth_client, tmp_path, temp_root, reply, status, words):
    from app.providers import mock_chat
    from app.providers.openrouter_chat import ModelDeclined

    aid = _avatar(auth_client)
    mock_chat.PICTURE_REPLY = ModelDeclined("no") if reply == "declined" else reply
    r = _upload(auth_client, _video(tmp_path / "a.mp4"), aid)
    assert r.status_code == status and words in r.json()["detail"], r.text
    assert _uploads() == 0 and _clip_files() == [] and _left(temp_root) == []


def test_clean_deployment_asks_whether_each_frame_is_explicit(auth_client, tmp_path, temp_root, monkeypatch):
    from app.providers import mock_chat

    from .test_adult_features_off import _clean

    aid = _avatar(auth_client)
    _clean(monkeypatch)
    r = _upload(auth_client, _video(tmp_path / "a.mp4"), aid)
    assert r.status_code == 201, r.text
    assert len(mock_chat.PICTURE_EXPLICIT_CALLS) == 3
    mock_chat.PICTURE_EXPLICIT_REPLY = {"explicit": True, "reason": "Nudity."}
    r = _upload(auth_client, _video(tmp_path / "b.mp4", size=(64, 64)), aid)
    assert r.status_code == 422 and "general audience" in r.json()["detail"]
    mock_chat.PICTURE_EXPLICIT_REPLY = RuntimeError("down")
    r = _upload(auth_client, _video(tmp_path / "c.mp4", size=(48, 48)), aid)
    assert r.status_code == 503
    assert _uploads() == 1 and _left(temp_root) == []


def test_patched_checkers_see_every_frame_and_any_refusal_refuses(auth_client, tmp_path, temp_root, monkeypatch):
    from app.services import explicit_check, minor_check

    aid = _avatar(auth_client)
    seen: dict = {}

    def pictures(name, frames):
        seen["frames"] = [Path(f.file_path).name for f in frames]
        assert all(Path(f.file_path).is_file() and str(temp_root) in f.file_path for f in frames)
        return {"verdict": "ok", "checked": [f.id for f in frames], "declined": []}

    explicit: list[str] = []

    def picture(path, digest):
        explicit.append(Path(path).name)
        if len(explicit) == 4:
            raise explicit_check.ExplicitContentError(explicit_check.PICTURE_REFUSED)

    monkeypatch.setattr(minor_check, "check_pictures", pictures)
    monkeypatch.setattr(explicit_check, "check_picture", picture)
    r = _upload(auth_client, _video(tmp_path / "a.mp4", seconds=35.0, size=(32, 32)), aid)
    assert r.status_code == 422, r.text
    assert seen["frames"] == [f"frame{i}.png" for i in range(5)]  # 35 s: 5 frames
    assert explicit == seen["frames"][:4]
    assert _uploads() == 0 and _left(temp_root) == []

    # A check answering for only some frames is never a yes.
    def partial(name, frames):
        return {"verdict": "ok", "checked": [0], "declined": []}

    monkeypatch.setattr(minor_check, "check_pictures", partial)
    assert _upload(auth_client, _video(tmp_path / "b.mp4"), aid).status_code == 503
    assert _uploads() == 0 and _left(temp_root) == []


def test_bad_files_and_limits(auth_client, tmp_path, temp_root, monkeypatch):
    from PIL import Image

    from app import config
    from app.services import video_ops

    aid = _avatar(auth_client)
    fake = tmp_path / "notes.mp4"
    fake.write_bytes(b"hello")
    r = _upload(auth_client, fake, aid)
    assert r.status_code == 422 and "isn't a video" in r.json()["detail"]
    png = tmp_path / "p.png"
    Image.new("RGB", (10, 10)).save(png)
    assert _upload(auth_client, png, aid, content_type="image/png").status_code == 422
    assert _upload(auth_client, _video(tmp_path / "a.mp4"), 9999).status_code == 404
    monkeypatch.setattr(video_ops, "UPLOAD_MAX_SECONDS", 1.0)
    r = _upload(auth_client, _video(tmp_path / "a.mp4"), aid)
    assert r.status_code == 422 and "long" in r.json()["detail"]
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "1000")
    config.get_settings.cache_clear()
    r = _upload(auth_client, _video(tmp_path / "a.mp4"), aid)
    assert r.status_code == 413
    assert _uploads() == 0 and _clip_files() == [] and _left(temp_root) == []


def test_a_scene_can_own_an_uploaded_video(auth_client, tmp_path):
    r = auth_client.post("/api/ingredients", json={"name": "Cafe", "kind": "scene"})
    iid = r.json()["id"]
    r = _upload(auth_client, _video(tmp_path / "a.mp4"), iid, owner_kind="ingredient")
    assert r.status_code == 201, r.text
    assert (r.json()["owner_kind"], r.json()["owner_id"]) == ("ingredient", iid)

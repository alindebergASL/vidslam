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
    # A frame already checked at upload (the first) is a cache hit.
    frame = auth_client.post(f"/api/studio/jobs/{up['id']}/frame-to-picture", json={"at": "first"})
    assert frame.status_code == 201
    assert len(mock_chat.PICTURE_CALLS) == calls


def test_any_frame_saved_from_an_uploaded_clip_or_its_join_is_checked(auth_client, tmp_path):
    """Only 3 to 8 frames were checked at upload: a frame saved from it
    later, or from a join it is part of, is checked as it is saved."""
    from app.providers import mock_chat

    from .test_continue_clip import _clip

    aid = _avatar(auth_client)
    up = _upload(auth_client, _video(tmp_path / "a.mp4", seconds=2.0), aid).json()
    calls = len(mock_chat.PICTURE_CALLS)
    r = auth_client.post(f"/api/studio/jobs/{up['id']}/frame-to-picture", json={"at": 0.5})
    assert r.status_code == 201, r.text
    assert len(mock_chat.PICTURE_CALLS) == calls + 1  # a frame never seen before: checked
    made = _clip(auth_client, aid)
    joined = auth_client.post("/api/studio/join-clips", json={"job_ids": [made["id"], up["id"]]}).json()
    # A frame judged under 18 is refused, from the upload or the join alike.
    mock_chat.PICTURE_REPLY = {"minor": True, "reason": "A child."}
    for jid, at in ((up["id"], 1.5), (joined["id"], 6.3)):
        r = auth_client.post(f"/api/studio/jobs/{jid}/frame-to-picture", json={"at": at})
        assert r.status_code == 422 and "under 18" in r.json()["detail"], r.text
    # Continuing it is refused before anything is made.
    before = auth_client.get("/api/studio/jobs", params={"limit": 200}).json()
    r = auth_client.post(f"/api/studio/jobs/{joined['id']}/continue", json={"prompt": "on she goes"})
    assert r.status_code == 422, r.text
    assert len(auth_client.get("/api/studio/jobs", params={"limit": 200}).json()) == len(before)
    # A generated clip's frames are not checked.
    calls = len(mock_chat.PICTURE_CALLS)
    r = auth_client.post(f"/api/studio/jobs/{made['id']}/frame-to-picture", json={"at": 0.5})
    assert r.status_code == 201 and len(mock_chat.PICTURE_CALLS) == calls


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


def test_only_sniffed_containers_are_read(auth_client, tmp_path, temp_root):
    """An animated GIF or an MPEG-TS sent as video/mp4 isn't one of the
    containers sniffed (MP4/MOV, Matroska/WebM, AVI): refused unread."""
    aid = _avatar(auth_client)
    gif = tmp_path / "x.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=32x32:rate=5:duration=2",
                    "-f", "gif", str(gif)], check=True)
    ts = tmp_path / "y.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=32x32:rate=5:duration=2",
                    "-c:v", "libx264", "-f", "mpegts", str(ts)], check=True)
    for path in (gif, ts):
        r = _upload(auth_client, path, aid)
        assert r.status_code == 422 and "isn't a video" in r.json()["detail"], r.text
    assert _uploads() == 0 and _left(temp_root) == []
    # A Matroska file (WebM) and an AVI are read, each with its own demuxer.
    avi = _video(tmp_path / "z.avi", codec="mpeg4")
    assert _upload(auth_client, avi, aid, content_type="video/x-msvideo").status_code == 201


def test_the_prepared_copys_length_is_what_counts(auth_client, tmp_path, temp_root, monkeypatch):
    """A file that claims to be shorter than it plays (its own metadata) is
    measured again once prepared, cut a second past the most, and refused."""
    from app.services import video_ops

    aid = _avatar(auth_client)
    monkeypatch.setattr(video_ops, "UPLOAD_MAX_SECONDS", 3.0)
    real = video_ops._probe_upload

    def lies(path, demuxer):
        return {**real(path, demuxer), "seconds": 1.0}

    monkeypatch.setattr(video_ops, "_probe_upload", lies)
    seen: list[float] = []
    real_norm = video_ops.normalize_upload

    def norm(src, dst, streams, **kw):
        out = real_norm(src, dst, streams, **kw)
        from app.services.renderer import _probe_clip

        seen.append(_probe_clip(dst)[0])
        return out

    monkeypatch.setattr(video_ops, "normalize_upload", norm)
    for name, codec in (("a.mp4", "libx264"), ("b.webm", "libvpx-vp9")):  # copied, re-encoded
        r = _upload(auth_client, _video(tmp_path / name, seconds=8.0, size=(32, 32), codec=codec), aid)
        assert r.status_code == 422 and "longer than" in r.json()["detail"], r.text
    assert all(s <= 4.5 for s in seen) and len(seen) == 2  # never decoded past the cap
    assert _uploads() == 0 and _clip_files() == [] and _left(temp_root) == []


def test_preparing_is_bounded_and_holds_no_database_connection(auth_client, tmp_path, temp_root, monkeypatch):
    from app.services import renderer, video_ops

    aid = _avatar(auth_client)
    # Every ffmpeg/ffprobe run on the raw upload has a time limit, well
    # inside the proxy's read timeout; a re-encode past it is refused.
    runs: list[tuple] = []
    real = renderer._run

    def spy(cmd, timeout=None, should_stop=None):
        runs.append((cmd, timeout))
        return real(cmd, timeout=timeout, should_stop=should_stop)

    monkeypatch.setattr(renderer, "_run", spy)
    in_tx: list[bool] = []
    real_vfu = video_ops._video_from_upload

    def watch(db, **kw):
        in_tx.append(db.in_transaction())
        return real_vfu(db, **kw)

    monkeypatch.setattr(video_ops, "_video_from_upload", watch)
    r = _upload(auth_client, _video(tmp_path / "a.webm", codec="libvpx-vp9"), aid, content_type="video/webm")
    assert r.status_code == 201, r.text
    assert in_tx == [False]
    raw = [(c, t) for c, t in runs if any("upload.mkv" in part for part in c)]
    assert raw and all(t is not None and t <= 400 for _c, t in raw)
    assert all(c[c.index("-f") + 1] == "matroska" and "-protocol_whitelist" in c for c, _t in raw)
    assert video_ops.UPLOAD_ENCODE_TIMEOUT < 600

    def slow(cmd, timeout=None, should_stop=None):
        if cmd[0] == "ffmpeg" and "libx264" in cmd:
            return -1, "timed out after 360s"
        return real(cmd, timeout=timeout, should_stop=should_stop)

    monkeypatch.setattr(renderer, "_run", slow)
    r = _upload(auth_client, _video(tmp_path / "b.webm", codec="libvpx-vp9", size=(32, 32)), aid,
                content_type="video/webm")
    assert r.status_code == 422 and "too long to prepare" in r.json()["detail"], r.text
    assert _left(temp_root) == []


def test_too_many_uploads_at_once_are_refused(auth_client, tmp_path, monkeypatch):
    import threading

    from app.services import video_ops

    aid = _avatar(auth_client)
    monkeypatch.setattr(video_ops, "_upload_slots", threading.BoundedSemaphore(1))
    assert video_ops._upload_slots.acquire(blocking=False)  # another upload being prepared
    r = _upload(auth_client, _video(tmp_path / "a.mp4"), aid)
    assert r.status_code == 429, r.text
    video_ops._upload_slots.release()
    assert _upload(auth_client, _video(tmp_path / "a.mp4"), aid).status_code == 201
    # The place is given back on every path, a refusal included.
    fake = tmp_path / "notes.mp4"
    fake.write_bytes(b"hello")
    assert _upload(auth_client, fake, aid).status_code == 422
    assert _upload(auth_client, _video(tmp_path / "b.mp4"), aid).status_code == 201


def test_a_scene_can_own_an_uploaded_video(auth_client, tmp_path):
    r = auth_client.post("/api/ingredients", json={"name": "Cafe", "kind": "scene"})
    iid = r.json()["id"]
    r = _upload(auth_client, _video(tmp_path / "a.mp4"), iid, owner_kind="ingredient")
    assert r.status_code == 201, r.text
    assert (r.json()["owner_kind"], r.json()["owner_id"]) == ("ingredient", iid)


# ---------- review fixes ----------


def test_a_live_webm_with_no_stated_length_is_measured_and_kept(auth_client, tmp_path):
    """A browser recording (MediaRecorder: live WebM) states no length; it
    is measured instead of being refused as not a video."""
    aid = _avatar(auth_client)
    webm = tmp_path / "recording.webm"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x48:rate=10:duration=3",
                    "-c:v", "libvpx", "-f", "webm", "-live", "1", str(webm)], check=True)
    from app.services import video_ops

    assert video_ops._probe_upload(webm, "matroska")["seconds"] == 0.0  # really no stated length
    r = _upload(auth_client, webm, aid, content_type="video/webm")
    assert r.status_code == 201, r.text
    assert r.json()["result_duration_seconds"] == pytest.approx(3.0, abs=0.25)


def test_a_live_webm_too_long_is_refused_before_it_is_prepared(auth_client, tmp_path, monkeypatch):
    from app.services import video_ops

    aid = _avatar(auth_client)
    monkeypatch.setattr(video_ops, "UPLOAD_MAX_SECONDS", 2.0)
    webm = tmp_path / "recording.webm"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=32x32:rate=10:duration=5",
                    "-c:v", "libvpx", "-f", "webm", "-live", "1", str(webm)], check=True)
    prepared: list = []
    monkeypatch.setattr(video_ops, "normalize_upload", lambda *a: prepared.append(a))
    r = _upload(auth_client, webm, aid, content_type="video/webm")
    assert r.status_code == 422 and "long" in r.json()["detail"], r.text
    assert prepared == []


def _without_ftyp(src: Path, dst: Path) -> Path:
    """The same QuickTime file starting with its second atom (a "wide" atom,
    or "moov" with fast start) instead of "ftyp": the two swap places and
    "ftyp" becomes a "free" atom of its size, so every later byte (the media
    data the movie header points into) stays where it was."""
    data = src.read_bytes()
    n = int.from_bytes(data[:4], "big")
    assert data[4:8] == b"ftyp"
    m = int.from_bytes(data[n:n + 4], "big")
    dst.write_bytes(data[n:n + m] + n.to_bytes(4, "big") + b"free" + b"\0" * (n - 8) + data[n + m:])
    return dst


@pytest.mark.parametrize("faststart,first", [(False, b"wide"), (True, b"moov")])
def test_a_mov_starting_with_another_atom_is_read(auth_client, tmp_path, faststart, first):
    from app.services import video_ops

    aid = _avatar(auth_client)
    mov = tmp_path / "a.mov"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x48:rate=10:duration=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", *(["-movflags", "+faststart"] if faststart else []),
                    "-f", "mov", str(mov)], check=True)
    bare = _without_ftyp(mov, tmp_path / "bare.mov")
    assert bare.read_bytes()[4:8] == first
    assert video_ops.sniffed_extension(bare) == ".mp4"
    r = _upload(auth_client, bare, aid, content_type="video/quicktime")
    assert r.status_code == 201, r.text
    assert r.json()["result_duration_seconds"] == pytest.approx(2.0, abs=0.2)


def test_sniffing_keeps_to_the_known_containers(tmp_path):
    from app.services import video_ops

    for head, ext in ((b"\0\0\0\x08free", ".mp4"), (b"\0\0\0\x08skip", ".mp4"), (b"\0\0\0\x08pnot", ".mp4"),
                      (b"\0\0\0\x08mdat", ".mp4"), (b"\0\0\0\x08junk", ""), (b"#EXTM3U\nhttp", "")):
        p = tmp_path / "f"
        p.write_bytes(head + b"\0" * 16)
        assert video_ops.sniffed_extension(p) == ext, head


def _fps(path: str) -> float:
    out = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-select_streams", "v:0",
                          "-show_entries", "stream=avg_frame_rate", path], capture_output=True, text=True,
                         check=True)
    num, den = json.loads(out.stdout)["streams"][0]["avg_frame_rate"].split("/")
    return float(num) / float(den)


def test_a_large_or_fast_h264_mp4_is_re_encoded_within_1080p_and_60fps(auth_client, tmp_path):
    aid = _avatar(auth_client)
    big = _video(tmp_path / "big.mp4", seconds=0.5, size=(2560, 1440))
    job = _upload(auth_client, big, aid).json()
    assert job["options"]["reencoded"] is True, job
    assert (job["result_width"], job["result_height"]) == (1920, 1080)
    fast = tmp_path / "fast.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x48:rate=120:duration=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(fast)], check=True)
    job = _upload(auth_client, fast, aid).json()
    assert job["options"]["reencoded"] is True, job
    assert _fps(job["result_path"]) <= 60.01
    # An ordinary one is still kept as it is.
    job = _upload(auth_client, _video(tmp_path / "ok.mp4", size=(1080, 1920), seconds=0.5), aid).json()
    assert job["options"]["reencoded"] is False


def test_every_step_after_preparing_is_bounded_too(auth_client, tmp_path, temp_root, monkeypatch):
    """The probes and frame grabs on the prepared copy have time limits as
    well, and one that runs out is "took too long", never a hang."""
    from app.services import renderer, video_ops

    aid = _avatar(auth_client)
    runs: list[tuple] = []
    real = renderer._run

    def spy(cmd, timeout=None, should_stop=None):
        runs.append((cmd, timeout))
        return real(cmd, timeout=timeout, should_stop=should_stop)

    monkeypatch.setattr(renderer, "_run", spy)
    r = _upload(auth_client, _video(tmp_path / "a.mp4"), aid)
    assert r.status_code == 201, r.text
    assert runs and all(t is not None for _c, t in runs), [c for c, t in runs if t is None]
    after = [t for c, t in runs if "clip.mp4" in " ".join(c) and "upload.mp4" not in " ".join(c)]
    assert after and sum(after) < 600 - video_ops.UPLOAD_ENCODE_TIMEOUT

    def stuck(cmd, timeout=None, should_stop=None):
        if "clip.mp4" in " ".join(cmd) and "-frames:v" in cmd:
            return -1, f"timed out after {timeout}s"
        return real(cmd, timeout=timeout, should_stop=should_stop)

    monkeypatch.setattr(renderer, "_run", stuck)
    r = _upload(auth_client, _video(tmp_path / "b.mp4", size=(32, 32)), aid)
    assert r.status_code == 422 and "too long to prepare" in r.json()["detail"], r.text
    assert _left(temp_root) == []


# ---------- an uploaded clip used in a project ----------


def _project_shot(client, aid: int, seconds: float) -> tuple[int, int]:
    from .test_studio_fixes import _body_shot, _planned_project

    pid = _planned_project(client, aid)
    return pid, _body_shot(client, pid, duration=seconds)


def _takes(sid: int) -> int:
    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        return db.query(models.ShotTake).filter(models.ShotTake.shot_id == sid).count()


def test_an_uploaded_clip_used_in_a_shot_has_every_stretch_used_checked(auth_client, tmp_path):
    from app.providers import mock_chat

    aid = _avatar(auth_client)
    up = _upload(auth_client, _video(tmp_path / "a.mp4", seconds=12.0), aid).json()
    pid, sid = _project_shot(auth_client, aid, 6.0)
    url = f"/api/projects/{pid}/shots/{sid}/takes/from-studio"
    seen: list = []
    mock_chat.PICTURE_REPLY = lambda i: seen.append(i)
    r = auth_client.post(url, json={"studio_job_id": up["id"]})
    assert r.status_code == 201, r.text
    # About one frame a second over the 6 s used (frames seen at upload are remembered).
    assert len(seen) >= 4
    # A frame judged under 18 anywhere in it refuses it; no take is made.
    before = _takes(sid)
    pid2, sid2 = _project_shot(auth_client, aid, 6.0)
    up2 = _upload(auth_client, _video(tmp_path / "b.mp4", seconds=12.0, size=(64, 64)), aid).json()
    mock_chat.PICTURE_REPLY = {"minor": True, "reason": "A child."}
    r = auth_client.post(f"/api/projects/{pid2}/shots/{sid2}/takes/from-studio", json={"studio_job_id": up2["id"]})
    assert r.status_code == 422 and "under 18" in r.json()["detail"], r.text
    assert _takes(sid2) == 0 and _takes(sid) == before


@pytest.mark.parametrize("reply,status", [(RuntimeError("down"), 503), ({"minor": True, "reason": "x"}, 422)])
def test_a_join_containing_an_upload_is_checked_before_it_is_used_in_a_shot(auth_client, tmp_path, reply, status):
    from app.providers import mock_chat

    from .test_continue_clip import _clip

    aid = _avatar(auth_client)
    up = _upload(auth_client, _video(tmp_path / "a.mp4", seconds=2.0), aid).json()
    made = _clip(auth_client, aid)
    joined = auth_client.post("/api/studio/join-clips", json={"job_ids": [made["id"], up["id"]]}).json()
    pid, sid = _project_shot(auth_client, aid, 30.0)
    mock_chat.PICTURE_REPLY = reply
    r = auth_client.post(f"/api/projects/{pid}/shots/{sid}/takes/from-studio", json={"studio_job_id": joined["id"]})
    assert r.status_code == status, r.text
    assert _takes(sid) == 0
    # A generated clip is used as before, unchecked.
    calls = len(mock_chat.PICTURE_CALLS)
    r = auth_client.post(f"/api/projects/{pid}/shots/{sid}/takes/from-studio", json={"studio_job_id": made["id"]})
    assert r.status_code == 201, r.text
    assert len(mock_chat.PICTURE_CALLS) == calls


def test_a_clean_deployment_checks_an_uploaded_clips_frames_for_nudity_before_a_shot(auth_client, tmp_path,
                                                                                     monkeypatch):
    from app.providers import mock_chat

    from .test_adult_features_off import _clean

    aid = _avatar(auth_client)
    _clean(monkeypatch)
    up = _upload(auth_client, _video(tmp_path / "a.mp4", seconds=4.0), aid).json()
    pid, sid = _project_shot(auth_client, aid, 4.0)
    mock_chat.PICTURE_EXPLICIT_REPLY = {"explicit": True, "reason": "Nudity."}
    r = auth_client.post(f"/api/projects/{pid}/shots/{sid}/takes/from-studio", json={"studio_job_id": up["id"]})
    assert r.status_code == 422, r.text
    assert _takes(sid) == 0

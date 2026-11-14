"""Upload a video: a video from this device kept as a finished Studio clip
(free), once frames across it passed the picture checks, so it can be
continued, joined or have its frames saved."""
from __future__ import annotations

import io
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
    assert job["options"] == {"upload": "holiday.mp4", "has_audio": True, "reencoded": True}  # always
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
    for name, codec in (("a.mp4", "libx264"), ("b.webm", "libvpx-vp9")):
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
    real_raw = video_ops._run_raw

    def spy(cmd, timeout=None, should_stop=None):
        runs.append((cmd, timeout))
        return real(cmd, timeout=timeout, should_stop=should_stop)

    def spy_raw(cmd, timeout):
        runs.append((cmd, timeout))
        return real_raw(cmd, timeout)

    monkeypatch.setattr(renderer, "_run", spy)
    monkeypatch.setattr(video_ops, "_run_raw", spy_raw)
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
    # Each run over the raw upload uses at most 2 threads, and the re-encode
    # never decodes a picture larger than 4K.
    assert all(c[c.index("-threads") + 1] == "2" for c, _t in raw), raw
    # Each also runs with no single allocation past 512 MB (ffmpeg's own
    # -max_alloc, before the input).
    assert all(c.index("-max_alloc") < c.index("-i") and c[c.index("-max_alloc") + 1] == "536870912"
               for c, _t in raw), raw
    encodes = [c for c, _t in raw if c[0] == "ffmpeg"]
    assert encodes and all(c[c.index("-max_pixels") + 1] == str(4096 * 2304)
                           and c[c.index("-filter_threads") + 1] == "2"
                           and c[len(c) - 1 - c[::-1].index("-threads") + 1] == "2" for c in encodes), encodes
    assert video_ops.UPLOAD_ENCODE_TIMEOUT < 600

    def slow(cmd, timeout):
        if cmd[0] == "ffmpeg" and "libx264" in cmd:
            return -1, "", "timed out after 360s"
        return real_raw(cmd, timeout)

    monkeypatch.setattr(video_ops, "_run_raw", slow)
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


def test_a_live_webm_too_long_is_refused_once_prepared_never_measured_raw(auth_client, tmp_path, monkeypatch):
    """A live WebM states no length: it goes straight to the capped
    re-encode, and the prepared copy (cut a second past the most) is what
    is measured and refused."""
    from app.services import video_ops

    aid = _avatar(auth_client)
    monkeypatch.setattr(video_ops, "UPLOAD_MAX_SECONDS", 2.0)
    webm = tmp_path / "recording.webm"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=32x32:rate=10:duration=5",
                    "-c:v", "libvpx", "-f", "webm", "-live", "1", str(webm)], check=True)
    seen: list[float] = []
    real_norm = video_ops.normalize_upload

    def norm(src, dst, streams, **kw):
        out = real_norm(src, dst, streams, **kw)
        from app.services.renderer import _probe_clip

        seen.append(_probe_clip(dst)[0])
        return out

    monkeypatch.setattr(video_ops, "normalize_upload", norm)
    r = _upload(auth_client, webm, aid, content_type="video/webm")
    assert r.status_code == 422 and "longer than" in r.json()["detail"], r.text
    assert len(seen) == 1 and seen[0] <= 3.5  # never decoded past the cap
    assert _uploads() == 0


def _ebml(eid: int, data: bytes) -> bytes:
    """One EBML element: its id, its size (8-byte vint) and its data."""
    ident = eid.to_bytes((eid.bit_length() + 7) // 8, "big")
    return ident + (0x01 << 56 | len(data)).to_bytes(8, "big") + data


def _uint(eid: int, value: int) -> bytes:
    return _ebml(eid, value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big"))


def _many_tiny_packets_mkv(path: Path, blocks: int, tmp: Path) -> Path:
    """A hand-built Matroska file (a live WebM: no stated length) of
    `blocks` tiny VP8 blocks one millisecond apart: very many packets in
    a small file, the shape that made walking the raw packets huge."""
    ivf = tmp / "one.ivf"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:size=16x16:rate=1:duration=1",
                    "-c:v", "libvpx", "-f", "ivf", str(ivf)], check=True)
    raw = ivf.read_bytes()
    frame = raw[32 + 12:32 + 12 + int.from_bytes(raw[32:36], "little")]  # the first (key) frame
    header = _ebml(0x1A45DFA3, _uint(0x4286, 1) + _uint(0x42F7, 1) + _uint(0x42F2, 4) + _uint(0x42F3, 8)
                   + _ebml(0x4282, b"webm") + _uint(0x4287, 2) + _uint(0x4285, 2))
    info = _ebml(0x1549A966, _uint(0x2AD7B1, 1_000_000))  # milliseconds; no Duration
    tracks = _ebml(0x1654AE6B, _ebml(0xAE, _uint(0xD7, 1) + _uint(0x73C5, 1) + _uint(0x83, 1)
                                     + _ebml(0x86, b"V_VP8") + _ebml(0xE0, _uint(0xB0, 16) + _uint(0xBA, 16))))
    per_cluster = 30_000
    with open(path, "wb") as f:
        f.write(header + bytes.fromhex("18538067") + bytes.fromhex("01FFFFFFFFFFFFFF") + info + tracks)
        for start in range(0, blocks, per_cluster):
            n = min(per_cluster, blocks - start)
            body = b"".join(_ebml(0xA3, b"\x81" + i.to_bytes(2, "big") + b"\x80" + frame) for i in range(n))
            f.write(_ebml(0x1F43B675, _uint(0xE7, start) + body))
    return path


def test_many_tiny_packets_are_never_walked_on_the_raw_upload(auth_client, tmp_path, temp_root, monkeypatch):
    """A file of very many tiny blocks that states no length: no per-packet
    ffprobe runs over the raw upload (nor is any such output parsed); it
    goes to the capped, limited re-encode and the prepared copy decides."""
    import time

    from app.services import renderer, video_ops

    aid = _avatar(auth_client)
    monkeypatch.setattr(video_ops, "UPLOAD_MAX_SECONDS", 20.0)
    mkv = _many_tiny_packets_mkv(tmp_path / "bomb.webm", 200_000, tmp_path)
    assert mkv.stat().st_size < 50 * 1024 * 1024
    assert video_ops._probe_upload(mkv, "matroska")["seconds"] == 0.0  # really no stated length
    runs: list[list[str]] = []
    real, real_raw = renderer._run, video_ops._run_raw

    def spy(cmd, timeout=None, should_stop=None):
        runs.append(cmd)
        return real(cmd, timeout=timeout, should_stop=should_stop)

    def spy_raw(cmd, timeout):
        runs.append(cmd)
        return real_raw(cmd, timeout)

    monkeypatch.setattr(renderer, "_run", spy)
    monkeypatch.setattr(video_ops, "_run_raw", spy_raw)
    begun = time.monotonic()
    r = _upload(auth_client, mkv, aid, content_type="video/webm")
    took = time.monotonic() - begun
    assert r.status_code == 422 and "longer than" in r.json()["detail"], r.text
    raw = [c for c in runs if any("upload.mkv" in part for part in c)]
    entries = [c[i + 1] for c in raw for i, part in enumerate(c[:-1]) if part == "-show_entries"]
    assert raw and entries and not [e for e in entries if "packet" in e or "frame=" in e], entries
    assert took < video_ops.UPLOAD_DEADLINE
    assert _uploads() == 0 and _left(temp_root) == []


def test_an_anamorphic_upload_is_stored_with_square_pixels_at_its_shown_shape(auth_client, tmp_path):
    """720x576 with a 64:45 sample aspect ratio (PAL 16:9) is shown at
    1024x576: stored so (square pixels), never squashed to 5:4, and its
    aspect is 16:9."""
    from app.services import video_ops

    aid = _avatar(auth_client)
    src = tmp_path / "pal.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=720x576:rate=10:duration=2",
                    "-vf", "setsar=64/45", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)], check=True)
    streams = video_ops._probe_upload(src, "mov")
    assert streams["size"] == (720, 576) and streams["display"] == (1024, 576)
    assert video_ops.aspect_for(streams["display"]) == "16:9"
    r = _upload(auth_client, src, aid)
    assert r.status_code == 201, r.text
    job = r.json()
    w, h = job["result_width"], job["result_height"]
    assert w / h == pytest.approx(16 / 9, rel=0.02), (w, h)
    assert job["aspect_ratio"] == "16:9"
    out = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-select_streams", "v:0",
                          "-show_entries", "stream=width,height,sample_aspect_ratio", job["result_path"]],
                         capture_output=True, text=True, check=True)
    st = json.loads(out.stdout)["streams"][0]
    assert (st["width"], st["height"]) == (w, h) and st.get("sample_aspect_ratio", "1:1") in ("1:1", "0:1")


def test_a_shown_size_past_4k_is_refused_before_decoding():
    from app.services import video_ops

    ok = {"video": "h264", "size": (2048, 2048), "display": (2048, 2048)}
    assert video_ops.upload_refusal(ok) is None
    wide = {**ok, "display": (8192, 2048)}  # a sample aspect ratio of 4:1
    assert "8192x2048" in video_ops.upload_refusal(wide)


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


def test_every_upload_is_re_encoded_within_1080p_and_60fps(auth_client, tmp_path):
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
    # An ordinary one is re-encoded too (never copied as it is), its size kept.
    job = _upload(auth_client, _video(tmp_path / "ok.mp4", size=(1080, 1920), seconds=0.5), aid).json()
    assert job["options"]["reencoded"] is True
    assert (job["result_width"], job["result_height"]) == (1080, 1920)


def _mono_png(path: Path, side: int) -> Path:
    """A black side x side PNG, 1 bit a pixel: a few KB on disk, however
    large its picture."""
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    z = zlib.compressobj(9)
    row = b"\0" * (1 + (side + 7) // 8)
    body = b"".join(z.compress(row) for _ in range(side)) + z.flush()
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 1, 0, 0, 0, 0))
                     + chunk(b"IDAT", body) + chunk(b"IEND", b""))
    return path


def test_a_huge_picture_or_a_non_video_codec_is_refused_before_it_is_decoded(auth_client, tmp_path, temp_root,
                                                                             monkeypatch):
    """A small MKV of 16000x16000 PNG frames (gigabytes once decoded) is
    refused on what its headers say: never decoded, never prepared. So is
    a picture over 4K either way, or one that isn't a video codec."""
    from app.services import renderer, video_ops

    aid = _avatar(auth_client)
    runs: list[list[str]] = []
    real, real_raw = renderer._run, video_ops._run_raw

    def spy(cmd, timeout=None, should_stop=None):
        runs.append(cmd)
        return real(cmd, timeout=timeout, should_stop=should_stop)

    def spy_raw(cmd, timeout):
        runs.append(cmd)
        return real_raw(cmd, timeout)

    monkeypatch.setattr(renderer, "_run", spy)
    monkeypatch.setattr(video_ops, "_run_raw", spy_raw)
    prepared: list = []
    monkeypatch.setattr(video_ops, "normalize_upload", lambda *a, **kw: prepared.append(a))
    big = tmp_path / "big.mkv"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "image2", "-framerate", "30", "-loop", "1", "-t", "2",
                    "-i", str(_mono_png(tmp_path / "big.png", 16000)), "-c:v", "copy", str(big)], check=True)
    assert big.stat().st_size < 2_000_000
    r = _upload(auth_client, big, aid, content_type="video/x-matroska")
    assert r.status_code == 422 and "16000x16000" in r.json()["detail"], r.text
    # Only its headers were read (ffprobe, nothing decoded).
    assert runs and all(c[0] == "ffprobe" and "-nofind_stream_info" in c for c in runs), runs
    # Over 4096 on one side, or more pixels than 4096x2304, is refused too.
    wide = _video(tmp_path / "wide.mp4", seconds=0.3, size=(4104, 16))
    r = _upload(auth_client, wide, aid)
    assert r.status_code == 422 and "4104x16" in r.json()["detail"], r.text
    # A small picture that isn't a video codec (PNG frames) is refused.
    pngs = tmp_path / "pngs.mkv"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=32x32:rate=5:duration=1",
                    "-c:v", "png", str(pngs)], check=True)
    r = _upload(auth_client, pngs, aid, content_type="video/x-matroska")
    assert r.status_code == 422 and "png" in r.json()["detail"], r.text
    assert prepared == [] and _uploads() == 0 and _clip_files() == [] and _left(temp_root) == []
    assert video_ops.upload_refusal({"video": "h264", "size": (4096, 2304)}) is None
    assert video_ops.upload_refusal({"video": "hevc", "size": (2304, 4096)}) is None
    for size in ((4096, 2305), (4097, 10), (10, 4097), None, (0, 0)):
        assert video_ops.upload_refusal({"video": "h264", "size": size}), size
    assert video_ops.upload_refusal({"video": "", "size": (32, 32)})


def _frames(path: str) -> tuple[set[tuple[int, int]], list[float]]:
    """(each frame's size, each frame's time) of a video, every frame read."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-print_format", "json",
                          "-show_entries", "frame=width,height,pts_time", path], capture_output=True, text=True,
                         check=True).stdout
    frames = json.loads(out)["frames"]
    return {(f["width"], f["height"]) for f in frames}, sorted(float(f["pts_time"]) for f in frames)


def test_a_stream_that_changes_size_midway_is_stored_within_1080p(auth_client, tmp_path, monkeypatch):
    """An H.264 stream whose first picture is 320x240 and that turns 4K
    midway (two raw streams one after the other) is stored with every
    frame, and every frame checked, inside its recorded size."""
    from PIL import Image

    from app.services import video_ops

    aid = _avatar(auth_client)
    parts = []
    for i, size in enumerate(("320x240", "3840x2160")):
        part = tmp_path / f"part{i}.h264"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=10:duration=1",
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-f", "h264", str(part)],
                       check=True)
        parts.append(part.read_bytes())
    both = tmp_path / "both.h264"
    both.write_bytes(b"".join(parts))
    video = tmp_path / "changes.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "h264", "-framerate", "10", "-i", str(both), "-c", "copy",
                    str(video)], check=True)
    checked: list[tuple[int, int]] = []
    real = video_ops.check_uploaded_frames

    def spy(frames, label, **kw):
        for _path, data in frames:
            with Image.open(io.BytesIO(data)) as im:
                checked.append(im.size)
        return real(frames, label, **kw)

    monkeypatch.setattr(video_ops, "check_uploaded_frames", spy)
    r = _upload(auth_client, video, aid)
    assert r.status_code == 201, r.text
    job = r.json()
    assert (job["result_width"], job["result_height"]) == (320, 240)
    assert checked and set(checked) == {(320, 240)}, checked
    sizes, _times = _frames(job["result_path"])
    assert sizes == {(320, 240)}, sizes


def test_a_burst_of_frames_is_stored_at_60fps_at_most(auth_client, tmp_path):
    """600 frames packed into the first second of a 19 s MP4 (its average
    rate about 32 fps) are not stored as they are: the stored copy never
    shows more than 60 frames a second."""
    aid = _avatar(auth_client)
    burst = tmp_path / "burst.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x48:rate=30", "-frames:v",
                    "610", "-vf", "setpts='if(lt(N,600),N/600,1+(N-600)*2)/TB'", "-fps_mode", "passthrough",
                    "-enc_time_base", "1/6000", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    str(burst)], check=True)
    assert _fps(str(burst)) <= 60  # its stated average rate is ordinary
    r = _upload(auth_client, burst, aid)
    assert r.status_code == 201, r.text
    _sizes, times = _frames(r.json()["result_path"])
    gaps = [b - a for a, b in zip(times[:-1], times[1:], strict=True)]
    assert gaps and min(gaps) >= 1 / 60 - 1e-3, min(gaps)


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


# ---------- final hardening: memory, shape, output size, sound ----------


def _sample_table_bomb(path: Path, tmp: Path, samples: int = 80_000_000) -> Path:
    """A tiny 1-frame MP4 whose sample tables (stts, stsz, stsc) claim
    `samples` samples: a few KB on disk, gigabytes of index once read."""
    import struct

    one = tmp / "one-frame.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:size=64x64:rate=1:duration=1",
                    "-frames:v", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(one)], check=True)
    data = bytearray(one.read_bytes())

    def body(name: bytes) -> int:
        i = data.find(name)
        assert i > 0 and data.count(name) == 1, name
        return i + 4 + 4  # past the name, version and flags

    stts = body(b"stts")  # entry count 1: (sample count, delta)
    assert struct.unpack_from(">I", data, stts)[0] == 1
    struct.pack_into(">I", data, stts + 4, samples)
    stsz = body(b"stsz")  # (sample size, count, sizes...): one constant size for them all
    size, count = struct.unpack_from(">II", data, stsz)
    if size == 0:
        assert count == 1
        size = struct.unpack_from(">I", data, stsz + 8)[0]
    struct.pack_into(">II", data, stsz, size, samples)
    stsc = body(b"stsc")  # entry count 1: (first chunk, samples per chunk, description)
    assert struct.unpack_from(">I", data, stsc)[0] == 1
    struct.pack_into(">I", data, stsc + 8, samples)
    path.write_bytes(bytes(data))
    return path


_PEAK_SCRIPT = """
import resource, sys
from pathlib import Path
from app.services import video_ops
from app.services.image_ops import OpError
try:
    video_ops.prepare_upload(Path(sys.argv[1]), Path(sys.argv[2]), lambda limit: limit)
    print("kept")
except OpError as e:
    print("refused", e.status)
print(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * 1024)
"""


def test_a_sample_table_bomb_is_refused_and_its_runs_stay_under_the_memory_cap(auth_client, tmp_path, temp_root):
    """A 1-frame MP4 whose tables claim 80 million samples (unchecked,
    reading it took about 1.9 GB): every run over it stays under the
    address-space cap, and the upload is refused."""
    import sys

    from app.services import video_ops

    bomb = _sample_table_bomb(tmp_path / "bomb.mp4", tmp_path)
    assert bomb.stat().st_size < 10_000
    # The whole preparation, in a process of its own so its children's peak
    # memory is theirs alone.
    work = tmp_path / "work"
    work.mkdir()
    raw = work / "upload"
    raw.write_bytes(bomb.read_bytes())
    out = subprocess.run([sys.executable, "-c", _PEAK_SCRIPT, str(raw), str(work / "prepared.mp4")],
                         capture_output=True, text=True, timeout=300, cwd=str(Path(__file__).resolve().parents[1]))
    assert out.returncode == 0, out.stderr[-2000:]
    verdict, peak = out.stdout.strip().splitlines()[-2:]
    assert verdict == "refused 422", out.stdout
    assert int(peak) < video_ops.UPLOAD_MEMORY_LIMIT, int(peak) // (1024 * 1024)
    aid = _avatar(auth_client)
    r = _upload(auth_client, bomb, aid)
    assert r.status_code == 422, r.text
    assert _uploads() == 0 and _clip_files() == [] and _left(temp_root) == []


def test_raw_runs_have_their_memory_capped_and_running_out_is_a_plain_refusal(tmp_path):
    import sys

    from app.services import video_ops
    from app.services.image_ops import OpError

    assert video_ops.UPLOAD_MEMORY_LIMIT == 1536 * 1024 * 1024
    rc, printed, _log = video_ops._run_raw(
        [sys.executable, "-c", "import resource; print(resource.getrlimit(resource.RLIMIT_AS)[0])"], timeout=30)
    assert rc == 0 and int(printed) == video_ops.UPLOAD_MEMORY_LIMIT
    # Killed (as the kernel does when memory runs out), or saying it ran out.
    for code in ("import os, signal; os.kill(os.getpid(), signal.SIGKILL)",
                 "import sys; sys.stderr.write('[h264] Cannot allocate memory'); sys.exit(1)"):
        with pytest.raises(OpError) as e:
            video_ops._run_raw([sys.executable, "-c", code], timeout=30)
        assert (e.value.status, e.value.message) == (422, "That video needs too much memory to read")


def test_raw_runs_keep_only_the_tail_of_a_huge_log(tmp_path):
    import sys

    from app.services import video_ops

    code = ("import sys; sys.stdout.write('o' * 5_000_000); "
            "[sys.stderr.write('x' * 1_000_000) for _ in range(20)]; sys.stderr.write('THE END')")
    rc, printed, log = video_ops._run_raw([sys.executable, "-c", code], timeout=60)
    assert rc == 0
    assert len(log) <= video_ops.RAW_LOG_TAIL and log.endswith("THE END")
    assert len(printed) <= video_ops.RAW_OUTPUT_MAX


@pytest.mark.parametrize("name,codec", [("hd.mp4", "libx264"), ("hd.webm", "libvpx-vp9")])
def test_ordinary_1080p_uploads_are_kept_under_the_memory_cap(auth_client, tmp_path, name, codec):
    aid = _avatar(auth_client)
    video = tmp_path / name
    extra = ["-deadline", "realtime", "-cpu-used", "8"] if codec == "libvpx-vp9" else ["-preset", "ultrafast"]
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=30:duration=1",
                    "-c:v", codec, *extra, "-pix_fmt", "yuv420p", str(video)], check=True)
    r = _upload(auth_client, video, aid, content_type="video/webm" if name.endswith(".webm") else "video/mp4")
    assert r.status_code == 201, r.text
    assert (r.json()["result_width"], r.json()["result_height"]) == (1920, 1080)


def _squared(size: str, sar: str) -> tuple[int, int]:
    """The size square_pixels_vf makes of one size x size frame with that
    sample aspect ratio."""
    import re

    from app.services import video_ops

    out = subprocess.run(["ffmpeg", "-hide_banner", "-f", "lavfi", "-i", f"color=size={size}:rate=1:duration=1,"
                          f"setsar={sar}", "-vf", video_ops.square_pixels_vf() + ",showinfo", "-frames:v", "1",
                          "-f", "null", "-"], capture_output=True, text=True, check=True).stderr
    w, h = re.search(r" s:(\d+)x(\d+)", out).groups()
    return int(w), int(h)


def test_square_pixels_keep_the_shape_within_4k_by_area_too():
    """Widened (ratio above 1) or made taller (below 1), then fitted so
    neither side passes 4096 and width x height stays within 4K's pixels,
    the shape kept: a tall picture whose ratio says 4:1, and an extreme
    100:1, no longer grow to 4096x4096 or lose their shape."""
    from app.services import video_ops

    most = video_ops.UPLOAD_MAX_PIXELS
    cases = {("1024x4096", "4"): 1.0, ("2048x2048", "100"): 100.0, ("512x4096", "1/4"): 1 / 32,
             ("4096x16", "1/1000"): 0.256, ("720x576", "64/45"): 1024 / 576, ("64x48", "1"): 64 / 48}
    for (size, sar), shape in cases.items():
        w, h = _squared(size, sar)
        assert w % 2 == 0 and h % 2 == 0 and w <= 4096 and h <= 4096 and w * h <= most, (size, sar, w, h)
        assert w / h == pytest.approx(shape, rel=0.06), (size, sar, w, h)
    assert _squared("720x576", "64/45") == (1024, 576)  # PAL 16:9 as before
    assert _squared("1024x4096", "4") == (3072, 3072)
    # The shown size read from the headers agrees: taller when below 1.
    assert video_ops.display_size({"width": 720, "height": 576, "sample_aspect_ratio": "64:45"}) == (1024, 576)
    assert video_ops.display_size({"width": 512, "height": 1024, "sample_aspect_ratio": "1:2"}) == (512, 2048)


def test_sound_in_an_unlisted_codec_is_dropped_not_refused(auth_client, tmp_path):
    from app.services import video_ops

    for codec in ("aac", "mp3", "opus", "vorbis", "flac", "ac3", "eac3", "alac", "pcm_s16le", "pcm_f32le"):
        assert video_ops.audio_kept(codec), codec
    for codec in ("wmav2", "cook", "", None):
        assert not video_ops.audio_kept(codec), codec
    aid = _avatar(auth_client)
    video = tmp_path / "wma.mkv"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x48:rate=10:duration=1",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "wmav2", str(video)], check=True)
    assert video_ops._probe_upload(video, "matroska")["audio"] == "wmav2"
    r = _upload(auth_client, video, aid, content_type="video/x-matroska")
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["options"]["has_audio"] is False
    assert _codecs(job["result_path"]) == {"video": ("h264", "yuv420p")}


def test_the_re_encode_is_rate_capped_and_an_output_reaching_its_size_limit_is_refused(auth_client, tmp_path,
                                                                                      temp_root, monkeypatch):
    from app.services import video_ops

    aid = _avatar(auth_client)
    runs: list[list[str]] = []
    real_raw = video_ops._run_raw

    def spy(cmd, timeout):
        runs.append(cmd)
        return real_raw(cmd, timeout)

    monkeypatch.setattr(video_ops, "_run_raw", spy)
    noise = tmp_path / "noise.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "nullsrc=size=320x240:rate=10:duration=3,geq=random(1)*255:128:128", "-c:v", "libx264",
                    "-crf", "0", "-pix_fmt", "yuv420p", str(noise)], check=True)
    monkeypatch.setattr(video_ops, "UPLOAD_MAX_OUTPUT_BYTES", 200_000)
    r = _upload(auth_client, noise, aid)
    assert r.status_code == 422 and "too large" in r.json()["detail"], r.text
    encode = [c for c in runs if c[0] == "ffmpeg"][-1]
    assert (encode[encode.index("-maxrate") + 1], encode[encode.index("-bufsize") + 1]) == ("12M", "24M")
    assert encode[encode.index("-fs") + 1] == "200000"
    assert _uploads() == 0 and _clip_files() == [] and _left(temp_root) == []
    monkeypatch.setattr(video_ops, "UPLOAD_MAX_OUTPUT_BYTES", 200 * 1000 * 1000)
    r = _upload(auth_client, noise, aid)
    assert r.status_code == 201, r.text
    encode = [c for c in runs if c[0] == "ffmpeg"][-1]
    assert encode[encode.index("-fs") + 1] == "200000000"

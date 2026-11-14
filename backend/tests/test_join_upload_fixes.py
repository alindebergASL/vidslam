"""Fixes from the review of joined clips (#51) and uploaded videos as clips
(#52): a cancelled join's FFmpeg really stops; the limit on uploads being
prepared holds across every API process; an upload answered after the
proxy gave up isn't kept; a clean deployment looks at an upload's frames
about every 5 seconds."""
from __future__ import annotations

import math
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from .test_image_ops import _avatar
from .test_join_clips import _video as _join_video
from .test_upload_video import _clip_files, _left, _upload, _uploads, _video


@pytest.fixture
def temp_root(tmp_path, monkeypatch):
    """Temporary files go here, so a test can see none is left behind."""
    root = tmp_path / "tmp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


# ---------- a cancelled join's FFmpeg ----------


def test_a_join_asked_to_stop_kills_its_ffmpeg_within_seconds(tmp_path):
    """join_clips_with_audio passes should_stop on: a join of ten minutes of
    video is stopped at the first poll (about a second in), not run to the
    end."""
    from app.services.renderer import JoinStopped, join_clips_with_audio

    a = _join_video(tmp_path / "a.mp4", seconds=300.0)
    asked: list[int] = []
    t0 = time.monotonic()
    with pytest.raises(JoinStopped):
        join_clips_with_audio([a, a], tmp_path / "out.mp4", should_stop=lambda: asked.append(1) or True)
    assert asked and time.monotonic() - t0 < 8


# ---------- uploads being prepared at once, across processes ----------


_HOLDER = """
import fcntl, os, sys
fds = []
for name in sys.argv[1:]:
    fd = os.open(name, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    fds.append(fd)
print("held", flush=True)
sys.stdin.read()
"""


def _hold_places(n: int) -> subprocess.Popen:
    """Another process (as another uvicorn worker would) holding `n` of
    the upload places until its stdin is closed."""
    from app.services import video_ops

    folder = video_ops._upload_slots._folder()
    names = [str(folder / f"upload-{i}.lock") for i in range(n)]
    proc = subprocess.Popen([sys.executable, "-c", _HOLDER, *names], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "held"
    return proc


def _let_go(proc: subprocess.Popen) -> None:
    proc.stdin.close()
    proc.wait(timeout=10)


def test_the_upload_limit_counts_uploads_in_every_api_process(auth_client, tmp_path):
    from app.services import video_ops

    from .test_frame_to_picture import _upload as _frame_upload

    aid = _avatar(auth_client)
    video = _video(tmp_path / "a.mp4")
    assert video_ops.UPLOAD_MAX_RUNNING == 2
    other = _hold_places(2)  # two uploads being prepared by another worker
    try:
        r = _upload(auth_client, video, aid)
        assert r.status_code == 429 and r.json()["detail"] == video_ops.UPLOAD_BUSY, r.text
        assert _frame_upload(auth_client, video, aid).status_code == 429
        assert _uploads() == 0
    finally:
        _let_go(other)
    other = _hold_places(1)  # one: a place is left here
    try:
        assert _upload(auth_client, video, aid).status_code == 201
    finally:
        _let_go(other)
    # Its process gone, its places are free again; and this process gives
    # its own back on every path.
    assert _upload(auth_client, video, aid).status_code == 201
    assert _frame_upload(auth_client, video, aid).status_code == 201
    assert _uploads() == 2


def test_places_are_counted_per_holder_and_given_back():
    import threading

    from app.services.video_ops import _Places

    places = _Places("test", 2)
    assert places.acquire(blocking=False) and places.acquire(blocking=False)
    got: list[bool] = []
    t = threading.Thread(target=lambda: got.append(places.acquire(blocking=False)))
    t.start()
    t.join()
    assert got == [False]  # full, from another thread too
    places.release()
    other = _Places("test", 2)
    assert other.acquire(blocking=False)  # another holder takes the free one
    assert not other.acquire(blocking=False)
    other.release()
    places.release()
    with pytest.raises(ValueError):
        places.release()  # nothing taken


# ---------- an upload answered after the proxy gave up ----------


def test_an_upload_checked_after_the_proxy_gave_up_is_not_kept_and_a_retry_is_free(
        auth_client, tmp_path, temp_root, monkeypatch):
    from app.providers import mock_chat
    from app.services import video_ops

    aid = _avatar(auth_client)
    video = _video(tmp_path / "a.mp4")
    real = video_ops.check_uploaded_frames

    def slow(frames, label, **kw):
        real(frames, label, **kw)
        monkeypatch.setattr(video_ops, "UPLOAD_ANSWER_BY", 0.0)  # the checks ended past the proxy's limit

    monkeypatch.setattr(video_ops, "check_uploaded_frames", slow)
    r = _upload(auth_client, video, aid)
    assert r.status_code == 504 and r.json()["detail"] == video_ops.UPLOAD_ANSWERED_LATE, r.text
    assert _uploads() == 0 and _clip_files() == [] and _left(temp_root) == []
    assert sorted(mock_chat.PICTURE_CALLS) == [0, 1, 2]
    # Trying again: kept, and the frames already judged aren't asked about
    # (or paid for) again.
    monkeypatch.setattr(video_ops, "check_uploaded_frames", real)
    monkeypatch.setattr(video_ops, "UPLOAD_ANSWER_BY", 570.0)
    assert _upload(auth_client, video, aid).status_code == 201
    assert sorted(mock_chat.PICTURE_CALLS) == [0, 1, 2] and _uploads() == 1


def test_no_check_is_paid_for_once_the_proxy_has_given_up(auth_client, tmp_path, temp_root, monkeypatch):
    from app.providers import mock_chat
    from app.services import video_ops

    from .test_frame_to_picture import _frame_jobs
    from .test_frame_to_picture import _upload as _frame_upload

    aid = _avatar(auth_client)
    video = _video(tmp_path / "a.mp4")
    monkeypatch.setattr(video_ops, "UPLOAD_ANSWER_BY", 0.0)
    r = _upload(auth_client, video, aid)
    assert r.status_code == 504 and r.json()["detail"] == video_ops.UPLOAD_ANSWERED_LATE, r.text
    r = _frame_upload(auth_client, video, aid)
    assert r.status_code == 504 and r.json()["detail"] == video_ops.UPLOAD_ANSWERED_LATE, r.text
    assert mock_chat.PICTURE_CALLS == [] and _uploads() == 0 and _frame_jobs() == 0
    assert _left(temp_root) == []


# ---------- a clean deployment looks at uploads more closely ----------


def test_clean_frame_times_are_about_5_seconds_apart_at_most_40():
    from app.services.video_ops import UPLOAD_CLEAN_MAX_FRAMES, upload_frame_times

    assert UPLOAD_CLEAN_MAX_FRAMES == 40
    assert upload_frame_times(2.0, clean=True) == [0.0, 1.0, math.inf]
    times = upload_frame_times(35.0, clean=True)
    assert len(times) == 8 and times[:2] == [0.0, 5.0] and times[-1] == math.inf
    assert len(upload_frame_times(35.0)) == 5  # an adult server's: about every 10 s
    assert len(upload_frame_times(195.0, clean=True)) == 40
    assert len(upload_frame_times(600.0, clean=True)) == 40


def test_a_clean_deployment_checks_an_upload_about_every_5_seconds(auth_client, tmp_path, temp_root, monkeypatch):
    from app.providers import mock_chat
    from app.services import explicit_check

    from .test_adult_features_off import _clean

    aid = _avatar(auth_client)
    _clean(monkeypatch)
    r = _upload(auth_client, _video(tmp_path / "a.mp4", seconds=35.0, size=(32, 32)), aid)
    assert r.status_code == 201, r.text
    assert sorted(mock_chat.PICTURE_CALLS) == list(range(8))
    assert len(mock_chat.PICTURE_EXPLICIT_CALLS) == 8
    # Any one of them explicit refuses the lot: here only the seventh,
    # which the old 10-second spacing (5 frames) never looked at.
    real_judge = explicit_check._judge_picture

    def judge(path):  # noqa: ANN001, ANN202
        if Path(path).name == "frame6.png":
            return True, "Nudity.", "test"
        return real_judge(path)

    monkeypatch.setattr(explicit_check, "_judge_picture", judge)
    r = _upload(auth_client, _video(tmp_path / "b.mp4", seconds=35.0, size=(48, 48)), aid)
    assert r.status_code == 422 and "general audience" in r.json()["detail"], r.text
    assert _uploads() == 1 and _left(temp_root) == []

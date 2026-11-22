"""Browser-safe preview copies: clips a browser can't play are served as an
8-bit H.264 yuv420p copy; the original is left as it is for renders."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from app.services import previews

from .test_shot_clip import _rendered_project


def _clip(path: Path, pix_fmt: str) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=1",
                    "-f", "lavfi", "-i", "sine=d=1", "-c:v", "libx264", "-pix_fmt", pix_fmt,
                    "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def _streams(path: Path) -> list[dict]:
    out = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_entries",
                          "stream=codec_type,codec_name,pix_fmt,color_space", str(path)],
                         capture_output=True, check=True).stdout
    return json.loads(out)["streams"]


def test_playable_clip_served_as_is(tmp_path):
    src = _clip(tmp_path / "a.mp4", "yuv420p")
    assert previews.browser_safe(src) == src
    assert not previews.preview_path(src).exists()


def test_422_clip_gets_h264_420_copy_and_original_kept(tmp_path):
    src = _clip(tmp_path / "b.mp4", "yuv422p")
    before = src.read_bytes()
    out = previews.browser_safe(src)
    assert out == previews.preview_path(src) and out.exists()
    v = [s for s in _streams(out) if s["codec_type"] == "video"][0]
    assert (v["codec_name"], v["pix_fmt"], v.get("color_space")) == ("h264", "yuv420p", "bt709")
    assert any(s["codec_type"] == "audio" for s in _streams(out))
    assert src.read_bytes() == before
    # Reused while fresh; rebuilt when the clip is replaced.
    mtime = out.stat().st_mtime_ns
    assert previews.browser_safe(src) == out and out.stat().st_mtime_ns == mtime
    _clip(src, "yuv422p")
    assert previews.browser_safe(src) == out and out.stat().st_mtime_ns > mtime
    previews.remove(src)
    assert not out.exists()


def test_failed_copy_falls_back_to_original(tmp_path, monkeypatch):
    src = _clip(tmp_path / "c.mp4", "yuv422p")
    real = previews.renderer._run
    monkeypatch.setattr(previews.renderer, "_run",
                        lambda cmd, **k: (1, "boom") if cmd[0] == "ffmpeg" else real(cmd, **k))
    assert previews.browser_safe(src) == src
    assert not list(tmp_path.glob(".*tmp*"))


def test_shot_clip_endpoint_serves_preview(auth_client):
    from app import models
    from app.db import SessionLocal

    pid = _rendered_project(auth_client)
    shots = auth_client.get(f"/api/projects/{pid}").json()["shots"]
    sid = [s for s in shots if s["shot_type"] != "end_card"][0]["id"]
    with SessionLocal() as db:
        path = Path(db.get(models.VideoShot, sid).clip_path)
    _clip(path, "yuv422p")
    r = auth_client.get(f"/api/projects/{pid}/shots/{sid}/clip")
    assert r.status_code == 200
    assert r.content == previews.preview_path(path).read_bytes()


def _count_ffmpeg(monkeypatch, fail: bool = False) -> list[list[str]]:
    calls: list[list[str]] = []
    real = previews.renderer._run

    def run(cmd, **k):
        if cmd[0] == "ffmpeg":
            calls.append(cmd)
            if fail:
                return 1, "boom"
        return real(cmd, **k)

    monkeypatch.setattr(previews.renderer, "_run", run)
    return calls


def test_failed_copy_is_not_retried_until_the_clip_changes(tmp_path, monkeypatch):
    src = _clip(tmp_path / "f.mp4", "yuv422p")
    calls = _count_ffmpeg(monkeypatch, fail=True)
    for _ in range(3):  # a <video>'s range requests
        assert previews.browser_safe(src) == src
    assert len(calls) == 1
    monkeypatch.undo()
    _clip(src, "yuv422p")  # replaced: tried again (and now succeeds)
    assert previews.browser_safe(src) == previews.preview_path(src)


def test_a_clip_being_copied_does_not_block_other_clips(tmp_path):
    a = _clip(tmp_path / "a.mp4", "yuv422p")
    b = _clip(tmp_path / "b.mp4", "yuv422p")
    lock = previews._lock_for(a)
    assert lock.acquire(blocking=False)  # a's copy "in progress"
    try:
        assert previews.browser_safe(a) == a  # served at once, not queued
        assert previews.browser_safe(b) == previews.preview_path(b)  # b unaffected
    finally:
        lock.release()
    assert previews.browser_safe(a) == previews.preview_path(a)


def test_busy_transcode_slots_serve_the_original(tmp_path, monkeypatch):
    src = _clip(tmp_path / "s.mp4", "yuv422p")
    calls = _count_ffmpeg(monkeypatch)
    held = [previews._SLOTS.acquire(blocking=False) for _ in range(previews.MAX_TRANSCODES)]
    try:
        assert all(held)
        assert previews.browser_safe(src) == src
        assert calls == []
    finally:
        for _ in held:
            previews._SLOTS.release()
    assert previews.browser_safe(src) == previews.preview_path(src)  # not remembered as failed


def test_same_name_different_extension_get_their_own_copy(tmp_path):
    mov = _clip(tmp_path / "clip.mov", "yuv422p")
    mp4 = _clip(tmp_path / "clip.mp4", "yuv444p")
    assert previews.preview_path(mov) != previews.preview_path(mp4)
    assert previews.browser_safe(mov) == previews.preview_path(mov)
    assert previews.browser_safe(mp4) == previews.preview_path(mp4)
    legacy = previews._legacy_path(mp4)
    legacy.write_bytes(b"old")
    previews.remove(mp4)
    assert not previews.preview_path(mp4).exists() and not legacy.exists()
    assert previews.preview_path(mov).exists()


def test_preview_ffmpeg_threads_are_capped(tmp_path, monkeypatch):
    src = _clip(tmp_path / "t.mp4", "yuv422p")
    calls = _count_ffmpeg(monkeypatch)
    previews.browser_safe(src)
    assert calls and calls[0].count("-threads") == 2


def test_clip_endpoint_returns_its_db_connection_before_transcoding(auth_client, monkeypatch):
    """A slow transcode must not hold a pooled connection: with several
    HEVC shots on a page that ran the pool dry for unrelated API calls."""
    from app import db as app_db

    pid = _rendered_project(auth_client)
    shots = auth_client.get(f"/api/projects/{pid}").json()["shots"]
    sid = [s for s in shots if s["shot_type"] != "end_card"][0]["id"]
    seen: list[int] = []
    real = previews.browser_safe

    def spy(src):
        seen.append(app_db.engine.pool.checkedout())
        return real(src)

    monkeypatch.setattr(previews, "browser_safe", spy)
    before = app_db.engine.pool.checkedout()
    assert auth_client.get(f"/api/projects/{pid}/shots/{sid}/clip").status_code == 200
    assert seen and seen[0] <= before

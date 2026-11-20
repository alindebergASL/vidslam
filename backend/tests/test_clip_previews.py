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

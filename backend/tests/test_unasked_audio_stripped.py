"""A clip asked for without sound that arrives with a sound track anyway
(OpenRouter's Wan 3.0 ignores generate_audio false; its track runs 20-80 ms
past the picture) is remuxed to its picture on download. A clip asked for
with sound (a Studio clip with sound on), or with nothing recorded (an
avatar's lip-sync voice), is kept as it came."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


def _clip_with_sound(tmp_path: Path) -> bytes:
    p = tmp_path / "with_sound.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=96x64:r=30:d=1",
                    "-f", "lavfi", "-i", "sine=frequency=330:duration=1.1", "-map", "0:v", "-map", "1:a",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(p)], check=True)
    return p.read_bytes()


def _streams(p: Path) -> list[str]:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(p)],
                         capture_output=True, text=True, check=True).stdout
    return [s["codec_type"] for s in json.loads(out)["streams"]]


def _video_md5(p: Path) -> str:
    return subprocess.run(["ffmpeg", "-v", "error", "-i", str(p), "-map", "0:v:0", "-f", "md5", "-"],
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.mark.parametrize("context, kept", [({"audio": False}, False), ({"audio": True}, True), ({}, True)])
def test_download_strips_only_unasked_for_sound(auth_client, tmp_path, monkeypatch, context, kept):
    from app import models
    from app.db import SessionLocal
    from app.services import video_jobs

    data = _clip_with_sound(tmp_path)
    original = tmp_path / "original.mp4"
    original.write_bytes(data)

    class Fake:
        def download(self, submitted, result_url=None):
            return data

    monkeypatch.setattr(video_jobs, "provider_for", lambda job: Fake())
    monkeypatch.setattr(video_jobs, "_submitted", lambda job: None)
    monkeypatch.setattr(video_jobs, "dispatch_completed", lambda db, job: None)
    monkeypatch.setattr(video_jobs, "_record_spend", lambda db, job: None)
    db = SessionLocal()
    try:
        job = models.VideoJob(purpose="studio", ref_id=1, project_id=None, provider="openrouter",
                              model="alibaba/wan-3.0", status="running", context_json=context)
        db.add(job)
        db.commit()
        assert video_jobs.complete(db, job.id) == "completed"
        db.refresh(job)
        path = Path(job.result_path)
        assert _streams(path).count("audio") == (1 if kept else 0)
        assert _video_md5(path) == _video_md5(original)  # the same picture, frame for frame
        if kept:
            assert path.read_bytes() == data
    finally:
        db.close()


def test_shot_and_segment_jobs_record_that_no_sound_was_asked(auth_client):
    """Storyboard shots and ambient segments ask for no sound, and say so on
    the job (the download strips what a model adds anyway)."""
    from app import models
    from app.db import SessionLocal

    from .test_ambient import SHORT

    r = auth_client.post("/api/ambient", json={"config": SHORT, "generate": True})
    assert r.status_code == 201, r.text
    db = SessionLocal()
    try:
        jobs = db.query(models.VideoJob).filter(models.VideoJob.project_id == r.json()["id"]).all()
        assert jobs and all((j.context_json or {}).get("audio") is False for j in jobs)
    finally:
        db.close()

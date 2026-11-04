"""Regression tests for the code-review findings (one test per finding)."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from PIL import Image, ImageStat

from .conftest import make_png_bytes

OR = "https://openrouter.ai/api/v1"


def _dur(p: Path) -> float:
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                 "-of", "csv=p=0", str(p)], capture_output=True, text=True, check=True).stdout)


def _last_frame_luma(video: Path, tmp: Path) -> float:
    png = tmp / "last.png"
    subprocess.run(["ffmpeg", "-y", "-sseof", "-0.1", "-i", str(video), "-frames:v", "1", "-update", "1", str(png)],
                   capture_output=True, check=True)
    with Image.open(png) as im:
        return ImageStat.Stat(im.convert("L")).mean[0]


def _talking_project(client, extra_shot=False) -> int:
    aid = client.post("/api/avatars", json={"name": "Host"}).json()["id"]
    client.post(f"/api/avatars/{aid}/assets", files={"file": ("h.png", make_png_bytes(), "image/png")},
                data={"asset_type": "hero", "rights_confirmed": "true"})
    pid = client.post("/api/projects", json={
        "title": "TH", "original_script": "Hello there, this is a talking head test with a few words.",
        "mode": "talking_head_beta", "target_duration_seconds": 10, "primary_avatar_id": aid,
        "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}]}).json()["id"]
    client.post(f"/api/projects/{pid}/generate-plan")
    from app import models
    from app.db import SessionLocal

    db = SessionLocal()
    p = db.get(models.VideoProject, pid)
    th = [s for s in p.shots if s.shot_type == "talking_head"]
    th[0].duration_seconds = 3.0
    if extra_shot:
        db.add(models.VideoShot(project_id=pid, shot_order=th[0].shot_order + 1, shot_type="talking_head",
                                prompt="second", caption_text="second part", duration_seconds=4.0))
        for s in p.shots:
            if s.shot_type == "end_card":
                s.shot_order = 99
    db.commit()
    db.close()
    return pid


# 1 ── real Avatar IV adapter must accept what the pipeline passes
@respx.mock
def test_real_avatar_adapter_runs_through_pipeline(auth_client, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
    from app import config, providers
    from app.providers.openrouter_avatar import OpenRouterAvatarProvider

    config.get_settings.cache_clear()
    clip = subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x568:d=6:r=30",
                           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "mp4", "-movflags", "frag_keyframe", "pipe:1"],
                          capture_output=True, check=True).stdout
    sub = respx.post(f"{OR}/videos").mock(return_value=httpx.Response(202, json={"id": "a1", "polling_url": f"{OR}/videos/a1"}))
    respx.get(f"{OR}/videos/a1").mock(return_value=httpx.Response(200, json={"status": "completed",
                                                                           "unsigned_urls": [f"{OR}/videos/a1/content"]}))
    respx.get(f"{OR}/videos/a1/content").mock(return_value=httpx.Response(200, content=clip))
    real = OpenRouterAvatarProvider(api_key="sk-or")
    real_animate = real.animate
    monkeypatch.setattr(real, "animate", lambda **kw: real_animate(poll_seconds=0, **kw))
    monkeypatch.setattr(providers, "get_avatar", lambda: real)
    pid = _talking_project(auth_client)
    auth_client.post(f"/api/projects/{pid}/generate-video?force=true")
    st = auth_client.get(f"/api/projects/{pid}/status").json()
    th = [s for s in st["shots"] if s["shot_type"] == "talking_head"][0]
    assert th["provider"] == "lipsync" and th["error"] == "", th["error"]
    assert sub.called


# 2 ── each talking-head shot gets its own voice slice; clip trimmed to the shot
def test_talking_head_shots_get_their_own_audio_slice(auth_client, monkeypatch):
    from app.providers import mock_avatar
    from app.providers.mock_video import _generate_clip
    from app.services import public_media

    calls = []

    def animate(self, **kw):
        calls.append(kw)
        return _generate_clip("avatar", 12.0, 1)  # providers size clips to their audio, not the shot

    monkeypatch.setattr(mock_avatar.MockAvatarProvider, "animate", animate)
    pid = _talking_project(auth_client, extra_shot=True)
    auth_client.put("/api/models/active", json={"kind": "lipsync", "model": "heygen/avatar-iv", "allow_unlisted": True})
    auth_client.post(f"/api/projects/{pid}/generate-video?force=true")
    st = auth_client.get(f"/api/projects/{pid}/status").json()
    assert st["latest_render"]["status"] == "completed", st["latest_render"]["error"]
    assert len(calls) == 2
    slices = [public_media.resolve(c["audio_url"].rsplit("/", 1)[1])[0] for c in calls]
    durs = [_dur(p) for p in slices]
    voice = _dur(Path(st["latest_render"]["audio_path"]))
    # Shot 1 speaks [0, 3); shot 2 speaks [3, 7) — or whatever voice is left.
    assert abs(durs[0] - min(3.0, voice)) < 0.3, (durs, voice)
    assert abs(durs[1] - min(4.0, voice - 3.0)) < 0.3, (durs, voice)
    assert calls[1]["script"] == "second part"
    th = [s for s in st["shots"] if s["shot_type"] == "talking_head"]
    assert [round(_dur(Path(s["clip_path"]))) for s in th] == [3, 4]


def test_shot_start_offsets():
    from types import SimpleNamespace as N

    from app.services.pipeline import _shot_start

    shots = [N(id=1, shot_type="hero", duration_seconds=2.5), N(id=2, shot_type="talking_head", duration_seconds=3),
             N(id=3, shot_type="end_card", duration_seconds=2), N(id=4, shot_type="talking_head", duration_seconds=4)]
    p = N(shots=shots)
    assert [_shot_start(p, s) for s in shots] == [0.0, 2.5, 5.5, 5.5]


# 3 ── the video fade-out must finish exactly at the end
@pytest.mark.parametrize("total", [20.0, 16.5, 7.0])
def test_ambient_fade_out_reaches_black_at_the_end(tmp_path, total):
    from app.services.renderer import AmbientRenderInputs, compose_ambient

    clip = tmp_path / "white.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=white:s=320x180:d=9:r=30",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)], capture_output=True, check=True)
    out = compose_ambient(AmbientRenderInputs(project_id=0, out_dir=tmp_path / "o", clip_path=clip,
                                              total_seconds=total, crossfade_seconds=1.0, fade_seconds=4.0))
    assert abs(_dur(out.final_video_path) - total) < 0.15
    assert _last_frame_luma(out.final_video_path, tmp_path) < 40  # faded to (near) black


# 4 ── a reconnect must invalidate the cached access token
@respx.mock
def test_youtube_token_cache_follows_stored_credentials(auth_client, monkeypatch):
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "cid")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "cs")
    from app import config
    from app.services import model_registry, secret_box, youtube

    config.get_settings.cache_clear()
    youtube._access.clear()
    model_registry.put_setting(youtube.CREDS_KEY, {"refresh_token": secret_box.seal("rt-A")})
    tok = respx.post(youtube.TOKEN_URL).mock(side_effect=[
        httpx.Response(200, json={"access_token": "at-A", "expires_in": 3600}),
        httpx.Response(200, json={"access_token": "at-B", "expires_in": 3600})])
    with httpx.Client() as c:
        assert youtube.access_token(c) == "at-A"
        assert youtube.access_token(c) == "at-A" and tok.call_count == 1  # cached
        # Another process reconnects to channel B.
        model_registry.put_setting(youtube.CREDS_KEY, {"refresh_token": secret_box.seal("rt-B")})
        assert youtube.access_token(c) == "at-B"
    assert parse_qs(tok.calls[1].request.content.decode())["refresh_token"] == ["rt-B"]


# 5 ── never redirect to an S3 object that isn't there; keep the filename
def test_s3_redirect_only_when_object_exists(auth_client, monkeypatch):
    from app.services import s3

    class Fake:
        present = False
        params = {}

        def head_object(self, **kw):
            if not Fake.present:
                raise RuntimeError("404")

        def generate_presigned_url(self, op, Params, ExpiresIn):
            Fake.params = Params
            return "https://bucket.example/x?sig=1"

        def put_object(self, **kw):
            pass

    monkeypatch.setenv("S3_BUCKET", "b")
    from app import config

    config.get_settings.cache_clear()
    monkeypatch.setattr(s3, "_client_or_none", lambda: Fake())
    s3._known_present.clear()
    sid = auth_client.post("/api/ambient", json={"config": {"loop_seconds": 5, "total_seconds": 20,
                                                            "music_mood": "none", "fade_seconds": 1}}).json()["id"]
    auth_client.post(f"/api/ambient/{sid}/render")
    rid = auth_client.get(f"/api/ambient/{sid}").json()["latest_render"]["id"]
    local = auth_client.get(f"/api/renders/{rid}/download", follow_redirects=False)
    assert local.status_code == 200  # not mirrored → served locally
    Fake.present = True
    red = auth_client.get(f"/api/renders/{rid}/download", follow_redirects=False)
    assert red.status_code == 302
    assert Fake.params["ResponseContentDisposition"] == f'attachment; filename="avatar-video-{rid}.mp4"'


def _publish_job(status: str, updated_minutes_ago: int = 0):
    from app import models
    from app.db import SessionLocal

    db = SessionLocal()
    p = models.VideoProject(title="p")
    db.add(p)
    db.flush()
    r = models.Render(project_id=p.id, status="completed")
    db.add(r)
    db.flush()
    j = models.PublishJob(render_id=r.id, status=status, title="t",
                          updated_at=datetime.utcnow() - timedelta(minutes=updated_minutes_ago))
    db.add(j)
    db.commit()
    ids = (p.id, r.id, j.id)
    db.close()
    return ids


# 6 ── no double enqueue; stale uploads recoverable; worker claims once
def test_publish_retry_states(auth_client):
    _, _, queued = _publish_job("queued")
    assert auth_client.post(f"/api/publish-jobs/{queued}/retry").status_code == 409
    _, _, active = _publish_job("uploading", updated_minutes_ago=1)
    assert auth_client.post(f"/api/publish-jobs/{active}/retry").status_code == 409
    _, _, stale = _publish_job("uploading", updated_minutes_ago=30)
    assert auth_client.post(f"/api/publish-jobs/{stale}/retry").status_code == 202


def test_worker_skips_jobs_already_done(auth_client, monkeypatch):
    from app.services import youtube
    from app.workers import jobs

    called = []
    monkeypatch.setattr(youtube, "upload", lambda db, job, **kw: called.append(job.id))
    _, _, done = _publish_job("published")
    jobs.publish_youtube_job(done)
    _, _, active = _publish_job("uploading", updated_minutes_ago=1)
    jobs.publish_youtube_job(active)
    assert called == []
    _, _, q = _publish_job("queued")
    jobs.publish_youtube_job(q)
    jobs.publish_youtube_job(q)  # duplicate enqueue
    assert called == [q]


# 7 ── retention must not delete a render that's being uploaded
def test_retention_spares_renders_with_active_uploads(auth_client, monkeypatch):
    monkeypatch.setenv("RENDER_RETENTION_KEEP", "1")
    from app import config, models
    from app.db import SessionLocal

    config.get_settings.cache_clear()
    sid = auth_client.post("/api/ambient", json={"config": {"loop_seconds": 5, "total_seconds": 20,
                                                            "music_mood": "none", "fade_seconds": 1}}).json()["id"]
    auth_client.post(f"/api/ambient/{sid}/render")
    first = auth_client.get(f"/api/ambient/{sid}").json()["latest_render"]
    db = SessionLocal()
    db.add(models.PublishJob(render_id=first["id"], status="uploading", title="t"))
    db.commit()
    db.close()
    auth_client.post(f"/api/ambient/{sid}/render")
    rows = {r["id"]: r for r in auth_client.get(f"/api/projects/{sid}/renders").json()}
    assert rows[first["id"]]["status"] == "completed" and Path(first["final_video_path"]).exists()


# 8 ── re-renders don't re-count generation spend
def test_ambient_render_costs_nothing_generation_cost_recorded_once(auth_client):
    sid = auth_client.post("/api/ambient", json={"config": {"loop_seconds": 5, "total_seconds": 20,
                                                            "fade_seconds": 1}, "generate": True}).json()["id"]
    gen = auth_client.get(f"/api/projects/{sid}").json()["ambient_config_json"]["_generated"]
    assert gen["estimated_cost"] > 0 and gen["billed"] == 0.0  # mock mode
    for _ in range(2):
        auth_client.post(f"/api/ambient/{sid}/render")
    rs = [r for r in auth_client.get(f"/api/projects/{sid}/renders").json()]
    assert all(r["estimated_cost"] == 0 and r["actual_cost"] == 0 for r in rs)


# 9 ── aspect change requires a new loop clip
def test_aspect_change_invalidates_loop(auth_client):
    scene = auth_client.post("/api/ambient", json={"config": {"loop_seconds": 5, "total_seconds": 20},
                                                   "generate": True}).json()
    assert scene["loop_status"] == "completed"
    r = auth_client.patch(f"/api/ambient/{scene['id']}", json={"aspect_ratio": "9:16"}).json()
    assert r["loop_status"] == "pending" and r["loop_shot"]["clip_path"] == ""
    assert auth_client.post(f"/api/ambient/{scene['id']}/render").status_code == 409
    # Same aspect again is a no-op for the clip.
    auth_client.post(f"/api/ambient/{scene['id']}/generate-loop")
    r = auth_client.patch(f"/api/ambient/{scene['id']}", json={"aspect_ratio": "9:16"}).json()
    assert r["loop_status"] == "completed"


# 10 ── escaped quotes inside strings
def test_extract_json_handles_escaped_quotes():
    from app.providers.openrouter_chat import extract_json

    text = 'Here it is: {"hook": "say \\"{wow\\"", "n": 1} — enjoy'
    assert extract_json(text) == {"hook": 'say "{wow"', "n": 1}
    assert extract_json('{not json} then {"x": "a\\\\"}') == {"x": "a\\"}


# ── modernization ─────────────────────────────────────────────────────────


@pytest.mark.skipif(bool(__import__("os").environ.get("TEST_DATABASE_URL")), reason="SQLite-only")
def test_sqlite_runs_in_wal_mode_with_busy_timeout(auth_client):
    from sqlalchemy import text

    from app.db import SessionLocal

    db = SessionLocal()
    try:
        assert db.execute(text("PRAGMA journal_mode")).scalar().lower() == "wal"
        assert db.execute(text("PRAGMA busy_timeout")).scalar() == 30000
        assert db.execute(text("PRAGMA foreign_keys")).scalar() == 1
    finally:
        db.close()


def test_defaults_are_current_models():
    from app.config import Settings

    s = Settings(_env_file=None)
    assert s.openrouter_chat_model != "openai/gpt-4o-mini"
    assert s.elevenlabs_model_id not in ("eleven_turbo_v2_5", "eleven_turbo_v2")  # deprecated by ElevenLabs
    assert s.elevenlabs_music_model_id != "music_v1"  # removed 2027-01-31


@respx.mock
def test_planner_uses_structured_outputs_then_falls_back(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
    from app import config
    from app.providers.base import CastContext
    from app.providers.mock_chat import MockChatProvider
    from app.providers.openrouter_chat import OpenRouterChatProvider

    config.get_settings.cache_clear()
    plan = MockChatProvider().generate_storyboard(
        project={"title": "t", "mode": "reel_montage", "original_script": "Hi there.", "target_duration_seconds": 10},
        cast=CastContext()).model_dump(mode="json")
    route = respx.post(f"{OR}/chat/completions").mock(side_effect=[
        httpx.Response(400, json={"error": {"message": "json_schema not supported on this route"}}),
        httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(plan)}}]}),
    ])
    p = OpenRouterChatProvider(model="x/y", json_mode=True)
    p.structured = True
    out = p.generate_storyboard(project={"title": "t"}, cast=CastContext())
    assert out.cleaned_voice_script == plan["cleaned_voice_script"]
    first = json.loads(route.calls[0].request.content)["response_format"]
    second = json.loads(route.calls[1].request.content)["response_format"]
    assert first["type"] == "json_schema" and first["json_schema"]["schema"]["title"] == "StoryboardPlan"
    assert second == {"type": "json_object"}



def test_deleting_project_with_logs_and_uploads(auth_client):
    """Rows referencing a project/its renders (provider logs, publish jobs)
    used to be orphaned on SQLite and blocked the delete on Postgres."""
    from app import models
    from app.db import SessionLocal

    sid = auth_client.post("/api/ambient", json={"config": {"loop_seconds": 5, "total_seconds": 20,
                                                            "fade_seconds": 1}, "generate": True}).json()["id"]
    auth_client.post(f"/api/ambient/{sid}/render")
    rid = auth_client.get(f"/api/ambient/{sid}").json()["latest_render"]["id"]
    db = SessionLocal()
    db.add(models.PublishJob(render_id=rid, title="t", status="published"))
    db.commit()
    assert db.query(models.ProviderLog).filter_by(project_id=sid).count() > 0
    db.close()
    assert auth_client.delete(f"/api/projects/{sid}").status_code == 204
    db = SessionLocal()
    assert db.query(models.ProviderLog).filter_by(project_id=sid).count() == 0
    assert db.query(models.ProviderLog).filter(models.ProviderLog.project_id.is_(None)).count() > 0  # kept, detached
    assert db.query(models.PublishJob).filter_by(render_id=rid).count() == 0
    db.close()

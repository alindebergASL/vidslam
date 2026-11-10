"""Follow-ups from the first live deploy (PR #2's "known follow-ups")."""
from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

import httpx
import respx
from PIL import Image

from .conftest import make_png_bytes
from .test_live_deploy_fixes import ERROR_200, PLAN, PROJECT, _cast, _chat_provider

OR = "https://openrouter.ai/api/v1"


# ---------- session cookie ----------


def _login(client):
    return client.post("/api/auth/login", json={"password": "test-pw"})


def test_session_cookie_is_secure_behind_https(client, monkeypatch):
    from app import config

    r = _login(client)  # conftest: PUBLIC_BASE_URL=http://test.local
    assert "secure" not in r.headers["set-cookie"].lower()

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://studio.example.com")
    config.get_settings.cache_clear()
    cookie = _login(client).headers["set-cookie"].lower()
    assert "secure" in cookie and "httponly" in cookie and "samesite=lax" in cookie


def test_session_token_expires_server_side(client, monkeypatch):
    from app.api import auth

    assert _login(client).status_code == 200
    assert client.get("/api/projects").status_code == 200
    monkeypatch.setattr(auth, "SESSION_SECONDS", -1)  # every token is now past its age
    assert client.get("/api/projects").status_code == 401


# ---------- planner format rejections are remembered ----------


def _ok(gen):
    return httpx.Response(200, json={"id": gen, "model": "test/model", "usage": {"cost": 0.001},
                                     "choices": [{"message": {"content": json.dumps(PLAN)}}]})


def _formats(route):
    return [json.loads(c.request.content).get("response_format", {}).get("type") for c in route.calls]


@respx.mock
def test_rejected_json_schema_is_skipped_on_the_next_plan(client, monkeypatch):
    provider = _chat_provider(monkeypatch)
    route = respx.post(f"{OR}/chat/completions").mock(side_effect=[ERROR_200, _ok("a"), _ok("b")])
    provider.generate_storyboard(project=PROJECT, cast=_cast())
    provider.generate_storyboard(project=PROJECT, cast=_cast())
    # 1st plan: json_schema rejected → json_object. 2nd plan: straight to json_object.
    assert _formats(route) == ["json_schema", "json_object", "json_object"]


@respx.mock
def test_format_rejection_is_retried_after_a_week(client, monkeypatch):
    from app.providers import openrouter_chat
    from app.services import model_registry as reg

    provider = _chat_provider(monkeypatch)
    old = (datetime.now(timezone.utc) - openrouter_chat.REJECTION_TTL - timedelta(hours=1)).isoformat()
    reg.put_setting("chat.rejected_formats", {"test/model": {"json_schema": old}})
    route = respx.post(f"{OR}/chat/completions").mock(side_effect=[_ok("a")])
    provider.generate_storyboard(project=PROJECT, cast=_cast())
    assert _formats(route) == ["json_schema"]  # the model may support it by now


# ---------- preflight: mock models / missing voice in live mode ----------


def _live(monkeypatch, **env):
    from app import config

    base = {"MOCK_PROVIDERS": "false", "OPENROUTER_API_KEY": "sk-or", "OPENROUTER_CHAT_MODEL": "x/chat",
            "OPENROUTER_VIDEO_MODEL": "x/video", "OPENROUTER_IMAGE_MODEL": "x/image",
            "ELEVENLABS_API_KEY": "", "ELEVENLABS_DEFAULT_VOICE_ID": ""}
    for k, v in {**base, **env}.items():
        monkeypatch.setenv(k, v)
    config.get_settings.cache_clear()


def _project(auth_client) -> int:
    return auth_client.post("/api/projects", json={"title": "P", "original_script": "Hello there friends",
                                                   "cta_text": "Go"}).json()["id"]


def _check(auth_client, pid, cid):
    checks = auth_client.get(f"/api/projects/{pid}/preflight").json()["checks"]
    return next(c for c in checks if c["id"] == cid)


def test_preflight_blocks_mock_video_model_in_live_mode(auth_client, monkeypatch):
    pid = _project(auth_client)
    _live(monkeypatch)
    assert _check(auth_client, pid, "models_selected")["status"] == "ok"

    _live(monkeypatch, OPENROUTER_VIDEO_MODEL="")
    c = _check(auth_client, pid, "models_selected")
    assert c["status"] == "fail" and "video → mock/video-default" in c["detail"]
    assert "OPENROUTER_VIDEO_MODEL" in c["detail"]

    _live(monkeypatch, OPENROUTER_IMAGE_MODEL="")  # images alone: warn, don't block
    assert _check(auth_client, pid, "models_selected")["status"] == "warn"


def test_preflight_blocks_when_elevenlabs_has_no_voice(auth_client, monkeypatch):
    # A failed voiceover now stops the render, so a missing voice blocks.
    pid = _project(auth_client)
    _live(monkeypatch, ELEVENLABS_API_KEY="el")
    c = _check(auth_client, pid, "voice_selected")
    assert c["status"] == "fail" and "voiceover can't be made" in c["message"]
    _live(monkeypatch, ELEVENLABS_API_KEY="el", ELEVENLABS_DEFAULT_VOICE_ID="v1")
    assert _check(auth_client, pid, "voice_selected")["status"] == "ok"


# ---------- Studio: clip duration and real image metadata ----------


def _avatar(auth_client) -> int:
    return auth_client.post("/api/avatars", json={"name": "A"}).json()["id"]


def test_studio_clip_uses_requested_duration(auth_client, monkeypatch):
    from app.providers import mock_video

    seen = []
    real = mock_video.MockVideoProvider.submit

    def spy(self, **kw):
        seen.append(kw["duration_seconds"])
        return real(self, **kw)

    monkeypatch.setattr(mock_video.MockVideoProvider, "submit", spy)
    aid = _avatar(auth_client)
    r = auth_client.post("/api/studio/generate-clip", json={
        "owner_kind": "avatar", "owner_id": aid, "output_kind": "video_clip",
        "prompt": "a wave", "duration_seconds": 10})
    assert r.status_code == 202 and r.json()["duration_seconds"] == 10
    assert seen == [10.0]
    bad = auth_client.post("/api/studio/generate-clip", json={
        "owner_kind": "avatar", "owner_id": aid, "prompt": "x", "duration_seconds": 600})
    assert bad.status_code == 422


def test_studio_image_keeps_the_providers_format_and_size(auth_client, monkeypatch):
    from app.providers import mock_image

    buf = io.BytesIO()
    Image.new("RGB", (96, 64), (10, 120, 200)).save(buf, "JPEG")
    monkeypatch.setattr(mock_image.MockImageProvider, "generate", lambda self, **kw: buf.getvalue())
    aid = _avatar(auth_client)
    job = auth_client.post("/api/studio/generate-image", json={
        "owner_kind": "avatar", "owner_id": aid, "prompt": "portrait"}).json()
    jobs = {j["id"]: j for j in auth_client.get("/api/studio/jobs").json()}
    assert jobs[job["id"]]["result_path"].endswith(".jpg")
    asset = auth_client.post(f"/api/studio/jobs/{job['id']}/save").json()
    assert (asset["mime_type"], asset["width"], asset["height"]) == ("image/jpeg", 96, 64)
    from app import models
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        assert len(db.get(models.Asset, asset["id"]).checksum) == 64
    finally:
        db.close()


def test_image_info_falls_back_for_unreadable_bytes():
    from app.services.storage import image_info

    assert image_info(b"not an image") == ("png", "image/png", 0, 0)
    assert image_info(make_png_bytes((8, 5)))[1:] == ("image/png", 8, 5)


# ---------- existing SQLite databases get new columns ----------


def test_existing_sqlite_db_is_migrated_at_boot(tmp_path, monkeypatch):
    """A dev database built by create_all before 0002 (no alembic_version,
    no duration_seconds) must be upgraded, not left missing the column."""
    import sqlite3

    from app import config
    from app import db as dbmod

    path = tmp_path / "old.db"
    url = f"sqlite:///{path}"
    monkeypatch.setenv("DATABASE_URL", url)
    config.get_settings.cache_clear()
    dbmod.reset_engine(url)
    dbmod.init_db()  # fresh file: create_all + stamped at head
    con = sqlite3.connect(path)
    assert con.execute("select version_num from alembic_version").fetchone()[0] == dbmod._alembic_head()
    # Make it look like a pre-0002 create_all database with a Studio row.
    con.execute("drop table alembic_version")
    con.execute("alter table asset_generation_jobs drop column duration_seconds")
    con.execute("insert into asset_generation_jobs (owner_kind, owner_id, output_kind, prompt, negative_prompt,"
                " reference_asset_ids_json, provider, provider_model, provider_job_id, status, result_path,"
                " error, created_at, updated_at) values ('avatar',1,'video_clip','old','','[]','mock','m','',"
                "'completed','','',datetime('now'),datetime('now'))")
    con.commit()
    con.close()

    dbmod.init_db()  # existing file: stamped at the baseline, then upgraded
    con = sqlite3.connect(path)
    assert con.execute("select prompt, duration_seconds from asset_generation_jobs").fetchall() == [("old", 5.0)]
    assert con.execute("select version_num from alembic_version").fetchone()[0] == dbmod._alembic_head()
    con.close()
    dbmod.init_db()  # already at head: no-op

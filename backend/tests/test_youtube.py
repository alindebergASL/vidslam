"""YouTube connect + resumable upload against the documented contract (respx)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

TOKEN = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
UP = "https://www.googleapis.com/upload/youtube/v3"
SESSION = f"{UP}/videos?uploadType=resumable&upload_id=XYZ"


@pytest.fixture
def yt_env(monkeypatch):
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("FRONTEND_ORIGIN", "http://front.local")
    from app import config
    from app.services import youtube

    config.get_settings.cache_clear()
    youtube._access.clear()
    monkeypatch.setattr(youtube.time, "sleep", lambda s: None)
    return youtube


def _connect(client) -> None:
    url = client.get("/api/youtube/connect").json()["url"]
    state = parse_qs(urlparse(url).query)["state"][0]
    respx.post(TOKEN).mock(return_value=httpx.Response(200, json={
        "access_token": "at-1", "expires_in": 3599, "refresh_token": "rt-secret", "token_type": "Bearer"}))
    respx.get(f"{API}/channels").mock(return_value=httpx.Response(200, json={"items": [
        {"id": "UC123", "snippet": {"title": "Calm Scenes", "thumbnails": {"default": {"url": "https://x/t.jpg"}}}}]}))
    r = client.get(f"/api/youtube/callback?code=abc&state={state}", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "http://front.local/settings?youtube=connected"


def _render(client, size=700 * 1024) -> int:
    """A completed render with a fake MP4 of `size` bytes + a thumbnail."""
    from app import models
    from app.db import SessionLocal
    from app.services import storage

    pid = client.post("/api/projects", json={"title": "P", "original_script": "x"}).json()["id"]
    d = storage.render_subdir(pid)
    (d / "v.mp4").write_bytes(bytes(range(256)) * (size // 256))
    (d / "v.jpg").write_bytes(b"\xff\xd8\xff" + b"0" * 100)
    db = SessionLocal()
    r = models.Render(project_id=pid, status="completed", final_video_path=str(d / "v.mp4"),
                      thumbnail_path=str(d / "v.jpg"))
    db.add(r)
    db.commit()
    rid = r.id
    db.close()
    return rid


@respx.mock
def test_connect_flow_stores_encrypted_refresh_token(auth_client, yt_env):
    st = auth_client.get("/api/youtube/status").json()
    assert st == {**st, "configured": True, "connected": False}
    assert st["redirect_uri"] == "http://test.local/api/youtube/callback"
    url = auth_client.get("/api/youtube/connect").json()["url"]
    q = parse_qs(urlparse(url).query)
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"] and q["response_type"] == ["code"]
    assert "https://www.googleapis.com/auth/youtube.upload" in q["scope"][0]
    _connect(auth_client)
    st = auth_client.get("/api/youtube/status").json()
    assert st["connected"] and st["channel"]["title"] == "Calm Scenes"
    exch = parse_qs(respx.calls[-2].request.content.decode())
    assert exch["grant_type"] == ["authorization_code"] and exch["code"] == ["abc"]
    from app.services import model_registry

    stored = json.dumps(model_registry.get_setting("youtube.credentials"))
    assert "rt-secret" not in stored  # encrypted at rest


@respx.mock
def test_callback_rejects_bad_state(auth_client, yt_env):
    auth_client.get("/api/youtube/connect")
    r = auth_client.get("/api/youtube/callback?code=abc&state=forged", follow_redirects=False)
    assert "youtube=error" in r.headers["location"] and "state" in r.headers["location"]
    assert not auth_client.get("/api/youtube/status").json()["connected"]


@respx.mock
def test_chunked_upload_with_thumbnail_and_disclosure(auth_client, yt_env, monkeypatch):
    monkeypatch.setattr(yt_env, "CHUNK", 256 * 1024)
    _connect(auth_client)
    rid = _render(auth_client)
    total = (700 * 1024 // 256) * 256
    init = respx.post(f"{UP}/videos").mock(return_value=httpx.Response(200, headers={"Location": SESSION}))
    puts = []

    def put(request):
        rng = request.headers.get("Content-Range")
        puts.append(rng)
        start, end = (int(x) for x in rng.split(" ")[1].split("/")[0].split("-"))
        if end + 1 < total:
            return httpx.Response(308, headers={"Range": f"bytes=0-{end}"})
        return httpx.Response(201, json={"id": "vid42", "status": {"uploadStatus": "uploaded"}})

    respx.put(SESSION).mock(side_effect=put)
    thumb = respx.post(f"{UP}/thumbnails/set").mock(return_value=httpx.Response(200, json={"items": []}))
    r = auth_client.post(f"/api/renders/{rid}/publish/youtube", json={
        "title": "Ocean — 1 Hour", "description": "calm", "tags": ["ocean", "sleep"], "category_id": "10",
        "privacy": "unlisted"})
    assert r.status_code == 202, r.text
    job = auth_client.get(f"/api/renders/{rid}/publish-jobs").json()[0]
    assert job["status"] == "published", job["error"]
    assert job["external_url"] == "https://www.youtube.com/watch?v=vid42"
    assert job["bytes_sent"] == job["bytes_total"] == total
    assert puts == ["bytes 0-262143/%d" % total, "bytes 262144-524287/%d" % total, "bytes 524288-%d/%d" % (total - 1, total)]
    req = init.calls[0].request
    assert parse_qs(urlparse(str(req.url)).query) == {"uploadType": ["resumable"], "part": ["snippet,status"]}
    assert req.headers["X-Upload-Content-Length"] == str(total) and req.headers["Authorization"] == "Bearer at-1"
    body = json.loads(req.content)
    assert body["snippet"] == {"title": "Ocean — 1 Hour", "description": "calm", "tags": ["ocean", "sleep"], "categoryId": "10"}
    assert body["status"]["privacyStatus"] == "unlisted"
    assert body["status"]["containsSyntheticMedia"] is True and body["status"]["selfDeclaredMadeForKids"] is False
    assert parse_qs(urlparse(str(thumb.calls[0].request.url)).query)["videoId"] == ["vid42"]


@respx.mock
def test_scheduled_upload_is_private_with_publish_at(auth_client, yt_env):
    _connect(auth_client)
    rid = _render(auth_client, size=1024)
    init = respx.post(f"{UP}/videos").mock(return_value=httpx.Response(200, headers={"Location": SESSION}))
    respx.put(SESSION).mock(return_value=httpx.Response(201, json={"id": "v2"}))
    respx.post(f"{UP}/thumbnails/set").mock(return_value=httpx.Response(403, json={"error": "forbidden"}))
    when = (datetime.utcnow() + timedelta(days=1)).replace(microsecond=0)
    auth_client.post(f"/api/renders/{rid}/publish/youtube", json={
        "title": "Later", "privacy": "public", "publish_at": when.isoformat() + "Z"})
    job = auth_client.get(f"/api/renders/{rid}/publish-jobs").json()[0]
    assert job["status"] == "scheduled" and job["privacy"] == "private"
    assert "thumbnail wasn't set" in job["error"]  # non-fatal (unverified channel)
    st = json.loads(init.calls[0].request.content)["status"]
    assert st["privacyStatus"] == "private" and st["publishAt"] == when.isoformat() + "Z"


@respx.mock
def test_upload_resumes_after_server_error(auth_client, yt_env, monkeypatch):
    monkeypatch.setattr(yt_env, "CHUNK", 256 * 1024)
    _connect(auth_client)
    rid = _render(auth_client, size=512 * 1024)
    total = 512 * 1024
    respx.post(f"{UP}/videos").mock(return_value=httpx.Response(200, headers={"Location": SESSION}))
    seen = []

    def put(request):
        rng = request.headers["Content-Range"]
        seen.append(rng)
        if rng == f"bytes */{total}":  # status query after the 503
            return httpx.Response(308, headers={"Range": "bytes=0-262143"})
        if rng.startswith("bytes 0-"):
            return httpx.Response(308, headers={"Range": "bytes=0-262143"})
        if len([s for s in seen if s.startswith("bytes 262144-")]) == 1:
            return httpx.Response(503)
        return httpx.Response(201, json={"id": "v3"})

    respx.put(SESSION).mock(side_effect=put)
    respx.post(f"{UP}/thumbnails/set").mock(return_value=httpx.Response(200, json={}))
    auth_client.post(f"/api/renders/{rid}/publish/youtube", json={"title": "Resume"})
    job = auth_client.get(f"/api/renders/{rid}/publish-jobs").json()[0]
    assert job["status"] == "published", job["error"]
    assert seen == ["bytes 0-262143/%d" % total, "bytes 262144-524287/%d" % total,
                    f"bytes */{total}", "bytes 262144-524287/%d" % total]


def test_publish_validation(auth_client, yt_env):
    rid = _render(auth_client, size=1024)
    r = auth_client.post(f"/api/renders/{rid}/publish/youtube", json={"title": "x"})
    assert r.status_code == 409 and "connect" in r.json()["detail"]


@respx.mock
def test_publish_validation_when_connected(auth_client, yt_env):
    _connect(auth_client)
    rid = _render(auth_client, size=1024)
    bad = auth_client.post(f"/api/renders/{rid}/publish/youtube", json={"title": "a <b>"})
    assert bad.status_code == 422
    soon = auth_client.post(f"/api/renders/{rid}/publish/youtube", json={
        "title": "x", "publish_at": (datetime.utcnow() + timedelta(minutes=2)).isoformat() + "Z"})
    assert soon.status_code == 422 and "15 minutes" in soon.json()["detail"]
    assert auth_client.post("/api/renders/9999/publish/youtube", json={"title": "x"}).status_code == 409


@respx.mock
def test_failed_upload_marks_job_and_can_retry(auth_client, yt_env):
    _connect(auth_client)
    rid = _render(auth_client, size=1024)
    respx.post(f"{UP}/videos").mock(side_effect=[
        httpx.Response(403, json={"error": {"message": "quotaExceeded"}}),
        httpx.Response(200, headers={"Location": SESSION})])
    respx.put(SESSION).mock(return_value=httpx.Response(201, json={"id": "v9"}))
    respx.post(f"{UP}/thumbnails/set").mock(return_value=httpx.Response(200, json={}))
    auth_client.post(f"/api/renders/{rid}/publish/youtube", json={"title": "Q"})
    job = auth_client.get(f"/api/renders/{rid}/publish-jobs").json()[0]
    assert job["status"] == "failed" and "quotaExceeded" in job["error"]
    from app.services import alerts

    assert any(a["key"] == f"publish-failed:{job['id']}" for a in alerts.recent())
    again = auth_client.post(f"/api/publish-jobs/{job['id']}/retry")
    assert again.status_code == 202
    assert auth_client.get(f"/api/renders/{rid}/publish-jobs").json()[0]["status"] == "published"


@respx.mock
def test_disconnect_revokes_and_secret_rotation_forces_reconnect(auth_client, yt_env, monkeypatch):
    _connect(auth_client)
    revoke = respx.post("https://oauth2.googleapis.com/revoke").mock(return_value=httpx.Response(200))
    assert auth_client.post("/api/youtube/disconnect").json()["connected"] is False
    assert parse_qs(urlparse(str(revoke.calls[0].request.url)).query)["token"] == ["rt-secret"]
    # Reconnect, then rotate SESSION_SECRET: the sealed token no longer opens.
    _connect(auth_client)
    yt_env._access.clear()
    monkeypatch.setenv("SESSION_SECRET", "a-completely-different-secret-value-123456")
    from app import config

    config.get_settings.cache_clear()
    with pytest.raises(yt_env.YouTubeError, match="connect in Settings"):
        yt_env.access_token(httpx.Client())

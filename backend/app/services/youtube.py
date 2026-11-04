"""YouTube publishing via the YouTube Data API v3 (no Google SDK).

OAuth (developers.google.com/identity/protocols/oauth2/web-server):
  auth   https://accounts.google.com/o/oauth2/v2/auth
         response_type=code, access_type=offline, prompt=consent, state
  token  https://oauth2.googleapis.com/token
         grant_type=authorization_code | refresh_token
Upload (…/guides/using_resumable_upload_protocol, …/docs/videos/insert):
  POST https://www.googleapis.com/upload/youtube/v3/videos
       ?uploadType=resumable&part=snippet,status
       X-Upload-Content-Length / X-Upload-Content-Type, JSON video resource
  → 200 + Location: session URI
  PUT session URI in chunks (multiples of 256 KiB), Content-Range
  → 308 Resume Incomplete (Range: bytes=0-N) … → 201 video resource
  Resume: PUT, Content-Range: bytes */TOTAL → 308 + Range, continue at N+1
Thumbnail: POST https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId=
Scheduling: status.privacyStatus=private + status.publishAt (ISO 8601).
AI disclosure: status.containsSyntheticMedia.
Quota: one upload = 1 unit of a separate 100 uploads/day pool; thumbnail ≈ 50 units.
"""
from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from . import model_registry as store
from . import secret_box

log = logging.getLogger("avs.youtube")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD_API = "https://www.googleapis.com/upload/youtube/v3"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
CHUNK = 8 * 1024 * 1024  # 32 × 256 KiB
CREDS_KEY = "youtube.credentials"
STATE_KEY = "youtube.oauth_state"

_access: dict = {}  # in-process access-token cache {token, expires}


class YouTubeError(RuntimeError):
    pass


def configured() -> bool:
    s = get_settings()
    return bool(s.youtube_client_id and s.youtube_client_secret)


def redirect_uri() -> str:
    s = get_settings()
    return s.youtube_redirect_uri or f"{s.public_base_url.rstrip('/')}/api/youtube/callback"


def status() -> dict:
    creds = store.get_setting(CREDS_KEY) or {}
    return {
        "configured": configured(),
        "connected": bool(creds.get("refresh_token")),
        "channel": creds.get("channel"),
        "redirect_uri": redirect_uri(),
        "connected_at": creds.get("connected_at"),
    }


def auth_url() -> str:
    if not configured():
        raise YouTubeError("YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET not set")
    state = secrets.token_urlsafe(24)
    store.put_setting(STATE_KEY, {"state": state, "exp": time.time() + 900})
    q = {
        "client_id": get_settings().youtube_client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",  # always return a refresh token
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(q)}"


def handle_callback(code: str, state: str, *, client: Optional[httpx.Client] = None) -> dict:
    saved = store.get_setting(STATE_KEY) or {}
    store.put_setting(STATE_KEY, None)
    if not state or state != saved.get("state") or time.time() > saved.get("exp", 0):
        raise YouTubeError("invalid or expired OAuth state — start Connect again")
    s = get_settings()
    own = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        r = client.post(TOKEN_URL, data={
            "code": code, "client_id": s.youtube_client_id, "client_secret": s.youtube_client_secret,
            "redirect_uri": redirect_uri(), "grant_type": "authorization_code",
        })
        if r.status_code >= 400:
            raise YouTubeError(f"token exchange failed: {r.text[:300]}")
        tok = r.json()
        refresh = tok.get("refresh_token")
        if not refresh:
            raise YouTubeError("Google returned no refresh token — remove the app's access in your "
                               "Google account and connect again")
        _access.update(token=tok["access_token"], expires=time.time() + int(tok.get("expires_in", 3600)) - 60)
        channel = None
        ch = client.get(f"{API}/channels", params={"part": "snippet", "mine": "true"},
                        headers={"Authorization": f"Bearer {tok['access_token']}"})
        if ch.status_code < 400 and ch.json().get("items"):
            item = ch.json()["items"][0]
            channel = {"id": item["id"], "title": item["snippet"]["title"],
                       "thumbnail": ((item["snippet"].get("thumbnails") or {}).get("default") or {}).get("url")}
    finally:
        if own:
            client.close()
    store.put_setting(CREDS_KEY, {
        "refresh_token": secret_box.seal(refresh),
        "channel": channel,
        "connected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    return status()


def disconnect() -> None:
    creds = store.get_setting(CREDS_KEY) or {}
    token = secret_box.open_(creds.get("refresh_token", "")) if creds else None
    if token:
        try:
            httpx.post(REVOKE_URL, params={"token": token}, timeout=10.0)
        except httpx.HTTPError:
            pass
    store.put_setting(CREDS_KEY, None)
    _access.clear()


def access_token(client: httpx.Client) -> str:
    if _access.get("token") and time.time() < _access.get("expires", 0):
        return _access["token"]
    creds = store.get_setting(CREDS_KEY) or {}
    refresh = secret_box.open_(creds.get("refresh_token", "")) if creds else None
    if not refresh:
        raise YouTubeError("YouTube isn't connected (or SESSION_SECRET changed) — connect in Settings")
    s = get_settings()
    r = client.post(TOKEN_URL, data={
        "client_id": s.youtube_client_id, "client_secret": s.youtube_client_secret,
        "refresh_token": refresh, "grant_type": "refresh_token",
    })
    if r.status_code >= 400:
        raise YouTubeError(f"token refresh failed ({r.status_code}): {r.text[:200]} — reconnect YouTube")
    tok = r.json()
    _access.update(token=tok["access_token"], expires=time.time() + int(tok.get("expires_in", 3600)) - 60)
    return _access["token"]


def video_resource(job: models.PublishJob) -> dict:
    status: dict = {
        "privacyStatus": job.privacy,
        "selfDeclaredMadeForKids": bool(job.made_for_kids),
        "containsSyntheticMedia": bool(job.contains_synthetic_media),
        "embeddable": True,
    }
    if job.publish_at:
        # Scheduling requires a private video; YouTube flips it public then.
        status["privacyStatus"] = "private"
        status["publishAt"] = job.publish_at.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "snippet": {
            "title": job.title[:100],
            "description": job.description,
            "tags": list(job.tags_json or []),
            "categoryId": job.category_id or "22",
        },
        "status": status,
    }


def _committed_offset(client: httpx.Client, url: str, total: int, token: str) -> int | dict:
    """Ask the session how much it has. Returns next byte offset, or the
    final video resource if the upload already completed."""
    r = client.put(url, headers={"Authorization": f"Bearer {token}", "Content-Range": f"bytes */{total}",
                                 "Content-Length": "0"})
    if r.status_code in (200, 201):
        return r.json()
    if r.status_code == 308:
        rng = r.headers.get("Range")
        return int(rng.split("-")[1]) + 1 if rng else 0
    if r.status_code == 404:
        return -1  # session expired → start a new one
    raise YouTubeError(f"upload status check failed ({r.status_code}): {r.text[:200]}")


def upload(db: Session, job: models.PublishJob, *, client: Optional[httpx.Client] = None,
           backoff: float = 2.0) -> models.PublishJob:
    render = db.get(models.Render, job.render_id)
    if render is None or not render.final_video_path or not Path(render.final_video_path).exists():
        raise YouTubeError("render file not found")
    path = Path(render.final_video_path)
    total = path.stat().st_size
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(60.0, write=300.0))
    try:
        token = access_token(client)
        job.status, job.bytes_total, job.error = "uploading", total, ""
        db.commit()

        offset = 0
        if job.upload_url:
            got = _committed_offset(client, job.upload_url, total, token)
            if isinstance(got, dict):
                return _finish(db, job, got, client, token, render)
            if got < 0:
                job.upload_url = ""
            else:
                offset = got
        if not job.upload_url:
            init = client.post(
                f"{UPLOAD_API}/videos",
                params={"uploadType": "resumable", "part": "snippet,status"},
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json; charset=UTF-8",
                         "X-Upload-Content-Length": str(total), "X-Upload-Content-Type": "video/mp4"},
                json=video_resource(job),
            )
            if init.status_code >= 400 or not init.headers.get("Location"):
                raise YouTubeError(f"upload init failed ({init.status_code}): {init.text[:300]}")
            job.upload_url = init.headers["Location"]
            db.commit()

        retries = 0
        with path.open("rb") as fh:
            while True:
                fh.seek(offset)
                chunk = fh.read(CHUNK)
                end = offset + len(chunk) - 1
                try:
                    r = client.put(job.upload_url, content=chunk, headers={
                        "Authorization": f"Bearer {token}", "Content-Type": "video/mp4",
                        "Content-Length": str(len(chunk)), "Content-Range": f"bytes {offset}-{end}/{total}",
                    })
                except httpx.TransportError:
                    r = None
                if r is not None and r.status_code in (200, 201):
                    return _finish(db, job, r.json(), client, token, render)
                if r is not None and r.status_code == 308:
                    rng = r.headers.get("Range")
                    offset = int(rng.split("-")[1]) + 1 if rng else 0
                    job.bytes_sent = offset
                    db.commit()
                    retries = 0
                    continue
                if r is not None and r.status_code == 401:
                    _access.clear()
                    token = access_token(client)
                    continue
                if r is None or r.status_code in (500, 502, 503, 504):
                    retries += 1
                    if retries > 6:
                        raise YouTubeError("upload kept failing; it will resume from the last byte on retry")
                    time.sleep(backoff * (2 ** (retries - 1)))
                    got = _committed_offset(client, job.upload_url, total, token)
                    if isinstance(got, dict):
                        return _finish(db, job, got, client, token, render)
                    offset = max(got, 0)
                    continue
                raise YouTubeError(f"upload failed ({r.status_code}): {r.text[:300]}")
    finally:
        if own:
            client.close()


def _finish(db: Session, job: models.PublishJob, video: dict, client: httpx.Client, token: str,
            render: models.Render) -> models.PublishJob:
    vid = video.get("id") or ""
    job.external_id = vid
    job.external_url = f"https://www.youtube.com/watch?v={vid}" if vid else ""
    job.bytes_sent = job.bytes_total
    job.upload_url = ""
    if job.set_thumbnail and render.thumbnail_path and Path(render.thumbnail_path).exists() and vid:
        t = client.post(f"{UPLOAD_API}/thumbnails/set", params={"videoId": vid},
                        headers={"Authorization": f"Bearer {token}", "Content-Type": "image/jpeg"},
                        content=Path(render.thumbnail_path).read_bytes())
        if t.status_code >= 400:
            # Custom thumbnails need a verified channel; don't fail the upload.
            job.error = f"uploaded, but thumbnail wasn't set ({t.status_code}): {t.text[:200]}"
    # Uploaded; YouTube processes it from here. "scheduled" goes live at publish_at.
    job.status = "scheduled" if job.publish_at else "published"
    db.commit()
    return job


def validate_publish_at(when: Optional[datetime]) -> Optional[datetime]:
    if when is None:
        return None
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc).replace(tzinfo=None)
    if when < datetime.utcnow() + timedelta(minutes=15):
        raise YouTubeError("schedule time must be at least 15 minutes in the future")
    return when

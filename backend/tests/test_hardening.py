"""Production hardening: per-render files, retention, S3 delivery, queue
mode + readiness, ops alerts."""
from __future__ import annotations

from pathlib import Path

import httpx
import respx

from .conftest import make_png_bytes

SHORT = {"scene_preset": "ocean", "loop_seconds": 5, "total_seconds": 20,
         "crossfade_seconds": 1.0, "fade_seconds": 1.0, "music_mood": "none"}


def _scene(client) -> int:
    r = client.post("/api/ambient", json={"config": SHORT, "generate": True})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _render(client, sid) -> dict:
    assert client.post(f"/api/ambient/{sid}/render").status_code == 202
    return client.get(f"/api/ambient/{sid}").json()["latest_render"]


def test_each_render_keeps_its_own_files(auth_client):
    sid = _scene(auth_client)
    r1 = _render(auth_client, sid)
    r2 = _render(auth_client, sid)
    assert r1["final_video_path"] != r2["final_video_path"]
    assert r1["final_video_path"].endswith(f"render_{r1['id']}.mp4")
    assert r2["thumbnail_path"].endswith(f"render_{r2['id']}.jpg")
    assert Path(r1["final_video_path"]).exists() and Path(r2["final_video_path"]).exists()
    # Downloads of the two versions serve different files.
    a = auth_client.get(f"/api/renders/{r1['id']}/download")
    assert a.status_code == 200 and a.headers["content-disposition"].endswith(f'{r1["id"]}.mp4"')
    # Compose scratch is cleaned; the loop clip survives for re-renders.
    out = Path(r2["final_video_path"]).parent
    assert not list(out.glob("ambient_head.mp4")) and not list(out.glob("final.mp4"))
    assert list(out.glob("shot_00_*.mp4"))


def test_storyboard_render_versions_are_distinct(auth_client):
    aid = auth_client.post("/api/avatars", json={"name": "X"}).json()["id"]
    auth_client.post(f"/api/avatars/{aid}/assets", files={"file": ("h.png", make_png_bytes(), "image/png")},
                     data={"asset_type": "hero", "rights_confirmed": "true"})
    pid = auth_client.post("/api/projects", json={
        "title": "T", "original_script": "one two three four five six", "target_duration_seconds": 10,
        "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}]}).json()["id"]
    auth_client.post(f"/api/projects/{pid}/generate-plan")
    auth_client.post(f"/api/projects/{pid}/generate-video?force=true")
    auth_client.post(f"/api/projects/{pid}/recompose")
    rs = [r for r in auth_client.get(f"/api/projects/{pid}/renders").json() if r["status"] == "completed"]
    assert len(rs) == 2
    assert len({r["final_video_path"] for r in rs}) == 2
    assert all(Path(r["final_video_path"]).exists() for r in rs)


def test_retention_expires_old_renders(auth_client, monkeypatch):
    monkeypatch.setenv("RENDER_RETENTION_KEEP", "1")
    from app import config

    config.get_settings.cache_clear()
    sid = _scene(auth_client)
    r1 = _render(auth_client, sid)
    r2 = _render(auth_client, sid)
    rows = {r["id"]: r for r in auth_client.get(f"/api/projects/{sid}/renders").json()}
    assert rows[r1["id"]]["status"] == "expired"
    assert "retention" in rows[r1["id"]]["render_log"]
    assert not Path(r1["final_video_path"]).exists()
    assert rows[r2["id"]]["status"] == "completed" and Path(r2["final_video_path"]).exists()
    assert auth_client.get(f"/api/renders/{r1['id']}/download").status_code == 404


def test_downloads_redirect_to_s3_when_configured(auth_client, monkeypatch):
    sid = _scene(auth_client)
    r = _render(auth_client, sid)
    from app.services import render_files

    monkeypatch.setattr(render_files, "delivery_url",
                        lambda path, ctype, download_name=None: f"https://bucket.s3.example/{Path(path).name}?X-Amz-Signature=x")
    d = auth_client.get(f"/api/renders/{r['id']}/download", follow_redirects=False)
    assert d.status_code == 302 and d.headers["location"].startswith("https://bucket.s3.example/render_")
    pub = auth_client.get(f"/api/public-renders/{r['share_token']}", follow_redirects=False)
    assert pub.status_code == 302
    th = auth_client.get(f"/api/public-renders/{r['share_token']}/thumbnail", follow_redirects=False)
    assert th.status_code == 302 and th.headers["location"].endswith(".jpg?X-Amz-Signature=x")


def test_renders_mirror_to_s3(auth_client, monkeypatch):
    from app.services import s3

    pushed = []
    monkeypatch.setattr(s3, "mirror_to_s3", lambda p, content_type: pushed.append((Path(p).name, content_type)) or True)
    sid = _scene(auth_client)
    r = _render(auth_client, sid)
    assert (f"render_{r['id']}.mp4", "video/mp4") in pushed
    assert (f"render_{r['id']}.jpg", "image/jpeg") in pushed


# ---------- queue ----------


def test_required_queue_refuses_inline_and_reports_not_ready(auth_client, monkeypatch):
    monkeypatch.setenv("QUEUE_MODE", "required")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")  # nothing listens here
    from app import config
    from app.workers import queue

    config.get_settings.cache_clear()
    monkeypatch.setattr(queue, "_queue_singleton", None)
    sid = auth_client.post("/api/ambient", json={"config": SHORT, "generate": False}).json()["id"]
    r = auth_client.post(f"/api/ambient/{sid}/generate-loop")
    assert r.status_code == 503 and "QUEUE_MODE=required" in r.json()["detail"]
    ready = auth_client.get("/readyz")
    assert ready.status_code == 503
    assert ready.json()["checks"]["queue"] == {"mode": "required", "redis": False, "workers": 0, "ok": False}


def test_auto_queue_is_ready_without_redis(auth_client):
    body = auth_client.get("/readyz").json()
    assert body["checks"]["queue"]["mode"] == "auto" and body["checks"]["queue"]["ok"] is True


# ---------- alerts ----------


def _capture(monkeypatch):
    from app.services import alerts

    sent = []
    monkeypatch.setattr(alerts, "_post", lambda url, text: sent.append((url, text)))
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example/abc")
    from app import config

    config.get_settings.cache_clear()
    return sent


def _wait(pred, n=50):
    import time

    for _ in range(n):
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_render_failure_alerts_webhook(auth_client, monkeypatch):
    sent = _capture(monkeypatch)
    from app.services import ambient

    def boom(_inputs):
        raise RuntimeError("ffmpeg exploded")

    monkeypatch.setattr(ambient, "compose_ambient", boom)
    sid = _scene(auth_client)
    auth_client.post(f"/api/ambient/{sid}/render")
    assert _wait(lambda: sent), "no webhook post"
    url, text = sent[0]
    assert url == "https://hooks.example/abc"
    assert "ambient render failed" in text and "ffmpeg exploded" in text


def test_provider_error_burst_alerts_once(auth_client, monkeypatch):
    sent = _capture(monkeypatch)
    from app.services import alerts

    for _ in range(4):
        alerts.provider_error("openrouter", "video.submit", "503")
    assert not _wait(lambda: sent, n=5)
    for _ in range(3):
        alerts.provider_error("openrouter", "video.submit", "503")
    assert _wait(lambda: sent)
    assert len([t for _, t in sent if "openrouter failing" in t]) == 1  # deduped


@respx.mock
def test_alert_when_active_model_disappears(auth_client, monkeypatch):
    from app.services import alerts

    OR = "https://openrouter.ai/api/v1"
    vids = {"data": [{"id": "alibaba/wan-3.0", "supported_durations": [5, 10]},
                     {"id": "x/other", "supported_durations": [5]}]}
    respx.get(f"{OR}/models").mock(return_value=httpx.Response(200, json={"data": []}))
    respx.get(f"{OR}/images/models").mock(return_value=httpx.Response(200, json={"data": []}))
    respx.get(f"{OR}/videos/models").mock(return_value=httpx.Response(200, json=vids))
    auth_client.post("/api/models/refresh")
    auth_client.put("/api/models/active", json={"kind": "video", "model": "alibaba/wan-3.0"})
    respx.get(f"{OR}/videos/models").mock(return_value=httpx.Response(200, json={"data": vids["data"][1:]}))
    auth_client.post("/api/models/refresh")
    hits = [a for a in alerts.recent() if a["key"] == "model-gone:video:alibaba/wan-3.0"]
    assert hits and hits[0]["severity"] == "warning"

from __future__ import annotations

import subprocess
from pathlib import Path

import respx
from httpx import Response

from .conftest import make_png_bytes


def _hero(client, avatar_id: int) -> None:
    client.post(
        f"/api/avatars/{avatar_id}/assets",
        files={"file": ("h.png", make_png_bytes(), "image/png")},
        data={"asset_type": "hero", "rights_confirmed": "true"},
    )


def _project(auth_client) -> int:
    aid = auth_client.post("/api/avatars", json={"name": "X"}).json()["id"]
    _hero(auth_client, aid)
    return auth_client.post(
        "/api/projects",
        json={
            "title": "T",
            "original_script": "one two three four five",
            "cta_text": "Go",
            "target_duration_seconds": 10,
            "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
        },
    ).json()["id"]


def test_mock_music_generation_attaches_to_project(auth_client):
    pid = _project(auth_client)
    r = auth_client.post(
        f"/api/projects/{pid}/music/generate",
        json={"prompt": "warm cinematic lo-fi", "duration_seconds": 6},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["music_upload_path"].endswith(("music.mp3", "music.m4a"))
    path = Path(body["music_upload_path"])
    assert path.exists()
    # Has audible duration close to what we asked for.
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert 5.0 <= float(out) <= 7.5, f"unexpected duration {out}"


def test_mock_music_default_duration_uses_target_duration(auth_client):
    pid = _project(auth_client)
    # No explicit duration → falls back to target_duration_seconds (10).
    r = auth_client.post(
        f"/api/projects/{pid}/music/generate",
        json={"prompt": "minimal piano"},
    )
    assert r.status_code == 200
    path = Path(r.json()["music_upload_path"])
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert 9.0 <= float(out) <= 11.5


def test_music_provider_status_reports_mock(auth_client):
    r = auth_client.get("/api/providers/status")
    assert r.json()["music"] == "mock"


def test_empty_prompt_rejected(auth_client):
    pid = _project(auth_client)
    r = auth_client.post(
        f"/api/projects/{pid}/music/generate",
        json={"prompt": "  ", "duration_seconds": 5},
    )
    assert r.status_code == 422


def test_generated_music_overwrites_prior_track(auth_client):
    pid = _project(auth_client)
    # First, upload a music file the old way.
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
         "-c:a", "libmp3lame", "/tmp/avs_pre.mp3"],
        capture_output=True, check=True,
    )
    with open("/tmp/avs_pre.mp3", "rb") as f:
        auth_client.post(
            f"/api/projects/{pid}/music",
            files={"file": ("m.mp3", f.read(), "audio/mpeg")},
        )
    before = Path(auth_client.get(f"/api/projects/{pid}").json()["music_upload_path"])
    assert before.exists()
    # Now generate — should replace the prior path.
    r = auth_client.post(
        f"/api/projects/{pid}/music/generate",
        json={"prompt": "warm pad", "duration_seconds": 6},
    )
    after = Path(r.json()["music_upload_path"])
    assert after.exists()
    # The previous "music.mp3" upload and the new "music.mp3" can land at the
    # same path; what we really need is that exactly one music.* file exists.
    project_dir = after.parent
    music_files = list(project_dir.glob("music.*"))
    assert len(music_files) == 1


@respx.mock
def test_elevenlabs_music_adapter_parses_response(monkeypatch, tmp_path):
    monkeypatch.setenv("MOCK_PROVIDERS", "false")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "fake")
    from app import config as _c

    _c.get_settings.cache_clear()

    fake_bytes = b"ID3" + b"\x00" * 1024
    respx.post("https://api.elevenlabs.io/v1/music").mock(
        return_value=Response(200, content=fake_bytes)
    )

    from app.providers.elevenlabs_music import ElevenLabsMusicProvider

    out = ElevenLabsMusicProvider().generate(prompt="warm pad", duration_seconds=8.0)
    assert out == fake_bytes

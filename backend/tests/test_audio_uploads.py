from __future__ import annotations

import io
import subprocess
from pathlib import Path

from .conftest import make_png_bytes


def _hero(client, avatar_id: int) -> None:
    client.post(
        f"/api/avatars/{avatar_id}/assets",
        files={"file": ("h.png", make_png_bytes(), "image/png")},
        data={"asset_type": "hero", "rights_confirmed": "true"},
    )


def _make_audio_bytes(seconds: float = 5.0, kind: str = "mp3") -> bytes:
    """Synthesize a real audio file using ffmpeg so the upload validator + ffmpeg mux see legit data."""
    out = Path("/tmp") / f"avs_test_{kind}.{kind}"
    codec = "libmp3lame" if kind == "mp3" else "aac"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-c:a", codec,
            str(out),
        ],
        capture_output=True, check=True,
    )
    return out.read_bytes()


def _new_project(auth_client) -> int:
    aid = auth_client.post("/api/avatars", json={"name": "N"}).json()["id"]
    _hero(auth_client, aid)
    return auth_client.post(
        "/api/projects",
        json={
            "title": "T",
            "original_script": "one two three four five six seven",
            "cta_text": "Go",
            "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
        },
    ).json()["id"]


def test_voiceover_upload_replaces_tts(auth_client):
    pid = _new_project(auth_client)
    audio = _make_audio_bytes(kind="mp3")
    r = auth_client.post(
        f"/api/projects/{pid}/voiceover",
        files={"file": ("v.mp3", audio, "audio/mpeg")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["voiceover_source"] == "upload"
    assert body["voiceover_upload_path"].endswith("voiceover.mp3")
    # Streaming endpoint returns the same bytes.
    s = auth_client.get(f"/api/projects/{pid}/voiceover")
    assert s.status_code == 200 and len(s.content) == len(audio)


def test_voiceover_delete_reverts_to_tts(auth_client):
    pid = _new_project(auth_client)
    auth_client.post(
        f"/api/projects/{pid}/voiceover",
        files={"file": ("v.mp3", _make_audio_bytes(kind="mp3"), "audio/mpeg")},
    )
    r = auth_client.delete(f"/api/projects/{pid}/voiceover")
    assert r.status_code == 200
    body = r.json()
    assert body["voiceover_source"] == "tts"
    assert body["voiceover_upload_path"] == ""


def test_silent_voiceover_renders_video_without_audio_track(auth_client, tmp_path):
    pid = _new_project(auth_client)
    auth_client.patch(f"/api/projects/{pid}", json={"voiceover_source": "silent"})
    auth_client.post(f"/api/projects/{pid}/generate-plan")
    r = auth_client.post(f"/api/projects/{pid}/generate-video?force=true")
    assert r.status_code == 202
    st = auth_client.get(f"/api/projects/{pid}/status").json()
    assert st["project_status"] == "completed"
    final = st["latest_render"]["final_video_path"]
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=codec_type", "-of", "csv=p=0", final],
        capture_output=True, text=True, check=True,
    )
    assert probe.stdout.strip() == "", f"expected no audio track, got {probe.stdout!r}"


def test_music_uploaded_appears_in_final_audio_mix(auth_client):
    pid = _new_project(auth_client)
    auth_client.post(
        f"/api/projects/{pid}/music",
        files={"file": ("m.mp3", _make_audio_bytes(seconds=8, kind="mp3"), "audio/mpeg")},
    )
    auth_client.post(f"/api/projects/{pid}/generate-plan")
    auth_client.post(f"/api/projects/{pid}/generate-video?force=true")
    st = auth_client.get(f"/api/projects/{pid}/status").json()
    assert st["project_status"] == "completed"
    final = st["latest_render"]["final_video_path"]
    # The final must have an audio stream (music) even though mock TTS is silent.
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=codec_name", "-of", "csv=p=0", final],
        capture_output=True, text=True, check=True,
    )
    assert probe.stdout.strip() == "aac"


def test_music_delete_removes_file(auth_client):
    pid = _new_project(auth_client)
    r = auth_client.post(
        f"/api/projects/{pid}/music",
        files={"file": ("m.mp3", _make_audio_bytes(kind="mp3"), "audio/mpeg")},
    )
    p = Path(r.json()["music_upload_path"])
    assert p.exists()
    r2 = auth_client.delete(f"/api/projects/{pid}/music")
    assert r2.json()["music_upload_path"] == ""
    assert not p.exists()


def test_upload_rejects_unknown_mime(auth_client):
    pid = _new_project(auth_client)
    r = auth_client.post(
        f"/api/projects/{pid}/voiceover",
        files={"file": ("v.ogg", b"fake", "audio/ogg")},
    )
    assert r.status_code == 400

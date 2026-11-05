"""Ambient: Look (style + strength), time of day / weather, nature-sound bed."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import respx

from .test_ambient import SHORT, _duration, _has_audio


def _cfg(**kw):
    from app.schemas.ambient import AmbientConfig

    return AmbientConfig(**{**SHORT, **kw})


def _mean_volume_db(path: Path) -> float:
    out = subprocess.run(["ffmpeg", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    line = next(ln for ln in out.splitlines() if "mean_volume" in ln)
    return float(line.split("mean_volume:")[1].split("dB")[0])


# ---------- prompts ----------


def test_default_look_is_photoreal_and_unchanged_in_meaning():
    from app.services.ambient import resolve_scene_prompt

    p = resolve_scene_prompt(_cfg())
    assert "photorealistic" in p and "Locked-off static camera" in p


def test_look_strength_time_and_weather_shape_the_prompt():
    from app.services.ambient import resolve_scene_prompt, style_phrase

    subtle = style_phrase(_cfg(look="watercolor", look_strength=0.1))
    mid = style_phrase(_cfg(look="watercolor", look_strength=0.5))
    strong = style_phrase(_cfg(look="watercolor", look_strength=0.9))
    assert "mostly realistic" in subtle and "watercolor" in subtle
    assert mid.startswith("in a watercolor style")
    assert "fully stylized" in strong
    assert "photorealistic" not in strong
    # Strength is meaningless for photoreal.
    assert style_phrase(_cfg(look_strength=0.1)) == style_phrase(_cfg(look_strength=0.9))

    p = resolve_scene_prompt(_cfg(look="anime", time_of_day="night", weather="snow"))
    assert "at night" in p and "snow falling" in p and "anime" in p
    drift = resolve_scene_prompt(_cfg(source="image_drift", look="painterly", time_of_day="golden_hour"))
    assert "golden hour" in drift and "oil painting" in drift and "photorealistic" not in drift


def test_changing_look_or_weather_invalidates_the_loop(auth_client):
    sid = auth_client.post("/api/ambient", json={"config": SHORT, "generate": True}).json()["id"]
    assert auth_client.get(f"/api/ambient/{sid}").json()["loop_status"] == "completed"
    r = auth_client.patch(f"/api/ambient/{sid}", json={"config": {**SHORT, "look": "claymation"}})
    assert r.status_code == 200, r.text
    assert r.json()["loop_status"] == "pending"  # needs a new clip in the new style

    auth_client.post(f"/api/ambient/{sid}/generate-loop")
    r = auth_client.patch(f"/api/ambient/{sid}", json={"config": {**SHORT, "look": "claymation",
                                                                    "sound_volume": 0.3}})
    assert r.json()["loop_status"] == "completed"  # a mixing knob keeps the clip


# ---------- sound ----------


def test_sound_prompt_resolution():
    from app.services.ambient import resolve_sound_prompt

    assert resolve_sound_prompt(_cfg()) == ""  # off by default
    assert "waterfall" in resolve_sound_prompt(_cfg(sound="auto"))  # follows the scene
    assert "fireplace" in resolve_sound_prompt(_cfg(sound="auto", scene_preset="fireplace"))
    assert "thunder" in resolve_sound_prompt(_cfg(sound="auto", weather="heavy_rain"))  # weather wins
    assert "ocean" in resolve_sound_prompt(_cfg(sound="ocean"))
    assert resolve_sound_prompt(_cfg(sound="ocean", sound_prompt="owls hooting")) == "owls hooting"


def test_sound_bed_is_generated_mixed_and_previewable(auth_client):
    # Music + nature sound together.
    cfg = {**SHORT, "sound": "auto", "sound_volume": 0.8}
    scene = auth_client.post("/api/ambient", json={"config": cfg, "generate": True}).json()
    sid = scene["id"]
    assert scene["music_ready"] and scene["sound_ready"], scene
    assert auth_client.get(f"/api/ambient/{sid}/sound").headers["content-type"] == "audio/mpeg"
    auth_client.post(f"/api/ambient/{sid}/render")
    rend = auth_client.get(f"/api/ambient/{sid}").json()["latest_render"]
    assert rend["status"] == "completed", rend
    final = Path(rend["final_video_path"])
    assert _has_audio(final) and 19.0 <= _duration(final) <= 21.0
    with_sound = _mean_volume_db(final)

    # Sound alone, no music: still has an audio track.
    cfg2 = {**SHORT, "music_mood": "none", "sound": "rain"}
    s2 = auth_client.post("/api/ambient", json={"config": cfg2, "generate": True}).json()
    assert not s2["music_ready"] and s2["sound_ready"]
    auth_client.post(f"/api/ambient/{s2['id']}/render")
    final2 = Path(auth_client.get(f"/api/ambient/{s2['id']}").json()["latest_render"]["final_video_path"])
    assert _has_audio(final2)

    # Music alone is quieter than music + sound (the sound bed really is in the mix).
    s3 = auth_client.post("/api/ambient", json={"config": SHORT, "generate": True}).json()
    assert not s3["sound_ready"]
    auth_client.post(f"/api/ambient/{s3['id']}/render")
    final3 = Path(auth_client.get(f"/api/ambient/{s3['id']}").json()["latest_render"]["final_video_path"])
    assert _mean_volume_db(final3) < with_sound


def test_sound_endpoints(auth_client):
    sid = auth_client.post("/api/ambient", json={"config": SHORT, "generate": True}).json()["id"]
    assert auth_client.get(f"/api/ambient/{sid}/sound").status_code == 404
    assert auth_client.post(f"/api/ambient/{sid}/sound").status_code == 409  # sound is off
    auth_client.patch(f"/api/ambient/{sid}", json={"config": {**SHORT, "sound": "stream"}})
    r = auth_client.post(f"/api/ambient/{sid}/sound")
    assert r.status_code == 200 and r.json()["sound_ready"] is True


def test_presets_and_estimate_include_new_options(auth_client):
    p = auth_client.get("/api/ambient/presets").json()
    assert {x["id"] for x in p["looks"]} >= {"photoreal", "watercolor", "anime"}
    assert {x["id"] for x in p["sounds"]} >= {"rain", "fireplace", "ocean"}
    assert "night" in {x["id"] for x in p["times_of_day"]} and "fog" in {x["id"] for x in p["weathers"]}
    est = auth_client.post("/api/ambient/estimate", json={"config": {**SHORT, "sound": "rain"}}).json()
    assert any("Nature sound" in line["item"] for line in est["lines"])
    est0 = auth_client.post("/api/ambient/estimate", json={"config": SHORT}).json()
    assert not any("Nature sound" in line["item"] for line in est0["lines"])


@respx.mock
def test_elevenlabs_sound_request_shape(client, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el")
    from app import config
    from app.providers.sound import ElevenLabsSoundProvider

    from .test_spend import _entries

    config.get_settings.cache_clear()
    route = respx.post("https://api.elevenlabs.io/v1/sound-generation").mock(
        return_value=httpx.Response(200, content=b"ID3mp3"))
    assert ElevenLabsSoundProvider().generate(prompt="rain", duration_seconds=99) == b"ID3mp3"
    req = route.calls[0].request
    body = json.loads(req.content)
    assert body == {"text": "rain", "duration_seconds": 30.0, "loop": True, "prompt_influence": 0.5,
                    "model_id": "eleven_text_to_sound_v3"}  # capped at 30 s; v2 is deprecated
    assert req.headers["xi-api-key"] == "el"
    assert req.url.params["output_format"] == "mp3_44100_128"
    (row,) = _entries()
    assert (row["provider"], row["kind"], row["is_estimate"]) == ("elevenlabs", "sound", True)

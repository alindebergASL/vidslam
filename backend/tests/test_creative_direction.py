from __future__ import annotations

import subprocess

from .conftest import make_png_bytes


def _project(auth_client, **extra) -> int:
    aid = auth_client.post("/api/avatars", json={"name": "Demo"}).json()["id"]
    auth_client.post(
        f"/api/avatars/{aid}/assets",
        files={"file": ("h.png", make_png_bytes(), "image/png")},
        data={"asset_type": "hero", "rights_confirmed": "true"},
    )
    body = {
        "title": "Tutorial",
        "original_script": "Open the dashboard and click new report to begin.",
        "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
        **extra,
    }
    return auth_client.post("/api/projects", json=body).json()["id"]


def test_creative_direction_persists_and_reaches_plan(auth_client):
    pid = _project(
        auth_client,
        creative_direction="Calm B2B SaaS product demo, clean and trustworthy",
    )
    proj = auth_client.get(f"/api/projects/{pid}").json()
    assert proj["creative_direction"].startswith("Calm B2B")
    auth_client.post(f"/api/projects/{pid}/generate-plan")
    shots = auth_client.get(f"/api/projects/{pid}").json()["shots"]
    # Mock planner folds the direction into the visual prompt in brackets.
    assert any("Calm B2B SaaS product demo" in s["prompt"] for s in shots)


def test_custom_disclosure_text_is_used_in_plan_and_default_fallback(auth_client):
    pid = _project(auth_client, disclosure_text="AI-generated product demo")
    auth_client.post(f"/api/projects/{pid}/generate-plan")
    plan = auth_client.get(f"/api/projects/{pid}").json()["generated_plan_json"]
    assert plan["disclosure_text"] == "AI-generated product demo"

    pid2 = _project(auth_client)  # no override
    auth_client.post(f"/api/projects/{pid2}/generate-plan")
    plan2 = auth_client.get(f"/api/projects/{pid2}").json()["generated_plan_json"]
    assert plan2["disclosure_text"] == "AI-generated virtual creator"


def test_custom_disclosure_is_burned_into_video(auth_client):
    pid = _project(auth_client, disclosure_text="Virtual presenter")
    auth_client.post(f"/api/projects/{pid}/generate-plan")
    auth_client.post(f"/api/projects/{pid}/generate-video")
    st = auth_client.get(f"/api/projects/{pid}/status").json()
    assert st["project_status"] == "completed"
    # The render exists and has a video stream (overlay burned in via drawtext).
    final = st["latest_render"]["final_video_path"]
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", final],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert out == "h264"


def test_disclosure_disabled_overrides_custom_text(auth_client):
    # include_disclosure=False should win even if a custom text is set.
    pid = _project(
        auth_client, disclosure_text="Should not appear", include_disclosure=False
    )
    auth_client.post(f"/api/projects/{pid}/generate-plan")
    plan = auth_client.get(f"/api/projects/{pid}").json()["generated_plan_json"]
    assert plan["disclosure_text"] == ""

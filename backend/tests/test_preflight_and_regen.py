from __future__ import annotations

from pathlib import Path

from .conftest import make_png_bytes


def _make_project_with_hero(auth_client, *, script: str = "a b c d e", cta: str = "Try it"):
    aid = auth_client.post("/api/avatars", json={"name": "Naina"}).json()["id"]
    auth_client.post(
        f"/api/avatars/{aid}/assets",
        files={"file": ("hero.png", make_png_bytes(), "image/png")},
        data={"asset_type": "hero", "rights_confirmed": "true"},
    )
    pid = auth_client.post(
        "/api/projects",
        json={
            "title": "T",
            "original_script": script,
            "mode": "reel_montage",
            "cta_text": cta,
            "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
        },
    ).json()["id"]
    return aid, pid


def test_preflight_passes_with_hero_and_cta(auth_client):
    _, pid = _make_project_with_hero(auth_client)
    r = auth_client.get(f"/api/projects/{pid}/preflight")
    assert r.status_code == 200
    result = r.json()
    assert result["ok"] is True
    by_id = {c["id"]: c for c in result["checks"]}
    assert by_id["avatar_hero"]["status"] == "ok"
    assert by_id["cta_or_disabled"]["status"] == "ok"
    assert by_id["providers_configured"]["status"] == "ok"
    assert by_id["output_writable"]["status"] == "ok"


def test_preflight_fails_when_avatar_has_no_hero(auth_client):
    # Avatar with no assets at all.
    aid = auth_client.post("/api/avatars", json={"name": "X"}).json()["id"]
    pid = auth_client.post(
        "/api/projects",
        json={
            "title": "T",
            "original_script": "hello world",
            "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
        },
    ).json()["id"]
    r = auth_client.get(f"/api/projects/{pid}/preflight")
    result = r.json()
    assert result["ok"] is False
    hero = next(c for c in result["checks"] if c["id"] == "avatar_hero")
    assert hero["status"] == "fail"
    assert "X" in hero["message"]


def test_preflight_warns_on_long_script_and_missing_cta(auth_client):
    _, pid = _make_project_with_hero(
        auth_client, script=" ".join(["word"] * 90), cta=""
    )
    auth_client.patch(f"/api/projects/{pid}", json={"target_duration_seconds": 25})
    result = auth_client.get(f"/api/projects/{pid}/preflight").json()
    by_id = {c["id"]: c for c in result["checks"]}
    assert by_id["script_length"]["status"] == "warn"
    assert by_id["cta_or_disabled"]["status"] == "warn"
    # Warns don't block; ok=True since no fail.
    assert result["ok"] is True


def test_generate_video_blocked_by_preflight_failure(auth_client):
    aid = auth_client.post("/api/avatars", json={"name": "X"}).json()["id"]
    pid = auth_client.post(
        "/api/projects",
        json={
            "title": "T",
            "original_script": "hello world",
            "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
        },
    ).json()["id"]
    auth_client.post(f"/api/projects/{pid}/generate-plan")
    r = auth_client.post(f"/api/projects/{pid}/generate-video")
    assert r.status_code == 422
    body = r.json()["detail"]
    assert body["preflight"]["ok"] is False


def test_generate_video_force_bypasses_preflight(auth_client):
    aid = auth_client.post("/api/avatars", json={"name": "X"}).json()["id"]
    pid = auth_client.post(
        "/api/projects",
        json={
            "title": "T",
            "original_script": "hello world from this project",
            "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
        },
    ).json()["id"]
    auth_client.post(f"/api/projects/{pid}/generate-plan")
    r = auth_client.post(f"/api/projects/{pid}/generate-video?force=true")
    assert r.status_code == 202


def test_regenerate_only_one_shot_reuses_other_clips(auth_client):
    _, pid = _make_project_with_hero(auth_client)
    auth_client.post(f"/api/projects/{pid}/generate-plan")
    auth_client.post(f"/api/projects/{pid}/generate-video")
    proj = auth_client.get(f"/api/projects/{pid}").json()
    assert all(s["status"] == "completed" for s in proj["shots"])

    # Remember the clip_paths so we can detect which ones changed on disk.
    body_shots = [s for s in proj["shots"] if s["shot_type"] != "end_card"]
    assert len(body_shots) >= 2
    first = body_shots[0]
    second = body_shots[1]
    first_before = Path(first["clip_path"]).stat().st_mtime
    second_before = Path(second["clip_path"]).stat().st_mtime

    # Wait so mtime resolution differs.
    import time
    time.sleep(1.1)

    # Regenerate only the first shot (default recompose=true triggers FFmpeg recompose).
    r = auth_client.post(
        f"/api/projects/{pid}/shots/{first['id']}/regenerate"
    )
    assert r.status_code == 202
    assert r.json()["will_recompose"] is True

    proj2 = auth_client.get(f"/api/projects/{pid}").json()
    new_first = next(s for s in proj2["shots"] if s["id"] == first["id"])
    new_second = next(s for s in proj2["shots"] if s["id"] == second["id"])

    # First shot has a new clip file; second is untouched.
    assert Path(new_first["clip_path"]).stat().st_mtime > first_before
    assert Path(new_second["clip_path"]).stat().st_mtime == second_before
    assert new_first["status"] == "completed"

    # A fresh Render row exists from the recompose with a playable final.
    status = auth_client.get(f"/api/projects/{pid}/status").json()
    assert status["latest_render"]["status"] == "completed"
    assert Path(status["latest_render"]["final_video_path"]).exists()


def test_explicit_recompose_endpoint(auth_client):
    _, pid = _make_project_with_hero(auth_client)
    auth_client.post(f"/api/projects/{pid}/generate-plan")
    auth_client.post(f"/api/projects/{pid}/generate-video")
    renders_before = auth_client.get(f"/api/projects/{pid}/status").json()["latest_render"]["id"]
    r = auth_client.post(f"/api/projects/{pid}/recompose")
    assert r.status_code == 202
    latest = auth_client.get(f"/api/projects/{pid}/status").json()["latest_render"]
    assert latest["status"] == "completed"
    assert latest["id"] != renders_before  # a new render row was created
    assert Path(latest["final_video_path"]).exists()

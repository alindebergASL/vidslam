from __future__ import annotations

from pathlib import Path

from app.providers.base import CastContext
from app.providers.mock_chat import MockChatProvider
from app.schemas.storyboard import StoryboardPlan


def test_mock_chat_returns_valid_storyboard_with_cast():
    provider = MockChatProvider()
    project = {
        "title": "T",
        "mode": "reel_montage",
        "target_duration_seconds": 25,
        "cta_text": "Kissmet coming soon",
        "include_disclosure": True,
        "original_script": "One two three four five six seven eight nine ten",
    }
    cast = CastContext(
        avatars=[{"name": "Naina", "role": "host"}, {"name": "Arjun", "role": "co-host"}],
        ingredients=[{"name": "Rooftop", "kind": "scene"}, {"name": "35mm", "kind": "style"}],
    )
    plan = provider.generate_storyboard(project=project, cast=cast)
    assert isinstance(plan, StoryboardPlan)
    assert plan.disclosure_text
    assert any(s.shot_type == "end_card" for s in plan.shots), "CTA should produce end_card"
    # Every visual prompt mentions at least one cast member by name.
    names = ["Naina", "Arjun"]
    assert all(any(n in s.visual_prompt for n in names) or s.shot_type == "end_card"
               for s in plan.shots)


def test_full_pipeline_produces_playable_mp4(auth_client, tmp_path: Path):
    aid = auth_client.post("/api/avatars", json={"name": "Naina"}).json()["id"]
    iid = auth_client.post(
        "/api/ingredients", json={"name": "Rooftop", "kind": "scene"}
    ).json()["id"]

    pid = auth_client.post(
        "/api/projects",
        json={
            "title": "Test",
            "original_script": "If your dating bio says food travel music you have described every human",
            "mode": "reel_montage",
            "cta_text": "Kissmet coming soon",
            "cast": [
                {"member_kind": "avatar", "avatar_id": aid, "role": "host"},
                {"member_kind": "ingredient", "ingredient_id": iid, "role": "location"},
            ],
        },
    ).json()["id"]

    r = auth_client.post(f"/api/projects/{pid}/generate-plan")
    assert r.status_code == 202
    plan = auth_client.get(f"/api/projects/{pid}").json()
    assert plan["status"] == "planned"
    assert len(plan["shots"]) >= 2

    r = auth_client.post(f"/api/projects/{pid}/generate-video")
    assert r.status_code == 202

    status = auth_client.get(f"/api/projects/{pid}/status").json()
    assert status["project_status"] == "completed"
    render = status["latest_render"]
    assert render["status"] == "completed"
    assert Path(render["final_video_path"]).exists()
    assert Path(render["thumbnail_path"]).exists()
    assert all(s["status"] == "completed" for s in status["shots"])

    # Download endpoint
    rid = render["id"]
    r = auth_client.get(f"/api/renders/{rid}/download")
    assert r.status_code == 200
    assert r.headers["content-type"] == "video/mp4"
    assert len(r.content) > 1000


def test_unsafe_script_blocked(auth_client):
    aid = auth_client.post("/api/avatars", json={"name": "X"}).json()["id"]
    pid = auth_client.post(
        "/api/projects",
        json={
            "title": "bad",
            "original_script": "nsfw content",
            "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
        },
    ).json()["id"]
    r = auth_client.post(f"/api/projects/{pid}/generate-plan")
    assert r.status_code == 422


def test_regenerate_shot(auth_client):
    aid = auth_client.post("/api/avatars", json={"name": "X"}).json()["id"]
    pid = auth_client.post("/api/projects", json={
        "title": "x", "original_script": "a b c d e f",
        "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
    }).json()["id"]
    auth_client.post(f"/api/projects/{pid}/generate-plan")
    auth_client.post(f"/api/projects/{pid}/generate-video")
    proj = auth_client.get(f"/api/projects/{pid}").json()
    shot_id = proj["shots"][0]["id"]
    r = auth_client.post(f"/api/projects/{pid}/shots/{shot_id}/regenerate")
    assert r.status_code == 202

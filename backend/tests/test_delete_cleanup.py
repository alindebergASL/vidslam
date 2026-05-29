from __future__ import annotations

from pathlib import Path

from .conftest import make_png_bytes


def _project_with_two_assets(auth_client):
    aid = auth_client.post("/api/avatars", json={"name": "N"}).json()["id"]
    hero = auth_client.post(
        f"/api/avatars/{aid}/assets",
        files={"file": ("h.png", make_png_bytes(), "image/png")},
        data={"asset_type": "hero", "rights_confirmed": "true"},
    ).json()
    extra = auth_client.post(
        f"/api/avatars/{aid}/assets",
        files={"file": ("x.png", make_png_bytes(color=(30, 30, 30)), "image/png")},
        data={"asset_type": "lifestyle", "rights_confirmed": "true"},
    ).json()
    pid = auth_client.post(
        "/api/projects",
        json={
            "title": "V",
            "original_script": "one two three four five",
            "cta_text": "Go",
            "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
        },
    ).json()["id"]
    auth_client.post(f"/api/projects/{pid}/generate-plan")
    return pid, hero, extra


def test_deleting_asset_strips_it_from_shot_references(auth_client):
    pid, hero, extra = _project_with_two_assets(auth_client)
    shot_id = auth_client.get(f"/api/projects/{pid}").json()["shots"][0]["id"]
    auth_client.patch(
        f"/api/projects/{pid}/shots/{shot_id}",
        json={"reference_asset_ids_json": [hero["id"], extra["id"]]},
    )

    assert auth_client.delete(f"/api/assets/{extra['id']}").status_code == 204

    refs = auth_client.get(f"/api/projects/{pid}").json()["shots"][0][
        "reference_asset_ids_json"
    ]
    assert refs == [hero["id"]]  # dangling id removed, surviving id kept


def test_deleting_asset_leaves_unrelated_shots_alone(auth_client):
    """Only shots that actually referenced the asset get rewritten."""
    pid, hero, extra = _project_with_two_assets(auth_client)
    shots = auth_client.get(f"/api/projects/{pid}").json()["shots"]
    # Shot 0 references extra; shot 1 references hero only.
    auth_client.patch(
        f"/api/projects/{pid}/shots/{shots[0]['id']}",
        json={"reference_asset_ids_json": [extra["id"]]},
    )
    auth_client.patch(
        f"/api/projects/{pid}/shots/{shots[1]['id']}",
        json={"reference_asset_ids_json": [hero["id"]]},
    )
    auth_client.delete(f"/api/assets/{extra['id']}")

    refreshed = auth_client.get(f"/api/projects/{pid}").json()["shots"]
    assert refreshed[0]["reference_asset_ids_json"] == []
    assert refreshed[1]["reference_asset_ids_json"] == [hero["id"]]


def test_deleting_project_removes_its_render_directory(auth_client):
    pid, *_ = _project_with_two_assets(auth_client)
    auth_client.post(f"/api/projects/{pid}/generate-video")
    final = Path(
        auth_client.get(f"/api/projects/{pid}/status").json()["latest_render"][
            "final_video_path"
        ]
    )
    render_dir = final.parent
    assert render_dir.exists()
    # Confirm there are render artifacts in there to prove we're not just probing
    # an empty placeholder.
    assert any(render_dir.iterdir())

    assert auth_client.delete(f"/api/projects/{pid}").status_code == 204

    assert not render_dir.exists(), "project delete should also remove render files"

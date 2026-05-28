from __future__ import annotations

from pathlib import Path

from .conftest import make_png_bytes


def _project_with_plan(auth_client) -> int:
    aid = auth_client.post("/api/avatars", json={"name": "X"}).json()["id"]
    auth_client.post(
        f"/api/avatars/{aid}/assets",
        files={"file": ("h.png", make_png_bytes(), "image/png")},
        data={"asset_type": "hero", "rights_confirmed": "true"},
    )
    pid = auth_client.post(
        "/api/projects",
        json={
            "title": "T",
            "original_script": "one two three four five six seven eight",
            "cta_text": "Go",
            "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
        },
    ).json()["id"]
    auth_client.post(f"/api/projects/{pid}/generate-plan")
    return pid


def test_cost_estimate_breakdown(auth_client):
    pid = _project_with_plan(auth_client)
    r = auth_client.get(f"/api/projects/{pid}/cost-estimate")
    assert r.status_code == 200
    body = r.json()
    assert body["currency"] == "USD"
    assert body["is_estimate"] is True
    items = {line["item"].split(" (")[0] for line in body["lines"]}
    assert "Storyboard plan" in items
    assert any("Video clips" in line["item"] for line in body["lines"])
    # Total equals the sum of the lines.
    assert round(sum(line["cost"] for line in body["lines"]), 4) == body["total"]
    assert body["total"] > 0


def test_cost_estimate_requires_plan(auth_client):
    aid = auth_client.post("/api/avatars", json={"name": "X"}).json()["id"]
    pid = auth_client.post(
        "/api/projects",
        json={
            "title": "T",
            "original_script": "hi",
            "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
        },
    ).json()["id"]
    assert auth_client.get(f"/api/projects/{pid}/cost-estimate").status_code == 409


def test_voiceover_silent_drops_tts_line(auth_client):
    pid = _project_with_plan(auth_client)
    auth_client.patch(f"/api/projects/{pid}", json={"voiceover_source": "silent"})
    body = auth_client.get(f"/api/projects/{pid}/cost-estimate").json()
    assert not any("Voiceover" in line["item"] for line in body["lines"])


def test_render_records_estimated_and_actual_cost(auth_client):
    pid = _project_with_plan(auth_client)
    auth_client.post(f"/api/projects/{pid}/generate-video")
    render = auth_client.get(f"/api/projects/{pid}/status").json()["latest_render"]
    assert render["estimated_cost"] > 0
    # Mock mode bills nothing.
    assert render["actual_cost"] == 0.0


def test_bulk_regenerate_reuses_unselected_clips(auth_client):
    pid = _project_with_plan(auth_client)
    auth_client.post(f"/api/projects/{pid}/generate-video")
    proj = auth_client.get(f"/api/projects/{pid}").json()
    body_shots = [s for s in proj["shots"] if s["shot_type"] != "end_card"]
    assert len(body_shots) >= 2
    targets = [body_shots[0]["id"], body_shots[1]["id"]]
    before = {s["id"]: Path(s["clip_path"]).stat().st_mtime for s in body_shots}

    import time
    time.sleep(1.1)

    r = auth_client.post(
        f"/api/projects/{pid}/shots/regenerate-bulk", json={"shot_ids": targets}
    )
    assert r.status_code == 202
    assert set(r.json()["shot_ids"]) == set(targets)

    proj2 = auth_client.get(f"/api/projects/{pid}").json()
    after = {s["id"]: s for s in proj2["shots"]}
    for sid in targets:
        assert Path(after[sid]["clip_path"]).stat().st_mtime > before[sid]
        assert after[sid]["status"] == "completed"
    # A fresh completed render exists from the single recompose.
    st = auth_client.get(f"/api/projects/{pid}/status").json()
    assert st["latest_render"]["status"] == "completed"


def test_bulk_regenerate_rejects_empty_selection(auth_client):
    pid = _project_with_plan(auth_client)
    r = auth_client.post(
        f"/api/projects/{pid}/shots/regenerate-bulk", json={"shot_ids": [999999]}
    )
    assert r.status_code == 422

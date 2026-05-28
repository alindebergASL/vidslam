from __future__ import annotations

from .conftest import make_png_bytes


def _planned(auth_client) -> int:
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


def test_edit_plan_updates_spoken_script_and_end_card(auth_client):
    pid = _planned(auth_client)
    r = auth_client.patch(
        f"/api/projects/{pid}/plan",
        json={"cleaned_voice_script": "A brand new spoken line.", "end_card_text": "Buy now"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["generated_plan_json"]["cleaned_voice_script"] == "A brand new spoken line."
    assert body["generated_plan_json"]["end_card_text"] == "Buy now"
    # End card text is mirrored onto the project so the renderer's CTA card matches.
    assert body["cta_text"] == "Buy now"


def test_resync_captions_rederives_chunks_from_script(auth_client):
    pid = _planned(auth_client)
    new_script = " ".join(f"word{i}" for i in range(20))  # 20 words -> 4 chunks of 5
    r = auth_client.patch(
        f"/api/projects/{pid}/plan",
        json={"cleaned_voice_script": new_script, "resync_captions": True},
    )
    chunks = r.json()["generated_plan_json"]["caption_chunks"]
    assert len(chunks) == 4
    assert chunks[0]["text"] == "word0 word1 word2 word3 word4"
    # Start hints are non-decreasing.
    hints = [c["start_hint"] for c in chunks]
    assert hints == sorted(hints)


def test_edited_script_drives_render_captions(auth_client):
    from pathlib import Path

    pid = _planned(auth_client)
    auth_client.patch(
        f"/api/projects/{pid}/plan",
        json={"cleaned_voice_script": "Totally different narration here now.", "resync_captions": True},
    )
    auth_client.post(f"/api/projects/{pid}/generate-video")
    st = auth_client.get(f"/api/projects/{pid}/status").json()
    assert st["project_status"] == "completed"
    ass = Path(st["latest_render"]["final_video_path"]).parent / "captions.ass"
    text = ass.read_text()
    assert "Totally" in text or "different" in text


def test_edit_plan_409_without_plan(auth_client):
    aid = auth_client.post("/api/avatars", json={"name": "X"}).json()["id"]
    pid = auth_client.post(
        "/api/projects",
        json={
            "title": "T",
            "original_script": "hi",
            "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
        },
    ).json()["id"]
    assert (
        auth_client.patch(f"/api/projects/{pid}/plan", json={"end_card_text": "x"}).status_code
        == 409
    )

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


def test_replan_preserve_edits_keeps_user_fields(auth_client):
    """Replanning with preserve_edits=true must keep per-shot prompt, duration,
    references, and caption_text untouched, while the planner-authoritative
    fields (shot_type, negative_prompt, reference_strategy, camera_direction)
    refresh from the new plan. Without the flag, all fields snap back to the
    planner output."""
    pid = _planned(auth_client)
    shots = auth_client.get(f"/api/projects/{pid}").json()["shots"]
    assert shots, "first plan should produce shots"
    sid = shots[0]["id"]

    # Hand-edit fields the user would tweak.
    edited_prompt = "MY HANDCRAFTED PROMPT — do not stomp"
    edited_duration = 12.5
    auth_client.patch(
        f"/api/projects/{pid}/shots/{sid}",
        json={
            "prompt": edited_prompt,
            "duration_seconds": edited_duration,
            "caption_text": "user-set caption",
            "reference_asset_ids_json": [],
        },
    )

    # Replan with preserve_edits=true — user fields must survive.
    r = auth_client.post(
        f"/api/projects/{pid}/generate-plan?preserve_edits=true"
    )
    assert r.status_code == 202
    after = {s["id"]: s for s in auth_client.get(f"/api/projects/{pid}").json()["shots"]}
    assert sid in after, "the existing shot row must be reused, not recreated"
    survived = after[sid]
    assert survived["prompt"] == edited_prompt
    assert survived["duration_seconds"] == edited_duration
    assert survived["caption_text"] == "user-set caption"

    # Sanity: a plain replan (preserve_edits omitted) still snaps prompts
    # back to the planner output, proving the flag actually gates behavior.
    auth_client.post(f"/api/projects/{pid}/generate-plan")
    fresh = auth_client.get(f"/api/projects/{pid}").json()["shots"]
    assert all(s["prompt"] != edited_prompt for s in fresh), (
        "without preserve_edits, the user's prompt should be replaced"
    )


def test_replan_preserve_edits_keeps_reference_ids_and_duration(auth_client):
    """The user's per-shot reference selection + caption + duration must
    survive a preserve-edits replan, not just the prompt text."""
    pid = _planned(auth_client)
    aid = (
        auth_client.get(f"/api/projects/{pid}").json()["cast_members"][0]["avatar_id"]
    )
    asset_id = auth_client.get(f"/api/avatars/{aid}").json()["assets"][0]["id"]

    shots = auth_client.get(f"/api/projects/{pid}").json()["shots"]
    sid = shots[0]["id"]
    auth_client.patch(
        f"/api/projects/{pid}/shots/{sid}",
        json={
            "reference_asset_ids_json": [asset_id],
            "duration_seconds": 9.5,
            "caption_text": "USER CAPTION",
        },
    )

    r = auth_client.post(f"/api/projects/{pid}/generate-plan?preserve_edits=true")
    assert r.status_code == 202
    survived = next(
        s for s in auth_client.get(f"/api/projects/{pid}").json()["shots"] if s["id"] == sid
    )
    assert survived["reference_asset_ids_json"] == [asset_id]
    assert survived["duration_seconds"] == 9.5
    assert survived["caption_text"] == "USER CAPTION"


def test_replan_preserve_edits_drops_extra_shots_when_new_plan_is_shorter(auth_client, monkeypatch):
    """If the new plan has fewer shots than the existing list, the extras
    must be dropped — not kept as orphan rows."""
    pid = _planned(auth_client)
    existing = auth_client.get(f"/api/projects/{pid}").json()["shots"]
    assert len(existing) >= 2, "mock planner should produce at least 2 shots"

    # Patch the mock planner to emit a single shot so the new plan is
    # strictly shorter than the existing list.
    from app.providers import mock_chat

    real = mock_chat.MockChatProvider.generate_storyboard

    def shorter(self, *, project, cast):  # type: ignore[no-untyped-def]
        plan = real(self, project=project, cast=cast)
        plan.shots = plan.shots[:1]
        return plan

    monkeypatch.setattr(mock_chat.MockChatProvider, "generate_storyboard", shorter)

    r = auth_client.post(f"/api/projects/{pid}/generate-plan?preserve_edits=true")
    assert r.status_code == 202
    after = auth_client.get(f"/api/projects/{pid}").json()["shots"]
    assert len(after) == 1, f"shorter replan should leave 1 shot, got {len(after)}"
    # The surviving shot must be the same row (preserve-edits merges by order).
    assert after[0]["id"] == existing[0]["id"]


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

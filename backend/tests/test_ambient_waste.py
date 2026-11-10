"""Ambient scenes shouldn't waste paid work:

- a made loop is only discarded (new picture, removed picture, a setting
  that needs a new clip) after an explicit go-ahead;
- a failed clip in a chained loop keeps the finished clips and takes, and
  the loop can be resumed from the failed clip;
- the estimate says what the picture alone costs, so the picture can be
  checked before the video is bought.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from .conftest import make_png_bytes
from .test_ambient import SHORT


def _scene(client, *, generate=True, **cfg) -> dict:
    r = client.post("/api/ambient", json={"config": {**SHORT, **cfg}, "generate": generate})
    assert r.status_code == 201, r.text
    return r.json()


def _get(client, sid: int) -> dict:
    return client.get(f"/api/ambient/{sid}").json()


def _upload(client, sid, *, discard=False):
    q = "?discard_loop=true" if discard else ""
    return client.post(f"/api/ambient/{sid}/picture{q}",
                       files={"file": ("pic.png", make_png_bytes((1920, 1080)), "image/png")})


def _spy_submits(monkeypatch, fail_on: set[int] | None = None) -> list[dict]:
    """Record every video submit; the Nth (1-based) in `fail_on` raises."""
    from app.providers import mock_video

    calls: list[dict] = []
    real = mock_video.MockVideoProvider.submit

    def spy(self, **kw):
        calls.append(kw)
        if fail_on and len(calls) in fail_on:
            raise RuntimeError("provider said no")
        return real(self, **kw)

    monkeypatch.setattr(mock_video.MockVideoProvider, "submit", spy)
    return calls


def _segments(client, sid: int) -> list[dict]:
    p = client.get(f"/api/projects/{sid}").json()
    return sorted((s for s in p["shots"] if s["shot_type"] == "ambient_segment"), key=lambda s: s["shot_order"])


# ---------- discarding a made loop ----------


def test_new_picture_over_a_made_loop_needs_a_go_ahead(auth_client):
    scene = _scene(auth_client)
    sid, clip = scene["id"], scene["loop_shot"]["clip_path"]
    assert scene["loop_status"] == "completed" and scene["loop_at_stake"] is not None

    r = _upload(auth_client, sid)
    assert r.status_code == 409 and "discard" in r.json()["detail"]
    after = _get(auth_client, sid)
    assert after["loop_status"] == "completed" and after["loop_shot"]["clip_path"] == clip
    assert after["picture_ready"] is False  # refused before anything changed

    r = _upload(auth_client, sid, discard=True)
    assert r.status_code == 200, r.text
    assert r.json()["loop_status"] == "pending" and r.json()["picture_ready"] is True
    assert r.json()["loop_at_stake"] is None

    # Nothing made any more: the next picture change needs no go-ahead.
    assert _upload(auth_client, sid).status_code == 200


def test_generating_a_picture_is_refused_before_the_image_call(auth_client, monkeypatch):
    sid = _scene(auth_client)["id"]
    from app.providers import mock_image

    calls = []
    real = mock_image.MockImageProvider.generate

    def spy(self, **kw):
        calls.append(kw)
        return real(self, **kw)

    monkeypatch.setattr(mock_image.MockImageProvider, "generate", spy)
    assert auth_client.post(f"/api/ambient/{sid}/picture/generate").status_code == 409
    assert calls == []  # no paid call for a refused request
    r = auth_client.post(f"/api/ambient/{sid}/picture/generate?discard_loop=true")
    assert r.status_code == 200 and r.json()["loop_status"] == "pending" and len(calls) == 1


def test_removing_the_picture_needs_a_go_ahead_once_a_loop_is_made(auth_client):
    sid = _scene(auth_client, generate=False)["id"]
    assert _upload(auth_client, sid).status_code == 200
    assert auth_client.post(f"/api/ambient/{sid}/generate-loop").status_code == 202
    assert _get(auth_client, sid)["loop_status"] == "completed"

    assert auth_client.delete(f"/api/ambient/{sid}/picture").status_code == 409
    assert _get(auth_client, sid)["picture_ready"] is True
    r = auth_client.delete(f"/api/ambient/{sid}/picture?discard_loop=true")
    assert r.status_code == 200 and r.json()["picture_ready"] is False and r.json()["loop_status"] == "pending"


@pytest.mark.parametrize("change", [
    {"config": {**SHORT, "scene_prompt": "A quiet lake at dawn"}},
    {"config": {**SHORT, "loop_seconds": 10}},
    {"aspect_ratio": "9:16"},
])
def test_settings_that_need_a_new_clip_need_a_go_ahead(auth_client, change):
    scene = _scene(auth_client)
    sid = scene["id"]
    r = auth_client.patch(f"/api/ambient/{sid}", json=change)
    assert r.status_code == 409 and "discard" in r.json()["detail"]
    after = _get(auth_client, sid)
    assert after["loop_status"] == "completed"
    assert after["config"] == scene["config"] and after["aspect_ratio"] == scene["aspect_ratio"]

    r = auth_client.patch(f"/api/ambient/{sid}?discard_loop=true", json=change)
    assert r.status_code == 200 and r.json()["loop_status"] == "pending"


def test_settings_that_keep_the_clip_need_no_go_ahead(auth_client):
    sid = _scene(auth_client)["id"]
    r = auth_client.patch(f"/api/ambient/{sid}", json={"config": {**SHORT, "fade_seconds": 3.0, "total_seconds": 40}})
    assert r.status_code == 200 and r.json()["loop_status"] == "completed"


def test_what_the_loop_cost_comes_from_the_spend_ledger(auth_client):
    from app.services import spend

    scene = _scene(auth_client)
    sid = scene["id"]
    # Mock providers bill nothing.
    assert scene["loop_at_stake"] == {"cost": 0.0, "from_ledger": False, "partial": False}

    spend.record(provider="openrouter", kind="video", model="m", cost=4.2, project_id=sid)
    spend.record(provider="openrouter", kind="image", model="m", cost=0.13, project_id=sid)
    spend.record(provider="elevenlabs", kind="music", model="m", cost=0.5, project_id=sid)  # kept, not part of it
    spend.record(provider="openrouter", kind="video", model="m", cost=9.0, project_id=sid + 1)
    stake = _get(auth_client, sid)["loop_at_stake"]
    assert stake == {"cost": pytest.approx(4.33), "from_ledger": True, "partial": False}
    r = auth_client.patch(f"/api/ambient/{sid}", json={"config": {**SHORT, "weather": "fog"}})
    assert r.status_code == 409 and "$4.33" in r.json()["detail"]


# ---------- a failed clip in a chain ----------

CHAIN = {"loop_seconds": 30, "total_seconds": 120}  # 3 × 10 s on the mock


def test_failed_clip_keeps_the_finished_ones_and_resumes(auth_client, monkeypatch):
    calls = _spy_submits(monkeypatch, fail_on={3})
    scene = _scene(auth_client, **CHAIN)
    sid = scene["id"]
    assert scene["status"] == "failed" and scene["loop_status"] == "failed"
    assert len(calls) == 3
    segs = _segments(auth_client, sid)
    assert [s["status"] for s in segs] == ["completed", "completed", "failed"]
    assert scene["resume"]["clip"] == 3 and scene["resume"]["clips"] == 3
    assert scene["resume"]["take"] == 1 and scene["resume"]["kept_clips"] == 2
    assert scene["loop_at_stake"]["partial"] is True

    anchor = calls[0]["references"][0].url
    r = auth_client.post(f"/api/ambient/{sid}/resume-loop")
    assert r.status_code == 202, r.text
    scene = _get(auth_client, sid)
    assert scene["loop_status"] == "completed" and scene["status"] == "planned", scene["loop_error"]
    assert scene["resume"] is None
    # Only the failed clip was bought again: from clip 2's last frame, closing on the anchor.
    assert len(calls) == 4
    refs = calls[3]["references"]
    assert [x.frame_type for x in refs] == ["first_frame", "last_frame"]
    assert refs[0].url not in (anchor, calls[1]["references"][0].url) and refs[1].url == anchor
    after = _segments(auth_client, sid)
    assert [s["id"] for s in after] == [s["id"] for s in segs]
    assert [s["clip_path"] for s in after[:2]] == [s["clip_path"] for s in segs[:2]]
    assert Path(scene["loop_shot"]["clip_path"]).exists()
    assert auth_client.post(f"/api/ambient/{sid}/render").status_code == 202

    assert auth_client.post(f"/api/ambient/{sid}/resume-loop").status_code == 409  # nothing left to resume


def test_provider_failure_after_submit_is_resumable_too(auth_client, monkeypatch):
    """The provider accepts the clip and fails it later (webhook / poll)."""
    calls = _spy_submits(monkeypatch, fail_on={2})
    scene = _scene(auth_client, **CHAIN)
    assert scene["resume"]["clip"] == 2 and scene["resume"]["kept_clips"] == 1
    sid = scene["id"]
    assert auth_client.post(f"/api/ambient/{sid}/resume-loop").status_code == 202
    assert len(calls) == 4 and _get(auth_client, sid)["loop_status"] == "completed"

    # A reported failure (not a refused submit) takes the same path.
    from app import models
    from app.db import SessionLocal
    from app.services import ambient

    db = SessionLocal()
    try:
        project = db.get(models.VideoProject, sid)
        seg = next(s for s in project.shots if s.shot_type == "ambient_segment" and s.shot_order == 2)
        ambient._set_state(project, ambient._CHAIN_KEY, {
            "segment_ids": [s.id for s in sorted(project.shots, key=lambda s: s.shot_order)
                            if s.shot_type == "ambient_segment"],
            "anchor_url": None, "plan": ambient.plan_loop(ambient._config_of(project)).model_dump(),
            "regenerate_music": False, "render_after": False, "variant": 0, "shot_id": None, "anchor0_url": None,
        })
        db.commit()
        job = models.VideoJob(purpose="ambient_segment", ref_id=seg.id, project_id=sid, status="failed",
                              error="content refused", context_json={"index": 1})
        ambient.on_segment_failed(db, job)
    finally:
        db.close()
    scene = _get(auth_client, sid)
    assert scene["status"] == "failed" and scene["resume"]["clip"] == 2


def test_failed_take_keeps_the_finished_takes(auth_client, monkeypatch):
    calls = _spy_submits(monkeypatch, fail_on={3})
    scene = _scene(auth_client, loop_seconds=10, total_seconds=60, variety="takes", variations=3)
    sid = scene["id"]
    assert scene["status"] == "failed"
    first = scene["loop_shot"]["clip_path"]
    second = scene["variant_shots"][0]["clip_path"]
    assert first and second and not scene["variant_shots"][1]["clip_path"]
    assert scene["resume"]["take"] == 3 and scene["resume"]["takes"] == 3 and scene["resume"]["clip"] == 1

    assert auth_client.post(f"/api/ambient/{sid}/resume-loop").status_code == 202
    scene = _get(auth_client, sid)
    assert scene["loop_status"] == "completed", scene["loop_error"]
    assert len(calls) == 4  # takes 1 and 2 were not bought again
    assert scene["loop_shot"]["clip_path"] == first and scene["variant_shots"][0]["clip_path"] == second
    assert Path(scene["variant_shots"][1]["clip_path"]).exists()


def test_start_over_is_still_there_and_drops_the_chain(auth_client, monkeypatch):
    calls = _spy_submits(monkeypatch, fail_on={2})
    sid = _scene(auth_client, **CHAIN)["id"]
    assert _get(auth_client, sid)["resume"] is not None
    assert auth_client.post(f"/api/ambient/{sid}/generate-loop").status_code == 202
    scene = _get(auth_client, sid)
    assert scene["loop_status"] == "completed" and scene["resume"] is None
    assert len(calls) == 2 + 3


def test_a_new_clip_setting_drops_the_kept_clips_only_with_a_go_ahead(auth_client, monkeypatch):
    _spy_submits(monkeypatch, fail_on={3})
    sid = _scene(auth_client, **CHAIN)["id"]
    change = {"config": {**SHORT, **CHAIN, "weather": "fog"}}
    assert auth_client.patch(f"/api/ambient/{sid}", json=change).status_code == 409
    assert _get(auth_client, sid)["resume"] is not None
    r = auth_client.patch(f"/api/ambient/{sid}?discard_loop=true", json=change)
    assert r.status_code == 200 and r.json()["resume"] is None and r.json()["loop_at_stake"] is None
    assert auth_client.post(f"/api/ambient/{sid}/resume-loop").status_code == 409


def test_resume_needs_something_to_resume(auth_client):
    sid = _scene(auth_client, generate=False)["id"]
    assert auth_client.post(f"/api/ambient/{sid}/resume-loop").status_code == 409


# ---------- picture before video ----------


def test_estimate_prices_the_picture_and_the_loop_after_it(auth_client):
    est = auth_client.post("/api/ambient/estimate", json={"config": {**SHORT, "music_mood": "none"}}).json()
    still = next(ln for ln in est["lines"] if ln["unit"] == "image")
    assert est["picture_cost"] == pytest.approx(still["cost"]) and est["picture_cost"] > 0
    assert est["after_picture_total"] == pytest.approx(est["total"] - still["cost"])
    assert est["plan"]["picture_usable"] is True

    scene = _scene(auth_client, generate=False, music_mood="none")
    assert scene["generate_cost"] == pytest.approx(est["total"])
    r = auth_client.post(f"/api/ambient/{scene['id']}/picture/generate")
    assert r.status_code == 200 and r.json()["picture_ready"] is True
    assert r.json()["generate_cost"] == pytest.approx(est["after_picture_total"])


def test_plan_says_when_the_model_cannot_use_a_picture():
    from app.providers.base import VideoCapabilities
    from app.schemas.ambient import AmbientConfig
    from app.services import ambient

    caps = VideoCapabilities(durations=[5, 10], first_frame=False, last_frame=False)
    assert ambient.plan_loop(AmbientConfig(**SHORT), caps).picture_usable is False


def test_music_failing_after_the_clips_does_not_cost_the_clips(auth_client, monkeypatch):
    """The music bed is made after the last clip: if that call fails, the
    loop is finished from the clips it has, with no new video."""
    from app.services import ambient

    calls = _spy_submits(monkeypatch)
    real = ambient.generate_music_bed
    state = {"fail": True}

    def flaky(*a, **kw):
        if state["fail"]:
            raise RuntimeError("music provider is down")
        return real(*a, **kw)

    monkeypatch.setattr(ambient, "generate_music_bed", flaky)
    scene = _scene(auth_client, **CHAIN)
    sid = scene["id"]
    assert scene["status"] == "failed" and len(calls) == 3
    assert scene["resume"]["clip"] == 4 and scene["resume"]["kept_clips"] == 3

    state["fail"] = False
    assert auth_client.post(f"/api/ambient/{sid}/resume-loop").status_code == 202
    scene = _get(auth_client, sid)
    assert scene["loop_status"] == "completed" and scene["music_ready"] is True and scene["resume"] is None
    assert len(calls) == 3

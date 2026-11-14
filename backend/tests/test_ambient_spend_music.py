"""Spend lands on the right scene, music lengths are honest, and the bed
endpoints wait while a scene is generating.

Mock providers record no spend, so the spies below record a rate-card row
the way the real adapters do: what is checked is that the service opened a
spend scope for the project around the call.
"""
from __future__ import annotations

import pytest

from .test_ambient import SHORT
from .test_music_generation import _project


def _scene(client, **cfg) -> int:
    r = client.post("/api/ambient", json={"config": {**SHORT, **cfg}, "generate": False})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _rows(kind: str) -> list:
    from app import models
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        return db.query(models.SpendEntry).filter(models.SpendEntry.kind == kind).all()
    finally:
        db.close()


def _set_status(sid: int, status: str) -> None:
    from app import models
    from app.db import SessionLocal

    db = SessionLocal()
    db.get(models.VideoProject, sid).status = status
    db.commit()
    db.close()


@pytest.fixture
def billed(monkeypatch):
    """Make the mock music, sound, image and planner calls record spend (as
    the real adapters do), and keep what each music call was asked for."""
    from app.providers import mock_image, mock_music, sound
    from app.providers.mock_chat import MockChatProvider
    from app.services import spend

    music_calls: list[float] = []
    real_music = mock_music.MockMusicProvider.generate

    def music(self, *, prompt, duration_seconds, settings=None):
        music_calls.append(duration_seconds)
        spend.record_rate_card("elevenlabs", "music", "m", 0.5)
        # The real mock makes audio as long as asked: keep the 5 min ones quick
        # (30 s still outlasts the crossfade a long bed joins its parts with).
        return real_music(self, prompt=prompt, duration_seconds=min(duration_seconds, 30), settings=settings)

    real_sound = sound.MockSoundProvider.generate

    def snd(self, *, prompt, duration_seconds=30):
        spend.record_rate_card("elevenlabs", "sound", "s", 0.2)
        return real_sound(self, prompt=prompt, duration_seconds=duration_seconds)

    real_image = mock_image.MockImageProvider.generate

    def image(self, **kw):
        spend.record_rate_card("openrouter", "image", "i", 0.1)
        return real_image(self, **kw)

    real_rewrite = MockChatProvider.rewrite_scene

    def rewrite(self, text):
        spend.record_rate_card("openrouter", "chat", "c", 0.001)
        return real_rewrite(self, text)

    monkeypatch.setattr(mock_music.MockMusicProvider, "generate", music)
    monkeypatch.setattr(sound.MockSoundProvider, "generate", snd)
    monkeypatch.setattr(mock_image.MockImageProvider, "generate", image)
    monkeypatch.setattr(MockChatProvider, "rewrite_scene", rewrite)
    return music_calls


# ---------- spend attribution ----------


def test_ambient_music_is_charged_to_the_scene(auth_client, billed):
    sid = _scene(auth_client)
    r = auth_client.post(f"/api/ambient/{sid}/music")
    assert r.status_code == 200, r.text
    rows = _rows("music")
    assert rows and all(e.project_id == sid for e in rows)


def test_ambient_long_music_bed_parts_are_charged_to_the_scene(auth_client, billed, monkeypatch):
    from app.services import ambient

    monkeypatch.setattr(ambient, "music_cap_seconds", lambda model=None: 30.0)
    sid = _scene(auth_client, total_seconds=120, music_bed_seconds=60)
    r = auth_client.post(f"/api/ambient/{sid}/music")
    assert r.status_code == 200, r.text
    rows = _rows("music")
    assert len(rows) >= 2 and all(e.project_id == sid for e in rows)


def test_ambient_sound_is_charged_to_the_scene(auth_client, billed):
    sid = _scene(auth_client, sound="rain")
    r = auth_client.post(f"/api/ambient/{sid}/sound")
    assert r.status_code == 200, r.text
    rows = _rows("sound")
    assert len(rows) == 1 and rows[0].project_id == sid


def test_generated_picture_is_charged_to_the_scene(auth_client, billed):
    sid = _scene(auth_client)
    r = auth_client.post(f"/api/ambient/{sid}/picture/generate")
    assert r.status_code == 200, r.text
    rows = _rows("image")
    assert rows and all(e.project_id == sid for e in rows)


def test_the_description_rewrite_on_create_is_charged_to_the_new_scene(auth_client, billed):
    r = auth_client.post("/api/ambient", json={"config": {
        **SHORT, "scene_preset": "custom", "scene_prompt": "an infinity pool over the ocean, 8k"},
        "generate": False})
    assert r.status_code == 201, r.text
    scene = r.json()
    assert scene["config"]["photo"] and scene["config"]["photo"]["scene"] in scene["loop_shot"]["prompt"]
    rows = _rows("chat")
    assert len(rows) == 1 and rows[0].project_id == scene["id"]


def test_storyboard_music_is_charged_to_the_project(auth_client, billed):
    pid = _project(auth_client)
    r = auth_client.post(f"/api/projects/{pid}/music/generate", json={"prompt": "warm pad", "duration_seconds": 6})
    assert r.status_code == 200, r.text
    rows = _rows("music")
    assert len(rows) == 1 and rows[0].project_id == pid


def test_nothing_leaks_into_later_unscoped_spend(auth_client, billed):
    from app.services import spend

    sid = _scene(auth_client)
    assert auth_client.post(f"/api/ambient/{sid}/music").status_code == 200
    spend.record_rate_card("openrouter", "other", "x", 0.01)
    assert _rows("other")[0].project_id is None


# ---------- honest music lengths ----------


def test_a_one_hour_request_asks_for_the_cap_and_logs_both(auth_client, billed):
    from app import models
    from app.db import SessionLocal
    from app.services import pipeline

    pid = _project(auth_client)
    db = SessionLocal()
    try:
        out = pipeline.generate_music_for_project(db, pid, prompt="warm pad", duration_seconds=3600)
        assert billed == [300.0]
        assert out["seconds"] == 300.0 and out["requested"] == 3600.0 and out["capped"] is True
        assert out["path"].exists()
        log = (db.query(models.ProviderLog)
               .filter(models.ProviderLog.project_id == pid, models.ProviderLog.endpoint == "music.generate")
               .order_by(models.ProviderLog.id.desc()).first())
        assert log.request_summary_json["requested"] == 3600.0
        assert log.request_summary_json["duration"] == 300.0
        assert log.response_summary_json["delivered"] == 300.0 and log.response_summary_json["capped"] is True
    finally:
        db.close()


def test_a_short_request_is_not_capped(auth_client, billed):
    from app.db import SessionLocal
    from app.services import pipeline

    pid = _project(auth_client)
    db = SessionLocal()
    try:
        out = pipeline.generate_music_for_project(db, pid, prompt="warm pad")  # the project's 10 s
        assert billed == [10.0] and out["capped"] is False and out["seconds"] == 10.0
    finally:
        db.close()


def test_storyboard_endpoint_caps_a_long_request(auth_client, billed):
    pid = _project(auth_client)
    r = auth_client.post(f"/api/projects/{pid}/music/generate", json={"prompt": "warm pad", "duration_seconds": 3600})
    assert r.status_code == 200, r.text
    assert billed == [300.0]


def test_elevenlabs_spend_note_says_what_was_delivered(monkeypatch):
    import respx
    from httpx import Response

    from app.config import get_settings
    from app.providers.elevenlabs_music import ElevenLabsMusicProvider
    from app.services import spend

    notes: list[str] = []
    monkeypatch.setattr(spend, "record_rate_card", lambda *a, note="", **k: notes.append(note))
    url = f"{get_settings().elevenlabs_base_url.rstrip('/')}/music"
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(url).mock(return_value=Response(200, content=b"ID3"))
        ElevenLabsMusicProvider(api_key="k").generate(prompt="p", duration_seconds=3600)
        import json

        assert json.loads(route.calls[0].request.content)["music_length_ms"] == 300_000
    assert notes == ["300s track (3600s asked)"]


# ---------- the bed endpoints wait while generating ----------


@pytest.mark.parametrize("bed", ["music", "sound"])
def test_bed_endpoints_wait_while_generating(auth_client, billed, bed):
    sid = _scene(auth_client, sound="rain")
    _set_status(sid, "generating")
    r = auth_client.post(f"/api/ambient/{sid}/{bed}")
    assert r.status_code == 409 and "generating" in r.json()["detail"]
    assert _rows(bed) == [] and billed == []
    _set_status(sid, "draft")
    assert auth_client.post(f"/api/ambient/{sid}/{bed}").status_code == 200


# ---------- backfilling older rows ----------


def test_backfill_matches_only_unambiguous_rows():
    from datetime import datetime, timedelta

    from scripts.backfill_spend_project_ids import Log, Row, match

    t = datetime(2026, 11, 1, 12, 0, 0)
    rows = [Row(1, "music", t), Row(2, "sound", t), Row(3, "image", t), Row(4, "video", t),
            Row(5, "music", t + timedelta(hours=1))]
    logs = [
        Log(7, "music.generate", t + timedelta(seconds=2)),
        Log(8, "sound.generate", t + timedelta(seconds=1)),
        Log(9, "sound.generate", t + timedelta(seconds=3)),  # two scenes at once: left alone
        Log(7, "image.generate(picture)", t + timedelta(seconds=5)),
        Log(6, "music.generate", t - timedelta(seconds=1)),  # before the row: not its log
    ]
    assert match(rows, logs) == {1: 7, 3: 7}


def test_backfill_is_a_dry_run_unless_applied(auth_client):
    from app import models
    from app.db import SessionLocal
    from app.services import pipeline, spend
    from scripts.backfill_spend_project_ids import run

    pid = _project(auth_client)
    db = SessionLocal()
    try:
        spend.record_rate_card("elevenlabs", "music", "m", 0.5)  # the old, unscoped way
        pipeline._log_provider_call(db, project_id=pid, provider="X", endpoint="music.generate",
                                    request_summary={}, response_summary={})
        assert run(False, db=db) and _rows("music")[0].project_id is None
        assert run(True, db=db)
        assert _rows("music")[0].project_id == pid
        assert db.query(models.SpendEntry).filter(models.SpendEntry.project_id.is_(None)).count() == 0
    finally:
        db.close()

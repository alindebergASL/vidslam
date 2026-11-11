"""Clip check (services/inspector.py): each new storyboard clip is looked at
by a vision model (mock here) and the verdict is kept on its take.

- frames are pulled from the clip (4, spread out, edges skipped, ≤ 512 px);
- the verdict is stored on the take and exposed on ShotOut, the takes list
  and the render plan ("2 clips flagged by the check");
- a refusal / DECLINED / garbage reply is "unsure", never "problem";
- the automatic check is skipped when switched off, noted when over the
  monthly budget; the manual check is guarded like any paid call;
- a failing check never touches the render;
- migration 0007.
"""
from __future__ import annotations

import io
import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest
import respx
from PIL import Image

from .test_storyboard_waste import _body as _body_now
from .test_storyboard_waste import _planned
from .test_storyboard_waste import _rendered as _rendered_now

PROBLEM = json.dumps({"verdict": "problem", "summary": "The face is off.", "issues": [
    {"kind": "likeness", "detail": "face doesn't match X"},
    {"kind": "anatomy", "detail": "six fingers on the left hand at the end"},
]})


def _settle() -> None:
    """Automatic checks run on a background thread (no Redis in tests)."""
    from app.workers.queue import wait_background

    assert wait_background(60)


def _rendered(client) -> int:
    pid = _rendered_now(client)
    _settle()
    return pid


def _body(client, pid) -> list[dict]:
    _settle()
    return _body_now(client, pid)


@pytest.fixture(autouse=True)
def _vision(monkeypatch):
    from app.providers import mock_chat

    monkeypatch.setattr(mock_chat, "VISION_REPLY", None)
    monkeypatch.setattr(mock_chat, "VISION_CALLS", [])
    return mock_chat


def _db():
    from app.db import SessionLocal

    return SessionLocal()


def _clip(path: Path, seconds: float = 3.0, size: str = "1280x720") -> Path:
    import subprocess

    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s={size}:d={seconds}:r=24",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast", str(path)], check=True)
    return path


def _over_budget(auth_client, monkeypatch) -> None:
    from app.services import spend

    from .test_spend import _add

    monkeypatch.setattr(spend, "_all_mocked", lambda: False)  # mock mode never hits the cap
    auth_client.put("/api/spend/budget", json={"monthly_usd": 1, "hard_stop": True, "alert_pcts": []})
    _add(1.5)


# ---------- frames ----------


def test_frames_are_spread_over_the_clip_skipping_the_edges(tmp_path):
    from app.services import inspector

    assert inspector.frame_times(5.0) == [0.2, 1.733, 3.267, 4.8]
    assert inspector.frame_times(0.3) == [0.15] * 4
    frames = inspector.extract_frames(_clip(tmp_path / "c.mp4", size="1280x720"))
    assert len(frames) == 4
    for f in frames:
        im = Image.open(io.BytesIO(f))
        assert im.format == "JPEG" and max(im.size) <= 512 and im.size == (512, 288)
    with pytest.raises(RuntimeError):
        inspector.extract_frames(tmp_path / "missing.mp4")


# ---------- the automatic check ----------


def test_each_new_clip_is_checked_and_the_verdict_follows_its_take(auth_client, _vision):
    pid = _rendered(auth_client)
    shots = _body(auth_client, pid)
    assert shots and all(s["inspection"]["verdict"] == "ok" for s in shots)
    ins = shots[0]["inspection"]
    assert ins["model"] == "mock/vision" and ins["auto"] is True and ins["checked_at"] and ins["issues"] == []
    assert len(_vision.VISION_CALLS) == len(shots)
    # The hero shot shows the cast member: their main picture goes first, then 4 frames.
    hero = next(s for s in shots if s["shot_type"] == "hero")
    text, n = _vision.VISION_CALLS[[s["id"] for s in shots].index(hero["id"])]
    assert n == 5 and "Image 1: X's main picture" in text and "Images 2-5: frames" in text
    assert hero["prompt"].strip() in text
    takes = auth_client.get(f"/api/projects/{pid}/shots/{hero['id']}/takes").json()["takes"]
    assert takes[0]["current"] and takes[0]["inspection"]["verdict"] == "ok"
    plan = auth_client.get(f"/api/projects/{pid}/render-plan").json()
    assert plan["flagged"] == [] and plan["flagged_count"] == 0


def test_a_flagged_clip_shows_its_issues_everywhere(auth_client, _vision):
    _vision.VISION_REPLY = PROBLEM
    pid = _rendered(auth_client)
    shots = _body(auth_client, pid)
    ins = shots[0]["inspection"]
    assert ins["verdict"] == "problem" and ins["summary"] == "The face is off."
    assert [i["kind"] for i in ins["issues"]] == ["likeness", "anatomy"]
    assert ins["issues"][0]["detail"] == "face doesn't match X"
    plan = auth_client.get(f"/api/projects/{pid}/render-plan").json()
    assert plan["flagged_count"] == len(shots)
    assert plan["flagged"][0] == {"shot_id": shots[0]["id"], "shot_order": shots[0]["shot_order"],
                                  "summary": "The face is off.", "first_issue": "face doesn't match X"}
    # The render is advisory-checked only: it still completed.
    assert auth_client.get(f"/api/projects/{pid}/status").json()["project_status"] == "completed"


def test_switched_off_the_automatic_check_never_runs(auth_client, _vision):
    r = auth_client.put("/api/settings/clip-check", json={"auto_check": False})
    assert r.status_code == 200 and r.json()["auto_check"] is False
    pid = _rendered(auth_client)
    shots = _body(auth_client, pid)
    assert all(s["inspection"] is None for s in shots) and _vision.VISION_CALLS == []
    # "Check this clip" still works when the automatic one is off.
    r = auth_client.post(f"/api/projects/{pid}/shots/{shots[0]['id']}/check")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["inspection"]["verdict"] == "ok" and body["inspection"]["auto"] is False
    assert body["shot"]["inspection"]["verdict"] == "ok" and len(_vision.VISION_CALLS) == 1


def test_the_automatic_check_is_skipped_over_budget(auth_client, monkeypatch, _vision):
    from app import models
    from app.services import inspector

    pid = _rendered(auth_client)
    shot_id = _body(auth_client, pid)[0]["id"]
    _vision.VISION_CALLS.clear()
    _over_budget(auth_client, monkeypatch)
    db = _db()
    try:
        take = db.query(models.ShotTake).filter_by(shot_id=shot_id).one()
        got = inspector.run_auto(db, take.id)
    finally:
        db.close()
    assert got["verdict"] == "skipped" and got["summary"] == "not checked: monthly budget reached"
    assert _vision.VISION_CALLS == []
    assert _body(auth_client, pid)[0]["inspection"]["verdict"] == "skipped"
    # Switched off: nothing at all (not even a note).
    auth_client.put("/api/settings/clip-check", json={"auto_check": False})
    db = _db()
    try:
        take = db.query(models.ShotTake).filter_by(shot_id=shot_id).one()
        assert inspector.run_auto(db, take.id) is None
    finally:
        db.close()


def test_the_manual_check_is_guarded(auth_client, monkeypatch, _vision):
    pid = _rendered(auth_client)
    sid = _body(auth_client, pid)[0]["id"]
    _vision.VISION_CALLS.clear()
    _over_budget(auth_client, monkeypatch)
    r = auth_client.post(f"/api/projects/{pid}/shots/{sid}/check")
    assert r.status_code == 402 and _vision.VISION_CALLS == []


def test_manual_check_errors(auth_client):
    pid = _planned(auth_client)
    sid = _body(auth_client, pid)[0]["id"]
    assert auth_client.post(f"/api/projects/{pid}/shots/{sid}/check").status_code == 409  # no clip yet
    assert auth_client.post(f"/api/projects/{pid}/shots/99999/check").status_code == 404
    pid = _rendered(auth_client)
    sid = _body(auth_client, pid)[0]["id"]
    r = auth_client.post(f"/api/projects/{pid}/shots/{sid}/check", json={"take_id": 99999})
    assert r.status_code == 404


@pytest.mark.parametrize("reply", [
    "DECLINED",
    "I'm sorry, but I can't help with that.",
    "REFUSED_MINOR",
    "lorem ipsum, not json at all",
    '{"verdict": "maybe", "summary": "?"}',
    "",
])
def test_refusals_and_garbage_read_unsure_never_problem(reply):
    from app.services import inspector

    got = inspector.parse_reply(reply)
    assert got == {"verdict": "unsure", "issues": [], "summary": "couldn't check this clip"}


def test_parse_reply_tidies_a_real_answer():
    from app.services import inspector

    got = inspector.parse_reply("Sure:\n```json\n" + json.dumps({
        "verdict": "PROBLEM", "summary": "  Hands   look wrong. ",
        "issues": [{"kind": "hands", "detail": "extra finger"}, {"kind": "text", "detail": ""}, "x"]}) + "\n```")
    assert got == {"verdict": "problem", "summary": "Hands look wrong.",
                   "issues": [{"kind": "other", "detail": "extra finger"}]}
    got = inspector.parse_reply('{"verdict": "problem", "summary": "Something is off."}')
    assert got["issues"] == [{"kind": "other", "detail": "Something is off."}]
    assert inspector.parse_reply('{"verdict": "ok"}')["summary"] == "Looks right."


def test_a_refused_check_is_stored_as_couldnt_check(auth_client, _vision):
    pid = _rendered(auth_client)
    sid = _body(auth_client, pid)[0]["id"]
    _vision.VISION_REPLY = "DECLINED"
    r = auth_client.post(f"/api/projects/{pid}/shots/{sid}/check").json()
    assert r["inspection"]["verdict"] == "unsure" and r["inspection"]["summary"] == "couldn't check this clip"
    _vision.VISION_REPLY = PROBLEM  # "Check again"
    r = auth_client.post(f"/api/projects/{pid}/shots/{sid}/check").json()
    assert r["inspection"]["verdict"] == "problem"


def test_a_failing_check_never_touches_the_render(auth_client, monkeypatch, _vision):
    from app.providers import mock_chat
    from app.services import inspector

    def boom(self, **kw):
        raise RuntimeError("vision model down")

    monkeypatch.setattr(mock_chat.MockChatProvider, "inspect_images", boom)
    pid = _rendered(auth_client)
    shots = _body(auth_client, pid)
    assert all(s["inspection"]["verdict"] == "unsure" for s in shots)
    assert shots[0]["inspection"]["summary"] == "couldn't check this clip"

    # Even an error outside the check itself leaves the render alone.
    def worse(db, take_id):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(inspector, "run_auto", worse)
    pid = _rendered(auth_client)
    st = auth_client.get(f"/api/projects/{pid}/status").json()
    assert st["project_status"] == "completed" and st["latest_render"]["status"] == "completed"


def test_a_new_take_starts_unchecked_and_picking_a_take_shows_its_verdict(auth_client, _vision):
    from app import models
    from app.services import inspector

    _vision.VISION_REPLY = PROBLEM
    pid = _rendered(auth_client)
    sid = _body(auth_client, pid)[0]["id"]
    first = auth_client.get(f"/api/projects/{pid}/shots/{sid}/takes").json()["takes"][0]
    _vision.VISION_REPLY = None
    auth_client.put("/api/settings/clip-check", json={"auto_check": False})
    assert auth_client.post(f"/api/projects/{pid}/shots/{sid}/regenerate").status_code == 202
    takes = auth_client.get(f"/api/projects/{pid}/shots/{sid}/takes").json()["takes"]
    assert len(takes) == 2 and takes[0]["current"] and takes[0]["inspection"] is None
    assert takes[1]["inspection"]["verdict"] == "problem"
    shot = next(s for s in _body(auth_client, pid) if s["id"] == sid)
    assert shot["inspection"] is None
    # The takes dialog can check an older take too.
    r = auth_client.post(f"/api/projects/{pid}/shots/{sid}/check", json={"take_id": first["id"]})
    assert r.status_code == 200 and r.json()["take_id"] == first["id"]
    assert r.json()["inspection"]["verdict"] == "ok" and r.json()["shot"]["inspection"] is None
    used = auth_client.post(f"/api/projects/{pid}/shots/{sid}/takes/{first['id']}/use").json()
    assert used["inspection"]["verdict"] == "ok"
    db = _db()
    try:
        take = db.get(models.ShotTake, first["id"])
        assert take.inspection_json["verdict"] == "ok"
        assert sid not in [f["shot_id"] for f in inspector.flagged_shots(take.shot.project)]
    finally:
        db.close()


# ---------- settings ----------


def test_settings_defaults_and_validation(auth_client):
    s = auth_client.get("/api/settings/clip-check").json()
    assert s["auto_check"] is True and s["is_default"] is True and s["model"] == "mock/vision"
    assert s["cost_per_check"] == 0.0 and s["provider"] == "mock" and s["currency"] == "USD"
    r = auth_client.put("/api/settings/clip-check", json={"model": "acme/unknown-vision"})
    assert r.status_code == 422
    r = auth_client.put("/api/settings/clip-check", json={"model": "acme/unknown-vision", "allow_unlisted": True})
    assert r.status_code == 200 and r.json()["model"] == "acme/unknown-vision" and r.json()["is_default"] is False
    r = auth_client.put("/api/settings/clip-check", json={"reset_model": True})
    assert r.json()["model"] == "mock/vision" and r.json()["auto_check"] is True
    assert auth_client.get("/api/settings/clip-check/models").json() == {"models": [], "active": "mock/vision"}


def _catalog(entries):
    from app.services.model_registry import put_setting

    put_setting("catalog.snapshot", {"fetched_at": datetime.utcnow().isoformat(), "entries": entries,
                                     "errors": {}, "counts": {}, "new_count": 0})


def test_default_model_and_cost_come_from_the_catalog(auth_client, monkeypatch):
    from app.config import get_settings
    from app.services import inspector

    monkeypatch.setattr(get_settings(), "mock_providers", False)
    assert inspector.default_model() == get_settings().inspector_model  # no catalog yet
    assert inspector.cost_per_check("acme/x") == get_settings().cost_clip_check
    _catalog([
        {"id": "google/gemini-9-flash", "kind": "chat", "created": 20, "input_modalities": ["text", "image"],
         "pricing": {"prompt": 1e-7, "completion": 4e-7}, "usable": True, "provider": "openrouter"},
        {"id": "google/gemini-8-flash", "kind": "chat", "created": 10, "input_modalities": ["text", "image"],
         "pricing": {}, "usable": True, "provider": "openrouter"},
        {"id": "acme/text-only", "kind": "chat", "created": 30, "input_modalities": ["text"],
         "pricing": {}, "usable": True, "provider": "openrouter"},
    ])
    # The configured default isn't listed: the newest fast Google model that takes pictures.
    assert inspector.default_model() == "google/gemini-9-flash"
    # ~5 pictures and a short reply at the listed per-token rates.
    assert inspector.cost_per_check("google/gemini-9-flash") == round(1e-7 * (600 + 5 * 450) + 4e-7 * 250, 4)
    assert [m["id"] for m in inspector.model_choices()] == ["google/gemini-9-flash", "google/gemini-8-flash"]
    r = auth_client.put("/api/settings/clip-check", json={"model": "acme/text-only"})
    assert r.status_code == 422 and "can't look at pictures" in r.json()["detail"]


# ---------- the provider call ----------


def test_openrouter_sends_the_pictures_and_records_the_spend(client):
    from app import models
    from app.providers.openrouter_chat import OpenRouterChatProvider

    p = OpenRouterChatProvider(api_key="k", model="google/gemini-9-flash", json_mode=False)
    seen = {}

    def reply(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "gen-1", "model": "google/gemini-9-flash",
                                         "usage": {"cost": 0.0012},
                                         "choices": [{"message": {"content": '{"verdict": "ok"}'}}]})

    with respx.mock(assert_all_called=True) as router:
        router.post(url__regex=r".*/chat/completions").mock(side_effect=reply)
        out = p.inspect_images(system="sys", text="look", images=["data:image/jpeg;base64,AAA"] * 2)
    assert out == '{"verdict": "ok"}'
    parts = seen["messages"][1]["content"]
    assert parts[0] == {"type": "text", "text": "look"}
    assert parts[1] == {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA"}}
    assert len(parts) == 3 and seen["messages"][0] == {"role": "system", "content": "sys"}
    db = _db()
    try:
        row = db.query(models.SpendEntry).one()
        assert row.note == "clip check" and row.cost_usd == 0.0012 and row.kind == "chat"
    finally:
        db.close()


# ---------- migration ----------


def test_migration_0007(tmp_path, monkeypatch):
    from alembic import command
    from sqlalchemy import inspect, text

    from app import config
    from app import db as dbmod

    url = f"sqlite:///{tmp_path / 'm.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config.get_settings.cache_clear()
    dbmod.reset_engine(url)
    command.upgrade(dbmod._alembic_config(), "0006")
    assert "inspection_json" not in {c["name"] for c in inspect(dbmod.engine).get_columns("shot_takes")}
    command.upgrade(dbmod._alembic_config(), "0007")
    cols = {c["name"]: c for c in inspect(dbmod.engine).get_columns("shot_takes")}
    assert cols["inspection_json"]["nullable"] is True and "JSON" in str(cols["inspection_json"]["type"]).upper()
    with dbmod.engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0007"
    command.downgrade(dbmod._alembic_config(), "0006")
    assert "inspection_json" not in {c["name"] for c in inspect(dbmod.engine).get_columns("shot_takes")}


def test_without_a_key_nothing_pretends_to_check(auth_client, monkeypatch, _vision):
    from app import models
    from app.config import get_settings
    from app.services import inspector

    auth_client.put("/api/settings/clip-check", json={"auto_check": False})
    pid = _rendered(auth_client)
    sid = _body(auth_client, pid)[0]["id"]
    auth_client.put("/api/settings/clip-check", json={"auto_check": True})
    monkeypatch.setattr(get_settings(), "mock_providers", False)  # no OpenRouter key in tests
    assert inspector.placeholder("google/gemini-9-flash") is True
    assert auth_client.get("/api/settings/clip-check").json()["available"] is False
    r = auth_client.post(f"/api/projects/{pid}/shots/{sid}/check")
    assert r.status_code == 409 and "OpenRouter key" in r.json()["detail"]
    db = _db()
    try:
        take = db.query(models.ShotTake).filter_by(shot_id=sid).one()
        assert inspector.run_auto(db, take.id) is None
    finally:
        db.close()
    assert _vision.VISION_CALLS == []

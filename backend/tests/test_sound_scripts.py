"""Sound Studio PR2: stories CRUD, the writer job (cancel, resume, the
sweeper), Enhance (a selection at once, a chapter or the story as a job),
prices before Write and Enhance-all, spend attribution, and the safety
checks on save, on every written chapter and on Enhance output (minors in
every mode, explicit premises on a clean deployment)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import httpx
import pytest
import respx

from .test_adult_features_off import _clean
from .test_models_catalog import OR, _real

MINOR_PREMISE = "A sexual story about a 14-year-old girl and her teacher."
EXPLICIT_PREMISE = "An explicit sex scene between two adult lovers."


def _new(c, **kw):
    body = {"title": "Harbour night", "premise": "A lighthouse keeper keeps the light through a calm, rainy night.",
            "target_minutes": 30, "settings": {"style": "cozy", "sfx_density": "medium"}}
    body.update(kw)
    r = c.post("/api/sound/stories", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _story(c, sid):
    r = c.get(f"/api/sound/stories/{sid}")
    assert r.status_code == 200, r.text
    return r.json()


def _db():
    from app.db import SessionLocal

    return SessionLocal()


def _written(c, **kw):
    s = _new(c, **kw)
    r = c.post(f"/api/sound/stories/{s['id']}/write")
    assert r.status_code == 202, r.text
    got = _story(c, s["id"])
    assert got["status"] == "ready", got["error"]
    return got


# ---------- the writer ----------


def test_mock_writer_writes_a_parseable_30_minute_script(auth_client):
    from app.providers import mock_chat
    from app.services import story_script

    mock_chat.STORY_CALLS.clear()
    got = _written(auth_client)
    doc = story_script.parse(got["script_text"], style="cozy")
    assert 25 <= doc.est_seconds / 60 <= 35
    titled = [c.title for c in doc.chapters if c.title]
    assert titled[:2] == ["Chapter 1: Part One", "Chapter 2: Part Two"] and len(titled) == 6
    assert {"NARRATOR", "MARA"} <= set(doc.cast) and doc.cast["MARA"].pan == "left"
    assert not doc.warnings and doc.sfx() and len(doc.speech()) > 100
    assert got["parsed"]["warnings"] == [] and got["writer"] == {
        "chapters_done": 6, "chapters": 6, "has_outline": True, "finished": True}
    # The outline once, then one call per chapter; the script saved per chapter.
    assert mock_chat.STORY_CALLS[0][0] == "outline"
    assert [i for k, i in mock_chat.STORY_CALLS if k == "chapter"] == [0, 1, 2, 3, 4, 5]
    assert got["script_version"] == 7
    evs = auth_client.get("/api/events/recent?limit=50").json()["events"]
    mine = [e for e in evs if e["kind"] == "sound_story" and e["entity_id"] == got["id"]]
    assert {e["status"] for e in mine} >= {"started", "progress", "completed"}
    assert all(e["url"] == f"/sound/stories/{got['id']}" and e["project_id"] is None for e in mine)


def test_write_refuses_a_story_that_already_has_lines_unless_replaced(auth_client):
    s = _new(auth_client, script_text="NARRATOR: Hello there.\n")
    assert s["status"] == "ready" and s["script_version"] == 1
    assert auth_client.post(f"/api/sound/stories/{s['id']}/write").status_code == 409
    r = auth_client.post(f"/api/sound/stories/{s['id']}/write", json={"replace": True})
    assert r.status_code == 202, r.text
    assert "Hello there" not in _story(auth_client, s["id"])["script_text"]


def _cancel_during(index: int, monkeypatch):
    """Make the mock writer cancel the story while it writes chapter `index`."""
    from app.providers.mock_chat import MockChatProvider
    from app.services import sound_studio

    real = MockChatProvider.story_chapter

    def chapter(self, **kw):  # noqa: ANN001, ANN003
        out = real(self, **kw)
        if kw["index"] == index:
            from app import models

            db = _db()
            try:
                sound_studio.cancel(db, db.query(models.SoundStory).first())
            finally:
                db.close()
        return out

    monkeypatch.setattr(MockChatProvider, "story_chapter", chapter)
    return real


def test_cancel_during_writing_keeps_finished_chapters_and_resume_continues(auth_client, monkeypatch):
    from app.providers import mock_chat
    from app.providers.mock_chat import MockChatProvider

    s = _new(auth_client)
    real = _cancel_during(1, monkeypatch)
    mock_chat.STORY_CALLS.clear()
    assert auth_client.post(f"/api/sound/stories/{s['id']}/write").status_code == 202
    got = _story(auth_client, s["id"])
    # The chapter in flight when Cancel came is paid for, so it is kept.
    assert got["status"] == "cancelled" and got["can_resume"]
    assert got["writer"]["chapters_done"] == 2 and got["script_text"].count("# Chapter") == 2
    assert [i for k, i in mock_chat.STORY_CALLS if k == "chapter"] == [0, 1]
    kept = got["script_text"]

    # Resume is priced at what is left.
    est = auth_client.post("/api/sound/estimate", json={"story_id": s["id"]}).json()
    assert est["write"]["calls"] == 4
    monkeypatch.setattr(MockChatProvider, "story_chapter", real)
    mock_chat.STORY_CALLS.clear()
    r = auth_client.post(f"/api/sound/stories/{s['id']}/resume")
    assert r.status_code == 202, r.text
    assert r.json()["price"]["calls"] == 4
    got = _story(auth_client, s["id"])
    assert got["status"] == "ready" and got["script_text"].startswith(kept.rstrip())
    assert got["script_text"].count("# Chapter") == 6
    # No outline again, and no chapter written twice.
    assert mock_chat.STORY_CALLS == [("chapter", 2), ("chapter", 3), ("chapter", 4), ("chapter", 5)]
    assert not got["can_resume"]
    assert auth_client.post(f"/api/sound/stories/{s['id']}/resume").status_code == 409


def test_cancel_while_queued_releases_the_claim_and_the_job_does_nothing(auth_client):
    from app import models
    from app.providers import mock_chat
    from app.services import sound_studio
    from app.workers.jobs import story_script_job

    s = _new(auth_client)
    db = _db()
    try:
        token, _ = sound_studio.claim(db, db.get(models.SoundStory, s["id"]), sound_studio.WRITER_KEY)
    finally:
        db.close()
    assert _story(auth_client, s["id"])["task"] == "writer"
    assert auth_client.patch(f"/api/sound/stories/{s['id']}", json={"title": "x"}).status_code == 409
    assert auth_client.delete(f"/api/sound/stories/{s['id']}").status_code == 409
    r = auth_client.post(f"/api/sound/stories/{s['id']}/cancel")
    assert r.json() == {"status": "cancelled", "action": "released"}
    mock_chat.STORY_CALLS.clear()
    story_script_job(s["id"], claim_token=token)
    assert mock_chat.STORY_CALLS == [] and _story(auth_client, s["id"])["status"] == "cancelled"


def test_a_stuck_writer_is_failed_by_the_sweeper_and_resume_continues(auth_client):
    from app import models
    from app.services import sound_studio, sweepers

    s = _new(auth_client)
    fresh = _new(auth_client, title="Still running")
    old = datetime.utcnow() - timedelta(hours=6)
    db = _db()
    try:
        for sid, when in ((s["id"], old), (fresh["id"], datetime.utcnow())):
            sound_studio.claim(db, db.get(models.SoundStory, sid), sound_studio.WRITER_KEY)
            db.query(models.SoundStory).filter(models.SoundStory.id == sid).update(
                {"claim_token": "", "queue_job_id": "inline:story_script_job", "updated_at": when, "heartbeat_at": when,
                 "progress_json": {"task": "writer", "previous": "draft",
                                   "writer": {"chapters": 6, "chapters_done": 3}}},
                synchronize_session=False)
        db.commit()
    finally:
        db.close()
    active = auth_client.get("/api/activity/active").json()
    assert active["counts"]["sound"] == 2
    assert sweepers.run_once()["sound"] == {"failed": 1}
    got = _story(auth_client, s["id"])
    assert got["status"] == "failed" and got["error"] == sound_studio.STOPPED and got["can_resume"]
    assert _story(auth_client, fresh["id"])["status"] == "scripting"
    assert sweepers.run_once()["sound"] == {"failed": 0}
    evs = auth_client.get("/api/events/recent?limit=20").json()["events"]
    assert any(e["kind"] == "sound_story" and e["status"] == "failed" and e["entity_id"] == s["id"] for e in evs)
    # Resume writes the three missing chapters (no outline was kept: it is written first).
    assert auth_client.post(f"/api/sound/stories/{s['id']}/resume").status_code == 202
    assert _story(auth_client, s["id"])["status"] == "ready"


def test_active_counts_a_story_being_written(auth_client):
    from app import models
    from app.services import events

    db = _db()
    try:
        db.add(models.SoundStory(title="T", status="scripting",
                                 progress_json={"task": "writer", "writer": {"chapters": 4, "chapters_done": 1}}))
        db.commit()
        out = events.active(db)
    finally:
        db.close()
    assert out["counts"]["sound"] == 1
    item = next(i for i in out["items"] if i["kind"] == "sound_story")
    assert item["stage"] == "Writing (1 of 4 chapters)" and item["url"].startswith("/sound/stories/")


# ---------- prices ----------


def test_the_price_is_returned_before_write_and_enhance_all(auth_client):
    from app.config import get_settings

    unit = get_settings().cost_chat_per_story_call
    draft = auth_client.post("/api/sound/estimate", json={"premise": "x", "target_minutes": 30}).json()
    assert draft["write"] == {"calls": 7, "unit_cost": unit, "cost": round(7 * unit, 4)}
    s = _new(auth_client)
    est = auth_client.post("/api/sound/estimate", json={"story_id": s["id"]}).json()
    assert est["write"]["cost"] == round(7 * unit, 4)
    got = _written(auth_client)
    est = auth_client.post("/api/sound/estimate", json={"story_id": got["id"], "enhance_scope": "story"}).json()
    assert est["enhance"]["calls"] == 6 and est["enhance"]["cost"] == round(6 * unit, 4)
    assert est["generate"]["total"] > 0 and est["minutes"] > 25


def test_write_over_budget_is_refused_before_anything_is_claimed(auth_client, monkeypatch):
    from app.services import spend

    monkeypatch.setattr(spend, "_all_mocked", lambda: False)
    r = auth_client.put("/api/spend/budget", json={"monthly_usd": 0.01, "hard_stop": True, "alert_pcts": []})
    assert r.status_code == 200, r.text
    s = _new(auth_client)
    r = auth_client.post(f"/api/sound/stories/{s['id']}/write")
    assert r.status_code == 402 and "would cost about $0.04" in r.json()["detail"]
    assert _story(auth_client, s["id"])["status"] == "draft"


def test_spend_is_recorded_under_the_story_with_no_project(auth_client, monkeypatch):
    from app import models
    from app.providers.mock_chat import MockChatProvider
    from app.services import spend

    real_ch = MockChatProvider.story_chapter
    real_en = MockChatProvider.enhance_prompt

    def chapter(self, **kw):  # noqa: ANN001, ANN003
        spend.record_rate_card("mock", "chat", "mock-writer", 0.001, note="story chapter")
        return real_ch(self, **kw)

    def enhance(self, *a, **kw):  # noqa: ANN001, ANN002, ANN003
        spend.record_rate_card("mock", "chat", "mock-writer", 0.001, note="prompt enhance")
        return real_en(self, *a, **kw)

    monkeypatch.setattr(MockChatProvider, "story_chapter", chapter)
    monkeypatch.setattr(MockChatProvider, "enhance_prompt", enhance)
    got = _written(auth_client)
    text = got["script_text"]
    a = text.index("# Chapter 1")
    r = auth_client.post(f"/api/sound/stories/{got['id']}/enhance",
                         json={"action": "asmr", "scope": "selection", "start": a, "end": a + 400,
                               "text": text[a:a + 400], "script_version": got["script_version"]})
    assert r.status_code == 200, r.text
    db = _db()
    try:
        rows = db.query(models.SpendEntry).all()
    finally:
        db.close()
    assert len(rows) == 7
    assert {(e.ref, e.project_id) for e in rows} == {(f"sound_story:{got['id']}", None)}


# ---------- Enhance ----------


def test_enhance_a_selection_runs_at_once_and_saves_nothing(auth_client):
    got = _written(auth_client, settings={"style": "asmr", "sfx_density": "low"})
    text = got["script_text"]
    a = text.index("# Chapter 2")
    b = text.index("\n", a + 1500)
    r = auth_client.post(f"/api/sound/stories/{got['id']}/enhance",
                         json={"action": "sleep", "scope": "selection", "start": a, "end": b, "text": text[a:b],
                               "script_version": got["script_version"]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["mode"] == "sync" and out["before"] == text[a:b] and "(pause 2)" in out["after"]
    assert out["after"].startswith("# Chapter 2")
    after = _story(auth_client, got["id"])
    assert after["script_text"] == text and after["status"] == "ready"
    # "Use it": the editor saves the text with the rewrite in place.
    new_text = text[:a] + out["after"] + text[b:]
    r = auth_client.patch(f"/api/sound/stories/{got['id']}",
                          json={"script_text": new_text, "script_version": got["script_version"]})
    assert r.status_code == 200, r.text
    assert r.json()["script_version"] == got["script_version"] + 1
    # A stale version is refused (Enhance and save).
    r = auth_client.post(f"/api/sound/stories/{got['id']}/enhance",
                         json={"action": "sleep", "scope": "selection", "start": a, "end": b,
                               "script_version": got["script_version"]})
    assert r.status_code == 409
    r = auth_client.patch(f"/api/sound/stories/{got['id']}",
                          json={"script_text": text, "script_version": got["script_version"]})
    assert r.status_code == 409


def test_enhance_a_chapter_or_a_long_selection_is_a_job_with_a_proposal(auth_client):
    from app.services import prompting

    got = _written(auth_client)
    text = got["script_text"]
    line = next(c["line"] for c in got["parsed"]["chapters"] if c["title"].startswith("Chapter 3"))
    r = auth_client.post(f"/api/sound/stories/{got['id']}/enhance",
                         json={"action": "sfx", "scope": "chapter", "chapter_line": line})
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["mode"] == "job" and text[job["start"]:].startswith("# Chapter 3")
    s = _story(auth_client, got["id"])
    assert s["status"] == "ready" and s["script_text"] == text
    prop = s["enhance"]
    assert prop["status"] == "ready" and prop["before"] == text[job["start"]:job["end"]]
    assert prop["after"].startswith("# Chapter 3") and "[sfx: soft wind outside | 3s]" in prop["after"]
    assert prop["done"] == prop["total"] == 1

    # A selection over the cap goes to a job too.
    a = text.index("# Chapter 1")
    b = text.index("# Chapter 4")
    assert b - a > prompting.AUDIO_SCRIPT_MAX
    r = auth_client.post(f"/api/sound/stories/{got['id']}/enhance",
                         json={"action": "tighten", "scope": "selection", "start": a, "end": b})
    assert r.status_code == 202 and r.json()["mode"] == "job"
    prop = _story(auth_client, got["id"])["enhance"]
    assert prop["status"] == "ready" and prop["total"] == 3 and prop["after"].count("# Chapter") == 3

    # The whole story: the cast header is kept as it was.
    r = auth_client.post(f"/api/sound/stories/{got['id']}/enhance", json={"action": "asmr", "scope": "story"})
    assert r.status_code == 202
    prop = _story(auth_client, got["id"])["enhance"]
    assert prop["status"] == "ready" and prop["after"].startswith("@cast NARRATOR")
    assert prop["after"].count("# Chapter") == 6 and "[whispers]" in prop["after"]
    # Keep mine.
    r = auth_client.delete(f"/api/sound/stories/{got['id']}/enhance")
    assert r.status_code == 200 and r.json()["enhance"] is None


def test_enhance_output_is_checked(auth_client, monkeypatch):
    from app.providers import mock_chat

    got = _written(auth_client)
    text = got["script_text"]
    a = text.index("# Chapter 1")
    monkeypatch.setattr(mock_chat, "mock_enhance_story", lambda t, d="": t + "\n" + MINOR_PREMISE)
    r = auth_client.post(f"/api/sound/stories/{got['id']}/enhance",
                         json={"action": "tighten", "scope": "selection", "start": a, "end": a + 300})
    assert r.status_code == 422 and "unchanged" in r.json()["detail"]
    line = next(c["line"] for c in got["parsed"]["chapters"] if c["title"].startswith("Chapter 2"))
    r = auth_client.post(f"/api/sound/stories/{got['id']}/enhance",
                         json={"action": "tighten", "scope": "chapter", "chapter_line": line})
    assert r.status_code == 202
    s = _story(auth_client, got["id"])
    assert s["enhance"]["status"] == "failed" and "unchanged" in s["enhance"]["error"]
    assert s["status"] == "ready" and s["script_text"] == text


# ---------- safety ----------


@pytest.mark.parametrize("mode", ["default", "adult", "clean"])
def test_a_sexual_premise_involving_minors_is_refused_in_every_mode(auth_client, monkeypatch, mode):
    from app.providers import mock_chat

    if mode == "clean":
        _clean(monkeypatch)
    elif mode == "adult":
        from app import config

        monkeypatch.setenv("ADULT_MODE", "true")
        config.get_settings.cache_clear()
    r = auth_client.post("/api/sound/stories", json={"premise": MINOR_PREMISE, "target_minutes": 10})
    assert r.status_code == 422 and "minors" in r.json()["detail"]
    s = _new(auth_client)
    assert auth_client.patch(f"/api/sound/stories/{s['id']}", json={"premise": MINOR_PREMISE}).status_code == 422
    # A pasted script is checked on save too.
    r = auth_client.patch(f"/api/sound/stories/{s['id']}", json={"script_text": f"NARRATOR: {MINOR_PREMISE}\n"})
    assert r.status_code == 422
    # A premise the words don't give away still goes to the minors check
    # (the model), whose refusal stands.
    mock_chat.MINOR_REPLY = {"minor_sexual": True, "reason": "a minor in a sexual context"}
    r = auth_client.post("/api/sound/stories", json={"premise": "A quiet evening at the lake house.",
                                                     "target_minutes": 10})
    assert r.status_code == 422
    assert any("lake house" in t for t in mock_chat.MINOR_CALLS)
    mock_chat.MINOR_REPLY = None
    assert _story(auth_client, s["id"])["premise"] != MINOR_PREMISE


def test_every_written_chapter_is_checked(auth_client, monkeypatch):
    from app.providers.mock_chat import MockChatProvider

    real = MockChatProvider.story_chapter

    def chapter(self, **kw):  # noqa: ANN001, ANN003
        out = real(self, **kw)
        return out + (f"\nMARA: {MINOR_PREMISE}" if kw["index"] == 2 else "")

    monkeypatch.setattr(MockChatProvider, "story_chapter", chapter)
    s = _new(auth_client)
    assert auth_client.post(f"/api/sound/stories/{s['id']}/write").status_code == 202
    got = _story(auth_client, s["id"])
    assert got["status"] == "failed" and "minors" in got["error"]
    # The refused chapter was never saved; the two before it were.
    assert got["writer"]["chapters_done"] == 2 and MINOR_PREMISE not in got["script_text"]


def test_a_clean_deployment_uses_the_clean_prompts(monkeypatch):
    from app.providers import openrouter_chat as oc

    default = oc.story_system()
    assert default.startswith("You write audio stories that are read aloud") and "[seductively]" in default
    assert "romance and adult themes between adults" in oc.audio_script_system("tighten").lower()
    _clean(monkeypatch)
    clean = oc.story_system()
    assert clean.startswith("You write audio stories for a general audience")
    assert "non-explicit" in clean and "DECLINED" in clean and "REFUSED_MINOR" in clean and "under 18" in clean
    assert "[seductively]" not in clean and "[moans]" not in clean and "[whispers]" in clean
    enhance = oc.audio_script_system("tighten").lower()
    assert "non-explicit" in enhance and "romance and adult themes" not in enhance
    assert oc.enhance_system_prompt("audio_script", "", "tighten").lower() == enhance


@respx.mock
def test_the_real_writer_sends_the_clean_system_on_a_clean_deployment(monkeypatch):
    from app.providers.openrouter_chat import OpenRouterChatProvider

    _real(monkeypatch)
    _clean(monkeypatch)
    route = respx.post(f"{OR}/chat/completions").mock(return_value=httpx.Response(200, json={
        "choices": [{"message": {"content": "{}"}}], "usage": {"cost": 0.002}}))
    chat = OpenRouterChatProvider(model="vendor/plain-chat", json_mode=False)
    chat.story_outline(premise="A calm night.", title="", minutes=10, style="sleep", sfx_density="low", chapters=2)
    sent = json.loads(route.calls[-1].request.content)
    assert sent["messages"][0]["content"].startswith("You write audio stories for a general audience")
    chat.story_chapter(premise="A calm night.", outline={"chapters": [{"title": "A"}, {"title": "B"}]}, index=1,
                       minutes=5, style="sleep", sfx_density="low")
    sent = json.loads(route.calls[-1].request.content)
    assert "general audience" in sent["messages"][0]["content"]
    assert "chapter 2 of 2" in sent["messages"][1]["content"]


def test_a_clean_deployment_refuses_explicit_premises(auth_client, monkeypatch):
    # Adult themes between adults are taken in default mode...
    s = _new(auth_client, premise=EXPLICIT_PREMISE)
    _clean(monkeypatch)
    # ...not on a clean deployment: not on create, save or write.
    r = auth_client.post("/api/sound/stories", json={"premise": EXPLICIT_PREMISE, "target_minutes": 10})
    assert r.status_code == 422
    assert auth_client.patch(f"/api/sound/stories/{s['id']}", json={"title": "New title"}).status_code == 422
    assert auth_client.post(f"/api/sound/stories/{s['id']}/write").status_code == 422
    assert _story(auth_client, s["id"])["status"] == "draft"
    # Romance presets are hidden there.
    assert "romance" not in auth_client.get("/api/sound/config").json()["styles"]
    r = auth_client.post("/api/sound/stories", json={"premise": "A calm night.", "settings": {"style": "romance"}})
    assert r.status_code == 422
    # A general-audience story is written with the clean prompts.
    ok = _new(auth_client, target_minutes=10)
    assert auth_client.post(f"/api/sound/stories/{ok['id']}/write").status_code == 202
    assert _story(auth_client, ok["id"])["status"] == "ready"


def test_a_declined_outline_fails_the_story_outside_adult_mode(auth_client, monkeypatch):
    from app.providers.mock_chat import MockChatProvider
    from app.services import prompting

    tried: list[str] = []
    real = MockChatProvider.story_outline

    def outline(self, **kw):  # noqa: ANN001, ANN003
        tried.append("x")
        return prompting.DECLINED if len(tried) == 1 else real(self, **kw)

    monkeypatch.setattr(MockChatProvider, "story_outline", outline)
    s = _new(auth_client, target_minutes=5)
    auth_client.post(f"/api/sound/stories/{s['id']}/write")
    got = _story(auth_client, s["id"])
    assert got["status"] == "failed" and "declined" in got["error"] and len(tried) == 1
    # Resume asks again (the outline is still to write).
    assert got["can_resume"]
    assert auth_client.post(f"/api/sound/stories/{s['id']}/resume").status_code == 202
    assert _story(auth_client, s["id"])["status"] == "ready" and len(tried) == 2


# ---------- CRUD ----------


def test_crud_and_parse(auth_client):
    s = _new(auth_client, script_text="@cast MARA [left]: adult keeper\n# One\nMARA [whispers]: Hello.\n[bogus] Hi.\n")
    assert s["parsed"]["chapters"][0]["title"] == "One" and s["parsed"]["warnings"]
    lst = auth_client.get("/api/sound/stories").json()
    assert lst[0]["id"] == s["id"] and "script_text" not in lst[0]
    r = auth_client.post("/api/sound/parse", json={"script_text": "[sfx: door | 3s]\nHello.", "style": "asmr"})
    assert r.status_code == 200 and r.json()["sfx"] == 1
    assert auth_client.patch(f"/api/sound/stories/{s['id']}", json={"target_minutes": 500}).status_code == 422
    r = auth_client.patch(f"/api/sound/stories/{s['id']}", json={"title": "Renamed", "settings": {"style": "sleep"}})
    assert r.status_code == 200 and r.json()["title"] == "Renamed" and r.json()["settings"]["style"] == "sleep"
    assert r.json()["script_version"] == 1
    cfg = auth_client.get("/api/sound/config").json()
    assert cfg["max_minutes"] == 90 and "romance" in cfg["styles"] and cfg["enhance_sync_max"] == 6000
    assert auth_client.delete(f"/api/sound/stories/{s['id']}").status_code == 204
    assert auth_client.get(f"/api/sound/stories/{s['id']}").status_code == 404

"""Sound Studio PR2, review round 2: text no voice reads is checked (notes,
headings, dropped markup, overridden cast lines, the outline's plan), a
refusal sentinel is caught however the writer wraps it, an Enhance reply
can't change the cast, a clean deployment checks titles and headings for
explicit content; Resume survives an Enhance, a job never saves over a
script saved meanwhile, a Cancel during the last chapter still finishes the
story, chapters are asked for what fits under the cap, and Lengthen's
minutes default and are capped."""
from __future__ import annotations

import json
import math

from .test_adult_features_off import _clean
from .test_sound_scripts import (
    EXPLICIT_PREMISE,
    MINOR_PREMISE,
    _cancel_during,
    _db,
    _new,
    _story,
    _written,
)


def _outline(title: str = "The Quiet Harbour", chapter_title: str = "Part One", n: int = 6, **extra) -> str:
    return json.dumps({
        "title": title,
        "cast": [{"name": "NARRATOR", "pan": "center", "description": "calm adult narrator"},
                 {"name": "MARA", "pan": "left", "description": "adult lighthouse keeper, low warm voice"}],
        "chapters": [{"title": chapter_title if i == 0 else f"Part {i + 1}", "summary": "A calm night."}
                     for i in range(n)],
        **extra,
    })


def _chapter_calls():
    from app.providers import mock_chat

    return [i for k, i in mock_chat.STORY_CALLS if k == "chapter"]


def _set_db(sid: int, **fields) -> None:
    from app import models

    db = _db()
    try:
        db.query(models.SoundStory).filter(models.SoundStory.id == sid).update(fields, synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _row(sid: int):
    from app import models

    db = _db()
    try:
        s = db.get(models.SoundStory, sid)
        db.expunge(s)
        return s
    finally:
        db.close()


# ---------- S1: text no voice reads ----------


def test_notes_headings_and_dropped_markup_are_checked_on_save(auth_client):
    bad = MINOR_PREMISE.rstrip(".")
    scripts = [
        f"NARRATOR: The rain fell.\n// {bad}\n",                       # a note
        f"NARRATOR: The rain [{bad}] fell.\n",                         # an unknown [tag]
        f"NARRATOR: The rain fell.\n@scene {bad}\n",                   # an unknown @ command
        f"@cast MARA {bad}\nNARRATOR: The rain fell.\n",               # a malformed @cast line
        f"NARRATOR: The rain fell.\n# {bad}\n",                        # an empty chapter's heading
        f"@cast MARA: {bad}\n@cast MARA: an adult keeper\nMARA: Hello.\n",  # an overridden declaration
    ]
    for script in scripts:
        r = auth_client.post("/api/sound/stories", json={"title": "Night", "premise": "A calm night.",
                                                         "script_text": script})
        assert r.status_code == 422, (script, r.text)
    s = _new(auth_client, script_text="NARRATOR: The rain fell.\n")
    r = auth_client.patch(f"/api/sound/stories/{s['id']}",
                          json={"script_text": f"NARRATOR: The rain fell.\n// {bad}\n", "script_version": 1})
    assert r.status_code == 422
    # Safe notes and headings still save.
    r = auth_client.patch(f"/api/sound/stories/{s['id']}",
                          json={"script_text": "// a note to self\n# Chapter 1: Rain\nNARRATOR: The rain fell.\n",
                                "script_version": 1})
    assert r.status_code == 200, r.text


def test_a_written_chapter_with_an_unsafe_note_is_never_saved(auth_client, monkeypatch):
    from app.providers.mock_chat import MockChatProvider

    real = MockChatProvider.story_chapter

    def chapter(self, **kw):  # noqa: ANN001, ANN003, ANN202
        out = real(self, **kw)
        return out + f"\n// {MINOR_PREMISE}" if kw["index"] == 1 else out

    monkeypatch.setattr(MockChatProvider, "story_chapter", chapter)
    s = _new(auth_client)
    assert auth_client.post(f"/api/sound/stories/{s['id']}/write").status_code == 202
    got = _story(auth_client, s["id"])
    assert got["status"] == "failed" and got["writer"]["chapters_done"] == 1
    assert "14-year-old" not in got["script_text"]


def test_the_writer_is_shown_only_voiced_lines_of_the_previous_chapter(auth_client, monkeypatch):
    from app.providers.mock_chat import MockChatProvider

    real = MockChatProvider.story_chapter
    seen: list[str] = []

    def chapter(self, **kw):  # noqa: ANN001, ANN003, ANN202
        seen.append(kw["previous"])
        return real(self, **kw)

    monkeypatch.setattr(MockChatProvider, "story_chapter", chapter)
    s = _new(auth_client, script_text="// keep the lamp lit for Ada\n")
    assert auth_client.post(f"/api/sound/stories/{s['id']}/write").status_code == 202
    assert _story(auth_client, s["id"])["status"] == "ready"
    assert len(seen) == 6 and seen[1].strip()
    assert not any("lamp lit for Ada" in p or "# Chapter" in p or "@cast" in p for p in seen), seen[:2]


def test_an_enhance_reply_with_an_unsafe_note_is_refused(auth_client, monkeypatch):
    from app.providers import mock_chat

    got = _written(auth_client)
    a = got["script_text"].index("# Chapter 1")
    monkeypatch.setattr(mock_chat, "mock_enhance_story", lambda t, d="": f"{t}\n// {MINOR_PREMISE}")
    r = auth_client.post(f"/api/sound/stories/{got['id']}/enhance",
                         json={"action": "tighten", "scope": "selection", "start": a + 20, "end": a + 300})
    assert r.status_code == 422, r.text


# ---------- S2: a refusal however it is wrapped ----------


def test_an_outline_refusal_wrapped_in_json_or_explained_is_a_refusal(auth_client, monkeypatch):
    from app.providers.mock_chat import MockChatProvider

    cases = [
        ('{"refusal": "REFUSED_MINOR"}', "minor"),
        (_outline(refusal="REFUSED_MINOR") + " " * 700, "minor"),
        ('{"title": "", "chapters": [], "error": "DECLINED"}', "declined"),
        ("REFUSED_MINOR\n\nI won't write this.", "minor"),
    ]
    for reply, why in cases:
        monkeypatch.setattr(MockChatProvider, "story_outline", lambda self, reply=reply, **kw: reply)
        s = _new(auth_client)
        assert auth_client.post(f"/api/sound/stories/{s['id']}/write").status_code == 202
        got = _story(auth_client, s["id"])
        assert got["status"] == "failed", (reply, got["error"])
        assert ("minor" in got["error"]) == (why == "minor"), (reply, got["error"])
        assert "declined" in got["error"] or why == "minor"
        assert not got["writer"]["has_outline"]


def test_a_chapter_refusal_with_an_explanation_is_not_saved_as_narration(auth_client, monkeypatch):
    from app.providers.mock_chat import MockChatProvider

    monkeypatch.setattr(MockChatProvider, "story_chapter",
                        lambda self, **kw: "REFUSED_MINOR\n\nI won't write this.")
    s = _new(auth_client)
    assert auth_client.post(f"/api/sound/stories/{s['id']}/write").status_code == 202
    got = _story(auth_client, s["id"])
    assert got["status"] == "failed" and "minor" in got["error"]
    assert "REFUSED" not in got["script_text"] and got["writer"]["chapters_done"] == 0

    _clean(monkeypatch)
    monkeypatch.setattr(MockChatProvider, "story_chapter",
                        lambda self, **kw: "DECLINED\n\nThis isn't something I can write for you.")
    s = _new(auth_client, settings={"style": "cozy"})
    assert auth_client.post(f"/api/sound/stories/{s['id']}/write").status_code == 202
    got = _story(auth_client, s["id"])
    assert got["status"] == "failed" and "general-audience" in got["error"], got["error"]
    assert "DECLINED" not in got["script_text"]


# ---------- S3: an Enhance reply can't change the cast ----------


def test_enhance_drops_cast_lines_the_range_did_not_have(auth_client, monkeypatch):
    from app.providers import mock_chat

    got = _written(auth_client)
    a = got["script_text"].index("# Chapter 1")
    monkeypatch.setattr(mock_chat, "mock_enhance_story",
                        lambda t, d="": f"@cast MARA [left]: someone else entirely\n{t}")
    r = auth_client.post(f"/api/sound/stories/{got['id']}/enhance",
                         json={"action": "tighten", "scope": "selection", "start": a + 20, "end": a + 300})
    assert r.status_code == 200, r.text
    assert "@cast" not in r.json()["after"]


def test_enhance_refuses_a_reply_that_redeclares_a_cast_member(auth_client, monkeypatch):
    from app.providers import mock_chat

    got = _written(auth_client)
    text = got["script_text"]
    old = next(ln for ln in text.splitlines() if ln.startswith("@cast MARA"))
    monkeypatch.setattr(mock_chat, "mock_enhance_story",
                        lambda t, d="": t.replace(old, "@cast MARA [left]: a different person"))
    r = auth_client.post(f"/api/sound/stories/{got['id']}/enhance",
                         json={"action": "tighten", "scope": "selection", "start": 0, "end": 400})
    assert r.status_code == 422 and "cast" in r.json()["detail"], r.text
    # The same cast lines back are fine.
    monkeypatch.setattr(mock_chat, "mock_enhance_story", lambda t, d="": t)
    r = auth_client.post(f"/api/sound/stories/{got['id']}/enhance",
                         json={"action": "tighten", "scope": "selection", "start": 0, "end": 400})
    assert r.status_code == 200, r.text


def test_text_the_parser_drops_without_a_warning_is_checked_and_never_sent_to_the_writer(auth_client):
    from app.services import sound_studio

    bad = MINOR_PREMISE.rstrip(".")
    script = f'@bed ambience "rain"\nNARRATOR: The rain fell.\n@bed stop {bad}\n'
    r = auth_client.post("/api/sound/stories", json={"title": "Night", "premise": "A calm night.",
                                                     "script_text": script})
    assert r.status_code == 422, r.text
    s = _new(auth_client, script_text="NARRATOR: The rain fell.\n")
    r = auth_client.patch(f"/api/sound/stories/{s['id']}", json={"script_text": script, "script_version": 1})
    assert r.status_code == 422, r.text
    tail = sound_studio.context_tail(script)
    assert "14-year-old" not in tail and "@bed stop" in tail and "The rain fell." in tail
    # A safe bed stop still saves, and the writer still sees cues as markup.
    r = auth_client.patch(f"/api/sound/stories/{s['id']}", json={
        "script_text": '@bed ambience "rain"\nNARRATOR: The rain fell.\n(pause 2)\n[sfx: thunder]\n@bed stop\n',
        "script_version": 1})
    assert r.status_code == 200, r.text
    tail = sound_studio.context_tail(r.json()["script_text"])
    assert '@bed ambience "rain"' in tail and "(pause 2)" in tail and "[sfx: thunder]" in tail


def test_a_cast_line_behind_a_skipped_cue_is_spotted_as_the_parser_spots_it(auth_client, monkeypatch):
    from app.providers import mock_chat
    from app.services import sound_studio, story_script

    hidden = "[sfx: door [creak]] @cast MARA [left]: a different person entirely"
    got = _written(auth_client)
    a = got["script_text"].index("# Chapter 1")
    # A range with no cast lines: the hidden one is dropped too.
    monkeypatch.setattr(mock_chat, "mock_enhance_story", lambda t, d="": f"{hidden}\n{t}")
    r = auth_client.post(f"/api/sound/stories/{got['id']}/enhance",
                         json={"action": "tighten", "scope": "selection", "start": a + 20, "end": a + 300})
    assert r.status_code == 200, r.text
    assert "@cast" not in r.json()["after"]
    # A range with the cast lines: the hidden re-declaration is refused.
    r = auth_client.post(f"/api/sound/stories/{got['id']}/enhance",
                         json={"action": "tighten", "scope": "selection", "start": 0, "end": 400})
    assert r.status_code == 422 and "cast" in r.json()["detail"], r.text
    # A changed placement alone is a change too.
    old = next(ln for ln in got["script_text"].splitlines() if ln.startswith("@cast MARA"))
    monkeypatch.setattr(mock_chat, "mock_enhance_story",
                        lambda t, d="": t.replace(old, old.replace("[left]", "[right]")))
    r = auth_client.post(f"/api/sound/stories/{got['id']}/enhance",
                         json={"action": "tighten", "scope": "selection", "start": 0, "end": 400})
    assert r.status_code == 422 and "cast" in r.json()["detail"], r.text
    # The overridden line counts as an override (checked as unvoiced text).
    text = f"@cast LILY [left]: {MINOR_PREMISE}\n\nNARRATOR: hi\n[sfx: door [creak]] @cast LILY [left]: an adult\n"
    doc = story_script.parse(text, max_minutes=0)
    assert [n for n, *_ in sound_studio._cast_decls(text)] == [1, 4]
    assert "14-year-old" in sound_studio.unvoiced_text(text, doc)
    r = auth_client.post("/api/sound/stories", json={"title": "Night", "premise": "A calm night.",
                                                     "script_text": text})
    assert r.status_code == 422, r.text


# ---------- S4: a clean deployment checks titles and headings ----------


def test_clean_deployment_checks_the_outline_title_before_saving_it(auth_client, monkeypatch):
    from app.providers.mock_chat import MockChatProvider

    _clean(monkeypatch)
    for reply in (_outline(title=EXPLICIT_PREMISE), _outline(chapter_title=EXPLICIT_PREMISE)):
        monkeypatch.setattr(MockChatProvider, "story_outline", lambda self, reply=reply, **kw: reply)
        s = _new(auth_client, title="", settings={"style": "cozy"})
        assert auth_client.post(f"/api/sound/stories/{s['id']}/write").status_code == 202
        got = _story(auth_client, s["id"])
        assert got["status"] == "failed", got
        assert got["title"] == "" and not got["writer"]["has_outline"]


def test_clean_deployment_checks_chapter_headings_on_save(auth_client, monkeypatch):
    _clean(monkeypatch)
    s = _new(auth_client, script_text="NARRATOR: The rain kept falling.\n", settings={"style": "cozy"})
    for script in (f"# Chapter 1: {EXPLICIT_PREMISE}\nNARRATOR: The rain kept falling.\n",
                   f"NARRATOR: The rain kept falling.\n# Chapter 2: {EXPLICIT_PREMISE}\n"):
        r = auth_client.patch(f"/api/sound/stories/{s['id']}", json={"script_text": script, "script_version": 1})
        assert r.status_code == 422, (script, r.text)
    r = auth_client.patch(f"/api/sound/stories/{s['id']}",
                          json={"script_text": "# Chapter 1: Rain\nNARRATOR: The rain kept falling.\n",
                                "script_version": 1})
    assert r.status_code == 200, r.text


# ---------- B1: Resume survives an Enhance ----------


def test_an_enhance_on_a_part_written_story_keeps_resume(auth_client, monkeypatch):
    from app import models
    from app.services import sound_studio

    s = _new(auth_client)
    real = _cancel_during(1, monkeypatch)
    assert auth_client.post(f"/api/sound/stories/{s['id']}/write").status_code == 202
    got = _story(auth_client, s["id"])
    assert got["status"] == "cancelled" and got["can_resume"]
    from app.providers.mock_chat import MockChatProvider

    monkeypatch.setattr(MockChatProvider, "story_chapter", real)
    line = next(c["line"] for c in got["parsed"]["chapters"] if c["title"].startswith("Chapter 1"))
    r = auth_client.post(f"/api/sound/stories/{s['id']}/enhance",
                         json={"action": "tighten", "scope": "chapter", "chapter_line": line})
    assert r.status_code == 202, r.text
    got = _story(auth_client, s["id"])
    assert got["status"] == "cancelled" and got["enhance"]["status"] == "ready" and got["can_resume"]
    # A cancelled Enhance and a queue that wasn't there keep it too.
    db = _db()
    try:
        story = db.get(models.SoundStory, s["id"])
        sound_studio.claim(db, story, sound_studio.ENHANCE_KEY)
        db.refresh(story)
        sound_studio.cancel(db, story)
        db.refresh(story)
        assert sound_studio.can_resume(story), story.status

        def gone(*a, **kw):  # noqa: ANN002, ANN003, ANN202
            raise ConnectionError("no queue")

        from app.workers import queue

        monkeypatch.setattr(queue, "enqueue", gone)
        try:
            sound_studio.enqueue_claimed(db, story, lambda *a, **k: None, task=sound_studio.ENHANCE_KEY)
        except ConnectionError:
            pass
        db.refresh(story)
        assert story.status == "cancelled" and sound_studio.can_resume(story)
    finally:
        db.close()
    monkeypatch.undo()
    r = auth_client.post(f"/api/sound/stories/{s['id']}/resume")
    assert r.status_code == 202, r.text
    got = _story(auth_client, s["id"])
    assert got["status"] == "ready" and got["writer"]["chapters_done"] == 6, got["error"]


# ---------- B2: a job never saves over a script saved meanwhile ----------


def test_the_outline_save_does_not_overwrite_a_script_saved_meanwhile(auth_client, monkeypatch):
    from app.providers.mock_chat import MockChatProvider
    from app.services import sound_studio

    s = _new(auth_client, script_text="@cast OLDIE: an old man\n")
    real = MockChatProvider.story_outline
    hand = "NARRATOR: Typed by hand while the outline was written.\n"

    def outline(self, **kw):  # noqa: ANN001, ANN003, ANN202
        out = real(self, **kw)
        from app import models

        db = _db()
        try:
            sound_studio.cancel(db, db.get(models.SoundStory, s["id"]))
        finally:
            db.close()
        # A save that didn't retire the run (only the script and its version change).
        _set_db(s["id"], script_text=hand, script_version=s["script_version"] + 1)
        return out

    monkeypatch.setattr(MockChatProvider, "story_outline", outline)
    assert auth_client.post(f"/api/sound/stories/{s['id']}/write").status_code == 202
    got = _story(auth_client, s["id"])
    assert got["script_text"] == hand and got["script_version"] == s["script_version"] + 1


def test_a_chapter_save_does_not_overwrite_a_script_saved_meanwhile(auth_client, monkeypatch):
    from app.providers.mock_chat import MockChatProvider
    from app.services import sound_studio

    s = _new(auth_client)
    real = MockChatProvider.story_chapter
    hand = "NARRATOR: Typed by hand while a chapter was written.\n"

    def chapter(self, **kw):  # noqa: ANN001, ANN003, ANN202
        out = real(self, **kw)
        if kw["index"] == 1:
            from app import models

            db = _db()
            try:
                story = db.get(models.SoundStory, s["id"])
                sound_studio.cancel(db, story)
                v = story.script_version
            finally:
                db.close()
            _set_db(s["id"], script_text=hand, script_version=v + 1)
        return out

    monkeypatch.setattr(MockChatProvider, "story_chapter", chapter)
    assert auth_client.post(f"/api/sound/stories/{s['id']}/write").status_code == 202
    got = _story(auth_client, s["id"])
    assert got["script_text"] == hand, got["script_text"][:300]


# ---------- B3: a Cancel during the last chapter ----------


def test_a_cancel_during_the_last_chapter_still_finishes_the_story(auth_client, monkeypatch):
    s = _new(auth_client)
    _cancel_during(5, monkeypatch)
    assert auth_client.post(f"/api/sound/stories/{s['id']}/write").status_code == 202
    got = _story(auth_client, s["id"])
    assert got["status"] == "ready" and got["error"] == "", (got["status"], got["error"])
    assert got["writer"]["finished"] and got["writer"]["chapters_done"] == 6 and not got["can_resume"]
    assert _row(s["id"]).claim_token == ""


def test_a_story_left_cancelled_with_every_chapter_written_resumes_to_ready(auth_client):
    from app.services import sound_studio

    got = _written(auth_client)
    _set_db(got["id"], status="cancelled", error=sound_studio.CANCELLED)
    s = _story(auth_client, got["id"])
    assert s["can_resume"]
    from app.providers import mock_chat

    mock_chat.STORY_CALLS.clear()
    r = auth_client.post(f"/api/sound/stories/{got['id']}/resume")
    assert r.status_code == 202, r.text
    assert r.json()["price"]["calls"] == 0
    s = _story(auth_client, got["id"])
    assert s["status"] == "ready" and s["error"] == "" and s["script_text"] == got["script_text"]
    assert mock_chat.STORY_CALLS == []


# ---------- B4: chapters asked for what fits under the cap ----------


def test_a_chapter_asks_for_fewer_words_than_its_minutes(auth_client):
    from app.providers.openrouter_chat import chapter_words, story_chapter_request
    from app.services import story_script

    wpm = story_script.WPM_BY_STYLE["cozy"]
    assert chapter_words(5, style="cozy", sfx_density="medium") <= 0.8 * 5 * wpm
    assert chapter_words(5, style="cozy", sfx_density="high") < chapter_words(5, style="cozy", sfx_density="none")
    msg = story_chapter_request(premise="p", outline={"chapters": [{"title": "a"}]}, index=0, minutes=5,
                                style="cozy", sfx_density="medium")
    assert f"about {int(0.8 * 5 * wpm)} spoken words" in msg


def test_the_next_chapter_gets_the_remaining_budget(auth_client, monkeypatch):
    from app.providers.mock_chat import MockChatProvider

    real = MockChatProvider.story_chapter
    asked: list[float] = []

    def chapter(self, **kw):  # noqa: ANN001, ANN003, ANN202
        asked.append(kw["minutes"])
        if kw["index"] == 0:
            kw = {**kw, "minutes": kw["minutes"] * 2}  # the first chapter runs long
        return real(self, **kw)

    monkeypatch.setattr(MockChatProvider, "story_chapter", chapter)
    s = _new(auth_client)
    assert auth_client.post(f"/api/sound/stories/{s['id']}/write").status_code == 202
    got = _story(auth_client, s["id"])
    assert got["status"] == "ready", got["error"]
    assert asked[0] == 5 and all(m < 5 for m in asked[1:]), asked


def test_a_resume_never_pays_again_for_a_chapter_that_cannot_fit(auth_client, monkeypatch):
    from app.config import get_settings
    from app.providers import mock_chat
    from app.providers.mock_chat import MockChatProvider

    monkeypatch.setattr(get_settings(), "sound_story_max_minutes", 30)
    real = MockChatProvider.story_chapter
    asked: list[float] = []

    def long_last(self, **kw):  # noqa: ANN001, ANN003, ANN202
        asked.append(kw["minutes"])
        if kw["index"] == 5 and asked.count(kw["minutes"]) == 1 and len(asked) == 6:
            kw = {**kw, "minutes": kw["minutes"] * 3}  # the first try runs far too long
        return real(self, **kw)

    monkeypatch.setattr(MockChatProvider, "story_chapter", long_last)
    s = _new(auth_client)
    assert auth_client.post(f"/api/sound/stories/{s['id']}/write").status_code == 202
    got = _story(auth_client, s["id"])
    assert got["status"] == "failed" and "longer than" in got["error"] and got["can_resume"], got["error"]
    assert got["writer"]["chapters_done"] == 5
    first = asked[-1]
    # Resume asks the same chapter for less, and it fits.
    assert auth_client.post(f"/api/sound/stories/{s['id']}/resume").status_code == 202
    assert asked[-1] < first
    got = _story(auth_client, s["id"])
    assert got["status"] == "ready", got["error"]
    # A story with no room left: refused before the chapter is paid for.
    s = _new(auth_client)
    sid = s["id"]
    inner = MockChatProvider.story_chapter

    def cancel_at_3(self, **kw):  # noqa: ANN001, ANN003, ANN202
        out = inner(self, **kw)
        if kw["index"] == 3:
            from app import models
            from app.services import sound_studio

            db = _db()
            try:
                sound_studio.cancel(db, db.get(models.SoundStory, sid))
            finally:
                db.close()
        return out

    monkeypatch.setattr(MockChatProvider, "story_chapter", cancel_at_3)
    assert auth_client.post(f"/api/sound/stories/{s['id']}/write").status_code == 202
    got = _story(auth_client, s["id"])
    assert got["status"] == "cancelled" and got["writer"]["chapters_done"] == 4
    est = got["parsed"]["est_seconds"] / 60
    monkeypatch.setattr(get_settings(), "sound_story_max_minutes", math.floor(est) + 1)
    mock_chat.STORY_CALLS.clear()
    assert auth_client.post(f"/api/sound/stories/{s['id']}/resume").status_code == 202
    got = _story(auth_client, s["id"])
    assert got["status"] == "failed" and "no room" in got["error"], got["error"]
    assert _chapter_calls() == []


# ---------- B5: Lengthen's minutes ----------


def test_lengthen_minutes_default_to_the_range_and_are_capped(auth_client):
    from app.services import sound_studio

    got = _written(auth_client)
    ch = next(c for c in got["parsed"]["chapters"] if c["title"].startswith("Chapter 2"))
    r = auth_client.post(f"/api/sound/stories/{got['id']}/enhance",
                         json={"action": "lengthen", "scope": "chapter", "chapter_line": ch["line"],
                               "minutes": sound_studio.max_minutes() + 1})
    assert r.status_code == 422, r.text
    r = auth_client.post(f"/api/sound/stories/{got['id']}/enhance",
                         json={"action": "lengthen", "scope": "chapter", "chapter_line": ch["line"]})
    assert r.status_code == 202, r.text
    s = _story(auth_client, got["id"])
    assert s["enhance"]["status"] == "ready", s["enhance"]["error"]
    assert s["enhance"]["minutes"] == math.ceil(ch["est_seconds"] / 60) and s["enhance"]["minutes"] >= 4

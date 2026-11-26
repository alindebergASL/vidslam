"""Sound Studio PR2 review fixes: a job whose story was cancelled and claimed
again changes nothing (Enhance and the writer), "Write again" keeps the old
script until a new chapter replaces it, an empty Enhance reply is refused,
a clean deployment refuses an explicit premise saved with a script, a
chapter is checked with the whole story before it is saved, and a fresh
Write puts the outline's cast in a script that has no lines yet."""
from __future__ import annotations

from .test_adult_features_off import _clean
from .test_sound_scripts import EXPLICIT_PREMISE, _db, _new, _story, _written


def _claim_like_the_api(sid: int, task: str, **progress_update) -> str:
    """What POST /enhance or /write does: its progress, then the claim
    (the job itself is not run)."""
    from app import models
    from app.services import sound_studio

    db = _db()
    try:
        story = db.get(models.SoundStory, sid)
        if task == sound_studio.WRITER_KEY:
            sound_studio.reset_writer(story, replace=True)
        progress = dict(story.progress_json or {})
        progress.update(progress_update)
        story.progress_json = progress
        db.commit()
        token, _ = sound_studio.claim(db, story, task)
        return token
    finally:
        db.close()


def _cancel_now(sid: int) -> None:
    from app import models
    from app.services import sound_studio

    db = _db()
    try:
        sound_studio.cancel(db, db.get(models.SoundStory, sid))
    finally:
        db.close()


# ---------- a cancelled job and the next claim ----------


def test_a_cancelled_enhance_does_not_put_its_rewrite_on_the_next_enhance(auth_client, monkeypatch):
    from app.providers import mock_chat
    from app.services import sound_studio
    from app.workers.jobs import story_enhance_job

    got = _written(auth_client)
    sid, text = got["id"], got["script_text"]
    real = mock_chat.mock_enhance_story
    second: dict = {}
    a5 = text.index("# Chapter 5")
    b5 = text.index("# Chapter 6")

    def enhance(t, d=""):  # noqa: ANN001, ANN202
        if not second:
            # Cancel while chapter 2's piece is in flight, then Enhance chapter 5.
            _cancel_now(sid)
            second["token"] = _claim_like_the_api(sid, sound_studio.ENHANCE_KEY, enhance={
                "status": "running", "action": "sfx", "scope": "chapter", "start": a5, "end": b5,
                "before": text[a5:b5], "after": "", "script_version": got["script_version"], "minutes": None,
                "done": 0, "total": 1, "error": ""})
        return real(t, d)

    monkeypatch.setattr(mock_chat, "mock_enhance_story", enhance)
    line = next(c["line"] for c in got["parsed"]["chapters"] if c["title"].startswith("Chapter 2"))
    r = auth_client.post(f"/api/sound/stories/{sid}/enhance",
                         json={"action": "tighten", "scope": "chapter", "chapter_line": line})
    assert r.status_code == 202, r.text
    # The old job returned and changed nothing: the newer claim still waits.
    s = _story(auth_client, sid)
    assert s["status"] == "scripting" and s["enhance"]["status"] == "running" and s["enhance"]["start"] == a5
    assert s["enhance"]["after"] == ""
    story_enhance_job(sid, claim_token=second["token"])
    s = _story(auth_client, sid)
    assert s["status"] == "ready" and s["enhance"]["status"] == "ready"
    assert s["enhance"]["before"] == text[a5:b5] and s["enhance"]["after"].startswith("# Chapter 5")
    assert "# Chapter 2" not in s["enhance"]["after"]


def test_a_cancelled_writer_does_not_undo_a_fresh_write(auth_client, monkeypatch):
    from app.providers import mock_chat
    from app.providers.mock_chat import MockChatProvider
    from app.services import sound_studio
    from app.workers.jobs import story_script_job

    s = _new(auth_client)
    sid = s["id"]
    real = MockChatProvider.story_chapter
    second: dict = {}

    def chapter(self, **kw):  # noqa: ANN001, ANN003
        out = real(self, **kw)
        if kw["index"] == 1 and not second:
            # Cancel during chapter 2, then "Write again" (claimed, not yet run).
            _cancel_now(sid)
            second["token"] = _claim_like_the_api(sid, sound_studio.WRITER_KEY)
        return out

    monkeypatch.setattr(MockChatProvider, "story_chapter", chapter)
    mock_chat.STORY_CALLS.clear()
    assert auth_client.post(f"/api/sound/stories/{sid}/write").status_code == 202
    got = _story(auth_client, sid)
    # The old job stopped without saving chapter 2 or putting its outline back.
    assert got["status"] == "scripting" and got["task"] == "writer"
    assert got["script_text"].count("# Chapter") == 1
    assert got["writer"] == {"chapters_done": 0, "chapters": 0, "has_outline": False, "finished": False}
    assert [i for k, i in mock_chat.STORY_CALLS if k == "chapter"] == [0, 1]
    mock_chat.STORY_CALLS.clear()
    story_script_job(sid, claim_token=second["token"])
    got = _story(auth_client, sid)
    assert got["status"] == "ready" and got["script_text"].count("# Chapter") == 6
    assert got["script_text"].count("# Chapter 1:") == 1
    assert mock_chat.STORY_CALLS[0][0] == "outline"


def test_cancel_still_keeps_the_chapter_in_flight(auth_client, monkeypatch):
    from app import models

    from .test_sound_scripts import _cancel_during

    s = _new(auth_client)
    _cancel_during(0, monkeypatch)
    assert auth_client.post(f"/api/sound/stories/{s['id']}/write").status_code == 202
    got = _story(auth_client, s["id"])
    assert got["status"] == "cancelled" and got["writer"]["chapters_done"] == 1
    db = _db()
    try:
        # The job's generation stays until a newer claim replaces it.
        assert db.get(models.SoundStory, s["id"]).claim_token.startswith("run:")
    finally:
        db.close()


# ---------- Write again ----------


def test_write_again_keeps_the_old_script_when_the_outline_is_declined(auth_client, monkeypatch):
    from app.providers.mock_chat import MockChatProvider
    from app.services import prompting

    s = _new(auth_client, script_text="NARRATOR: Hello there.\n")
    monkeypatch.setattr(MockChatProvider, "story_outline", lambda self, **kw: prompting.DECLINED)
    r = auth_client.post(f"/api/sound/stories/{s['id']}/write", json={"replace": True})
    assert r.status_code == 202, r.text
    got = _story(auth_client, s["id"])
    assert got["status"] == "failed" and got["script_text"] == "NARRATOR: Hello there.\n"
    assert got["script_version"] == s["script_version"]


def test_write_again_keeps_the_old_script_when_the_claim_fails(auth_client, monkeypatch):
    from app.services import sound_studio

    s = _new(auth_client, script_text="NARRATOR: Hello there.\n")

    def busy(*a, **kw):  # noqa: ANN002, ANN003, ANN202
        raise LookupError("this story is busy; wait or cancel first")

    monkeypatch.setattr(sound_studio, "claim", busy)
    r = auth_client.post(f"/api/sound/stories/{s['id']}/write", json={"replace": True})
    assert r.status_code == 409
    got = _story(auth_client, s["id"])
    assert got["script_text"] == "NARRATOR: Hello there.\n" and got["script_version"] == s["script_version"]


def test_write_again_replaces_the_script_with_the_first_new_chapter(auth_client):
    s = _new(auth_client, script_text="@cast OLD: an old voice\nOLD: Hello there.\n")
    r = auth_client.post(f"/api/sound/stories/{s['id']}/write", json={"replace": True})
    assert r.status_code == 202, r.text
    got = _story(auth_client, s["id"])
    assert got["status"] == "ready" and "Hello there" not in got["script_text"]
    assert "@cast OLD" not in got["script_text"] and got["script_text"].startswith("@cast NARRATOR")
    assert got["parsed"]["warnings"] == []


# ---------- Enhance: an empty reply ----------


def test_an_empty_enhance_reply_is_refused(auth_client, monkeypatch):
    from app.providers import mock_chat

    got = _written(auth_client)
    text = got["script_text"]
    a = text.index("# Chapter 1")
    for reply in ("", "```\n```"):
        monkeypatch.setattr(mock_chat, "mock_enhance_story", lambda t, d="", reply=reply: reply)
        r = auth_client.post(f"/api/sound/stories/{got['id']}/enhance",
                             json={"action": "tighten", "scope": "selection", "start": a + 20, "end": a + 300})
        assert r.status_code == 422 and "empty" in r.json()["detail"], r.text
    # Only the chapter heading back is no rewrite either.
    monkeypatch.setattr(mock_chat, "mock_enhance_story", lambda t, d="": t.splitlines()[0])
    r = auth_client.post(f"/api/sound/stories/{got['id']}/enhance",
                         json={"action": "tighten", "scope": "selection", "start": a, "end": a + 300})
    assert r.status_code == 422 and "empty" in r.json()["detail"]
    line = next(c["line"] for c in got["parsed"]["chapters"] if c["title"].startswith("Chapter 2"))
    monkeypatch.setattr(mock_chat, "mock_enhance_story", lambda t, d="": "")
    r = auth_client.post(f"/api/sound/stories/{got['id']}/enhance",
                         json={"action": "tighten", "scope": "chapter", "chapter_line": line})
    assert r.status_code == 202
    s = _story(auth_client, got["id"])
    assert s["enhance"]["status"] == "failed" and "empty" in s["enhance"]["error"]
    assert not s["enhance"]["error"].startswith("Enhance stopped") and s["script_text"] == text


# ---------- a clean deployment: the premise with a script ----------


def test_a_clean_deployment_refuses_an_explicit_premise_saved_with_a_script(auth_client, monkeypatch):
    s = _new(auth_client, script_text="NARRATOR: The rain kept falling.\n")
    _clean(monkeypatch)
    r = auth_client.post("/api/sound/stories", json={"premise": EXPLICIT_PREMISE, "target_minutes": 10,
                                                     "script_text": "NARRATOR: The rain kept falling.\n"})
    assert r.status_code == 422
    r = auth_client.patch(f"/api/sound/stories/{s['id']}", json={"premise": EXPLICIT_PREMISE})
    assert r.status_code == 422
    assert _story(auth_client, s["id"])["premise"] != EXPLICIT_PREMISE
    # A general-audience premise with the same script is fine.
    assert auth_client.patch(f"/api/sound/stories/{s['id']}", json={"premise": "A calm night."}).status_code == 200


# ---------- every chapter checked with the whole story ----------


def test_a_chapter_that_fails_with_the_story_before_it_is_never_saved(auth_client, monkeypatch):
    from app.providers.mock_chat import MockChatProvider
    from app.services import story_script
    from app.services.safety import UnsafeScriptError

    real_chapter = MockChatProvider.story_chapter
    real_check = story_script.check_story

    def chapter(self, **kw):  # noqa: ANN001, ANN003
        out = real_chapter(self, **kw)
        extra = {0: "\nMARA: I remember the bluebell field.", 2: "\nMARA: The lantern by the gate is lit."}
        return out + extra.get(kw["index"], "")

    def check_story(doc, **kw):  # noqa: ANN001, ANN003, ANN202
        # Stands in for a fact from an earlier chapter that makes a later one
        # unsafe: each line passes alone, the two together don't.
        said = " ".join(i.spoken_text for i in doc.speech())
        if "bluebell field" in said and "lantern by the gate" in said:
            raise UnsafeScriptError("refused by the whole-story minors check")
        return real_check(doc, **kw)

    monkeypatch.setattr(MockChatProvider, "story_chapter", chapter)
    monkeypatch.setattr(story_script, "check_story", check_story)
    s = _new(auth_client)
    assert auth_client.post(f"/api/sound/stories/{s['id']}/write").status_code == 202
    got = _story(auth_client, s["id"])
    assert got["status"] == "failed" and "whole-story" in got["error"]
    assert got["writer"]["chapters_done"] == 2 and "lantern by the gate" not in got["script_text"]


# ---------- the outline's cast ----------


def test_a_write_on_a_cast_only_script_uses_the_outline_cast(auth_client):
    s = _new(auth_client, script_text="@cast OLDIE: an old man\n")
    r = auth_client.post(f"/api/sound/stories/{s['id']}/write")
    assert r.status_code == 202, r.text
    got = _story(auth_client, s["id"])
    assert got["status"] == "ready", got["error"]
    assert "@cast OLDIE" not in got["script_text"] and "@cast MARA" in got["script_text"]
    assert got["parsed"]["warnings"] == []


# ---------- Codex findings on #68 ----------


def test_lengthen_shares_the_asked_minutes_across_the_pieces(auth_client, monkeypatch):
    """A whole-story Lengthen to N minutes asks for N minutes in all, not N
    for every chapter it rewrites."""
    from app.services import sound_studio

    from .test_sound_scripts import _written

    got = _written(auth_client)
    asked: list = []
    real = sound_studio.enhance_text

    def spy(text, action, **kw):  # noqa: ANN001, ANN003, ANN202
        asked.append(kw.get("minutes"))
        return real(text, action, **kw)

    monkeypatch.setattr(sound_studio, "enhance_text", spy)
    r = auth_client.post(f"/api/sound/stories/{got['id']}/enhance",
                         json={"action": "lengthen", "scope": "story", "minutes": 20})
    assert r.status_code in (200, 202), r.text
    assert len(asked) > 1, asked
    assert all(m is not None and 1 <= m < 20 for m in asked), asked
    assert sum(asked) <= 20 + len(asked), asked


def test_an_edit_saved_after_cancel_is_not_overwritten_by_the_chapter_in_flight(auth_client, monkeypatch):
    """Cancel keeps a running job's chapter in flight, but an edit saved
    before it lands retires that run: the edit stays."""
    from app import models
    from app.api.sound import update_story
    from app.providers.mock_chat import MockChatProvider
    from app.schemas.sound import StoryUpdate
    from app.services import sound_studio

    from .test_sound_scripts import _db, _new, _story

    s = _new(auth_client, script_text="NARRATOR: The first night.\n")
    real = MockChatProvider.story_chapter
    edited = "NARRATOR: My own words, saved by hand.\n"

    def chapter(self, **kw):  # noqa: ANN001, ANN003, ANN202
        out = real(self, **kw)
        if kw["index"] == 0:
            db = _db()
            try:
                story = db.query(models.SoundStory).get(s["id"])
                sound_studio.cancel(db, story)
                db.refresh(story)
                update_story(s["id"], StoryUpdate(script_text=edited, script_version=story.script_version), db)
            finally:
                db.close()
        return out

    monkeypatch.setattr(MockChatProvider, "story_chapter", chapter)
    r = auth_client.post(f"/api/sound/stories/{s['id']}/write", json={"replace": True})
    assert r.status_code == 202, r.text
    got = _story(auth_client, s["id"])
    assert got["script_text"] == edited, got["script_text"][:200]

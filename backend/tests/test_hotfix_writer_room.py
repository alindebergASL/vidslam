"""Hotfix after the #68 live check: the writer and the judges running out of
room, and a failure after Cancel leaving no trace.

- The writer (_story_call) sent no reasoning setting: a route that thinks by
  default spent 7,450 of 8,000 tokens reasoning, stopped at "length" with a
  null content, and was billed; the story failed with "OpenRouter returned
  no message". Reasoning is now off for the writer, Enhance and hook ideas;
  a reply with no text or one stopped at "length" raises WriterOutOfRoom
  (never saved), and sound_studio._ask asks once more with more room, then
  takes the adult fallback path as for a refusal.
- classify_minor / classify_explicit asked for low reasoning inside 300
  tokens: a model that thought past it sent nothing, and the check failed
  closed. The first ask now has JUDGE_TOKENS[0] of room and a reply that ran
  out of room is asked once more with JUDGE_TOKENS[1]; two in a row still
  fail closed.
- A writer call that failed or was refused after a Cancel updated 0 rows
  (finish() is scoped to status 'scripting'): no error, event or log. The
  story stays cancelled and now says which chapter wasn't kept and why.
"""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from .test_minor_check import OR, _real_planner
from .test_sound_scripts import _db, _new, _story


def _reply(content, finish: str = "stop", *, native: str = "", reasoning: int = 0) -> httpx.Response:
    choice = {"message": {"content": content}, "finish_reason": finish}
    if native:
        choice["finish_reason"] = None
        choice["native_finish_reason"] = native
    usage = {"cost": 0.002, "completion_tokens": 8000,
             "completion_tokens_details": {"reasoning_tokens": reasoning}}
    return httpx.Response(200, json={"id": "gen-1", "model": "acme/planner-1", "usage": usage,
                                     "choices": [choice]})


def _provider():
    from app.providers.openrouter_chat import OpenRouterChatProvider

    return OpenRouterChatProvider(model="acme/planner-1")


@pytest.fixture
def no_wait(monkeypatch):
    from app.providers import openrouter_chat

    monkeypatch.setattr(openrouter_chat, "_sleep", lambda _s: None)


# ---------- item 2: the writer's reply budget ----------


@respx.mock
def test_writer_calls_switch_reasoning_off(client, monkeypatch):
    _real_planner(monkeypatch)
    route = respx.post(f"{OR}/chat/completions").mock(side_effect=lambda req: _reply(
        '{"hooks": []}' if b"opening_line" in req.content else "A calm night."))
    p = _provider()
    p._story_call("Write chapter 1.", note="story chapter", max_tokens=8000)
    p.enhance_prompt("A rainy harbour.", "image")
    p.hook_ideas(script="One. Two.", hook="One.", n=1, styles=["question"])
    bodies = [json.loads(c.request.content) for c in route.calls]
    assert [b.get("reasoning") for b in bodies] == [{"enabled": False}] * 3
    assert bodies[0]["max_tokens"] == 8000


@pytest.mark.parametrize("reply", [
    lambda: _reply(None, "length", reasoning=7450),
    lambda: _reply("MARA: The light is ste", "length"),
    lambda: _reply("MARA: The light is ste", native="max_tokens"),
    lambda: _reply(None, native="max_output_tokens"),
    lambda: _reply(None, "stop"),
    lambda: _reply("   ", "stop"),
])
@respx.mock
def test_a_writer_reply_out_of_room_raises_after_its_spend_is_recorded(client, monkeypatch, reply):
    from app import models
    from app.providers.openrouter_chat import WriterOutOfRoom

    _real_planner(monkeypatch)
    respx.post(f"{OR}/chat/completions").mock(return_value=reply())
    with pytest.raises(WriterOutOfRoom):
        _provider()._story_call("Write chapter 1.", note="story chapter", max_tokens=8000)
    db = _db()
    try:
        notes = [r.note for r in db.query(models.SpendEntry).all()]
    finally:
        db.close()
    assert "story chapter" in notes


@respx.mock
def test_more_room_on_the_re_ask(client, monkeypatch):
    _real_planner(monkeypatch)
    route = respx.post(f"{OR}/chat/completions").mock(return_value=_reply("A calm night."))
    p = _provider()
    p.writer_room = 1.5
    p._story_call("Write chapter 1.", note="story chapter", max_tokens=8000)
    assert json.loads(route.calls[0].request.content)["max_tokens"] == 12000


class _Chat:
    def __init__(self, model: str) -> None:
        self.model = model
        self.writer_room = 1.0


def _fake_writer(monkeypatch, replies: dict, *, adult: bool, fallback: str = "acme/fallback"):
    """sound_studio._ask with fake chat models: `replies[model]` is a list of
    replies in order (an exception is raised). Returns the calls made."""
    from app import config
    from app.services import content_mode, prompting

    calls: list[tuple[str, float]] = []
    monkeypatch.setattr(prompting, "_chat_for", lambda m: _Chat(m or "acme/writer"))
    monkeypatch.setattr(content_mode, "adult_mode", lambda: adult)
    monkeypatch.setenv("ADULT_TEXT_FALLBACK_MODEL", fallback)
    config.get_settings.cache_clear()

    def call(chat):  # noqa: ANN001
        calls.append((chat.model, chat.writer_room))
        out = replies[chat.model].pop(0)
        if isinstance(out, BaseException):
            raise out
        return out

    return call, calls


def test_ask_re_asks_once_with_more_room_then_uses_the_reply(client, monkeypatch):
    from app.providers.openrouter_chat import WriterOutOfRoom
    from app.services import sound_studio

    call, calls = _fake_writer(monkeypatch, {"acme/writer": [WriterOutOfRoom("x", "length"), "MARA: Hello."]},
                               adult=False)
    assert sound_studio._ask(call) == ("MARA: Hello.", "acme/writer")
    assert calls == [("acme/writer", 1.0), ("acme/writer", sound_studio.WRITER_RETRY_ROOM)]


def test_ask_out_of_room_twice_goes_to_the_adult_fallback(client, monkeypatch):
    from app.providers.openrouter_chat import WriterOutOfRoom
    from app.services import sound_studio

    oor = WriterOutOfRoom("x", "length")
    call, calls = _fake_writer(monkeypatch, {"acme/writer": [oor, oor], "acme/fallback": ["MARA: Hello."]},
                               adult=True)
    assert sound_studio._ask(call) == ("MARA: Hello.", "acme/fallback")
    assert [m for m, _ in calls] == ["acme/writer", "acme/writer", "acme/fallback"]


@pytest.mark.parametrize("adult", [False, True])
def test_ask_out_of_room_everywhere_is_a_plain_out_of_room_refusal(client, monkeypatch, adult):
    from app.providers.openrouter_chat import WriterOutOfRoom
    from app.services import sound_studio

    oor = WriterOutOfRoom("x", "length")
    call, calls = _fake_writer(monkeypatch, {"acme/writer": [oor, oor], "acme/fallback": [oor]}, adult=adult)
    with pytest.raises(sound_studio.WriterRefused) as e:
        sound_studio._ask(call)
    assert str(e.value) == sound_studio.OUT_OF_ROOM == "The writer ran out of room; Resume tries again."
    assert e.value.reason == "out_of_room"
    assert len(calls) == (3 if adult else 2)


def _chapter_out_of_room(monkeypatch, index: int, *, times: int = 99, cancel: bool = False) -> list:
    """The mock writer runs out of room on chapter `index` (`times` calls in
    a row), cancelling the story first when `cancel`. Returns the writer_room
    of each call for that chapter."""
    from app.providers.mock_chat import MockChatProvider
    from app.providers.openrouter_chat import WriterOutOfRoom
    from app.services import sound_studio

    real = MockChatProvider.story_chapter
    rooms: list[float] = []

    def chapter(self, **kw):  # noqa: ANN001, ANN003
        if kw["index"] != index:
            return real(self, **kw)
        rooms.append(getattr(self, "writer_room", 1.0))
        if cancel and len(rooms) == 1:
            from app import models

            db = _db()
            try:
                sound_studio.cancel(db, db.query(models.SoundStory).first())
            finally:
                db.close()
        if len(rooms) <= times:
            # What _story_call raises for a reply stopped at "length": the
            # half-written chapter is never returned.
            raise WriterOutOfRoom("the writer stopped at max_tokens (8000) before finishing", "length")
        return real(self, **kw)

    monkeypatch.setattr(MockChatProvider, "story_chapter", chapter)
    return rooms


def test_a_chapter_out_of_room_is_asked_again_and_the_story_finishes(auth_client, monkeypatch):
    from app.services import sound_studio

    rooms = _chapter_out_of_room(monkeypatch, 1, times=1)
    s = _new(auth_client)
    assert auth_client.post(f"/api/sound/stories/{s['id']}/write").status_code == 202
    got = _story(auth_client, s["id"])
    assert got["status"] == "ready", got["error"]
    assert rooms == [1.0, sound_studio.WRITER_RETRY_ROOM]
    assert got["writer"]["chapters_done"] == 6


def test_a_chapter_stopped_at_length_is_never_saved(auth_client, monkeypatch):
    rooms = _chapter_out_of_room(monkeypatch, 1)
    s = _new(auth_client)
    assert auth_client.post(f"/api/sound/stories/{s['id']}/write").status_code == 202
    got = _story(auth_client, s["id"])
    assert got["status"] == "failed" and got["error"] == "The writer ran out of room; Resume tries again."
    assert len(rooms) == 2 and got["can_resume"]
    assert got["writer"]["chapters_done"] == 1 and got["script_text"].count("# Chapter") == 1


# ---------- item 4: a failure after Cancel leaves a trace ----------


def test_a_failure_after_cancel_keeps_cancelled_and_says_why(auth_client, monkeypatch, caplog):
    import logging

    _chapter_out_of_room(monkeypatch, 1, cancel=True)
    s = _new(auth_client)
    with caplog.at_level(logging.WARNING, logger="avs.sound_studio"):
        assert auth_client.post(f"/api/sound/stories/{s['id']}/write").status_code == 202
    got = _story(auth_client, s["id"])
    want = ("Cancelled; chapter 2 wasn't kept: The writer ran out of room; Resume tries again. "
            "Resume writes it again.")
    assert got["status"] == "cancelled" and got["error"] == want and got["can_resume"]
    assert got["writer"]["chapters_done"] == 1 and got["script_text"].count("# Chapter") == 1
    evs = auth_client.get("/api/events/recent?limit=50").json()["events"]
    mine = [e for e in evs if e["kind"] == "sound_story" and e["entity_id"] == s["id"]]
    assert [e["detail"] for e in mine if e["status"] == "cancelled"][:1] == [want]
    assert any("cancelled, and the call in flight failed" in r.getMessage() for r in caplog.records)


def test_a_refusal_after_cancel_of_the_outline_says_the_outline(auth_client, monkeypatch):
    from app.providers.mock_chat import MockChatProvider
    from app.services import sound_studio

    def outline(self, **kw):  # noqa: ANN001, ANN003
        from app import models

        db = _db()
        try:
            sound_studio.cancel(db, db.query(models.SoundStory).first())
        finally:
            db.close()
        return "DECLINED"

    monkeypatch.setattr(MockChatProvider, "story_outline", outline)
    s = _new(auth_client)
    assert auth_client.post(f"/api/sound/stories/{s['id']}/write").status_code == 202
    got = _story(auth_client, s["id"])
    assert got["status"] == "cancelled"
    assert got["error"].startswith("Cancelled; the outline wasn't kept: The writer") and "declined" in got["error"]


def test_a_cancel_with_no_failure_keeps_the_plain_cancelled_message(auth_client, monkeypatch):
    from app.services import sound_studio

    from .test_sound_scripts import _cancel_during

    _cancel_during(1, monkeypatch)
    s = _new(auth_client)
    assert auth_client.post(f"/api/sound/stories/{s['id']}/write").status_code == 202
    got = _story(auth_client, s["id"])
    assert got["status"] == "cancelled" and got["error"] == sound_studio.CANCELLED


# ---------- item 3: the judges' reply budget ----------


def _judge_reply(content: str, finish: str = "stop") -> httpx.Response:
    return httpx.Response(200, json={"id": "gen-1", "model": "acme/planner-1", "usage": {"cost": 0.0004},
                                     "choices": [{"message": {"content": content}, "finish_reason": finish}]})


@pytest.mark.parametrize("method, verdict, key", [
    ("classify_minor", '{"minor_sexual": false, "reason": "adults"}', "minor_sexual"),
    ("classify_explicit", '{"explicit": false, "reason": "plain"}', "explicit"),
])
@pytest.mark.parametrize("first", [
    lambda: _judge_reply("", "length"),
    lambda: _judge_reply("", "stop"),
    lambda: _judge_reply('{"minor_sexual": fa', "length"),
])
@respx.mock
def test_a_judge_reply_out_of_room_is_asked_again_with_more_room(client, monkeypatch, no_wait, method, verdict,
                                                                  key, first):
    from app.providers.openrouter_chat import JUDGE_TOKENS

    _real_planner(monkeypatch)
    route = respx.post(f"{OR}/chat/completions").mock(side_effect=[first(), _judge_reply(verdict)])
    out = getattr(_provider(), method)("Two adults talk.")
    assert out[key] is False
    rooms = [json.loads(c.request.content)["max_tokens"] for c in route.calls]
    assert rooms == list(JUDGE_TOKENS) and JUDGE_TOKENS[0] >= 1000 and JUDGE_TOKENS[1] > JUDGE_TOKENS[0]


@respx.mock
def test_two_empty_judge_replies_still_fail_closed(client, monkeypatch, no_wait):
    from app.services import minor_check, safety

    from .test_minor_check import ADULT_SEXUAL

    _real_planner(monkeypatch)
    route = respx.post(f"{OR}/chat/completions").mock(side_effect=[
        _judge_reply("", "length"), _judge_reply("", "length")])
    with pytest.raises(ValueError):
        _provider().classify_minor("Two adults kiss.")
    assert route.call_count == 2
    route.side_effect = [_judge_reply("", "length"), _judge_reply("", "length")]
    with pytest.raises(minor_check.CheckUnavailable):
        minor_check._classify(ADULT_SEXUAL, "")
    route.side_effect = [_judge_reply("", "length"), _judge_reply("", "length")]
    with pytest.raises(safety.UnsafePromptError) as e:
        minor_check.check_generation(ADULT_SEXUAL, kind="image")
    assert "couldn't run" in str(e.value)


@respx.mock
def test_a_judge_refusal_or_a_whole_wrong_reply_is_not_asked_again(client, monkeypatch, no_wait):
    from app.providers.openrouter_chat import ModelDeclined

    _real_planner(monkeypatch)
    route = respx.post(f"{OR}/chat/completions").mock(side_effect=[_judge_reply("I can't help with that.")])
    with pytest.raises(ModelDeclined):
        _provider().classify_minor("Two adults kiss.")
    route.side_effect = [_judge_reply('{"verdict": "fine"}')]
    with pytest.raises(ValueError):
        _provider().classify_minor("Two adults kiss.")
    assert route.call_count == 2


# ---------- review fix: no further paid call after a Cancel ----------


@pytest.mark.parametrize("adult", [False, True])
def test_ask_makes_no_more_calls_once_the_story_is_cancelled(client, monkeypatch, adult):
    """A Cancel during an out-of-room (or refused) first call: no re-ask and
    no adult fallback, each a new paid call; the reply in hand stands."""
    from app.providers.openrouter_chat import WriterOutOfRoom
    from app.services import sound_studio

    oor = WriterOutOfRoom("x", "length")
    call, calls = _fake_writer(monkeypatch, {"acme/writer": [oor, "MARA: Hello."],
                                             "acme/fallback": ["MARA: Hello."]}, adult=adult)
    with pytest.raises(sound_studio.WriterRefused) as e:
        sound_studio._ask(call, alive=lambda: False)
    assert e.value.reason == "out_of_room" and str(e.value) == sound_studio.OUT_OF_ROOM
    assert calls == [("acme/writer", 1.0)]
    call, calls = _fake_writer(monkeypatch, {"acme/writer": ["   "], "acme/fallback": ["MARA: Hello."]},
                               adult=adult)
    with pytest.raises(sound_studio.WriterRefused) as e:
        sound_studio._ask(call, alive=lambda: False)
    assert e.value.reason == "declined" and len(calls) == 1


@pytest.mark.parametrize("adult", [False, True])
def test_a_cancel_during_an_out_of_room_chapter_makes_exactly_one_call(auth_client, monkeypatch, adult):
    from app import config
    from app.services import content_mode

    monkeypatch.setattr(content_mode, "adult_mode", lambda: adult)
    monkeypatch.setenv("ADULT_TEXT_FALLBACK_MODEL", "acme/fallback")
    config.get_settings.cache_clear()
    rooms = _chapter_out_of_room(monkeypatch, 1, times=1, cancel=True)
    s = _new(auth_client)
    assert auth_client.post(f"/api/sound/stories/{s['id']}/write").status_code == 202
    got = _story(auth_client, s["id"])
    assert rooms == [1.0]
    assert got["status"] == "cancelled" and got["error"].startswith("Cancelled; chapter 2 wasn't kept: The writer ran")
    assert got["writer"]["chapters_done"] == 1 and got["script_text"].count("# Chapter") == 1

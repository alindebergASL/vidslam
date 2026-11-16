"""Room for a long story facts reply (#57 review, finding 8).

The story facts call had max_tokens=800: a window naming more than about
15-18 people (a history of 56 presidents and first ladies puts 41 in its
first window) came back as cut-off JSON, read as "the check couldn't run",
so a long voiceover or lip-sync about many adults was paused. The reply
now gets far more room, and one cut off for room is asked for once more
with more, not read as unavailable.
"""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.providers.openrouter_chat import MINOR_CHECK_SYSTEM, STORY_FACTS_SYSTEM

from .test_minor_check import OR, _real_planner
from .test_sound_story_round11 import _mode, _voice
from .test_sound_story_round12 import FIG, HISTORY, _adults

WINDOW_41 = FIG[:41]


def _facts_json(names) -> str:  # noqa: ANN001
    return json.dumps({**_adults(names), "narrator": None})


class FakeModel:
    """A planner that writes the whole facts reply, cut off at its room
    (about 4 characters a token) with finish_reason "length", as a real
    one does. `room_floor`: a reply is also cut off below this room (a
    model that runs out sooner); `report_length`: whether it says so."""

    def __init__(self, names, *, room_floor: int = 0, report_length: bool = True):  # noqa: ANN001
        self.names = names
        self.room_floor = room_floor
        self.report_length = report_length
        self.facts_rooms: list[int] = []
        self.verdicts = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system = body["messages"][0]["content"]
        if system == MINOR_CHECK_SYSTEM:
            self.verdicts += 1
            return self._reply('{"minor_sexual": false, "reason": "adults"}', "stop")
        assert system == STORY_FACTS_SYSTEM
        room = int(body["max_tokens"])
        self.facts_rooms.append(room)
        full = _facts_json(self.names)
        cut = room * 4 if room >= self.room_floor else 200
        if len(full) <= cut:
            return self._reply(full, "stop")
        return self._reply(full[:cut], "length" if self.report_length else "stop")

    @staticmethod
    def _reply(content: str, finish: str) -> httpx.Response:
        return httpx.Response(200, json={"id": "gen-1", "model": "acme/planner-1", "usage": {"cost": 0.0004},
                                         "choices": [{"message": {"content": content}, "finish_reason": finish}]})


def _provider():
    from app.providers.openrouter_chat import OpenRouterChatProvider

    return OpenRouterChatProvider(model="acme/planner-1")


@respx.mock
def test_a_window_naming_41_people_is_read_in_one_call(client, monkeypatch):
    _real_planner(monkeypatch)
    fake = FakeModel(WINDOW_41)
    respx.post(f"{OR}/chat/completions").mock(side_effect=fake)
    out = _provider().story_characters("Story text naming 41 people.")
    assert [c["name"] for c in out["characters"]] == WINDOW_41
    assert fake.facts_rooms == [fake.facts_rooms[0]] and fake.facts_rooms[0] >= 4000


@pytest.mark.parametrize("report_length", [True, False])
@respx.mock
def test_a_reply_cut_off_for_room_is_asked_again_with_more(client, monkeypatch, report_length):
    """finish_reason "length", or JSON cut off with no reason given."""
    _real_planner(monkeypatch)
    fake = FakeModel(WINDOW_41, room_floor=10_000, report_length=report_length)
    respx.post(f"{OR}/chat/completions").mock(side_effect=fake)
    out = _provider().story_characters("Story text naming 41 people.")
    assert len(out["characters"]) == 41
    assert len(fake.facts_rooms) == 2 and fake.facts_rooms[1] > fake.facts_rooms[0]


@respx.mock
def test_a_reply_still_cut_off_with_more_room_is_unreadable(client, monkeypatch):
    _real_planner(monkeypatch)
    fake = FakeModel(WINDOW_41, room_floor=10 ** 9)
    respx.post(f"{OR}/chat/completions").mock(side_effect=fake)
    with pytest.raises(ValueError):
        _provider().story_characters("Story text naming 41 people.")
    assert len(fake.facts_rooms) == 2


@respx.mock
def test_a_finished_but_unreadable_reply_is_not_asked_again(client, monkeypatch):
    _real_planner(monkeypatch)
    route = respx.post(f"{OR}/chat/completions").mock(return_value=FakeModel._reply("no JSON here", "stop"))
    with pytest.raises(ValueError):
        _provider().story_characters("Story text.")
    assert route.call_count == 1


@pytest.mark.parametrize("mode", ["default", "adult"])
@respx.mock
def test_a_long_voiceover_about_56_presidents_is_made_with_a_real_planner(client, monkeypatch, mode):
    """The whole path (check_long's facts, then each window judged), with
    the planner's real reply format: not paused as "couldn't run"."""
    _real_planner(monkeypatch)
    _mode(monkeypatch, mode)
    assert len(HISTORY) > 6000
    fake = FakeModel(FIG)
    respx.post(f"{OR}/chat/completions").mock(side_effect=fake)
    _voice(HISTORY)
    assert fake.facts_rooms and fake.verdicts

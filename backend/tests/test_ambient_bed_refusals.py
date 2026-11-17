"""A refused (or paused) sound or music prompt is a 422 with the check's
message, never a bare 500 (#57 review, finding 3).

The guarded sound and music providers raise safety.UnsafePromptError (the
minors check, its word filter, or the check not running); the bed
endpoints caught only the explicit check's error, so everything else was a
500 {"error_type": "UnsafePromptError"} with no message.
"""
from __future__ import annotations

from .test_ambient_spend_music import _scene


def _check_down() -> None:
    from app.providers import mock_chat

    mock_chat.MINOR_REPLY = TimeoutError("the safety check timed out")


def test_sound_bed_with_the_check_down_is_a_422_with_its_message(auth_client):
    sid = _scene(auth_client, sound="rain", sound_prompt="gentle rain on a tin roof at the cabin")
    _check_down()
    r = auth_client.post(f"/api/ambient/{sid}/sound")
    assert r.status_code == 422, r.text
    assert "safety check couldn't run" in r.json()["detail"]


def test_music_bed_with_the_check_down_is_a_422_with_its_message(auth_client):
    sid = _scene(auth_client, music_prompt="slow warm piano for a cabin evening")
    _check_down()
    r = auth_client.post(f"/api/ambient/{sid}/music")
    assert r.status_code == 422, r.text
    assert "safety check couldn't run" in r.json()["detail"]


def test_music_bed_refused_by_the_minors_check_is_a_422(auth_client):
    from app.providers import mock_chat

    sid = _scene(auth_client, music_prompt="slow warm piano for a cabin evening")
    mock_chat.MINOR_REPLY = {"minor_sexual": True, "reason": "test"}
    r = auth_client.post(f"/api/ambient/{sid}/music")
    assert r.status_code == 422, r.text
    assert "refused by the safety check" in r.json()["detail"]


def test_any_route_that_lets_a_safety_refusal_through_answers_422(client):
    """The app-wide backstop (main.py): a route that doesn't catch it."""
    from app.main import app
    from app.services.safety import UnsafePromptError

    def boom() -> None:
        raise UnsafePromptError("This prompt was refused by the safety check: test.")

    app.add_api_route("/api/_test_refusal_backstop", boom, methods=["GET"])
    try:
        r = client.get("/api/_test_refusal_backstop")
    finally:
        app.router.routes[:] = [x for x in app.router.routes
                                if getattr(x, "path", "") != "/api/_test_refusal_backstop"]
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "This prompt was refused by the safety check: test."

"""Hook lab: an opening is checked with what is heard after it.

The new opening is spoken, then the base voiceover plays on from where the
opening ends (the window). The window can be moved anywhere, even inside
the original first sentence, so the rest of that sentence is heard after
the new opening. These tests make sure the minors check sees that, at every
step (create, change, Listen, Make, compose, suggestions), and that a
planner's refusal wrapped in JSON is a refusal, not "try again"."""
from __future__ import annotations

import pytest

from .test_hook_lab import (
    _adult_base,
    _base,
    _create,
    _db,
    _get,
    _jobs,
    _make,
    _set_hook,
    _waiting_variant,
)

# An adult base: fine on its own. "She is fifteen and" with the window just
# after its "and" is heard as "She is fifteen and tonight she strips naked
# for you!", while the stored script says "She is fifteen and. Follow for more."
_WIFE = "My wife is thirty and tonight she strips naked for you! Follow for more."
_SPLICE_HOOK = "She is fifteen and"


def _set_script(base_id: int, script: str) -> None:
    """The base was made with `script` (and its first sentence as the hook)."""
    from app import models
    from app.services.hook_lab import first_sentence

    db = _db()
    try:
        r = db.get(models.Render, base_id)
        r.composition_json = {**(r.composition_json or {}), "script": script, "hook": first_sentence(script)}
        db.commit()
    finally:
        db.close()


def _window_at(base_id: int, word: str) -> float:
    """When the base voiceover says `word`: its first sentence evenly paced
    up to where the lab finds that sentence's end."""
    from app import models
    from app.services import hook_lab

    db = _db()
    try:
        base = db.get(models.Render, base_id)
        script = hook_lab.base_script(db, base)
        end = hook_lab.find_hook_window(base, script)
    finally:
        db.close()
    rest_at = script.index(hook_lab.rest_of_script(script))
    return round(end * script.index(word) / rest_at, 2)


def _post(auth_client, pid, base_id, hook, window=None):
    body = {"base_render_id": base_id, "variants": [{"hook_text": hook, "style": "custom"}]}
    if window is not None:
        body["window_seconds"] = window
    return auth_client.post(f"/api/projects/{pid}/hook-lab/variants", json=body)


def _spliced_base(auth_client, script: str = _WIFE) -> tuple[int, int]:
    from app.services import minor_check

    pid, base_id = _base(auth_client)
    _set_script(base_id, script)
    minor_check.check_generation(script, kind="script")  # the base passes on its own
    return pid, base_id


# ---------- where the voiceover picks up ----------


def test_heard_cuts_follow_the_window_into_the_first_sentence():
    from app.services.hook_lab import heard_cuts

    script = _WIFE
    rest_at = script.index("Follow")
    at = script.index("tonight")
    # Evenly paced up to where the first sentence ends (4.0 s of 5.2 s).
    best, near = heard_cuts(script, 4.0 * at / rest_at, 5.2, 4.0)
    assert best == at and at in near
    assert script.index("My") not in near and rest_at not in near
    # At the end of the first sentence: the rest, as usual.
    assert heard_cuts(script, 4.0, 5.2, 4.0)[0] == rest_at
    # Not known where it ends: the whole voiceover evenly paced.
    best, _ = heard_cuts(script, 5.2 * at / len(script), 5.2)
    assert best == at
    # The opening fills the whole voiceover: nothing of the original is heard.
    assert heard_cuts(script, 5.2, 5.2, 4.0) == (0, [])


# ---------- the splice: the rest of the first sentence after the opening ----------


def test_an_opening_spliced_into_the_first_sentence_is_refused_everywhere(auth_client, tmp_path):
    from app import models
    from app.services import hook_lab, minor_check, safety

    pid, base_id = _spliced_base(auth_client)
    window = _window_at(base_id, "tonight")
    # The gap: the opening alone and the stored script both pass ...
    minor_check.check_generation(_SPLICE_HOOK, kind="script")
    minor_check.check_generation(hook_lab.replace_first_sentence(_WIFE, _SPLICE_HOOK), kind="script")
    # ... but what is heard doesn't.
    with pytest.raises(safety.UnsafeScriptError):
        minor_check.check_generation(f"{_SPLICE_HOOK} tonight she strips naked for you! Follow for more.",
                                     kind="script")

    # Create: refused at that window, and at the found one too (the opening
    # is checked in front of the whole original script).
    for w in (window, None):
        r = _post(auth_client, pid, base_id, _SPLICE_HOOK, w)
        assert r.status_code == 422 and "minor" in r.json()["detail"], r.text
    assert not auth_client.get(f"/api/projects/{pid}/hook-lab").json()["bases"][0]["variants"]

    # A plain opening at the same window is fine.
    made = _create(auth_client, pid, base_id, [{"hook_text": "Why stop now?", "style": "question"}], window=window)
    vid = made["variants"][0]["id"]
    # Change: refused, nothing stored.
    r = auth_client.patch(f"/api/hook-variants/{vid}", json={"hook_text": _SPLICE_HOOK})
    assert r.status_code == 422 and "minor" in r.json()["detail"]
    assert _get(models.HookVariant, vid).hook_text == "Why stop now?"

    # Got in anyway (written before this check): moving the window ...
    _set_hook(vid, _SPLICE_HOOK)
    r = auth_client.patch(f"/api/hook-variants/{vid}", json={"window_seconds": round(window + 0.1, 2)})
    assert r.status_code == 422 and "minor" in r.json()["detail"]
    assert _get(models.HookVariant, vid).window_seconds == window
    # ... Listen makes no voice ...
    r = auth_client.post(f"/api/hook-variants/{vid}/preview-audio")
    assert r.status_code == 422 and "minor" in r.json()["detail"]
    v = _get(models.HookVariant, vid)
    assert not v.audio_path and v.voicing_until is None
    db = _db()
    try:
        assert not hook_lab.raw_hook_path(db, db.get(models.VideoProject, pid), v).is_file()
    finally:
        db.close()
    # ... and Make fails it before anything is paid.
    assert _make(auth_client, pid, [vid]).status_code == 202
    v = _get(models.HookVariant, vid)
    assert v.status == "failed" and "minor" in v.error and not v.audio_path
    assert not _jobs(pid, "hook_shot") and not _jobs(pid, "hook_avatar")

    # Compose checks it again (one that got as far as its clips).
    vid2, rid2 = _waiting_variant(pid, base_id, [], tmp_path)
    db = _db()
    try:
        v2 = db.get(models.HookVariant, vid2)
        v2.hook_text, v2.window_seconds, v2.status = _SPLICE_HOOK, window, "ready"
        db.commit()
        assert hook_lab.compose_variant(db, vid2) is None
    finally:
        db.close()
    v2 = _get(models.HookVariant, vid2)
    assert v2.status == "failed" and "minor" in v2.error
    assert _get(models.Render, rid2).status == "failed"


def test_a_sexual_opening_before_a_minor_in_the_first_sentence_is_refused(auth_client):
    """The other way round: the minor is in the original's first sentence,
    the sexual words in the opening."""
    from app import models
    from app.services import hook_lab, minor_check

    pid, base_id = _spliced_base(auth_client, "Tonight we celebrate a girl who is 15 today. Follow for more.")
    hook = "Watch me strip naked for"
    minor_check.check_generation(hook, kind="script")
    minor_check.check_generation(
        hook_lab.replace_first_sentence("Tonight we celebrate a girl who is 15 today. Follow for more.", hook),
        kind="script")
    window = _window_at(base_id, "a girl")
    r = _post(auth_client, pid, base_id, hook, window)
    assert r.status_code == 422 and "minor" in r.json()["detail"]

    made = _create(auth_client, pid, base_id, [{"hook_text": "Big night for us?", "style": "question"}],
                   window=window)
    vid = made["variants"][0]["id"]
    _set_hook(vid, hook)
    r = auth_client.post(f"/api/hook-variants/{vid}/preview-audio")
    assert r.status_code == 422 and "minor" in r.json()["detail"]
    assert _make(auth_client, pid, [vid]).status_code == 202
    v = _get(models.HookVariant, vid)
    assert v.status == "failed" and "minor" in v.error and not v.audio_path


def test_a_phrase_formed_across_the_join_is_refused_only_where_it_is_heard(auth_client):
    """ "she is" + "sixteen tips" is an age only when joined there: the word
    filter sees the joins near the window, not only the whole script."""
    from app import models
    from app.services import hook_lab

    script = ("Here we go: sixteen tips that changed my whole life this year, for real. "
              "Watch her strip naked on camera. Follow for more.")
    pid, base_id = _spliced_base(auth_client, script)
    hook = "Tonight, she is"
    db = _db()
    try:
        base = db.get(models.Render, base_id)
        hook_lab.check_opening(db, base, hook)  # alone, in the script, in front of the whole script: fine
    finally:
        db.close()
    r = _post(auth_client, pid, base_id, hook, _window_at(base_id, "sixteen"))
    assert r.status_code == 422 and "minor" in r.json()["detail"]
    assert "checked together with the original voiceover" in r.json()["detail"]
    # Where the first sentence ends, "sixteen" isn't heard after it.
    assert _post(auth_client, pid, base_id, hook).status_code == 201


def test_the_ai_check_sees_the_opening_with_what_is_heard_after_it(auth_client, monkeypatch):
    """The replaced words ("my wife is thirty") can make the joined text read
    as being about an adult. The AI check also sees the opening with only the
    words heard after it."""
    from app.providers import mock_chat

    seen: list[str] = []

    def judge(self, text):
        seen.append(text)
        # A check misled by the adult in the replaced words.
        bad = "braces" in text and "bikini" in text and "thirty" not in text
        return {"minor_sexual": bad, "reason": "probe"}

    monkeypatch.setattr(mock_chat.MockChatProvider, "classify_minor", judge)
    script = "My wife is thirty and tonight she slips into a bikini for you! Follow for more."
    pid, base_id = _spliced_base(auth_client, script)
    hook = "She still wears braces and"
    r = _post(auth_client, pid, base_id, hook, _window_at(base_id, "tonight"))
    assert r.status_code == 422 and "minor" in r.json()["detail"]
    assert any("braces and tonight she slips into a bikini" in t for t in seen)
    # Where the first sentence ends, none of it is heard: fine.
    assert _post(auth_client, pid, base_id, hook).status_code == 201


def test_ordinary_openings_at_any_window_are_still_made(auth_client):
    """Only what can be heard is refused: a plain or adult opening over an
    adult script, with the window anywhere, still goes through to the end."""
    from app import models

    pid, base_id = _spliced_base(auth_client)
    inside = _window_at(base_id, "strips")  # "… tonight she | strips naked for you!"
    for hook in ("Why stop now?", "Watch her strip naked now."):
        for w in (inside, None):
            assert _post(auth_client, pid, base_id, hook, w).status_code == 201
    made = _create(auth_client, pid, base_id, [{"hook_text": "Why keep scrolling?", "style": "question"}],
                   window=inside)
    vid = made["variants"][0]["id"]
    assert auth_client.patch(f"/api/hook-variants/{vid}",
                             json={"window_seconds": round(inside + 0.2, 2)}).status_code == 200
    r = auth_client.post(f"/api/hook-variants/{vid}/preview-audio")
    assert r.status_code == 200, r.text
    assert _make(auth_client, pid, [vid]).status_code == 202
    assert _get(models.HookVariant, vid).status == "completed"


def test_suggestions_are_checked_with_what_is_heard_after_them(auth_client, monkeypatch):
    import json

    from app.providers import mock_chat

    pid, base_id = _spliced_base(auth_client)
    # About as many words as the opening (11): the first is fine in front of
    # the rest ("Follow for more.") but not of the first sentence it replaces.
    first = "Did you know she is fifteen and loves this channel?"
    second = "Honestly, my wife is thirty and tonight she is all yours!"
    reply = json.dumps({"hooks": [{"style": "question", "text": first},
                                  {"style": "bold_claim", "text": second}]})
    monkeypatch.setattr(mock_chat.MockChatProvider, "hook_ideas", lambda self, **kw: reply)
    r = auth_client.post(f"/api/projects/{pid}/hook-lab/suggest", json={"base_render_id": base_id, "n": 2})
    assert r.status_code == 200, r.text
    assert [i["text"] for i in r.json()["suggestions"]] == [second]


# ---------- a planner refusal wrapped in JSON ----------


@pytest.mark.parametrize("reply, why", [
    ('"REFUSED_MINOR"', "minor"),
    ('{"hooks": "REFUSED_MINOR"}', "minor"),
    ('{"error": "REFUSED_MINOR"}', "minor"),
    ('{"hooks": [], "refusal": "REFUSED_MINOR"}', "minor"),
    ('{"hooks": [{"style": "question", "text": "REFUSED_MINOR"}]}', "minor"),
    ('```json\n{"hooks": "REFUSED_MINOR"}\n```', "minor"),
    ('{"hooks": [], "refusal": "DECLINED: the script sexualises a minor"}', "minor"),
    ('{"hooks": "DECLINED"}', "declined"),
    ('{"hooks": [], "refusal": "DECLINED"}', "declined"),
    ('{"hooks": [{"style": "question", "text": "DECLINED"}]}', "declined"),
    ('{"hooks": [], "reason": "I can\'t help with this request."}', "declined"),
    ('{"hooks": [], "refusal": true}', "declined"),
])
def test_a_refusal_wrapped_in_json_is_a_refusal(auth_client, monkeypatch, reply, why):
    from app.providers import mock_chat

    pid, base_id = _adult_base(auth_client)
    monkeypatch.setattr(mock_chat.MockChatProvider, "hook_ideas", lambda self, **kw: reply)
    r = auth_client.post(f"/api/projects/{pid}/hook-lab/suggest", json={"base_render_id": base_id})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert why in detail and "Try again" not in detail


def test_an_opening_that_says_declined_is_still_an_opening(auth_client, monkeypatch):
    from app.providers import mock_chat

    pid, base_id = _adult_base(auth_client)
    url = f"/api/projects/{pid}/hook-lab/suggest"
    reply = '{"hooks": [{"style": "bold_claim", "text": "Loan DECLINED, then approved?"}]}'
    monkeypatch.setattr(mock_chat.MockChatProvider, "hook_ideas", lambda self, **kw: reply)
    r = auth_client.post(url, json={"base_render_id": base_id, "n": 1})
    assert r.status_code == 200, r.text
    assert [i["text"] for i in r.json()["suggestions"]] == ["Loan DECLINED, then approved?"]
    # Nothing usable and no refusal: still "try again".
    monkeypatch.setattr(mock_chat.MockChatProvider, "hook_ideas", lambda self, **kw: '{"hooks": []}')
    r = auth_client.post(url, json={"base_render_id": base_id})
    assert r.status_code == 502 and "Try again" in r.json()["detail"]


def test_the_hook_ideas_instructions_say_how_to_refuse_in_json():
    from app.providers.openrouter_chat import HOOK_IDEAS_SYSTEM

    assert '"refusal"' in HOOK_IDEAS_SYSTEM and "REFUSED_MINOR" in HOOK_IDEAS_SYSTEM

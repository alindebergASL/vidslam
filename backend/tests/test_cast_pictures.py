"""Pictures added to the card of a Cast character ticked "Adult content
allowed" are checked automatically (services/cast_pictures.py): every way
a picture lands on a card queues the check (ticked characters only, never
for a logo), the job covers what the picture model judged adult, lists
what it wouldn't judge, takes the tick off for a picture that looks under
18, leaves the filter on when it can't run, writes nothing when the tick
changed meanwhile, and the jobs don't pile up."""
from __future__ import annotations

import pytest

from app.providers import mock_chat
from app.services import cast_pictures, content_mode, minor_check
from app.workers import queue

from .test_character_check import _avatar, _covered, _tick, _ticked, _upload


def _status(client) -> dict:
    return client.get("/api/settings/content").json()


def _drain() -> None:
    assert queue.wait_background(60)


@pytest.fixture
def queued(monkeypatch) -> list[int]:
    """The characters a check was queued for (the check itself doesn't run)."""
    calls: list[int] = []
    monkeypatch.setattr(cast_pictures, "queue_check", lambda avatar_id: calls.append(avatar_id) or "job")
    return calls


def _ticked_anna(client) -> tuple[int, int]:
    aid = _avatar(client, "Anna", description="Anna, 34, accountant")
    first = _upload(client, aid, "first.png")
    assert _tick(client, aid).status_code == 200 and _covered(first)
    return aid, first


# ---------- the job ----------


def test_a_picture_added_to_a_ticked_card_is_checked_automatically(auth_client):
    aid, _ = _ticked_anna(auth_client)
    calls = list(mock_chat.PICTURE_CALLS)
    new = _upload(auth_client, aid, "new.png")
    _drain()
    assert mock_chat.PICTURE_CALLS == [*calls, new]  # only the new picture is asked about
    assert _covered(new) and aid in _ticked()
    s = _status(auth_client)
    assert s["adult_ok_unchecked"] == {} and s["adult_ok_checking"] == [] and s["adult_ok_declined"] == {}


def test_a_picture_that_looks_under_18_takes_the_tick_off_and_the_card_says_why(auth_client):
    aid, first = _ticked_anna(auth_client)
    mock_chat.PICTURE_REPLY = {"minor": True, "reason": "A child."}
    young = _upload(auth_client, aid, "young.png")
    _drain()
    assert aid not in _ticked() and not _covered(young) and not _covered(first)
    notice = _status(auth_client)["adult_ok_notices"][str(aid)]
    assert notice.startswith("Adult content is no longer allowed for Anna") and '("young.png")' in notice
    assert "It said: A child." in notice and "Safety check memory" in notice
    # Remembered: ticking again is refused without asking.
    mock_chat.PICTURE_REPLY = None
    r = _tick(auth_client, aid)
    assert r.status_code == 422 and '"young.png"' in r.json()["detail"]
    # Unticking (or a tick that holds) clears the notice.
    assert _tick(auth_client, aid, False).status_code == 200
    assert _status(auth_client)["adult_ok_notices"] == {}


def test_a_picture_the_model_wont_judge_stays_uncovered_and_is_listed(auth_client):
    from app.providers.openrouter_chat import ModelDeclined

    aid, first = _ticked_anna(auth_client)
    mock_chat.PICTURE_REPLY = ModelDeclined("no")
    shy = _upload(auth_client, aid, "shy.png")
    _drain()
    assert aid in _ticked() and _covered(first) and not _covered(shy)
    s = _status(auth_client)
    assert s["adult_ok_declined"] == {str(aid): [{"id": shy, "name": "shy.png"}]} and s["adult_ok_unchecked"] == {}
    # The next added picture's check doesn't ask about it again ...
    mock_chat.PICTURE_REPLY = None
    calls = list(mock_chat.PICTURE_CALLS)
    later = _upload(auth_client, aid, "later.png")
    _drain()
    assert mock_chat.PICTURE_CALLS == [*calls, later] and _covered(later) and not _covered(shy)
    # ... "Check new pictures" does (a decline isn't remembered).
    r = _tick(auth_client, aid)
    assert r.status_code == 200 and _covered(shy) and r.json()["adult_ok_declined"] == {}


@pytest.mark.parametrize("reply", [RuntimeError("HTTP 500"), {"minor": "no"}])
def test_when_the_check_cant_run_the_picture_keeps_the_filter(auth_client, reply):
    aid, first = _ticked_anna(auth_client)
    mock_chat.PICTURE_REPLY = reply
    new = _upload(auth_client, aid, "new.png")
    _drain()
    assert aid in _ticked() and _covered(first) and not _covered(new)
    s = _status(auth_client)
    assert s["adult_ok_unchecked"] == {str(aid): 1} and s["adult_ok_declined"] == {}
    assert s["adult_ok_checking"] == []
    mock_chat.PICTURE_REPLY = None
    assert _tick(auth_client, aid).status_code == 200 and _covered(new)  # the button retries


def test_the_job_writes_nothing_when_the_tick_changed_meanwhile(auth_client, monkeypatch):
    aid, first = _ticked_anna(auth_client)
    monkeypatch.setattr(cast_pictures, "queue_check", lambda avatar_id: "")
    new = _upload(auth_client, aid, "new.png")
    real = minor_check.check_pictures

    def ticked_again_meanwhile(name, assets):
        content_mode.set_avatar_adult_ok(aid, True, card=content_mode.tick_row(aid)["card"], assets=[first])
        return real(name, assets)

    monkeypatch.setattr(minor_check, "check_pictures", ticked_again_meanwhile)
    assert cast_pictures.run(aid) == "changed"
    assert _covered(first) and not _covered(new)

    # Unticked meanwhile: nothing written either.
    def unticked_meanwhile(name, assets):
        content_mode.set_avatar_adult_ok(aid, False)
        return real(name, assets)

    monkeypatch.setattr(minor_check, "check_pictures", unticked_meanwhile)
    assert cast_pictures.run(aid) == "changed"
    assert content_mode.tick_row(aid) is None


def test_the_job_writes_nothing_when_the_card_changed(auth_client, monkeypatch):
    from app import models
    from app.db import SessionLocal

    aid, first = _ticked_anna(auth_client)
    monkeypatch.setattr(cast_pictures, "queue_check", lambda avatar_id: "")
    new = _upload(auth_client, aid, "new.png")
    real = minor_check.check_pictures

    def edited_meanwhile(name, assets):
        with SessionLocal() as db:
            db.get(models.Avatar, aid).persona = "edited outside the app"
            db.commit()
        return real(name, assets)

    monkeypatch.setattr(minor_check, "check_pictures", edited_meanwhile)
    assert cast_pictures.run(aid) == "changed"
    assert not _covered(new) and content_mode.tick_row(aid)["assets"] == [first]
    # A card that no longer matches its tick isn't checked at all.
    assert cast_pictures.run(aid) == "stale"


def test_a_picture_under_18_takes_the_tick_off_even_if_it_changed_meanwhile(auth_client, monkeypatch):
    aid, first = _ticked_anna(auth_client)
    monkeypatch.setattr(cast_pictures, "queue_check", lambda avatar_id: "")
    _upload(auth_client, aid, "young.png")
    mock_chat.PICTURE_REPLY = {"minor": True, "reason": "A child."}
    real = minor_check.check_pictures

    def ticked_again_meanwhile(name, assets):
        content_mode.set_avatar_adult_ok(aid, True, card=content_mode.tick_row(aid)["card"], assets=[first])
        return real(name, assets)

    monkeypatch.setattr(minor_check, "check_pictures", ticked_again_meanwhile)
    assert cast_pictures.run(aid) == "minor" and aid not in _ticked()


def test_a_deleted_picture_or_an_untick_meanwhile_leaves_nothing_behind(auth_client, monkeypatch):
    aid, first = _ticked_anna(auth_client)
    monkeypatch.setattr(cast_pictures, "queue_check", lambda avatar_id: "")
    young = _upload(auth_client, aid, "young.png")
    mock_chat.PICTURE_REPLY = {"minor": True, "reason": "A child."}
    real = minor_check.check_pictures

    def deleted_meanwhile(name, assets):
        out = real(name, assets)
        assert auth_client.delete(f"/api/assets/{young}").status_code == 204
        return out

    monkeypatch.setattr(minor_check, "check_pictures", deleted_meanwhile)
    assert cast_pictures.run(aid) == "minor" and aid in _ticked()  # it's gone from the card: no untick
    assert _status(auth_client)["adult_ok_notices"] == {}
    monkeypatch.setattr(minor_check, "check_pictures", real)
    _tick(auth_client, aid, False)
    assert cast_pictures.run(aid) == "unticked"


CHILD_REFUSAL = "I'm sorry, but I can't help with this image because it appears to show a child."
PLAIN_REFUSAL = "I'm sorry, but I can't help with that."


def test_a_refusal_that_names_a_child_takes_the_tick_off(auth_client):
    """A picture model that refuses because the picture shows a child has
    judged it under 18: never a plain decline that leaves the tick on."""
    aid, first = _ticked_anna(auth_client)
    mock_chat.PICTURE_REPLY = CHILD_REFUSAL
    young = _upload(auth_client, aid, "young.png")
    _drain()
    assert aid not in _ticked() and not _covered(young) and not _covered(first)
    notice = _status(auth_client)["adult_ok_notices"][str(aid)]
    assert '("young.png")' in notice and "appears to show a child" in notice
    assert _status(auth_client)["adult_ok_declined"] == {}


def test_a_refusal_that_names_no_minor_is_a_decline(auth_client):
    aid, first = _ticked_anna(auth_client)
    mock_chat.PICTURE_REPLY = PLAIN_REFUSAL
    shy = _upload(auth_client, aid, "shy.png")
    _drain()
    assert aid in _ticked() and _covered(first) and not _covered(shy)
    assert _status(auth_client)["adult_ok_declined"] == {str(aid): [{"id": shy, "name": "shy.png"}]}


def test_a_refusal_found_meanwhile_beats_the_jobs_yes(auth_client, monkeypatch):
    """The job judged the new picture adult, but a check racing it (a tick,
    or the same file on another card) stored "under 18" first: the refusal
    stands, and the tick comes off."""
    aid, first = _ticked_anna(auth_client)
    monkeypatch.setattr(cast_pictures, "queue_check", lambda avatar_id: "")
    new = _upload(auth_client, aid, "new.png")
    real = minor_check._classify_picture

    def refused_meanwhile(asset):  # noqa: ANN001
        out = real(asset)
        minor_check._store(minor_check.picture_hash(asset), "minor_sexual", out[2], "A child.")
        return out

    monkeypatch.setattr(minor_check, "_classify_picture", refused_meanwhile)
    assert cast_pictures.run(aid) == "minor"
    assert aid not in _ticked() and not _covered(new) and not _covered(first)
    assert "It said: A child." in _status(auth_client)["adult_ok_notices"][str(aid)]


def test_a_covered_file_found_under_18_on_another_card_ends_the_tick(auth_client, monkeypatch):
    """The same file on two cards, judged on both at once: card A's check
    stores "under 18" after card B's covered it. B's tick stops holding at
    once (nothing of B's skips the filter), and ticking B again says why."""
    from app import models
    from app.db import SessionLocal

    monkeypatch.setattr(cast_pictures, "queue_check", lambda avatar_id: "")
    b, first = _ticked_anna(auth_client)
    shared = _upload(auth_client, b, "shared.png")
    assert _tick(auth_client, b).status_code == 200 and _covered(shared)
    a = _avatar(auth_client, "Bea", description="Bea, 30, nurse")
    on_a = _upload(auth_client, a, "shared.png")  # the same bytes
    with SessionLocal() as db:  # card A's check, which asked before B's "ok" was stored
        pic = db.get(models.Asset, on_a)
        minor_check._store(minor_check.picture_hash(pic), "minor_sexual", minor_check._picture_identity(),
                           "A child.")
    assert b not in _ticked() and not _covered(shared) and not _covered(first)
    assert _status(auth_client)["adult_ok_stale"] == [b]
    r = _tick(auth_client, b)
    assert r.status_code == 422 and '"shared.png"' in r.json()["detail"]
    assert content_mode.tick_row(b) is None


def test_the_job_rereads_refusals_before_it_merges(auth_client, monkeypatch):
    aid, first = _ticked_anna(auth_client)
    monkeypatch.setattr(cast_pictures, "queue_check", lambda avatar_id: "")
    _upload(auth_client, aid, "new.png")
    real = minor_check.check_pictures

    def first_refused_meanwhile(name, assets):  # noqa: ANN001
        out = real(name, assets)
        from app import models
        from app.db import SessionLocal

        with SessionLocal() as db:
            minor_check._store(minor_check.picture_hash(db.get(models.Asset, first)), "minor_sexual",
                               minor_check._picture_identity(), "A child.")
        return out

    monkeypatch.setattr(minor_check, "check_pictures", first_refused_meanwhile)
    assert cast_pictures.run(aid) == "minor"
    assert content_mode.tick_row(aid) is None
    assert '("first.png")' in _status(auth_client)["adult_ok_notices"][str(aid)]


# ---------- what queues it ----------


def test_an_upload_queues_the_check_for_ticked_characters_only(auth_client, queued):
    aid, _ = _ticked_anna(auth_client)
    bob = _avatar(auth_client, "Bob", description="a carpenter, 40")
    assert queued == []  # the tick itself checked the pictures
    _upload(auth_client, bob, "bob.png")
    assert queued == []  # not ticked
    from .conftest import make_png_bytes

    r = auth_client.post(f"/api/avatars/{aid}/assets", files={"file": ("logo.png", make_png_bytes(), "image/png")},
                         data={"asset_type": "logo", "rights_confirmed": "true"})
    assert r.status_code == 201 and queued == []  # a logo is never covered
    _upload(auth_client, aid, "second.png")
    assert queued == [aid] and mock_chat.PICTURE_CALLS == [_ticked_first_picture(aid)]  # queued, not run here


def _ticked_first_picture(aid: int) -> int:
    return sorted(content_mode.tick_row(aid)["assets"])[0]


def test_a_stale_tick_queues_nothing(auth_client, queued):
    from app import models
    from app.db import SessionLocal

    aid, _ = _ticked_anna(auth_client)
    with SessionLocal() as db:
        db.get(models.Avatar, aid).persona = "edited outside the app"
        db.commit()
    _upload(auth_client, aid, "second.png")
    assert queued == []


def test_a_logo_moved_to_a_picture_slot_queues_the_check(auth_client, queued):
    from .conftest import make_png_bytes

    aid, _ = _ticked_anna(auth_client)
    r = auth_client.post(f"/api/avatars/{aid}/assets", files={"file": ("logo.png", make_png_bytes(), "image/png")},
                         data={"asset_type": "logo", "rights_confirmed": "true"})
    logo = r.json()["id"]
    assert auth_client.patch(f"/api/assets/{logo}", json={"asset_type": "outfit_sheet"}).status_code == 200
    assert queued == [aid]
    assert auth_client.patch(f"/api/assets/{logo}", json={"asset_type": "lifestyle"}).status_code == 200
    assert queued == [aid]  # already a picture: nothing new
    assert auth_client.patch(f"/api/assets/{logo}", json={"asset_type": "logo"}).status_code == 200
    assert queued == [aid]


def _studio_result(tmp_path, aid: int, name: str) -> int:
    from app import models
    from app.db import SessionLocal

    from .conftest import make_png_bytes

    f = tmp_path / name
    f.write_bytes(make_png_bytes((96, 96), (len(name) * 20 % 255, 80, 40)))
    with SessionLocal() as db:
        job = models.AssetGenerationJob(owner_kind="avatar", owner_id=aid, output_kind="image", prompt="p",
                                        status="completed", result_path=str(f))
        db.add(job)
        db.commit()
        return job.id


def test_studio_saves_queue_the_check(auth_client, queued, tmp_path):
    aid, _ = _ticked_anna(auth_client)
    bob = _avatar(auth_client, "Bob", description="a carpenter, 40")
    one = _studio_result(tmp_path, aid, "one.png")
    assert auth_client.post(f"/api/studio/jobs/{one}/save", json={"asset_type": "lifestyle"}).status_code == 200
    assert queued == [aid]
    # A result saved to another (unticked) character's card, or as a logo: nothing.
    two = _studio_result(tmp_path, aid, "two.png")
    r = auth_client.post(f"/api/studio/jobs/{two}/save", json={"owner_kind": "avatar", "owner_id": bob})
    assert r.status_code == 200 and queued == [aid]
    three = _studio_result(tmp_path, aid, "three.png")
    assert auth_client.post(f"/api/studio/jobs/{three}/save", json={"asset_type": "logo"}).status_code == 200
    assert queued == [aid]
    # Bulk save (a character sheet's views, cutouts and upscales go the same way).
    ids = [_studio_result(tmp_path, bob, f"b{i}.png") for i in range(3)]
    r = auth_client.post("/api/studio/jobs/bulk-save", json={
        "ids": ids, "owner_kind": "avatar", "owner_id": aid, "asset_type": "reference_sheet"})
    assert r.status_code == 200 and len(r.json()["saved"]) == 3
    assert queued == [aid] * 4
    assert mock_chat.PICTURE_CALLS == [_ticked_first_picture(aid)]


def test_a_file_the_startup_repair_makes_a_picture_queues_the_check(auth_client, queued):
    from app import models
    from app.db import SessionLocal
    from app.main import _repair_image_types

    aid, _ = _ticked_anna(auth_client)
    second = _upload(auth_client, aid, "second.png")
    queued.clear()
    with SessionLocal() as db:
        db.get(models.Asset, second).mime_type = "application/octet-stream"
        db.commit()
    _repair_image_types()
    assert queued == [aid]


# ---------- no pile-up, and never in the request ----------


class _FakeRQ:
    """RQ's queue as enqueue_once uses it. Job ids go through RQ's own check
    (Job.fetch and enqueue raise ValueError for an id RQ won't take)."""

    def __init__(self) -> None:
        self.jobs: dict[str, str] = {}
        self.enqueued: list[str] = []
        self.at_front: list[bool] = []

    def enqueue(self, func, *args, job_id, at_front=False, **kw):  # noqa: ANN001
        from rq.job import validate_job_id

        validate_job_id(job_id)
        self.jobs[job_id] = "queued"
        self.enqueued.append(job_id)
        self.at_front.append(at_front)
        return type("Job", (), {"id": job_id})()

    def status(self, job_id: str):  # noqa: ANN201
        from rq.job import validate_job_id

        validate_job_id(job_id)
        return self.jobs.get(job_id)


def _fake_rq(monkeypatch) -> _FakeRQ:
    rq = _FakeRQ()
    monkeypatch.setattr(queue, "_try_redis_queue", lambda: rq)
    monkeypatch.setattr(queue, "_keyed_status", rq.status)
    monkeypatch.setattr(queue, "_drop_keyed", lambda job_id: rq.jobs.pop(job_id, None))
    monkeypatch.setattr(queue, "queue_entry_state", lambda job_id: "alive" if rq.status(job_id) in (
        "queued", "started", "deferred", "scheduled") else "gone")
    return rq


def test_the_job_ids_are_ones_rq_takes():
    from rq.job import validate_job_id

    for job_id in queue.keyed_job_ids(cast_pictures.job_key(12)):
        validate_job_id(job_id)  # RQ 3 refuses "cast-pictures-12.next"
    with pytest.raises(ValueError):
        queue.keyed_job_ids("cast.pictures")


def test_one_check_waits_per_character_with_redis(auth_client, monkeypatch):
    rq = _fake_rq(monkeypatch)
    aid, _ = _ticked_anna(auth_client)
    key = cast_pictures.job_key(aid)
    following = f"{key}-next"
    for i in range(5):
        _upload(auth_client, aid, f"p{i}.png")
    assert rq.enqueued == [key]  # one waiting job sees all five
    assert rq.at_front == [True]  # ahead of the renders waiting on the default queue
    assert _status(auth_client)["adult_ok_checking"] == [aid]
    rq.jobs[key] = "started"  # it may have read the card before the next one
    _upload(auth_client, aid, "q1.png")
    _upload(auth_client, aid, "q2.png")
    assert rq.enqueued == [key, following]
    assert _status(auth_client)["adult_ok_checking"] == [aid]
    rq.jobs[following] = "started"
    _upload(auth_client, aid, "q3.png")
    assert rq.enqueued == [key, following]  # two under way: not queued again
    rq.jobs[key] = "finished"
    _upload(auth_client, aid, "q4.png")
    assert rq.enqueued == [key, following, key]  # a finished id is used again
    assert len(mock_chat.PICTURE_CALLS) == 1  # nothing ran in the requests


def test_a_check_still_waiting_in_the_queue_keeps_saying_checking(auth_client, monkeypatch):
    """A check can wait for a render running on the default queue: past
    CHECKING_TTL it still says "checking" while its job is waiting or
    running, and stops once the job is gone."""
    from datetime import datetime, timedelta, timezone

    from app.services.model_registry import put_setting

    rq = _fake_rq(monkeypatch)
    aid, _ = _ticked_anna(auth_client)
    key = cast_pictures.job_key(aid)
    _upload(auth_client, aid, "new.png")
    old = datetime.now(timezone.utc) - timedelta(seconds=content_mode.CHECKING_TTL_SECONDS + 60)
    put_setting(f"{content_mode.CHECKING_PREFIX}{aid}", {"at": old.isoformat()})
    assert _status(auth_client)["adult_ok_checking"] == [aid]  # still queued
    rq.jobs[key] = "started"
    assert _status(auth_client)["adult_ok_checking"] == [aid]  # running
    rq.jobs[key] = "failed"
    assert _status(auth_client)["adult_ok_checking"] == []  # it died: the card offers the button


def test_without_redis_a_waiting_check_keeps_saying_checking(auth_client, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app.services.model_registry import put_setting

    monkeypatch.setattr(queue, "_run_in_background", lambda func, *a, **kw: None)
    monkeypatch.setattr(queue, "_bg_waiting", set())
    aid, _ = _ticked_anna(auth_client)
    _upload(auth_client, aid, "new.png")
    old = datetime.now(timezone.utc) - timedelta(seconds=content_mode.CHECKING_TTL_SECONDS + 60)
    put_setting(f"{content_mode.CHECKING_PREFIX}{aid}", {"at": old.isoformat()})
    assert _status(auth_client)["adult_ok_checking"] == [aid]
    queue._bg_waiting.clear()
    assert _status(auth_client)["adult_ok_checking"] == []


def test_one_check_waits_per_character_without_redis(auth_client, monkeypatch):
    waiting: list[tuple] = []
    monkeypatch.setattr(queue, "_run_in_background", lambda func, *a, **kw: waiting.append((func, a)))
    monkeypatch.setattr(queue, "_bg_waiting", set())
    aid, _ = _ticked_anna(auth_client)
    for i in range(4):
        _upload(auth_client, aid, f"p{i}.png")
    assert len(waiting) == 1 and len(mock_chat.PICTURE_CALLS) == 1
    func, args = waiting[0]
    func(*args)  # it runs: every picture added so far is checked in one go
    assert len(mock_chat.PICTURE_CALLS) == 5
    s = _status(auth_client)
    assert s["adult_ok_unchecked"] == {} and s["adult_ok_checking"] == []
    _upload(auth_client, aid, "after.png")
    assert len(waiting) == 2  # a new one after it started


def test_no_queue_means_no_check_but_the_upload_works(auth_client, monkeypatch):
    from app import config

    aid, _ = _ticked_anna(auth_client)
    monkeypatch.setenv("QUEUE_MODE", "required")
    config.get_settings.cache_clear()
    monkeypatch.setattr(queue, "_try_redis_queue", lambda: None)
    new = _upload(auth_client, aid, "new.png")
    _drain()
    assert not _covered(new)
    s = _status(auth_client)
    assert s["adult_ok_unchecked"] == {str(aid): 1} and s["adult_ok_checking"] == []


def test_a_check_that_died_stops_saying_checking(auth_client, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app.services.model_registry import put_setting

    aid, _ = _ticked_anna(auth_client)
    put_setting(f"{content_mode.CHECKING_PREFIX}{aid}", {"at": datetime.now(timezone.utc).isoformat()})
    assert _status(auth_client)["adult_ok_checking"] == [aid]
    old = datetime.now(timezone.utc) - timedelta(seconds=content_mode.CHECKING_TTL_SECONDS + 1)
    put_setting(f"{content_mode.CHECKING_PREFIX}{aid}", {"at": old.isoformat()})
    assert _status(auth_client)["adult_ok_checking"] == []

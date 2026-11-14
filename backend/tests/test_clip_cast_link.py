"""The Cast link through clips made from clips: "Continue this clip", joined
clips and videos uploaded as clips. Wherever a picture of an unticked Cast
character ends up (a continuation's first frame, a frame of a join, a frame
or continuation of an upload), its content filter is forced on, exactly as
for the Cast picture itself; a ticked character's follows the tick (only a
card picture the tick's check looked at skips it), as "Animate this" does.
A clean deployment keeps every one of these locked."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from .test_frame_to_picture import (
    _ambient_mark,
    _clip_row,
    _frame_of,
    _ingredient_with_picture,
    _source,
)


@contextmanager
def _first_frames():
    """The first-frame pictures sent to the video provider meanwhile."""
    from app.providers import mock_video

    sent: list = []
    real = mock_video.MockVideoProvider.submit

    def spy(self, **kw):
        sent.extend(r for r in kw["references"] if r.frame_type == "first_frame")
        return real(self, **kw)

    mock_video.MockVideoProvider.submit = spy
    try:
        yield sent
    finally:
        mock_video.MockVideoProvider.submit = real


def _continued(client, clip_id: int, **body) -> tuple:
    """"Continue this clip": (the first frame the provider got, the new clip)."""
    with _first_frames() as sent:
        r = client.post(f"/api/studio/jobs/{clip_id}/continue", json={"prompt": "she turns", **body})
    assert r.status_code == 202, r.text
    job = client.get(f"/api/studio/jobs/{r.json()['id']}").json()
    assert job["status"] == "completed", job
    (ref,) = sent
    return ref, job


def _seen(ref) -> tuple:
    from app.services import content_mode

    return ref.role, ref.adult_ok, content_mode.forces_filter(ref)


FORCED = ("character", False, True)
TICKED = ("character", True, False)
NEW_PICTURE = ("scene", False, False)


def _animate_ref(asset_id: int):
    """How "Animate this" sends a Cast picture itself as a first frame."""
    from app import models
    from app.db import SessionLocal
    from app.services import pipeline

    with SessionLocal() as db:
        return pipeline.studio_first_frame(db, models.AssetGenerationJob(first_frame_asset_id=asset_id))


def _join(client, ids: list[int]) -> dict:
    r = client.post("/api/studio/join-clips", json={"job_ids": ids})
    assert r.status_code == 202, r.text
    job = client.get(f"/api/studio/jobs/{r.json()['id']}").json()
    assert job["status"] == "completed", job
    return job


def _cast():
    """Adult mode on; Bob not ticked; Anna ticked with two checked pictures
    and one added since (not checked)."""
    from app.services import content_mode

    from .test_content_mode import _asset, _tick_now
    from .test_content_mode import _avatar as _avatar_row

    bob, anna = _avatar_row("Bob"), _avatar_row("Anna")
    bob_pic, anna_pic, anna_pic2 = _asset(bob), _asset(anna), _asset(anna)
    _tick_now(anna)
    anna_new = _asset(anna)
    content_mode.set_adult_mode(True)
    return bob, bob_pic, anna, anna_pic, anna_pic2, anna_new


# ---------- continue a clip ----------


def test_a_continuation_starts_from_the_clips_cast_picture_as_animate_does(auth_client, tmp_path):
    from app.services import content_mode

    bob, bob_pic, anna, anna_pic, _anna_pic2, anna_new = _cast()
    beach, _beach_pic = _ingredient_with_picture("Beach")
    cases = {
        "bob": (_clip_row(tmp_path, bob, first_frame_asset_id=bob_pic), bob, bob_pic, FORCED),
        "anna": (_clip_row(tmp_path, anna, first_frame_asset_id=anna_pic), anna, anna_pic, TICKED),
        "anna, a picture added since the tick": (_clip_row(tmp_path, anna, first_frame_asset_id=anna_new),
                                                 anna, anna_new, FORCED),
        # A scene's clip made with Bob's picture as a reference: his picture.
        "bob as a reference": (_clip_row(tmp_path, beach, owner_kind="ingredient",
                                         reference_asset_ids_json=[bob_pic]), bob, bob_pic, FORCED),
    }
    for name, (clip, owner, pic, expected) in cases.items():
        ref, cont = _continued(auth_client, clip)
        assert _seen(ref) == expected, name
        animate = _animate_ref(pic)
        assert (ref.role, ref.adult_ok) == (animate.role, animate.adult_ok), name
        assert _source(cont["first_frame_job_id"]) == (owner, pic), name
    # A prompt-only clip: a new picture, nobody's likeness.
    ref, _cont = _continued(auth_client, _clip_row(tmp_path, bob))
    assert _seen(ref) == NEW_PICTURE
    # Anna unticked since: her clip's continuation keeps her filter, as Animate would.
    content_mode.set_avatar_adult_ok(anna, False)
    ref, _cont = _continued(auth_client, cases["anna"][0])
    assert _seen(ref) == FORCED and _seen(_animate_ref(anna_pic)) == FORCED


def test_a_continuation_of_a_continuation_or_of_its_join_keeps_the_link(auth_client, tmp_path):
    bob, bob_pic, anna, anna_pic, _anna_pic2, _anna_new = _cast()
    for owner, pic, expected in ((bob, bob_pic, FORCED), (anna, anna_pic, TICKED)):
        original = _clip_row(tmp_path, owner, first_frame_asset_id=pic)
        ref, first = _continued(auth_client, original)
        assert _seen(ref) == expected
        join = first["options"]["join_job_id"]  # the original and the continuation, joined
        ref, second = _continued(auth_client, first["id"])
        assert _seen(ref) == expected
        ref, third = _continued(auth_client, second["id"], auto_join=False)
        assert _seen(ref) == expected
        ref, _of_join = _continued(auth_client, join)
        assert _seen(ref) == expected
        # Frames saved of any of them are still that picture.
        for clip in (first["id"], second["id"], third["id"], join):
            assert _source(_frame_of(auth_client, clip, at="last")) == (owner, pic), clip


def test_a_scenes_continuation_keeps_the_link_once_the_picture_is_deleted(auth_client, tmp_path):
    from app.services import pipeline

    bob, bob_pic, _anna, _anna_pic, _anna_pic2, _anna_new = _cast()
    beach, _beach_pic = _ingredient_with_picture("Beach")
    remembered = _clip_row(tmp_path, beach, owner_kind="ingredient", first_frame_asset_id=bob_pic)
    unknown = _clip_row(tmp_path, beach, owner_kind="ingredient", first_frame_asset_id=99999)
    assert auth_client.delete(f"/api/assets/{bob_pic}").status_code == 204
    ref, cont = _continued(auth_client, remembered)
    assert _seen(ref) == FORCED and _source(cont["first_frame_job_id"]) == (bob, None)
    ref, cont = _continued(auth_client, unknown)
    assert _seen(ref) == FORCED and _source(cont["first_frame_job_id"]) == (pipeline.UNKNOWN_CAST, None)


def test_a_long_chain_of_continuations_is_followed_to_its_start(auth_client):
    """Hundreds of continuations deep (no recursion limit), and a malformed
    join that names itself, are followed without failing."""
    from app import models
    from app.db import SessionLocal
    from app.services import pipeline

    from .test_content_mode import _asset
    from .test_content_mode import _avatar as _avatar_row

    bob = _avatar_row("Bob")
    bob_pic = _asset(bob)
    Job = models.AssetGenerationJob
    with SessionLocal() as db:
        clip = Job(owner_kind="avatar", owner_id=bob, output_kind="video_clip", prompt="p", status="completed",
                   first_frame_asset_id=bob_pic, reference_asset_ids_json=[])
        db.add(clip)
        db.flush()
        for _ in range(800):
            frame = Job(owner_kind="avatar", owner_id=bob, output_kind="image", prompt="f", status="completed",
                        operation="extract_frame", source_job_id=clip.id, reference_asset_ids_json=[])
            db.add(frame)
            db.flush()
            clip = Job(owner_kind="avatar", owner_id=bob, output_kind="video_clip", prompt="p", status="completed",
                       first_frame_job_id=frame.id, reference_asset_ids_json=[])
            db.add(clip)
            db.flush()
        loop = Job(owner_kind="avatar", owner_id=bob, output_kind="video_clip", prompt="j", status="completed",
                   operation="join_clips", reference_asset_ids_json=[])
        db.add(loop)
        db.flush()
        loop.options_json = {"job_ids": [loop.id, clip.id]}
        db.commit()
        assert pipeline.studio_cast_source(db, clip) == (bob, bob_pic)
        assert pipeline.studio_cast_source(db, loop) == (bob, bob_pic)


# ---------- joined clips ----------


def test_a_join_is_every_cast_picture_its_clips_were_made_from(auth_client, tmp_path):
    """Whichever clip a frame of a join shows, it counts as every Cast
    picture any of its clips was made from: a scene's join that starts on
    scenery and ends on Bob forces Bob's filter for its first frame, its
    continuation and an ambient scene made from it."""
    from app import models
    from app.db import SessionLocal
    from app.services import content_mode, pipeline

    bob, bob_pic, anna, anna_pic, anna_pic2, _anna_new = _cast()
    beach, beach_pic = _ingredient_with_picture("Beach")
    big = (640, 480)  # big enough for an ambient scene's picture
    scenery = _clip_row(tmp_path, beach, big, owner_kind="ingredient", first_frame_asset_id=beach_pic)
    bob_clip = _clip_row(tmp_path, bob, big, first_frame_asset_id=bob_pic)
    anna_clip = _clip_row(tmp_path, anna, big, first_frame_asset_id=anna_pic)
    anna_clip2 = _clip_row(tmp_path, anna, big, reference_asset_ids_json=[anna_pic2])
    marked = content_mode.adult_ok_avatars()

    mixed = _join(auth_client, [scenery, bob_clip])
    assert (mixed["owner_kind"], mixed["owner_id"]) == ("ingredient", beach)
    first = _frame_of(auth_client, mixed["id"], at="first")  # the scenery
    assert _source(first) == (bob, bob_pic)
    ref, cont = _continued(auth_client, mixed["id"])
    assert _seen(ref) == FORCED
    assert _ambient_mark(auth_client, first) == {"cast_picture": True, "adult_ok": False}
    # A join of a join (and its continuation's join) still leads back to Bob.
    again = _join(auth_client, [scenery, mixed["id"]])
    assert _source(_frame_of(auth_client, again["id"])) == (bob, bob_pic)
    assert _source(_frame_of(auth_client, cont["options"]["join_job_id"])) == (bob, bob_pic)

    # Anna's checked pictures only: her tick covers it.
    hers = _join(auth_client, [anna_clip, anna_clip2])
    ref, _cont = _continued(auth_client, hers["id"])
    assert _seen(ref) == TICKED
    # Anna and Bob: Bob's filter wins; with no ticks to go by, never covered.
    both = _join(auth_client, [anna_clip, bob_clip])
    frame = _frame_of(auth_client, both["id"])
    assert _source(frame, marked) == (bob, bob_pic) and _source(frame) == (anna, None)
    ref, _cont = _continued(auth_client, both["id"])
    assert _seen(ref) == FORCED
    assert _ambient_mark(auth_client, frame) == {"cast_picture": True, "adult_ok": False}
    # Scenery only: a new picture.
    ref, _cont = _continued(auth_client, _join(auth_client, [scenery, scenery])["id"])
    assert _seen(ref) == NEW_PICTURE

    # A clip of the join that can't be found any more: whose it was isn't
    # known, so the filter stays on.
    gone = _join(auth_client, [scenery, scenery])
    with SessionLocal() as db:
        row = db.get(models.AssetGenerationJob, gone["id"])
        row.options_json = {**row.options_json, "job_ids": [scenery, 999999]}
        db.commit()
        assert pipeline.studio_cast_source(db, row) == (pipeline.UNKNOWN_CAST, None)
    ref, _cont = _continued(auth_client, gone["id"])
    assert _seen(ref) == FORCED


def test_a_deleted_clip_of_a_join_keeps_the_link(auth_client, tmp_path):
    """Deleting a clip (a tombstone keeps where it came from) after it was
    joined: the join's frames are still its Cast picture."""
    bob, bob_pic, _anna, _anna_pic, _anna_pic2, _anna_new = _cast()
    a = _clip_row(tmp_path, bob, first_frame_asset_id=bob_pic)
    b = _clip_row(tmp_path, bob)
    joined = _join(auth_client, [b, a])
    assert auth_client.delete(f"/api/studio/jobs/{a}").status_code == 204
    ref, _cont = _continued(auth_client, joined["id"])
    assert _seen(ref) == FORCED


# ---------- videos uploaded as clips ----------


def _uploaded(client, tmp_path: Path, owner: int, owner_kind: str = "avatar") -> dict:
    from .test_upload_video import _upload, _video

    name = f"up{len(list(tmp_path.glob('up*.mp4')))}.mp4"
    r = _upload(client, _video(tmp_path / name), owner, owner_kind=owner_kind)
    assert r.status_code == 201, r.text
    return r.json()


def test_an_uploaded_clip_is_its_owners_picture_with_no_card_picture(auth_client, tmp_path):
    """Like a frame of an uploaded video: a character's upload forces that
    character's filter wherever it goes next (a frame, a continuation, a
    join with it), ticked or not, since no checked card picture is behind
    it; a scene's upload is not a Cast picture."""
    bob, bob_pic, anna, _anna_pic, _anna_pic2, _anna_new = _cast()
    beach, _beach_pic = _ingredient_with_picture("Beach")
    for owner, owner_kind, expected, source in ((bob, "avatar", FORCED, (bob, None)),
                                                (anna, "avatar", FORCED, (anna, None)),
                                                (beach, "ingredient", NEW_PICTURE, (None, None))):
        up = _uploaded(auth_client, tmp_path, owner, owner_kind)
        assert _source(_frame_of(auth_client, up["id"])) == source, owner_kind
        ref, cont = _continued(auth_client, up["id"])
        assert _seen(ref) == expected, (owner, owner_kind)
        # The continuation of the continuation, and the auto-join, too.
        ref, _cont = _continued(auth_client, cont["id"], auto_join=False)
        assert _seen(ref) == expected
        ref, _cont = _continued(auth_client, cont["options"]["join_job_id"], auto_join=False)
        assert _seen(ref) == expected
    # A scene's upload joined with Bob's clip: Bob's filter.
    scene_up = _uploaded(auth_client, tmp_path, beach, "ingredient")
    joined = _join(auth_client, [scene_up["id"], _clip_row(tmp_path, bob, first_frame_asset_id=bob_pic)])
    ref, _cont = _continued(auth_client, joined["id"])
    assert _seen(ref) == FORCED


def test_paid_checks_on_uploads_are_refused_past_the_budget(auth_client, tmp_path, monkeypatch):
    """Upload a video, a frame of a clip with pictures of an upload, and
    such a clip used in a project shot each run a (tiny) paid vision check:
    refused at the budget's hard stop before anything is read. A frame of
    any other clip makes no call and isn't asked."""
    from fastapi import HTTPException

    from app.api import studio
    from app.providers import mock_chat
    from app.services import spend

    from .test_frame_to_picture import _avatar
    from .test_upload_video import _project_shot, _upload, _video

    aid = _avatar(auth_client)
    up = _uploaded(auth_client, tmp_path, aid)
    plain = _clip_row(tmp_path, aid)
    pid, sid = _project_shot(auth_client, aid, 2.0)
    asked: list[str] = []

    def guard():
        asked.append("asked")
        raise HTTPException(402, "Monthly budget reached")

    monkeypatch.setattr(studio, "budget_guard", guard)
    monkeypatch.setattr(spend, "budget_guard", guard)
    calls = len(mock_chat.PICTURE_CALLS)
    r = _upload(auth_client, _video(tmp_path / "late.mp4"), aid)
    assert r.status_code == 402 and asked == ["asked"]
    r = auth_client.post(f"/api/studio/jobs/{up['id']}/frame-to-picture", json={"at": "first"})
    assert r.status_code == 402 and len(asked) == 2
    r = auth_client.post(f"/api/projects/{pid}/shots/{sid}/takes/from-studio", json={"studio_job_id": up["id"]})
    assert r.status_code == 402 and len(asked) == 3
    assert len(mock_chat.PICTURE_CALLS) == calls
    # A frame of a generated clip: free, never asked.
    assert auth_client.post(f"/api/studio/jobs/{plain}/frame-to-picture", json={"at": "first"}).status_code == 201
    assert len(asked) == 3


# ---------- a clean deployment ----------


def test_a_clean_deployment_keeps_every_continuation_and_join_filtered(auth_client, tmp_path, monkeypatch):
    """ADULT_FEATURES=false: a tick left from before covers nothing, so the
    continuation of a ticked character's clip, and of a join of it, is sent
    with the filter on; continuing an uploaded clip asks the explicit
    picture check about its last frame first, and is refused (nothing
    made) when it says the frame is explicit."""
    from app.providers import mock_chat

    from .test_adult_features_off import _clean
    from .test_continue_clip import _rows

    _bob, _bob_pic, anna, anna_pic, _anna_pic2, _anna_new = _cast()
    anna_clip = _clip_row(tmp_path, anna, first_frame_asset_id=anna_pic)
    ref, _cont = _continued(auth_client, anna_clip, auto_join=False)
    assert _seen(ref) == TICKED  # adult features on
    up = _uploaded(auth_client, tmp_path, anna)
    _clean(monkeypatch)
    ref, _cont = _continued(auth_client, anna_clip)
    assert ref.role == "character" and _seen(ref)[2] is True
    ref, _cont = _continued(auth_client, _join(auth_client, [anna_clip, anna_clip])["id"])
    assert _seen(ref)[2] is True
    clips = len(_rows(output_kind="video_clip"))
    mock_chat.PICTURE_EXPLICIT_REPLY = {"explicit": True, "reason": "Nudity."}
    r = auth_client.post(f"/api/studio/jobs/{up['id']}/continue", json={"prompt": "she turns"})
    assert r.status_code == 422 and "general audience" in r.json()["detail"], r.text
    assert len(mock_chat.PICTURE_EXPLICIT_CALLS) == 1
    assert len(_rows(output_kind="video_clip")) == clips

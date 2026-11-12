"""Pictures added to the card of a Cast character ticked "Adult content
allowed" are checked on their own, off the request that added them.

Every way a picture lands on a Cast card calls picture_added() once it is
committed: an upload (api/assets.py), a Studio result saved to the card
(pipeline.save_studio_result_as_asset: single and bulk saves, a character
sheet's views, a cutout or upscale saved back to its slot), a logo moved
to a picture slot, and a file whose type the startup repair corrected to a
picture. If the character's tick holds, a job on the default queue
(check_cast_pictures_job, one waiting per character: workers/queue.py
enqueue_once) asks the picture model (minor_check.check_pictures) about
the pictures on the card that the tick doesn't cover and that it hasn't
declined before, and then:

- judged adult: added to the pictures the tick covers;
- declined: it keeps the filter, and the Cast card lists it ("Check new
  pictures" asks again);
- under 18: the tick comes off and the Cast card says why;
- the check couldn't run: it keeps the filter ("Check new pictures"
  retries).

The tick is updated with a compare-and-set (content_mode.merge_tick): if
it changed while the job ran (unticked, ticked again, the card edited),
nothing is written. A picture judged under 18 takes the tick off whatever
it is by then, as long as the picture is still on the card.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from . import content_mode, minor_check

log = logging.getLogger("avs.cast_pictures")


def job_key(avatar_id: int) -> str:
    return f"cast-pictures-{int(avatar_id)}"


def picture_added(avatar_id: Optional[int], asset=None) -> None:  # noqa: ANN001 — models.Asset
    """A picture was added to this character's card (committed). Queues a
    check when the character's tick holds and the picture is one a tick
    covers (not a logo). Never raises, never waits for the check."""
    if avatar_id is None:
        return
    try:
        if asset is not None and not content_mode.is_tick_picture(asset):
            return
        if not content_mode.holds(int(avatar_id)):
            return
        queue_check(int(avatar_id))
    except Exception as e:  # noqa: BLE001 — the picture keeps the filter until it's checked
        log.warning("picture check for cast character %s not queued (%s)", avatar_id, e)


def queue_check(avatar_id: int) -> str:
    """Queue the check (at most one waiting per character). Returns the
    job id, "" when it couldn't be queued (the pictures keep the filter;
    "Check new pictures" on the Cast card checks them)."""
    from ..workers import jobs
    from ..workers.queue import enqueue_once

    _set_checking(avatar_id)
    try:
        job_id = enqueue_once(jobs.check_cast_pictures_job, int(avatar_id), key=job_key(avatar_id))
    except Exception as e:  # noqa: BLE001
        log.warning("picture check for cast character %s not queued (%s)", avatar_id, e)
        job_id = ""
    if not job_id:
        _clear_checking(avatar_id, None)
    return job_id


def run(avatar_id: int) -> str:
    """The job. Returns what happened (for logs and tests): "unticked",
    "gone", "stale" (the tick no longer holds), "nothing" (no picture to
    ask about), "minor", "changed" (the tick changed meanwhile: nothing
    written), "ok" or "unavailable" (with what the model did answer
    written)."""
    started = _now()
    try:
        out = _run(int(avatar_id))
    finally:
        _clear_checking(avatar_id, started)
    log.info("picture check of cast character %s: %s", avatar_id, out)
    return out


def _run(avatar_id: int) -> str:
    rec = content_mode.tick_row(avatar_id)
    if rec is None:
        return "unticked"
    card, pictures = content_mode.card_copy(avatar_id)
    if card is None:
        return "gone"
    if rec.get("card") != content_mode.card_hash(card) or content_mode.describes_minor(card):
        return "stale"
    covered = content_mode.picture_ids(rec.get("assets"))
    said_no = content_mode.picture_ids(rec.get("declined"))
    todo = [p for p in pictures if p.id not in covered and p.id not in said_no]
    if not todo:
        return "nothing"
    seen = minor_check.check_pictures(card.name, todo)
    if seen["verdict"] == "minor":
        _untick(avatar_id, card.name, seen)
        return "minor"
    written = content_mode.merge_tick(avatar_id, rec, covered=seen["checked"],
                                      declined=[d["id"] for d in seen["declined"]])
    if not written:
        return "changed"
    return seen["verdict"]


def _untick(avatar_id: int, name: str, seen: dict) -> None:
    """A picture the check thinks shows someone under 18: the tick comes off
    (whatever tick it is by now), while that picture is still on the card
    and the character is still ticked."""
    _, pictures = content_mode.card_copy(avatar_id)
    if seen.get("id") not in {p.id for p in pictures} or content_mode.tick_row(avatar_id) is None:
        return
    why = f" It said: {seen['reason'].rstrip('.')}." if seen.get("reason") else ""
    notice = content_mode.PICTURE_UNTICKED.format(name=name or "this character", picture=f'"{seen["picture"]}"',
                                                  why=why)
    content_mode.set_avatar_adult_ok(avatar_id, False, notice=notice)
    log.warning("cast character %s: a picture added to the card looks under 18; adult content no longer allowed",
                avatar_id)


# ---------- "Checking new pictures…" on the Cast card ----------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _set_checking(avatar_id: int) -> None:
    from .model_registry import put_setting

    try:
        put_setting(f"{content_mode.CHECKING_PREFIX}{int(avatar_id)}", {"at": _now().isoformat()})
    except Exception as e:  # noqa: BLE001 — a note on a card
        log.warning("cast character %s: checking note not saved (%s)", avatar_id, type(e).__name__)


def _clear_checking(avatar_id: int, started: Optional[datetime]) -> None:
    """Clear the note, unless a picture was added after this run started
    (its own run, queued behind this one, clears it). `started` None:
    clear it whatever its time."""
    from .model_registry import get_setting, put_setting

    key = f"{content_mode.CHECKING_PREFIX}{int(avatar_id)}"
    try:
        rec = get_setting(key)
        if rec is None:
            return
        if started is not None:
            try:
                if datetime.fromisoformat(str(rec.get("at"))) > started:
                    return
            except (ValueError, TypeError):
                pass
        put_setting(key, None)
    except Exception as e:  # noqa: BLE001
        log.warning("cast character %s: checking note not cleared (%s)", avatar_id, type(e).__name__)

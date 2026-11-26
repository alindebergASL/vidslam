"""Sound Studio API: audio stories (scripts, the writer, Enhance).

Mounted at /api/sound. Stories live in their own table (models.SoundStory),
never in VideoProject. The writer and an Enhance of a chapter or the whole
story run as jobs that claim the story ('scripting'); Cancel stops them
between calls and Resume continues the writer at the next missing chapter.
Paid endpoints are rate-limited, refused over the budget (counting what
they would cost) and refused when the chat model is switched off. POST
/parse and /estimate are free.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..schemas.sound import EnhanceIn, EstimateIn, ParseIn, StoryCreate, StoryUpdate, WriteIn
from ..services import (
    content_mode,
    prompting,
    provider_gate,
    sound_estimate,
    sound_studio,
    spend,
    story_script,
)
from ..services.ratelimit import rate_limit
from ..services.safety import UnsafeScriptError
from ..services.spend import budget_guard
from ..workers.jobs import story_enhance_job, story_script_job
from .auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])

PAID = [Depends(rate_limit("generation")), Depends(budget_guard)]


def _get(db: Session, story_id: int) -> models.SoundStory:
    story = db.get(models.SoundStory, story_id)
    if story is None:
        raise HTTPException(404, "story not found")
    return story


def _not_busy(story: models.SoundStory) -> None:
    if story.status in sound_studio.BUSY:
        raise HTTPException(409, "this story is busy; wait or cancel first")


def _check_settings(settings, minutes: Optional[int]) -> None:  # noqa: ANN001 — StorySettings
    cap = sound_studio.max_minutes()
    if minutes is not None and minutes > cap:
        raise HTTPException(422, f"A story is at most {cap} minutes long.")
    if settings is None:
        return
    style = (settings.style or "").strip().lower()
    if style and style not in sound_studio.STYLES:
        raise HTTPException(422, f"unknown style {settings.style!r}")
    if style in sound_studio.ADULT_STYLES and not content_mode.features():
        raise HTTPException(422, "This style isn't available on this server.")


def _checked(fn, *args, **kw):  # noqa: ANN001, ANN002, ANN003, ANN202
    """Run a safety check: a refusal is a 422 with its message."""
    try:
        return fn(*args, **kw)
    except UnsafeScriptError as e:
        raise HTTPException(422, str(e)) from None


def _enqueue(db: Session, story: models.SoundStory, job, task: str) -> str:  # noqa: ANN001
    try:
        return sound_studio.enqueue_claimed(db, story, job, task=task)
    except LookupError as e:
        raise HTTPException(409, str(e)) from None


def _require_writer() -> None:
    provider_gate.require_model("chat", content_mode.chat_model())


@router.get("/config")
def config() -> dict:
    """What the pages need to know: the longest story, the styles this
    deployment offers, and how much of a script Enhance rewrites at once."""
    adult = content_mode.features()
    return {"max_minutes": sound_studio.max_minutes(),
            "styles": [s for s in sound_studio.STYLES if adult or s not in sound_studio.ADULT_STYLES],
            "enhance_sync_max": prompting.AUDIO_SCRIPT_MAX,
            "minutes_per_chapter": sound_studio.MINUTES_PER_CHAPTER_CALL}


# ---------- stories ----------


@router.get("/stories")
def list_stories(db: Session = Depends(get_db)) -> list[dict]:
    rows = db.query(models.SoundStory).order_by(models.SoundStory.updated_at.desc()).limit(200).all()
    return [sound_studio.to_out(s, full=False) for s in rows]


@router.post("/stories", status_code=201, dependencies=[Depends(rate_limit("generation"))])
def create_story(body: StoryCreate, db: Session = Depends(get_db)) -> dict:
    _check_settings(body.settings, body.target_minutes)
    settings = body.settings.model_dump()
    settings["style"] = settings["style"].strip().lower()
    _checked(sound_studio.check_script_text, body.script_text, style=settings["style"], title=body.title,
             premise=body.premise, settings=settings)
    story = models.SoundStory(
        title=body.title.strip(), kind=body.kind, premise=body.premise.strip(),
        target_minutes=body.target_minutes, settings_json=settings, script_text=body.script_text,
        script_version=1 if body.script_text.strip() else 0,
        status="ready" if body.script_text.strip() else "draft",
    )
    db.add(story)
    db.commit()
    db.refresh(story)
    return sound_studio.to_out(story)


@router.get("/stories/{story_id}")
def get_story(story_id: int, db: Session = Depends(get_db)) -> dict:
    return sound_studio.to_out(_get(db, story_id))


@router.patch("/stories/{story_id}")
def update_story(story_id: int, body: StoryUpdate, db: Session = Depends(get_db)) -> dict:
    """Save the title, premise, length, settings or script. A new script
    bumps script_version; a save from an older version is a 409. What is
    saved is checked as a whole (sound_studio.check_script_text)."""
    story = _get(db, story_id)
    _not_busy(story)
    _check_settings(body.settings, body.target_minutes)
    if body.script_version is not None and body.script_text is not None \
            and body.script_version != (story.script_version or 0):
        raise HTTPException(409, "The script changed since you opened it; reload to see the newest version.")
    title = story.title if body.title is None else body.title.strip()
    premise = story.premise if body.premise is None else body.premise.strip()
    settings = dict(story.settings_json or {})
    if body.settings is not None:
        settings.update(body.settings.model_dump())
        settings["style"] = str(settings.get("style") or "").strip().lower()
    text = story.script_text if body.script_text is None else body.script_text
    changed = (title != story.title or premise != story.premise or text != story.script_text
               or settings != (story.settings_json or {}))
    if changed:
        _checked(sound_studio.check_script_text, text, style=str(settings.get("style") or ""), title=title,
                 premise=premise, settings=settings)
    if body.script_text is not None and text != story.script_text:
        story.script_text = text
        story.script_version = (story.script_version or 0) + 1
        if (story.claim_token or "").startswith(sound_studio.RUNNING):
            # A cancelled job's chapter still in flight must not land over
            # this edit: its run is retired (its save then finds no claim).
            story.claim_token = ""
        if story.status == "draft" and text.strip():
            story.status = "ready"
    story.title, story.premise, story.settings_json = title, premise, settings
    if body.target_minutes is not None:
        story.target_minutes = body.target_minutes
    db.commit()
    db.refresh(story)
    return sound_studio.to_out(story)


@router.delete("/stories/{story_id}", status_code=204)
def delete_story(story_id: int, db: Session = Depends(get_db)) -> Response:
    story = _get(db, story_id)
    _not_busy(story)
    db.delete(story)
    db.commit()
    return Response(status_code=204)


# ---------- free: parse and price ----------


@router.post("/parse")
def parse(body: ParseIn) -> dict:
    doc = story_script.parse(body.script_text, style=(body.style or "").strip().lower())
    return sound_studio.doc_out(doc)


def _range(story: models.SoundStory, scope: Optional[str], start: Optional[int], end: Optional[int],
           chapter_line: Optional[int]) -> tuple[int, int]:
    text = story.script_text or ""
    if scope == "story":
        return 0, len(text)
    if scope == "chapter":
        if not chapter_line:
            raise HTTPException(422, "say which chapter (chapter_line)")
        lines = text.splitlines(keepends=True)
        offset = sum(len(x) for x in lines[: chapter_line - 1])
        for a, b, _ in sound_studio.segments(text):
            if a <= offset < b:
                return a, b
        raise HTTPException(422, "no chapter at that line")
    if start is None or end is None or not (0 <= start < end <= len(text)):
        raise HTTPException(422, "select some of the script first")
    return start, end


@router.post("/estimate")
def estimate(body: EstimateIn, db: Session = Depends(get_db)) -> dict:
    """What writing, enhancing and (later) generating would cost. Free."""
    if body.story_id is not None:
        story = _get(db, body.story_id)
        gen = sound_estimate.estimate(story, db=db, write=False)
        w = sound_studio.remaining_write_estimate(story) if sound_studio.can_resume(story) \
            else sound_studio.write_estimate(story.target_minutes or 10)
        out = {"generate": gen, "write": w, "minutes": gen.get("minutes")}
        if body.enhance_scope:
            a, b = _range(story, body.enhance_scope, body.start, body.end, body.chapter_line)
            out["enhance"] = sound_studio.enhance_estimate(story, a, b)
        return out
    _check_settings(body.settings, body.target_minutes)
    settings = body.settings.model_dump()
    doc = story_script.parse(body.script_text, style=settings["style"].strip().lower())
    gen = sound_estimate.estimate(doc, settings, write=False)
    return {"generate": gen, "write": sound_studio.write_estimate(body.target_minutes), "minutes": gen.get("minutes")}


# ---------- the writer ----------


@router.post("/stories/{story_id}/write", status_code=202, dependencies=PAID)
def write(story_id: int, body: Optional[WriteIn] = None, db: Session = Depends(get_db)) -> dict:
    """Write the story from its premise, chapter by chapter (a job)."""
    story = _get(db, story_id)
    _not_busy(story)
    if not (story.premise or "").strip():
        raise HTTPException(422, "Write a premise first.")
    has_lines = bool(sound_studio.parse_story(story).speech())
    if has_lines and not (body and body.replace):
        raise HTTPException(409, "This story already has a script; use Enhance, or write it again from scratch.")
    _checked(sound_studio.check_premise, story.title or "", story.premise or "")
    _require_writer()
    price = sound_studio.write_estimate(story.target_minutes or 10)
    spend.require_budget_for(db, price["cost"])
    # A replaced script stays until the job writes the first new chapter
    # over it (a refused outline or a failed claim loses nothing).
    sound_studio.reset_writer(story, replace=has_lines)
    story.estimated_cost = price["cost"]
    db.commit()
    job_id = _enqueue(db, story, story_script_job, sound_studio.WRITER_KEY)
    return {"story_id": story_id, "job_id": job_id, "status": "enqueued", "price": price}


@router.post("/stories/{story_id}/resume", status_code=202, dependencies=PAID)
def resume(story_id: int, db: Session = Depends(get_db)) -> dict:
    """Continue a writer run that was cancelled or stopped: the finished
    chapters are kept, the next missing one is written next."""
    story = _get(db, story_id)
    _not_busy(story)
    if not sound_studio.can_resume(story):
        raise HTTPException(409, "Nothing to resume: this story isn't part-way written.")
    _checked(sound_studio.check_premise, story.title or "", story.premise or "")
    _require_writer()
    price = sound_studio.remaining_write_estimate(story)
    spend.require_budget_for(db, price["cost"])
    job_id = _enqueue(db, story, story_script_job, sound_studio.WRITER_KEY)
    return {"story_id": story_id, "job_id": job_id, "status": "enqueued", "price": price}


@router.post("/stories/{story_id}/cancel")
def cancel(story_id: int, db: Session = Depends(get_db)) -> dict:
    return sound_studio.cancel(db, _get(db, story_id))


# ---------- Enhance ----------


@router.post("/stories/{story_id}/enhance", dependencies=PAID)
def enhance(story_id: int, body: EnhanceIn, db: Session = Depends(get_db)) -> Response:
    """Enhance a selection (up to prompting.AUDIO_SCRIPT_MAX characters: at
    once, 200 with the before and after), or a chapter or the whole story
    (202: a job whose before/after the story's `enhance` holds). Nothing is
    saved until the editor saves the text it keeps."""
    story = _get(db, story_id)
    _not_busy(story)
    if body.script_version is not None and body.script_version != (story.script_version or 0):
        raise HTTPException(409, "The script changed since you opened it; reload to see the newest version.")
    a, b = _range(story, body.scope, body.start, body.end, body.chapter_line)
    text = (story.script_text or "")[a:b]
    if body.scope == "selection" and body.text is not None and body.text != text:
        raise HTTPException(409, "The selection doesn't match the saved script; save first.")
    if not text.strip():
        raise HTTPException(422, "There is nothing to enhance there.")
    _require_writer()
    price = sound_studio.enhance_estimate(story, a, b)
    spend.require_budget_for(db, price["cost"])
    style = sound_studio.style_of(story)
    if body.scope == "selection" and len(text) <= prompting.AUDIO_SCRIPT_MAX:
        try:
            with spend.scope(ref=sound_studio.ref(story_id)):
                after = sound_studio.enhance_text(text, body.action, header=sound_studio.header_of(story.script_text),
                                                  style=style, title=story.title or "",
                                                  premise=story.premise or "", minutes=body.minutes)
        except prompting.EnhanceRefused as e:
            raise HTTPException(422, sound_studio.refused_message(e)) from None
        except UnsafeScriptError as e:
            raise HTTPException(422, f"The rewrite was refused: {e} Your text is unchanged.") from None
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        return JSONResponse({"mode": "sync", "start": a, "end": b, "before": text, "after": after,
                             "script_version": story.script_version or 0, "price": price})
    progress = dict(story.progress_json or {})
    progress[sound_studio.ENHANCE_KEY] = {
        "status": "running", "action": body.action, "scope": body.scope, "start": a, "end": b, "before": text,
        "after": "", "script_version": story.script_version or 0, "minutes": body.minutes, "done": 0,
        "total": sum(1 for _, _, on in sound_studio.segments(text) if on) or 1, "error": "",
    }
    story.progress_json = progress
    db.commit()
    job_id = _enqueue(db, story, story_enhance_job, sound_studio.ENHANCE_KEY)
    return JSONResponse({"mode": "job", "job_id": job_id, "start": a, "end": b, "price": price}, status_code=202)


@router.delete("/stories/{story_id}/enhance")
def dismiss_enhance(story_id: int, db: Session = Depends(get_db)) -> dict:
    """Forget the Enhance proposal (Keep mine, or once it was used)."""
    story = _get(db, story_id)
    _not_busy(story)
    progress = dict(story.progress_json or {})
    progress.pop(sound_studio.ENHANCE_KEY, None)
    story.progress_json = progress
    db.commit()
    db.refresh(story)
    return sound_studio.to_out(story)

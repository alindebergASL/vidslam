"""Sound Studio API: audio stories (scripts, the writer, Enhance, the
voice clips and sound effects, previews) and the sound effects tool.

Mounted at /api/sound. Stories live in their own table (models.SoundStory),
never in VideoProject. The writer and an Enhance of a chapter or the whole
story run as jobs that claim the story ('scripting'); Cancel stops them
between calls and Resume continues the writer at the next missing chapter.
Paid endpoints are rate-limited, refused over the budget (counting what
they would cost) and refused when the chat model is switched off. POST
/parse and /estimate are free.

Clips (services/sound_clips.py): Generate queues a job that claims the
story ('generating') and makes every missing voice clip and sound effect;
Cancel stops it before its next paid call and Resume makes only what is
missing. A clip or cue is regenerated, and a preview made, in the request
under the same per-story lock. Everything that will be voiced or prompted
is checked before anything is claimed or paid (sound_clips.prepare).
"""
from __future__ import annotations

import math
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..schemas.sound import (
    EffectsIn,
    EnhanceIn,
    EstimateIn,
    ParseIn,
    PreviewIn,
    RegenerateIn,
    StoryCreate,
    StoryUpdate,
    WriteIn,
)
from ..services import (
    content_mode,
    prompting,
    provider_gate,
    sound_clips,
    sound_estimate,
    sound_studio,
    spend,
    story_script,
)
from ..services.ratelimit import rate_limit
from ..services.safety import UnsafeScriptError
from ..services.spend import budget_guard
from ..workers.jobs import story_clips_job, story_enhance_job, story_script_job
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
    _check_voice(settings.voice_provider, settings.voice_model)


def _check_voice(provider: str, model: str) -> None:
    """A story's or a character's voice: ElevenLabs (eleven_v4 by default;
    eleven_v3 and eleven_v4_turbo), an OpenRouter voice model from the
    catalog, or the mock."""
    from ..config import get_settings
    from ..services import model_catalog

    p = (provider or "").strip().lower()
    m = (model or "").strip()
    if p and p not in sound_clips.VOICE_PROVIDERS:
        raise HTTPException(422, f"unknown voice provider {provider!r}")
    if p in ("", "elevenlabs") and m and m not in sound_clips.ELEVENLABS_MODELS:
        raise HTTPException(422, f"unknown ElevenLabs voice model {model!r}")
    if p == "openrouter":
        if not m:
            raise HTTPException(422, "Pick an OpenRouter voice model.")
        if model_catalog.find("tts", m) is None and not get_settings().mock_providers:
            raise HTTPException(422, f"{m} isn't a voice model in the catalog.")


def _cast_json(cast: dict) -> dict:  # noqa: ANN001 — {name: CastVoice}
    out = {}
    for name, v in cast.items():
        key = " ".join(str(name).split()).upper()[:40]
        if not key:
            continue
        _check_voice(v.provider, v.model)
        d = v.model_dump(exclude_none=True)
        d["provider"] = d.get("provider", "").strip().lower()
        if v.voice_settings is not None:
            d["voice_settings"] = v.voice_settings.model_dump(exclude_none=True) or None
            if d["voice_settings"] is None:
                d.pop("voice_settings")
        out[key] = {k: val for k, val in d.items() if val not in ("", None)}
    return out


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
    from ..providers.registry import story_voice
    from ..services import model_catalog

    adult = content_mode.features()
    provider, model = story_voice()
    snap = model_catalog.get_snapshot() or {}
    openrouter = [{"id": e["id"], "name": e.get("name") or e["id"]} for e in snap.get("entries") or []
                  if e.get("kind") == "tts" and e.get("provider") == "openrouter"]
    return {"max_minutes": sound_studio.max_minutes(),
            "styles": [s for s in sound_studio.STYLES if adult or s not in sound_studio.ADULT_STYLES],
            "enhance_sync_max": prompting.AUDIO_SCRIPT_MAX,
            "minutes_per_chapter": sound_studio.MINUTES_PER_CHAPTER_CALL,
            "voices": {"default": {"provider": provider, "model": model},
                       "providers": list(sound_clips.VOICE_PROVIDERS),
                       "elevenlabs_models": list(sound_clips.ELEVENLABS_MODELS),
                       "openrouter_models": openrouter},
            "preview_max_minutes": sound_clips.PREVIEW_MAX_MINUTES,
            "max_clip_chars": sound_clips.MAX_CLIP_CHARS}


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
    if body.cast is not None:
        # The voices are checked with the story before anything is voiced
        # (check_story binds its pass to them): saved as they are.
        story.cast_json = _cast_json(body.cast)
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
    # Its clips go with it (SQLite may not cascade); the shared sound cache stays.
    db.query(models.SoundClip).filter(models.SoundClip.story_id == story_id).delete(synchronize_session=False)
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
        full = sound_estimate.estimate(story, db=db, write=False)
        doc, clips = sound_clips.plan(story)
        # Generate: what making the missing clips costs now.
        gen = sound_clips.estimate(db, story, clips=clips)
        w = sound_studio.remaining_write_estimate(story) if sound_studio.can_resume(story) \
            else sound_studio.write_estimate(story.target_minutes or 10)
        out = {"generate": gen, "full": full, "write": w, "minutes": full.get("minutes")}
        if body.cue_key:
            try:
                sound_clips.find(clips, body.cue_key)
            except LookupError as e:
                raise HTTPException(404, str(e)) from None
            out["regenerate"] = sound_clips.estimate(db, story, clips=clips, keys={body.cue_key},
                                                     regenerate=True)
        if body.preview is not None:
            chosen = _preview_scope(doc, clips, body.preview)
            out["preview"] = sound_clips.estimate(db, story, clips=clips, keys={c.key for c in chosen})
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
    if sound_clips.can_resume(story):
        # A clips run that stopped: only the missing clips are made.
        return _queue_clips(db, story)
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
    cap = sound_studio.max_minutes()
    minutes = body.minutes
    if minutes is not None and minutes > cap:
        raise HTTPException(422, f"A story is at most {cap} minutes long.")
    if body.action == "lengthen" and minutes is None:
        # Lengthen with no length asked: from the range's own length.
        now = story_script.parse(text, style=sound_studio.style_of(story), max_minutes=0).est_seconds
        minutes = max(1, min(cap, math.ceil(now / 60.0)))
    _require_writer()
    price = sound_studio.enhance_estimate(story, a, b)
    spend.require_budget_for(db, price["cost"])
    style = sound_studio.style_of(story)
    if body.scope == "selection" and len(text) <= prompting.AUDIO_SCRIPT_MAX:
        try:
            with spend.scope(ref=sound_studio.ref(story_id)):
                after = sound_studio.enhance_text(text, body.action, header=sound_studio.header_of(story.script_text),
                                                  style=style, title=story.title or "",
                                                  premise=story.premise or "", minutes=minutes)
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
        "after": "", "script_version": story.script_version or 0, "minutes": minutes, "done": 0,
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


# ---------- clips: voices and sound effects ----------


def _prepared(story: models.SoundStory):  # noqa: ANN202
    """sound_clips.prepare as a reply: a refusal is a 422 (nothing paid)."""
    try:
        return sound_clips.prepare(story)
    except UnsafeScriptError as e:
        raise HTTPException(422, str(e)) from None
    except sound_clips.NothingToVoice as e:
        raise HTTPException(422, str(e)) from None


def _providers(clips) -> None:  # noqa: ANN001
    try:
        sound_clips.require_providers(clips)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None


def _queue_clips(db: Session, story: models.SoundStory) -> dict:
    _not_busy(story)
    _, clips, _check = _prepared(story)
    _providers(clips)
    price = sound_clips.estimate(db, story, clips=clips)
    spend.require_budget_for(db, price["total"])
    progress = dict(story.progress_json or {})
    progress[sound_studio.CLIPS_KEY] = {"total": len(clips), "done": 0, "finished": False, "error": ""}
    story.progress_json = progress
    story.estimated_cost = price["total"]
    db.commit()
    try:
        job_id = sound_studio.enqueue_claimed(db, story, story_clips_job, task=sound_studio.CLIPS_KEY,
                                              status="generating")
    except LookupError as e:
        raise HTTPException(409, str(e)) from None
    return {"story_id": story.id, "job_id": job_id, "status": "enqueued", "price": price}


@router.post("/stories/{story_id}/generate", status_code=202, dependencies=PAID)
def generate(story_id: int, db: Session = Depends(get_db)) -> dict:
    """Make every missing voice clip and sound effect (a job). Clips already
    made with the same fingerprint are kept and cost nothing."""
    return _queue_clips(db, _get(db, story_id))


@router.get("/stories/{story_id}/clips")
def list_clips(story_id: int, db: Session = Depends(get_db)) -> dict:
    """The cue blocks: every clip of the current script, its state, takes
    and the price of a new take."""
    return sound_clips.clips_out(db, _get(db, story_id))


@router.post("/stories/{story_id}/clips/regenerate", dependencies=PAID)
def regenerate_clip(story_id: int, body: RegenerateIn, db: Session = Depends(get_db)) -> dict:
    """A new take of one clip or sound effect, made now (a new seed) and
    selected; the earlier takes are kept."""
    story = _get(db, story_id)
    _not_busy(story)
    prepared = _prepared(story)
    clips = prepared[1]
    try:
        clip = sound_clips.find(clips, body.cue_key)
    except LookupError as e:
        raise HTTPException(404, str(e)) from None
    _providers([clip])
    price = sound_clips.estimate(db, story, clips=clips, keys={clip.key}, regenerate=True)
    spend.require_budget_for(db, price["total"])
    try:
        row = sound_clips.regenerate(db, story, clip.key, prepared=prepared)
    except LookupError as e:
        raise HTTPException(409, str(e)) from None
    except sound_clips.BudgetReached as e:
        raise HTTPException(402, str(e)) from None
    except sound_clips.Stopped:
        raise HTTPException(409, "Cancelled.") from None
    except UnsafeScriptError as e:
        raise HTTPException(422, str(e)) from None
    return {"take": sound_clips.take_out(row, story_id), "price": price}


@router.post("/stories/{story_id}/clips/{clip_id}/select")
def select_take(story_id: int, clip_id: int, db: Session = Depends(get_db)) -> dict:
    """Use this take of its clip (free)."""
    story = _get(db, story_id)
    _not_busy(story)
    try:
        row = sound_clips.select_take(db, story, clip_id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from None
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    return sound_clips.take_out(row, story_id)


@router.get("/stories/{story_id}/clips/{clip_id}/audio")
def clip_audio(story_id: int, clip_id: int, db: Session = Depends(get_db)) -> FileResponse:
    row = db.get(models.SoundClip, clip_id)
    if row is None or row.story_id != story_id or not row.path:
        raise HTTPException(404, "clip not found")
    from pathlib import Path

    if not Path(row.path).exists():
        raise HTTPException(404, "clip audio not found")
    return FileResponse(row.path, media_type="audio/wav")


def _preview_scope(doc, clips, body: PreviewIn):  # noqa: ANN001, ANN202
    if body.chapter is not None:
        return sound_clips.scope_clips(doc, clips, chapter=body.chapter)
    if body.start_line is not None or body.end_line is not None:
        if body.start_line and body.end_line and body.end_line < body.start_line:
            raise HTTPException(422, "the range ends before it starts")
        return sound_clips.scope_clips(doc, clips, start_line=body.start_line, end_line=body.end_line)
    return sound_clips.scope_clips(doc, clips, first_minutes=body.first_minutes or sound_clips.PREVIEW_MAX_MINUTES)


@router.post("/stories/{story_id}/preview", dependencies=PAID)
def preview(story_id: int, body: PreviewIn, db: Session = Depends(get_db)) -> dict:
    """Make only the clips of a chapter, a range of lines or the first
    minutes (at most 3), and a quick voice + sound effects mix of them."""
    story = _get(db, story_id)
    _not_busy(story)
    prepared = _prepared(story)
    doc, clips = prepared[0], prepared[1]
    chosen = _preview_scope(doc, clips, body)
    if not chosen:
        raise HTTPException(422, "Nothing to preview there.")
    _providers(chosen)
    price = sound_clips.estimate(db, story, clips=clips, keys={c.key for c in chosen})
    spend.require_budget_for(db, price["total"])
    try:
        out = sound_clips.preview(db, story, chapter=body.chapter, start_line=body.start_line,
                                  end_line=body.end_line,
                                  first_minutes=None if body.chapter is not None or body.start_line or body.end_line
                                  else (body.first_minutes or sound_clips.PREVIEW_MAX_MINUTES), prepared=prepared)
    except LookupError as e:
        raise HTTPException(409, str(e)) from None
    except sound_clips.BudgetReached as e:
        raise HTTPException(402, str(e)) from None
    except sound_clips.Stopped:
        raise HTTPException(409, "Cancelled.") from None
    except (UnsafeScriptError, sound_clips.NothingToVoice) as e:
        raise HTTPException(422, str(e)) from None
    return {**out, "price": price, "url": f"/api/sound/stories/{story_id}/preview/audio"}


@router.get("/stories/{story_id}/preview/audio")
def preview_audio(story_id: int, db: Session = Depends(get_db)) -> FileResponse:
    path = sound_clips.preview_path(_get(db, story_id))
    if path is None:
        raise HTTPException(404, "no preview yet")
    return FileResponse(str(path), media_type="audio/mpeg")


# ---------- the sound effects tool and the library ----------


@router.post("/effects/estimate")
def effects_estimate(body: EffectsIn, db: Session = Depends(get_db)) -> dict:
    return sound_clips.effects_estimate(db, body.prompt, body.seconds, loop=body.loop, variations=body.variations)


@router.post("/effects", dependencies=PAID)
def effects(body: EffectsIn, db: Session = Depends(get_db)) -> dict:
    """1-4 variations of a sound effect, saved to the Library. The prompt is
    checked before anything is paid; a sound made before is free."""
    provider_gate.require_sound()
    price = sound_clips.effects_estimate(db, body.prompt, body.seconds, loop=body.loop,
                                         variations=body.variations)
    spend.require_budget_for(db, price["total"])
    try:
        assets = sound_clips.make_effects(db, body.prompt, body.seconds, loop=body.loop,
                                          variations=body.variations)
    except UnsafeScriptError as e:
        raise HTTPException(422, str(e)) from None
    except sound_clips.BudgetReached as e:
        raise HTTPException(402, str(e)) from None
    return {"assets": [{**a, "url": f"/api/sound/assets/{a['id']}/audio"} for a in assets], "price": price}


@router.get("/assets")
def list_assets(db: Session = Depends(get_db)) -> list[dict]:
    rows = db.query(models.SoundAsset).order_by(models.SoundAsset.id.desc()).limit(500).all()
    return [{**sound_clips.asset_out(a), "url": f"/api/sound/assets/{a.id}/audio"} for a in rows]


@router.get("/assets/{asset_id}/audio")
def asset_audio(asset_id: int, db: Session = Depends(get_db)) -> FileResponse:
    from pathlib import Path

    a = db.get(models.SoundAsset, asset_id)
    if a is None or not a.path or not Path(a.path).exists():
        raise HTTPException(404, "sound not found")
    return FileResponse(a.path, media_type="audio/mpeg" if a.path.endswith(".mp3") else "audio/wav")


@router.delete("/assets/{asset_id}", status_code=204)
def delete_asset(asset_id: int, db: Session = Depends(get_db)) -> Response:
    """Remove a sound from the Library (stories keep the clips made from it;
    the next story that asks for it pays for it again)."""
    from pathlib import Path

    a = db.get(models.SoundAsset, asset_id)
    if a is None:
        raise HTTPException(404, "sound not found")
    path = a.path
    db.delete(a)
    db.commit()
    if path:
        Path(path).unlink(missing_ok=True)
    return Response(status_code=204)

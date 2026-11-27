"""Sound Studio stories: the checks on save, the writer job, Enhance, the
claims and Cancel / Resume (api/sound.py).

A story is written chapter by chapter in a job on RQ that holds a claim
(status 'scripting', a random claim_token the job consumes before it
starts, as ambient's loop job does). Every finished chapter is committed at
once, so a Cancel (checked between chapters) or a killed worker keeps the
chapters already paid for, and Resume continues at the next missing
chapter (progress_json["writer"]). Enhance on a selection up to
prompting.AUDIO_SCRIPT_MAX characters runs in the request; a chapter or the
whole story is a job under the same claim, and its result is kept as a
proposal (progress_json["enhance"]) the editor shows as before/after.

Every script that is saved, every written chapter and every Enhance output
is checked (check_script_text / check_part): the spoken words with the
markup removed (safety.validate_script and check_script), what no voice
reads but is kept or sent to models (notes, headings, dropped markup,
overridden cast lines: check_unvoiced, as prose with the cast), then the whole
story check of PR1 (story_script.check_story: the minors check of every
window with the cast header in front and, on a clean deployment, the
explicit-content check). A premise alone is checked by check_premise. Spend
is recorded with ref "sound_story:<id>" and no project.
"""
from __future__ import annotations

import json
import logging
import math
import re
import uuid
from datetime import datetime
from typing import Callable, Optional

from sqlalchemy import case, update
from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from . import events, story_script
from .sound_estimate import MINUTES_PER_CHAPTER_CALL
from .story_script import StoryDoc

log = logging.getLogger("avs.sound_studio")

# A story a job is working on: no edit, no second job.
BUSY = ("scripting", "generating", "rendering")
STYLES = tuple(story_script.WPM_BY_STYLE)
# Styles left out on a clean deployment (romance-type presets).
ADULT_STYLES = ("romance",)
SFX_DENSITIES = ("none", "low", "medium", "high")
ENHANCE_ACTIONS = ("tighten", "lengthen", "asmr", "sfx", "sleep")
WRITER_KEY = "writer"
ENHANCE_KEY = "enhance"
TASK_KEY = "task"

# A job that took its claim over keeps it as RUNNING + token (its
# generation). Cancel keeps it (the call in flight is paid for, so its
# result is still saved) but stops the job before its next call; a newer
# claim replaces it, and every write the job makes is filtered on it, so a
# job whose story was claimed again changes nothing.
RUNNING = "run:"

STOPPED = "Stopped unexpectedly; Resume continues where it left off"
CANCELLED = "Cancelled; Resume continues where it left off"


class EmptyRewrite(ValueError):
    """Enhance came back with nothing (or only the chapter heading)."""


class _Superseded(Exception):
    """The job's claim was cancelled or replaced: it stops, writing nothing."""


class WriterRefused(ValueError):
    """The writer (or Enhance) model would not write it: `reason` "minor"
    or "declined" (prompting.refusal), or the reply couldn't be read."""

    def __init__(self, message: str, reason: str = "declined") -> None:
        super().__init__(message)
        self.reason = reason


def ref(story_id: int) -> str:
    return f"sound_story:{story_id}"


def story_url(story_id: int) -> str:
    return f"/sound/stories/{story_id}"


def style_of(story: models.SoundStory) -> str:
    return str((story.settings_json or {}).get("style") or "")


def parse_story(story: models.SoundStory, text: Optional[str] = None) -> StoryDoc:
    return story_script.parse(story.script_text if text is None else text, style=style_of(story))


def chapter_count(minutes: int) -> int:
    return max(1, math.ceil(max(1, int(minutes or 1)) / MINUTES_PER_CHAPTER_CALL))


def max_minutes() -> int:
    return int(get_settings().sound_story_max_minutes)


# ---------- checks ----------


def check_premise(title: str, premise: str) -> None:
    """A premise (and title) before anything is written: the script word
    check (minors and sexual content: every mode), the minors check of the
    story path (minor_check.check_long, kind "story", fails closed) and, on a
    clean deployment, the explicit-content check. Raises UnsafeScriptError
    (UnsafePromptError / ExplicitContentError): a 422."""
    from . import explicit_check, minor_check
    from .safety import check_script

    text = "\n".join(x.strip() for x in (title or "", premise or "") if x and x.strip())
    if not text:
        return
    check_script(text)
    minor_check.check_long(text, kind="story")
    explicit_check.check(text)


def _spoken(doc: StoryDoc) -> str:
    """The words read aloud, every tag and cue removed."""
    return "\n".join(i.spoken_text for i in doc.speech() if i.spoken_text.strip())


# A parser warning that some of the line was dropped (an unknown [tag], an
# unknown @ command, a malformed @cast line, a cue it couldn't read): the
# raw line is then text no voice reads and check_story never judges.
_DROPPED_RE = re.compile(r"\b(?:skipped|removed|ignored)\b", re.I)
_CAST_DECL_RE = re.compile(r"^@cast\s+([^\[:]+?)\s*((?:\[[^\]]*\]\s*)*):\s*(.*)$", re.I)


def _raw_lines(text: str) -> list[str]:
    """The script's lines as story_script.parse numbers them (from 1)."""
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _cast_decls(text: str) -> list[tuple[int, str, str]]:
    """Every well-formed @cast line: (line number, NAME, the declaration
    after the name, spaces folded)."""
    out = []
    for n, ln in enumerate(_raw_lines(text), start=1):
        m = _CAST_DECL_RE.match(ln.strip())
        if m:
            name = " ".join(m.group(1).split()).upper()
            out.append((n, name, " ".join(f"{m.group(2)}:{m.group(3)}".split()).lower()))
    return out


def _dropped_lines(doc: StoryDoc) -> set[int]:
    return {w.line for w in doc.warnings if w.line and _DROPPED_RE.search(w.message)}


def unvoiced_text(text: str, doc: StoryDoc) -> str:
    """The text of `text` (parsed as `doc`) that no voice reads and
    check_story never judges, one piece per line: notes (// lines), chapter
    headings (an empty chapter's too), a @cast declaration a later one
    overrides, and the raw line wherever the parser dropped part of one
    (an unknown [tag], an unknown @ command, a malformed @cast line, a cue
    it couldn't read). It is still saved, shown and sent to models as
    context, so it is checked too (check_unvoiced)."""
    lines = _raw_lines(text)
    picked: dict[int, str] = {}
    for i in doc.items:
        if i.kind == "comment" and i.text.strip():
            picked[i.line] = i.text
    for ch in doc.chapters:
        if ch.line and ch.title.strip():
            picked[ch.line] = ch.title
    for n in _dropped_lines(doc):
        if 0 < n <= len(lines) and lines[n - 1].strip():
            picked[n] = lines[n - 1].strip()
    for n, name, _ in _cast_decls(text):
        final = doc.cast.get(name)
        m = _CAST_DECL_RE.match(lines[n - 1].strip())
        if final is None or " ".join(m.group(3).split()) != final.description:
            picked[n] = lines[n - 1].strip()
    return "\n".join(picked[k] for k in sorted(picked))


def check_prose(text: str, doc: StoryDoc) -> None:
    """Text that isn't read aloud but is kept or sent to models (notes,
    headings, the outline's chapter plan), checked as prose with the cast
    in front: the script word check, the minors check of the story path
    (minor_check.check_long, kind "story") and, on a clean deployment, the
    explicit-content check. Raises UnsafeScriptError."""
    from . import explicit_check, minor_check
    from .safety import check_script

    if not (text or "").strip():
        return
    check_script(text)
    minor_check.check_long(text, kind="story", context=story_script.cast_header(doc))
    explicit_check.check(story_script.normalize(text, dashes=False))


def check_unvoiced(text: str, doc: StoryDoc) -> None:
    check_prose(unvoiced_text(text, doc), doc)


def context_tail(text: str, *, style: str = "", size: int = 1500) -> str:
    """The end of a script as the writer is shown it (the end of the
    previous chapter): only the lines it voices and its cues, never a note,
    heading or line the parser dropped part of."""
    doc = story_script.parse(text, style=style, max_minutes=0)
    keep = {i.line for i in doc.items if i.kind in ("speech", "sfx", "pause", "bed")} - _dropped_lines(doc)
    lines = _raw_lines(text)
    out = "\n".join(lines[n - 1].strip() for n in sorted(keep) if 0 < n <= len(lines))
    return out[-size:]


def music_prompts(settings: Optional[dict]) -> tuple[str, ...]:
    m = str((settings or {}).get("music_prompt") or "").strip()
    return (m,) if m else ()


def check_doc(doc: StoryDoc, *, title: str = "", premise: str = "", settings: Optional[dict] = None,
              text: Optional[str] = None) -> str:
    """The checks a script gets wherever it comes from (a save, a written
    chapter, an Enhance rewrite): the spoken words without markup
    (safety.validate_script and check_script), with `text` (the script
    `doc` was parsed from) what no voice reads (check_unvoiced), then
    story_script.check_story (minors in every mode, explicit content on a
    clean deployment). Returns the story context. Raises UnsafeScriptError."""
    from .safety import check_script, validate_script

    spoken = _spoken(doc)
    if spoken.strip():
        validate_script(spoken)
        check_script(spoken)
    if text is not None:
        check_unvoiced(text, doc)
    return str(story_script.check_story(doc, title=title, premise=premise,
                                        music_prompts=music_prompts(settings)))


def check_script_text(text: str, *, style: str = "", title: str = "", premise: str = "",
                      settings: Optional[dict] = None) -> Optional[StoryDoc]:
    """check_doc on a whole script (None when it is empty: only the
    premise is checked then). With a script, the title and premise still
    get the explicit-content check of a clean deployment (check_story's
    covers the script, not them)."""
    from . import explicit_check

    if not (text or "").strip():
        check_premise(title, premise)
        return None
    about = "\n".join(x.strip() for x in (title or "", premise or "") if x and x.strip())
    if about:
        explicit_check.check(about)
    doc = story_script.parse(text, style=style)
    check_doc(doc, title=title, premise=premise, settings=settings, text=text)
    return doc


def header_of(text: str) -> str:
    """The script's @cast lines (the cast every part is read with)."""
    return "\n".join(ln for ln in (text or "").splitlines() if ln.strip().lower().startswith("@cast"))


def check_part(header: str, part: str, *, style: str = "", title: str = "", premise: str = "") -> None:
    """One chapter (or one Enhance rewrite) checked with the cast in front
    (check_doc on the header plus the part)."""
    text = f"{header}\n\n{part}" if header else part
    doc = story_script.parse(text, style=style, max_minutes=0)
    check_doc(doc, title=title, premise=premise, text=text)


# ---------- claims ----------


def claim(db: Session, story: models.SoundStory, task: str) -> tuple[str, str]:
    """Claim the story for a job (conditional UPDATE: two requests can't
    both get through): status 'scripting' and a fresh claim_token. Returns
    (token, previous status). Raises LookupError when it is busy."""
    previous = story.status
    token = uuid.uuid4().hex
    progress = dict(story.progress_json or {})
    progress[TASK_KEY] = task
    progress["previous"] = previous
    done = (db.query(models.SoundStory)
            .filter(models.SoundStory.id == story.id, models.SoundStory.status.notin_(BUSY))
            .update({"status": "scripting", "claim_token": token, "queue_job_id": "", "error": "",
                     "progress_json": progress, "heartbeat_at": datetime.utcnow()},
                    synchronize_session=False))
    db.commit()
    if done != 1:
        raise LookupError("this story is busy; wait or cancel first")
    return token, previous


def enqueue_claimed(db: Session, story: models.SoundStory, job: Callable, *args, task: str) -> str:
    """Claim, then queue `job(story.id, *args, claim_token=token)`; the
    queue entry is recorded while the story is still claimed. If nothing
    was queued (Redis gone), the claim is given back."""
    from ..workers.queue import enqueue

    token, previous = claim(db, story, task)
    try:
        job_id = enqueue(job, story.id, *args, claim_token=token)
    except Exception:
        db.query(models.SoundStory).filter(
            models.SoundStory.id == story.id, models.SoundStory.status == "scripting",
            models.SoundStory.claim_token == token,
        ).update({"status": previous, "claim_token": ""}, synchronize_session=False)
        db.commit()
        raise
    db.query(models.SoundStory).filter(
        models.SoundStory.id == story.id, models.SoundStory.status == "scripting",
        models.SoundStory.claim_token.in_((token, running_token(token))),
    ).update({"queue_job_id": str(job_id or "")[:80]}, synchronize_session=False)
    db.commit()
    return str(job_id or "")


def consume_claim(db: Session, story_id: int, token: str, status: str = "scripting") -> bool:
    """The job takes the claim over (conditional UPDATE, committed by the
    caller): a cancel while it waited, or a newer claim, makes it a no-op.
    The token becomes running_token(token), the job's generation."""
    if not token or token.startswith(RUNNING):
        return False
    done = (db.query(models.SoundStory)
            .filter(models.SoundStory.id == story_id, models.SoundStory.status == status,
                    models.SoundStory.claim_token == token)
            .update({"claim_token": running_token(token)}, synchronize_session=False))
    if done != 1:
        log.info("sound story %s claim was cancelled or replaced; skipping its job", story_id)
        return False
    return True


def running_token(token: str) -> str:
    return f"{RUNNING}{token}"


def _held(query, story_id: int, gen: str):  # noqa: ANN001, ANN202 — a SoundStory query
    """`query` narrowed to the story while no newer claim replaced this job's
    (generation `gen`): it may still save the result of its call in flight,
    even after a Cancel."""
    S = models.SoundStory
    return query.filter(S.id == story_id, S.claim_token == gen)


def _mine(query, story_id: int, gen: str):  # noqa: ANN001, ANN202 — a SoundStory query
    """`query` narrowed to the story while this job (generation `gen`)
    still runs it: claimed by it and not cancelled."""
    return _held(query, story_id, gen).filter(models.SoundStory.status == "scripting")


def _alive(story_id: int, gen: str) -> bool:
    """Whether this job still holds the story, read in a fresh session (a
    Cancel or a newer claim commits there while the job runs)."""
    from ..db import SessionLocal

    s = SessionLocal()
    try:
        return _mine(s.query(models.SoundStory.id), story_id, gen).first() is not None
    finally:
        s.close()


def _back_to(progress: dict) -> str:
    prev = str(progress.get("previous") or "draft")
    return "draft" if prev in BUSY else prev


def cancel(db: Session, story: models.SoundStory) -> dict:
    """Cancel the story's job. Waiting in the queue (its claim not taken
    yet): released at once. Running: marked, and the job stops before its
    next call (the call in flight is still paid for and kept)."""
    if story.status != "scripting":
        return {"status": story.status, "action": "noop"}
    progress = dict(story.progress_json or {})
    task = progress.get(TASK_KEY)
    to = "cancelled" if task == WRITER_KEY else _back_to(progress)
    if task == ENHANCE_KEY:
        progress[ENHANCE_KEY] = {**(progress.get(ENHANCE_KEY) or {}), "status": "cancelled"}
    waiting = not (story.claim_token or "").startswith(RUNNING)
    S = models.SoundStory
    # A running job keeps its generation (its call in flight is still
    # saved); a claim no job has taken yet is given back.
    keep = case((S.claim_token.startswith(RUNNING), S.claim_token), else_="")
    done = (db.query(S)
            .filter(S.id == story.id, S.status == "scripting")
            .update({"status": to, "claim_token": keep, "progress_json": progress,
                     "error": CANCELLED if to == "cancelled" else ""}, synchronize_session=False))
    if done != 1:
        db.rollback()
        db.refresh(story)
        return {"status": story.status, "action": "noop"}
    db.refresh(story)
    story_event(db, story, "cancelled")
    db.commit()
    return {"status": story.status, "action": "released" if waiting else "signalled"}


# ---------- events ----------


def story_event(db: Session, story: models.SoundStory, status: str, *, detail: str = "",
                error: str = "") -> None:
    events.sound_story_event(db, story, status, detail=detail, error=error)


# ---------- the writer ----------


def _ask(call: Callable, *, text: str = "") -> tuple[str, str]:
    """One writer call through the active chat model (prompting._chat_for:
    the planner, or the adult text model in adult mode): (reply, model). A
    refusal (prompting.refusal) in adult mode is retried once with the
    adult fallback model; outside adult mode it stands. A refusal that
    remains raises WriterRefused."""
    from . import content_mode, model_registry, prompting

    adult = content_mode.adult_mode()

    def run(m: str) -> tuple[str, str]:
        chat = prompting._chat_for(m)
        out = call(chat)
        return out, getattr(chat, "model", "") or m or model_registry.active_model("chat")

    out, used = run("")
    why = writer_refusal(out, text) if out.strip() else "declined"
    if why == "declined" and adult:
        fallback = get_settings().adult_text_fallback_model or ""
        if fallback and fallback != used:
            log.info("story writer: %s declined; retrying with %s", used, fallback)
            out, used = run(fallback)
            why = writer_refusal(out, text) if out.strip() else "declined"
    if why:
        raise _refused(why, used)
    return out, used


def _refused(why: str, used: str) -> WriterRefused:
    from . import content_mode

    if why == "minor":
        return WriterRefused("The writer refused this story: it appears to sexualise a minor.", "minor")
    if not content_mode.features():
        msg = f"The writer ({used}) declined to write this; this server writes general-audience stories only."
    else:
        msg = f"The writer ({used}) declined to write this; change the premise and try again."
    return WriterRefused(msg, "declined")


_MINOR_SENTINEL_RE = re.compile(r"\bREFUSED_MINOR\b")
_DECLINED_RE = re.compile(r"\bDECLINED\b")
_DECLINED_LEAD_RE = re.compile(r"^\W*DECLINED\b")
# A reply this short that says DECLINED anywhere is a refusal, explained
# or wrapped ("DECLINED\n\nI won't write this.", {"error": "DECLINED"}).
SHORT_REPLY = 600


def writer_refusal(out: str, text: str = "") -> Optional[str]:
    """prompting.refusal, and the sentinel however it comes: REFUSED_MINOR
    anywhere in a reply is a minors refusal (no story says it); DECLINED
    leading a reply, or anywhere in a short one, is a refusal (a minors one
    when it names a minor or `text` mentions one)."""
    from . import prompting

    why = prompting.refusal(out, text)
    if why:
        return why
    flat = " ".join((out or "").split())
    if _MINOR_SENTINEL_RE.search(flat):
        return "minor"
    if _DECLINED_LEAD_RE.match(flat) or (len(flat) <= SHORT_REPLY and _DECLINED_RE.search(flat)):
        return prompting.refusal(f"DECLINED: {flat[:200]}", text) or "declined"
    return None


def outline_refusal(data: object, text: str = "") -> Optional[str]:
    """A refusal wrapped in the outline's JSON ({"refusal": "REFUSED_MINOR"},
    {"title": "DECLINED"}, {"chapters": [], "reason": "I can't write
    this."}), as hook_lab.wrapped_refusal reads a hook planner's: REFUSED_MINOR
    anywhere; DECLINED as a whole value or leading one, or under a refusal
    key; a refusal in words when the outline has no chapter to write."""
    from . import prompting
    from .hook_lab import _REFUSAL_KEYS, _strings

    found: Optional[str] = None
    for t, _ in _strings(data):
        flat = " ".join(str(t).split())
        if _MINOR_SENTINEL_RE.search(flat):
            return "minor"
        if _DECLINED_LEAD_RE.match(flat):
            found = prompting.refusal(f"DECLINED: {flat[:200]}", text) or "declined"
    if found:
        return found
    if isinstance(data, dict) and any(data.get(k) for k in _REFUSAL_KEYS):
        return prompting.refusal("DECLINED", text) or "declined"
    chapters = data.get("chapters") if isinstance(data, dict) else None
    if not (isinstance(chapters, list) and any(isinstance(c, dict) for c in chapters)):
        for t, _ in _strings(data):
            why = prompting.refusal(str(t), text)
            if why:
                return why
    return None


_NAME_OK = re.compile(r"^[A-Z][A-Z0-9 .'\-]{0,39}$")


def read_outline(reply: str, chapters: int, *, text: str = "", model: str = "") -> dict:
    """The writer's outline, checked and trimmed: a title, a cast of at most
    12 (NARRATOR first), exactly `chapters` chapters. A refusal wrapped in
    its JSON (outline_refusal) raises WriterRefused."""
    from ..providers.openrouter_chat import extract_json

    try:
        data = extract_json(reply)
    except Exception:  # noqa: BLE001
        raise WriterRefused("The writer's outline couldn't be read; try again.", "unreadable") from None
    why = outline_refusal(data, text)
    if why:
        raise _refused(why, model or "the writer")
    if not isinstance(data, dict):
        raise WriterRefused("The writer's outline couldn't be read; try again.", "unreadable")
    cast: list[dict] = []
    seen: set[str] = set()
    for c in data.get("cast") or []:
        if not isinstance(c, dict):
            continue
        name = " ".join(str(c.get("name") or "").upper().replace("[", " ").replace("]", " ").replace(":", " ")
                        .split())[:40]
        if not name or not _NAME_OK.match(name) or name in seen:
            continue
        seen.add(name)
        pan = str(c.get("pan") or "").lower()
        desc = " ".join(str(c.get("description") or "").replace("[", "(").replace("]", ")").split())[:300]
        cast.append({"name": name, "pan": pan if pan in story_script.PLACEMENTS else "",
                     "description": desc})
    if story_script.NARRATOR not in seen:
        cast.insert(0, {"name": story_script.NARRATOR, "pan": "center", "description": "calm adult narrator"})
    cast = cast[:12]
    raw = [c for c in data.get("chapters") or [] if isinstance(c, dict)]
    out_ch = []
    for i in range(chapters):
        c = raw[i] if i < len(raw) else {}
        title = " ".join(str(c.get("title") or f"Chapter {i + 1}").replace("#", " ").split())[:120]
        out_ch.append({"title": title, "summary": " ".join(str(c.get("summary") or "").split())[:800]})
    return {"title": " ".join(str(data.get("title") or "").split())[:200], "cast": cast, "chapters": out_ch}


def cast_lines(outline: dict) -> str:
    out = []
    for c in outline.get("cast") or []:
        pan = f" [{c['pan']}]" if c.get("pan") else ""
        out.append(f"@cast {c['name']}{pan}: {c.get('description') or ''}".rstrip())
    return "\n".join(out)


# A chapter's own structure lines are the app's (headings, cast): the
# writer's are dropped.
_STRUCTURE_RE = re.compile(r"^\s*(?:#|@cast\b)", re.I)


def clean_chapter(reply: str) -> str:
    t = (reply or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return "\n".join(ln.rstrip() for ln in t.splitlines() if not _STRUCTURE_RE.match(ln)).strip()


def chapter_heading(index: int, title: str) -> str:
    return f"# Chapter {index + 1}: {title}".rstrip(": ")


def _with_cast(header: str, text: str) -> str:
    """`text` (a script with no lines yet) with its @cast lines replaced by
    `header`: the cast the writer's outline gave is the one the chapters are
    written and checked with."""
    rest = "\n".join(ln for ln in (text or "").splitlines() if not ln.strip().lower().startswith("@cast")).strip()
    return f"{header}\n\n{rest}\n" if rest else f"{header}\n"


def write_story(db: Session, story_id: int, gen: str = "") -> None:
    """The writer job's body (claim consumed by the caller; `gen` is the
    claim it holds, running_token(token)): the outline, then each missing
    chapter, committed one by one; stops before the next call when the
    story was cancelled or claimed again, and none of its writes land once
    its claim is gone. Ends 'ready', 'cancelled' or 'failed' (with what was
    written kept).

    A "Write again" (writer["replace"]) keeps the old script until the first
    new chapter is written over it. Every chapter is checked alone with the
    cast and then with the whole story it would join before it is saved."""
    from .safety import UnsafeScriptError

    story = db.get(models.SoundStory, story_id)
    if story is None:
        return
    settings = dict(story.settings_json or {})
    style = str(settings.get("style") or "")
    density = str(settings.get("sfx_density") or "medium")
    minutes = max(1, min(int(story.target_minutes or 10), max_minutes()))
    progress = dict(story.progress_json or {})
    writer = dict(progress.get(WRITER_KEY) or {})
    n = chapter_count(minutes)
    start_text = story.script_text or ""
    story_event(db, story, "started")
    db.commit()

    def save(expect: Optional[int] = None, **fields) -> None:  # noqa: ANN003
        """Commit-ready update while the claim is this job's; with `expect`
        (a script it saves) only over the script_version it read, so a script
        saved meanwhile is never overwritten."""
        nonlocal progress
        progress = {**progress, WRITER_KEY: dict(writer)}
        q = _held(db.query(models.SoundStory), story_id, gen)
        if expect is not None:
            q = q.filter(models.SoundStory.script_version == expect)
        done = q.update({"progress_json": progress, "heartbeat_at": datetime.utcnow(), **fields},
                        synchronize_session=False)
        if done != 1:
            raise _Superseded()

    def finish(status: str, error: str = "", *, held: bool = False) -> None:
        # `held`: the story is finished even when it was cancelled after its
        # last chapter was asked for (that chapter is paid for and saved).
        scope = _held if held else _mine
        done = scope(db.query(models.SoundStory), story_id, gen).update(
            {"status": status, "error": error, "claim_token": ""}, synchronize_session=False)
        db.commit()
        if done == 1:
            db.refresh(story)
            story_event(db, story, status if status != "ready" else "completed", error=error)
            db.commit()

    def check_whole(text: str) -> None:
        check_script_text(text, style=style, title=story.title or "", premise=story.premise or "",
                          settings=settings)

    try:
        outline = writer.get("outline")
        if not outline:
            seen_version = story.script_version or 0
            reply, used = _ask(lambda chat: chat.story_outline(
                premise=story.premise or "", title=story.title or "", minutes=minutes, style=style,
                sfx_density=density, chapters=n), text=story.premise or "")
            outline = read_outline(reply, n, text=story.premise or "", model=used)
            header = cast_lines(outline)
            # The cast (who each character is, and their age) is checked with
            # the premise before any chapter is written.
            check_part(header, "", style=style, title=outline.get("title") or story.title or "",
                       premise=story.premise or "")
            # The plan every chapter call is sent (the title, each chapter's
            # title and summary) and the title saved: checked as prose with
            # the cast, explicit content too on a clean deployment.
            check_prose(outline_text(outline), story_script.parse(header, style=style, max_minutes=0))
            writer.update({"outline": outline, "chapters_done": 0, "chapters": n, "model": used})
            fields: dict = {}
            old = story.script_text or ""
            if not writer.get("replace") and not parse_story(story, old).speech():
                # No lines yet: the outline's cast replaces any cast there.
                new_text = _with_cast(header, old)
                if new_text != old:
                    fields = {"script_text": new_text, "script_version": seen_version + 1}
            if not (story.title or "").strip() and outline.get("title"):
                fields["title"] = outline["title"]
            save(expect=seen_version if "script_text" in fields else None, **fields)
            db.commit()
            db.refresh(story)
        total = len(outline.get("chapters") or []) or n
        cap = max_minutes() * 60.0
        for i in range(int(writer.get("chapters_done") or 0), total):
            if not _alive(story_id, gen):
                log.info("sound story %s cancelled before chapter %s", story_id, i + 1)
                return
            db.refresh(story)
            replacing = bool(writer.get("replace"))
            seen_text, seen_version = story.script_text or "", story.script_version or 0
            header = cast_lines(outline) if replacing else (header_of(seen_text) or cast_lines(outline))
            previous = "" if replacing else context_tail(seen_text, style=style)
            per = chapter_minutes(0.0 if replacing else parse_story(story, seen_text).est_seconds,
                                  minutes=minutes, cap=cap, left=total - i,
                                  squeeze=float(writer.get("squeeze") or 1.0), index=i)
            reply, _ = _ask(lambda chat, i=i, previous=previous, per=per: chat.story_chapter(
                premise=story.premise or "", outline=outline, index=i, minutes=per, style=style,
                sfx_density=density, previous=previous), text=story.premise or "")
            body = clean_chapter(reply)
            if not body:
                raise WriterRefused("The writer returned an empty chapter; Resume tries again.", "unreadable")
            part = f"{chapter_heading(i, outline['chapters'][i]['title'])}\n{body}"
            check_part(header, part, style=style, title=story.title or "", premise=story.premise or "")
            base = header if replacing else seen_text.rstrip()
            text = base + "\n\n" + part + "\n"
            if story_script.parse(text, style=style, max_minutes=0).est_seconds > cap:
                # Too long for the cap: the next try asks for less (and one
                # that can't fit is refused before it is paid for).
                writer["squeeze"] = round(float(writer.get("squeeze") or 1.0) * 0.6, 3)
                save()
                db.commit()
                raise WriterRefused(f"Chapter {i + 1} came out longer than the {max_minutes()}-minute limit "
                                    "allows; Resume asks for a shorter one.", "too_long")
            # The chapter with the story it joins (facts from earlier chapters,
            # such as a character's age, count): nothing unsafe is saved.
            check_whole(text)
            writer["chapters_done"] = i + 1
            writer.pop("replace", None)
            writer.pop("squeeze", None)
            save(expect=seen_version, script_text=text, script_version=seen_version + 1)
            story_event(db, story, "progress", detail=f"Chapter {i + 1} of {total} written")
            db.commit()
            db.refresh(story)
        # The whole story, as a save checks it.
        try:
            check_whole(story.script_text)
        except UnsafeScriptError:
            # Never leave refused text saved: back to the script this run began with.
            if (story.script_text or "") != start_text:
                v = story.script_version or 0
                save(expect=v, script_text=start_text, script_version=v + 1)
                db.commit()
            raise
        writer["finished"] = True
        save()
        db.commit()
        # Ready even when Cancel came while the last chapter was written.
        finish("ready", held=True)
    except _Superseded:
        db.rollback()
        log.info("sound story %s: the writer's claim was cancelled or replaced; stopping", story_id)
    except (WriterRefused, UnsafeScriptError) as e:
        db.rollback()
        finish("failed", str(e))
    except Exception as e:  # noqa: BLE001
        db.rollback()
        log.exception("sound story %s: writing failed", story_id)
        finish("failed", f"Writing stopped: {str(e)[:300]}. Resume continues where it left off.")


# The shortest chapter worth asking for, in seconds: with less room left
# under the cap, the chapter is refused before it is paid for.
MIN_CHAPTER_SECONDS = 30.0


def chapter_minutes(so_far: float, *, minutes: float, cap: float, left: int, squeeze: float = 1.0,
                    index: int = 0) -> float:
    """The minutes chapter `index` is asked for: its share of what is left
    of the story's length (`so_far` seconds written of `minutes`), never
    more than its share of the room left under the cap (`cap` seconds,
    less the gaps between chapters), times `squeeze` (smaller after a
    chapter came out too long). Raises WriterRefused when there's no room
    left for a chapter."""
    from .story_script import CHAPTER_GAP

    left = max(1, left)
    room = cap - so_far - CHAPTER_GAP * left
    if room / left < MIN_CHAPTER_SECONDS:
        raise WriterRefused(f"The story is already about {so_far / 60:.0f} minutes, at the "
                            f"{cap / 60:.0f}-minute limit: there's no room for chapter {index + 1}. Shorten "
                            "the script or lower the length, then Resume.", "too_long")
    share = max(MIN_CHAPTER_SECONDS, (minutes * 60.0 - so_far) / left)
    return round(max(MIN_CHAPTER_SECONDS, min(share, room / left) * squeeze) / 60.0, 2)


def outline_text(outline: dict) -> str:
    """The outline's title and chapter plan as prose (what check_prose reads)."""
    out = [str(outline.get("title") or "")]
    for c in outline.get("chapters") or []:
        out += [str(c.get("title") or ""), str(c.get("summary") or "")]
    return "\n".join(x for x in out if x.strip())


def can_resume(story: models.SoundStory) -> bool:
    """A writer run that stopped part-way (cancelled, failed or swept):
    Resume continues at the next missing chapter (or the outline, when that
    is what stopped; or only the final check, when every chapter landed).
    Judged on the writer's own progress, so an Enhance on the story since
    (its task, its cancel, the sweep or a queue rollback) keeps it."""
    progress = story.progress_json or {}
    w = progress.get(WRITER_KEY) or {}
    if story.status not in ("cancelled", "failed"):
        return False
    return progress.get(TASK_KEY) in (WRITER_KEY, ENHANCE_KEY) or (bool(w) and not w.get("finished"))


def writer_progress(story: models.SoundStory) -> dict:
    w = (story.progress_json or {}).get(WRITER_KEY) or {}
    return {"chapters_done": int(w.get("chapters_done") or 0), "chapters": int(w.get("chapters") or 0),
            "has_outline": bool(w.get("outline")), "finished": bool(w.get("finished"))}


def reset_writer(story: models.SoundStory, *, replace: bool = False) -> None:
    """A fresh Write: forget an earlier run's outline and chapters. With
    `replace` the script it has is kept until the job writes the first new
    chapter over it."""
    progress = dict(story.progress_json or {})
    progress.pop(WRITER_KEY, None)
    if replace:
        progress[WRITER_KEY] = {"replace": True}
    story.progress_json = progress


def write_estimate(minutes: int) -> dict:
    calls = 1 + chapter_count(minutes)
    unit = float(get_settings().cost_chat_per_story_call)
    return {"calls": calls, "unit_cost": unit, "cost": round(calls * unit, 4)}


def remaining_write_estimate(story: models.SoundStory) -> dict:
    w = (story.progress_json or {}).get(WRITER_KEY) or {}
    n = int(w.get("chapters") or chapter_count(story.target_minutes or 10))
    calls = (0 if w.get("outline") else 1) + max(0, n - int(w.get("chapters_done") or 0))
    unit = float(get_settings().cost_chat_per_story_call)
    return {"calls": calls, "unit_cost": unit, "cost": round(calls * unit, 4)}


# ---------- Enhance ----------


def direction(action: str, minutes: Optional[int] = None) -> str:
    from ..providers.openrouter_chat import AUDIO_SCRIPT_ACTIONS

    text = AUDIO_SCRIPT_ACTIONS[action]
    return f"{action}: " + text.replace("{minutes}", str(int(minutes or 5)))


def pieces(text: str, size: int) -> list[str]:
    """`text` cut at line ends into pieces of at most `size` characters (a
    single longer line is cut between words)."""
    out: list[str] = []
    buf = ""
    for ln in text.split("\n"):
        while len(ln) > size:
            cut = ln.rfind(" ", 0, size)
            cut = cut if cut > 0 else size
            if buf:
                out.append(buf)
                buf = ""
            out.append(ln[:cut])
            ln = ln[cut:].lstrip()
        cand = f"{buf}\n{ln}" if buf else ln
        if len(cand) > size and buf:
            out.append(buf)
            buf = ln
        else:
            buf = cand
    if buf.strip():
        out.append(buf)
    return out


def segments(text: str) -> list[tuple[int, int, bool]]:
    """`text` cut at its chapter headings into (start, end, enhance) parts
    that cover all of it: each chapter from its heading to the next one,
    and the text before the first heading (enhanced only when it says
    anything besides the cast and notes)."""
    starts: list[int] = []
    pos = 0
    for ln in text.splitlines(keepends=True):
        if ln.startswith("#"):
            starts.append(pos)
        pos += len(ln)
    if not starts or starts[0] != 0:
        head = text[: starts[0]] if starts else text
        said = any(ln.strip() and not ln.strip().lower().startswith(("@cast", "//")) for ln in head.splitlines())
        out = [(0, len(head), said)]
    else:
        out = []
    for k, st in enumerate(starts):
        out.append((st, starts[k + 1] if k + 1 < len(starts) else len(text), True))
    return [x for x in out if x[1] > x[0]]


def _bare(reply: str) -> str:
    t = (reply or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def enhance_text(text: str, action: str, *, header: str = "", style: str = "", title: str = "",
                 premise: str = "", minutes: Optional[int] = None) -> str:
    """One Enhance call on at most prompting.AUDIO_SCRIPT_MAX characters,
    checked as a written part is (check_part). A chapter keeps its own
    heading. Raises EnhanceRefused, UnsafeScriptError or EmptyRewrite (an
    empty reply, or only the heading back, is never a rewrite)."""
    from . import prompting

    # What no voice reads is sent to the model too: checked first (a script
    # saved before these checks may hold some).
    sent = f"{header}\n\n{text}" if header else text
    check_unvoiced(sent, story_script.parse(sent, style=style, max_minutes=0))
    out = _bare(prompting.enhance(text, "audio_script", direction=direction(action, minutes))["text"])
    out = _reply_cast(text, out, header)
    first = text.lstrip().splitlines()[0] if text.strip() else ""
    if first.startswith("#") and not out.startswith(first):
        out = f"{first}\n" + "\n".join(ln for ln in out.splitlines() if not ln.startswith("#")).strip()
    said = text.strip() != first.strip() if first.startswith("#") else bool(text.strip())
    if said and (not out.strip() or (first.startswith("#") and out.strip() == first.strip())):
        raise EmptyRewrite("The text model returned an empty rewrite; try again. Your text is unchanged.")
    check_part(header, out, style=style, title=title, premise=premise)
    return out


def _reply_cast(text: str, out: str, header: str) -> str:
    """An Enhance reply's @cast lines: dropped when the range it rewrote
    had none (the cast is the header's); otherwise a reply that declares a
    name the header (or the reply itself) already declares, differently,
    is refused: the later line would win, and the story would be checked
    with the new declaration in place of the one it has."""
    from .safety import UnsafeScriptError

    def is_cast(ln: str) -> bool:
        return ln.strip().lower().startswith("@cast")

    if not any(is_cast(ln) for ln in text.splitlines()):
        return "\n".join(ln for ln in out.splitlines() if not is_cast(ln)).strip()
    known = {name: decl for _, name, decl in _cast_decls(header)}
    for _, name, decl in _cast_decls(out):
        if name in known and known[name] != decl:
            raise UnsafeScriptError(f"The rewrite changes how {name} is declared in the cast; the cast is "
                                    "edited by hand only.")
        known[name] = decl
    return out


def enhance_estimate(story: models.SoundStory, start: int, end: int) -> dict:
    from . import prompting

    text = (story.script_text or "")[start:end]
    calls = sum(len(pieces(text[a:b].strip("\n"), prompting.AUDIO_SCRIPT_MAX))
                for a, b, on in segments(text) if on and text[a:b].strip()) or 1
    unit = float(get_settings().cost_chat_per_story_call)
    return {"calls": calls, "unit_cost": unit, "cost": round(calls * unit, 4)}


def enhance_range(db: Session, story_id: int, gen: str = "") -> None:
    """The Enhance job's body (claim consumed by the caller; `gen` is the
    claim it holds): each chapter (or piece) of the range rewritten and
    checked, the result kept as a proposal (progress_json["enhance"]), then
    the story goes back to what it was. Stops before the next call when
    cancelled or claimed again; once a newer claim replaced its own, nothing
    it writes lands (the proposal belongs to the newer Enhance)."""
    from . import prompting
    from .safety import UnsafeScriptError

    story = db.get(models.SoundStory, story_id)
    if story is None:
        return
    progress = dict(story.progress_json or {})
    job = dict(progress.get(ENHANCE_KEY) or {})
    start, end = int(job.get("start") or 0), int(job.get("end") or 0)
    text = (story.script_text or "")[start:end]
    style = style_of(story)
    header = header_of(story.script_text)
    out_parts: list[str] = []
    story_event(db, story, "started", detail="Enhance")
    db.commit()

    def finish(state: str, error: str = "", after: str = "") -> None:
        prog = dict(story.progress_json or {})
        cur = dict(prog.get(ENHANCE_KEY) or job)
        cur.update({"status": state, "error": error})
        if after:
            cur["after"] = after
        prog[ENHANCE_KEY] = cur
        done = _mine(db.query(models.SoundStory), story_id, gen).update(
            {"status": _back_to(prog), "claim_token": "", "progress_json": prog}, synchronize_session=False)
        db.commit()
        if done == 1:
            db.refresh(story)
            story_event(db, story, "completed" if state == "ready" else "failed", detail="Enhance", error=error)
            db.commit()

    try:
        parts = segments(text)
        total = sum(1 for _, _, on in parts if on) or 1
        # Lengthen asks for N minutes in all: each piece gets its share, by
        # length, so four chapters don't each grow to N minutes.
        asked = job.get("minutes")
        voiced = sum(len(text[a:b].strip("\n")) for a, b, on in parts if on and text[a:b].strip()) or 1
        k = -1
        for a, b, on in parts:
            span = text[a:b]
            if not on or not span.strip():
                out_parts.append(span)
                continue
            k += 1
            if not _alive(story_id, gen):
                return
            lead = span[: len(span) - len(span.lstrip("\n"))]
            trail = span[len(span.rstrip("\n")):]
            body = span.strip("\n")
            rewritten = []
            for piece in pieces(body, prompting.AUDIO_SCRIPT_MAX):
                if not _alive(story_id, gen):
                    return
                share = max(1, round(float(asked) * len(piece) / voiced)) if asked else None
                rewritten.append(enhance_text(piece, str(job.get("action")), header=header, style=style,
                                              title=story.title or "", premise=story.premise or "",
                                              minutes=share))
            new = "\n".join(rewritten)
            # Every rewritten chapter is checked whole, with the cast.
            check_part(header, new, style=style, title=story.title or "", premise=story.premise or "")
            out_parts.append(lead + new + (trail or ("\n" if b < len(text) else "")))
            db.refresh(story)
            prog = dict(story.progress_json or {})
            prog[ENHANCE_KEY] = {**(prog.get(ENHANCE_KEY) or job), "done": k + 1, "total": total}
            if _mine(db.query(models.SoundStory), story_id, gen).update(
                    {"progress_json": prog, "heartbeat_at": datetime.utcnow()}, synchronize_session=False) != 1:
                db.rollback()
                return
            story_event(db, story, "progress", detail=f"Enhance: part {k + 1} of {total}")
            db.commit()
            db.refresh(story)
        finish("ready", after="".join(out_parts))
    except prompting.EnhanceRefused as e:
        db.rollback()
        finish("failed", refused_message(e))
    except UnsafeScriptError as e:
        db.rollback()
        finish("failed", f"The rewrite was refused: {e} Your text is unchanged.")
    except EmptyRewrite as e:
        db.rollback()
        finish("failed", str(e))
    except Exception as e:  # noqa: BLE001
        db.rollback()
        log.exception("sound story %s: enhance failed", story_id)
        finish("failed", f"Enhance stopped: {str(e)[:300]}. Your text is unchanged.")


def refused_message(e) -> str:  # noqa: ANN001 — prompting.EnhanceRefused
    if e.reason == "minor":
        return "This text was refused: it appears to sexualise a minor. Your text is unchanged."
    if e.reason == "unsafe_rewrite":
        return "The rewrite was refused by the minors check, so your text is unchanged."
    return f"The text model ({e.model}) declined to rewrite this, so your text is unchanged."


# ---------- the sweeper ----------


def sweep(db: Session, *, now: datetime, limit, heartbeat) -> dict:  # noqa: ANN001 — timedeltas
    """Stories a dead worker left 'scripting' (or generating / rendering):
    past the queue time limit, or without a heartbeat for `heartbeat`, and
    whose queue entry is gone -> failed (Resume continues)."""
    from ..workers.queue import queue_entry_state

    S = models.SoundStory
    out = {"failed": 0}
    rows = (db.query(S).filter(S.status.in_(BUSY), S.updated_at <= now - min(limit, heartbeat))
            .order_by(S.id).limit(50).all())
    for story in rows:
        beat = story.heartbeat_at or story.updated_at
        stale = story.updated_at <= now - limit or (beat is not None and beat <= now - heartbeat)
        if not stale or queue_entry_state(story.queue_job_id or "") != "gone":
            continue
        progress = dict(story.progress_json or {})
        to = "failed"
        if progress.get(TASK_KEY) == ENHANCE_KEY:
            to = _back_to(progress)
            progress[ENHANCE_KEY] = {**(progress.get(ENHANCE_KEY) or {}), "status": "failed", "error": STOPPED}
        res = db.execute(update(S).where(S.id == story.id, S.status == story.status,
                                         S.updated_at == story.updated_at)
                         .values(status=to, error=STOPPED if to == "failed" else "", claim_token="",
                                 progress_json=progress, updated_at=datetime.utcnow()))
        if res.rowcount != 1:
            db.rollback()
            continue
        db.refresh(story)
        story_event(db, story, "failed", error=STOPPED)
        db.commit()
        out["failed"] += 1
    return out


# ---------- output ----------


def doc_out(doc: StoryDoc) -> dict:
    return {
        "chapters": [{"index": c.index, "title": c.title, "line": c.line, "items": c.item_count,
                      "est_seconds": round(c.est_seconds, 1)} for c in doc.chapters],
        "cast": [{"name": c.name, "pan": c.pan, "description": c.description, "declared": c.declared}
                 for c in doc.cast.values()],
        "warnings": [{"line": w.line, "message": w.message} for w in doc.warnings],
        "est_seconds": round(doc.est_seconds, 1),
        "voiced_chars": doc.voiced_chars,
        "words": doc.word_count,
        "sfx": len(doc.sfx()),
    }


def to_out(story: models.SoundStory, *, full: bool = True) -> dict:
    progress = dict(story.progress_json or {})
    out = {
        "id": story.id, "title": story.title or "", "kind": story.kind or "story", "premise": story.premise or "",
        "target_minutes": story.target_minutes or 10, "settings": dict(story.settings_json or {}),
        "script_version": story.script_version or 0, "status": story.status, "error": story.error or "",
        "estimated_cost": story.estimated_cost or 0.0, "actual_cost": story.actual_cost or 0.0,
        "task": progress.get(TASK_KEY) if story.status in BUSY else None,
        "writer": writer_progress(story), "can_resume": can_resume(story),
        "created_at": story.created_at.isoformat() if story.created_at else None,
        "updated_at": story.updated_at.isoformat() if story.updated_at else None,
    }
    if full:
        out["script_text"] = story.script_text or ""
        out["enhance"] = progress.get(ENHANCE_KEY)
        out["parsed"] = doc_out(parse_story(story))
    return out


def dumps(x) -> str:  # noqa: ANN001
    return json.dumps(x, ensure_ascii=False)

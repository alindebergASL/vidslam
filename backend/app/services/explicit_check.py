"""The explicit-content check of a clean deployment (ADULT_FEATURES=false).

On a clean deployment the planner is told to keep everything non-sexual,
but the spoken words can come from elsewhere: a script typed into the
Voiceover & captions card, a shot's caption, an Enhance rewrite, a hook-lab
opening, edits kept by "Keep my edits", or the original script itself. They
are voiced, lip-synced and captioned, so before any of them is saved or
voiced, the planner (the same model and cache as the minors check,
services/minor_check.py) is asked one question: is this sexually explicit
or pornographic? A yes is refused with a plain message.

It fails closed: when the check can't run (no key, the planner switched
off, an error or an unreadable reply), or the model won't judge the text,
the text is refused too. In mock mode a word list answers (mock_chat).

With ADULT_FEATURES unset (true) nothing here runs: check() returns at once.
"""
from __future__ import annotations

import hashlib
import logging

from . import content_mode, minor_check
from .safety import UnsafeScriptError

log = logging.getLogger("avs.explicit_check")

# Bump to invalidate every cached verdict when the question changes.
POLICY_VERSION = "explicit-1"
# What the model reads at a time (openrouter_chat.classify_explicit cuts at
# minor_check.JUDGE_WINDOW); longer text is read in overlapping parts.
PART = 5600
OVERLAP = 300

REFUSED = ("This text was refused: this app is for a general audience, and the content check found it "
           "sexually explicit. Reword it without sexual content.")
UNAVAILABLE = ("The content check couldn't run, so this text is paused until it's available (check the "
               "planner model in Settings → Models).")
DECLINED = ("This text was refused: the content check model declined to judge it, which counts as a no. "
            "A different planner model (Settings → Models) may answer.")


class ExplicitContentError(UnsafeScriptError):
    """Script text refused on a clean deployment: explicit, or the check
    couldn't say it isn't."""


def text_hash(text: str) -> str:
    raw = "\0".join((POLICY_VERSION, minor_check._key_text(text)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parts(text: str) -> list[str]:
    """The text in parts the model reads whole, each starting a little
    before the last one ended, so no phrase is only ever seen cut in two."""
    text = " ".join((text or "").split())
    if len(text) <= PART:
        return [text] if text else []
    out: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + PART, len(text))
        if end < len(text):
            space = text.rfind(" ", start + PART // 2, end)
            end = space if space > start else end
        out.append(text[start:end].strip())
        if end >= len(text):
            break
        back = text.rfind(" ", end - OVERLAP, end)
        start = back + 1 if back > start else end
    return [p for p in out if p]


def _judge(text: str) -> tuple[bool, str, str]:
    """(explicit, reason, judge); raises minor_check.CheckUnavailable (or
    CheckDeclined)."""
    chat, model = minor_check._classifier()
    judge = minor_check._identity() or model
    fn = getattr(chat, "classify_explicit", None)
    if not callable(fn):
        raise minor_check.CheckUnavailable("the planner has no content check")
    from ..providers.openrouter_chat import ModelDeclined

    try:
        out = fn(text)
    except ModelDeclined as e:
        raise minor_check.CheckDeclined(str(e)[:200]) from None
    except Exception as e:  # noqa: BLE001 — HTTP error, timeout, garbage
        raise minor_check.CheckUnavailable(f"{type(e).__name__}: {str(e)[:200]}") from None
    verdict = out.get("explicit") if isinstance(out, dict) else None
    if not isinstance(verdict, bool):
        raise minor_check.CheckUnavailable("unreadable verdict")
    return verdict, str(out.get("reason") or ""), judge


def _check_part(text: str) -> None:
    key = text_hash(text)
    if minor_check.cached(key) == "ok":
        return
    try:
        explicit, reason, judge = _judge(text)
    except minor_check.CheckDeclined as e:
        log.warning("content check declined a script (%s): refused", e)
        raise ExplicitContentError(DECLINED) from None
    except minor_check.CheckUnavailable as e:
        log.warning("content check unavailable (%s): script paused", e)
        raise ExplicitContentError(UNAVAILABLE) from None
    if explicit:
        # Not remembered: asked again next time (a clean deployment has no
        # place to clear a remembered answer).
        log.warning("content check refused a script: %s", reason[:200])
        raise ExplicitContentError(REFUSED)
    minor_check._store(key, "ok", judge, reason)


def active() -> bool:
    """Whether the check runs: only on a clean deployment."""
    return not content_mode.features()


def check(*texts: str | None) -> None:
    """On a clean deployment, raise ExplicitContentError when any of
    `texts` (spoken script, opening, caption, end card) is sexually
    explicit, or when the check can't say it isn't. Otherwise nothing."""
    if not active():
        return
    for text in texts:
        for part in parts(text or ""):
            _check_part(part)


def plan_texts(plan: dict) -> list[str]:
    """The words a storyboard plan puts in the video: the spoken script, its
    opening, the end card, the captions and each shot's caption."""
    def text(v: object) -> str:
        return v if isinstance(v, str) else ""

    chunks = plan.get("caption_chunks") if isinstance(plan.get("caption_chunks"), list) else []
    shots = plan.get("shots") if isinstance(plan.get("shots"), list) else []
    return [text(plan.get("cleaned_voice_script")), text(plan.get("hook")), text(plan.get("end_card_text")),
            "\n".join(text(c.get("text")) for c in chunks if isinstance(c, dict)),
            "\n".join(text(s.get("caption_text")) for s in shots if isinstance(s, dict))]


def check_or_422(*texts: str | None) -> None:
    """check() for API endpoints: a refusal is a 422 with its message."""
    from fastapi import HTTPException

    try:
        check(*texts)
    except ExplicitContentError as e:
        raise HTTPException(422, str(e)) from None

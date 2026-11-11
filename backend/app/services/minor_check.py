"""The AI minors check: a second guard after the word filter (safety.py).

Every image, video and lip-sync prompt (and a sexual voice script) goes
through check_generation() before it reaches a provider. The word filter
runs first; then the planner model (Settings → Models, never the adult text
model) is asked whether the text puts a minor in a sexual context:

- adult mode on: every image/video/lip-sync prompt (provider filters are off);
- adult mode off: only prompts that are sexual, mention a minor, or have an
  avoid list that removes signs of adulthood (provider filters are on).

Verdicts are cached by a hash of the policy version and the text the model
was shown (in process, and in the prompt_checks table): "ok" for 30 days and
only for the planner that gave it, refusals from a real planner for good.
Verdicts from the mock classifier are used only in mock mode. When the check
can't run, prompts with a plainly sexual word (or an avoid list that steers
young) are refused; other prompts, including ones with only an everyday word
such as "breast" or "swimsuit", go ahead (the word filter and provider
filters still apply).
"""
from __future__ import annotations

import hashlib
import logging
import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import safety
from .safety import UnsafePromptError

log = logging.getLogger("avs.minor_check")

# Bump to invalidate every cached verdict when the check's rules change.
POLICY_VERSION = "minors-1"
OK_TTL = timedelta(days=30)
LRU_SIZE = 2000

REFUSED = "This prompt was refused by the safety check: it may involve a minor in a sexual context."
UNAVAILABLE = ("The safety check couldn't run, so sexual prompts are paused until it's available "
               "(check the planner model in Settings → Models)")

KINDS = ("image", "video", "lipsync", "script")

_lru: "OrderedDict[str, tuple[str, datetime, str]]" = OrderedDict()
_lock = threading.Lock()


class CheckUnavailable(RuntimeError):
    """The safety check model gave no verdict (no key, switched off, error,
    timeout, refusal, unreadable reply)."""


def reset_cache() -> None:
    """Forget the in-process verdicts (tests)."""
    with _lock:
        _lru.clear()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _key_text(text: str) -> str:
    """The text as hashed: NFC, whitespace collapsed, lower case. Hidden
    characters and tag joiners are kept, so two texts the model may read
    differently ("na ked" and "na\u200bked") never share a verdict."""
    import unicodedata

    return " ".join(unicodedata.normalize("NFC", str(text or "")).split()).lower()


def text_hash(prompt: str, negative: str = "") -> str:
    raw = "\0".join((POLICY_VERSION, _key_text(prompt), _key_text(negative)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def classifier_text(prompt: str, negative: str = "") -> str:
    from ..providers.mock_chat import AVOID_MARK

    text = f"Prompt: {prompt.strip()}"
    if (negative or "").strip():
        text += AVOID_MARK + negative.strip()
    return text


# ---------- model ----------

def check_model() -> str:
    """The model the check asks: always the planner."""
    from . import model_registry

    return model_registry.active_model("chat")


def availability() -> dict:
    """{"model", "available"} for the Settings page: available when the
    planner can answer (a real provider, or the mock one in mock mode)."""
    from ..config import get_settings
    from . import model_registry

    model = check_model()
    try:
        provider, _ = model_registry.route_info("chat", model)
    except Exception:  # noqa: BLE001
        provider = "unavailable"
    available = provider not in ("unavailable", "off") and (
        provider != "mock" or bool(get_settings().mock_providers))
    return {"model": model, "available": bool(available)}


def _classifier():
    """(provider, model) for the planner; raises CheckUnavailable when it
    can't answer. A mock planner outside mock mode (no OpenRouter key) is
    not a check."""
    from ..config import get_settings
    from . import model_registry, prompting

    model = check_model()
    provider, _ = model_registry.route_info("chat", model)
    if provider == "unavailable":
        raise CheckUnavailable(f"{model} is switched off")
    if provider == "mock" and not get_settings().mock_providers:
        raise CheckUnavailable("no key for the planner")
    try:
        return prompting._chat_for(model), model
    except Exception as e:  # noqa: BLE001
        raise CheckUnavailable(str(e)[:200]) from None


MOCK_PREFIX = "mock:"


def _identity() -> Optional[str]:
    """Who would judge a prompt now: the planner model, or "mock:<model>"
    when the mock classifier answers. None when that can't be worked out."""
    from . import model_registry

    try:
        model = check_model()
        provider, _ = model_registry.route_info("chat", model)
    except Exception:  # noqa: BLE001
        return None
    return MOCK_PREFIX + model if provider == "mock" else model


def _classify(prompt: str, negative: str) -> tuple[bool, str, str]:
    """(minor_sexual, reason, judge); raises CheckUnavailable. `judge` is
    the _identity() of the classifier that answered."""
    chat, model = _classifier()
    judge = _identity() or model
    fn = getattr(chat, "classify_minor", None)
    if not callable(fn):
        raise CheckUnavailable("the planner has no safety check")
    try:
        out = fn(classifier_text(prompt, negative))
    except Exception as e:  # noqa: BLE001 — HTTP error, timeout, refusal, garbage
        raise CheckUnavailable(f"{type(e).__name__}: {str(e)[:200]}") from None
    verdict = out.get("minor_sexual") if isinstance(out, dict) else None
    if not isinstance(verdict, bool):
        raise CheckUnavailable("unreadable verdict")
    return verdict, str(out.get("reason") or ""), judge


# ---------- cache ----------

def _fresh(verdict: str, at: datetime) -> bool:
    return verdict == "minor_sexual" or _now() - at < OK_TTL


def _usable(verdict: str, judge: str, current: Optional[str]) -> bool:
    """A cached verdict counts only when it comes from the same kind of
    classifier as now (mock verdicts only in mock mode, real ones only with
    a real planner), and an "ok" only from the planner that is set now."""
    judge = judge or ""
    was_mock = judge.startswith(MOCK_PREFIX)
    if current is None:
        return not was_mock
    if was_mock != current.startswith(MOCK_PREFIX):
        return False
    return verdict == "minor_sexual" or judge == current


def _remember_lru(key: str, verdict: str, at: datetime, judge: str) -> None:
    with _lock:
        old = _lru.get(key)
        if (old is not None and old[0] == "minor_sexual" and verdict != "minor_sexual"
                and old[2].startswith(MOCK_PREFIX) == judge.startswith(MOCK_PREFIX)):
            return  # a refusal is never replaced by a later "ok"
        _lru[key] = (verdict, at, judge)
        _lru.move_to_end(key)
        while len(_lru) > LRU_SIZE:
            _lru.popitem(last=False)


def cached(key: str) -> Optional[str]:
    """The cached verdict ("ok" | "minor_sexual") or None."""
    current = _identity()
    with _lock:
        hit = _lru.get(key)
        if hit is not None:
            if _fresh(hit[0], hit[1]) and _usable(hit[0], hit[2], current):
                _lru.move_to_end(key)
                return hit[0]
            if not _fresh(hit[0], hit[1]):
                _lru.pop(key, None)
    from .. import models
    from ..db import SessionLocal

    try:
        db = SessionLocal()
    except Exception:  # noqa: BLE001 — no database (scripts)
        return None
    try:
        row = db.get(models.PromptCheck, key)
        if row is None or row.verdict not in ("ok", "minor_sexual"):
            return None
        at = row.created_at or _now()
        if at.tzinfo is not None:
            at = at.astimezone(timezone.utc).replace(tzinfo=None)
        if not _fresh(row.verdict, at) or not _usable(row.verdict, row.model or "", current):
            return None
        _remember_lru(key, row.verdict, at, row.model or "")
        return row.verdict
    except Exception as e:  # noqa: BLE001 — a cache, never a reason to fail
        log.warning("prompt check cache unreadable (%s)", type(e).__name__)
        return None
    finally:
        db.close()


def _store(key: str, verdict: str, model: str, reason: str) -> None:
    """Cache a verdict. `model` is the judge (see _identity). A refusal is
    never replaced by an "ok" (two checks of the same text racing, or a
    borderline prompt answered differently), except a mock refusal by a
    real verdict."""
    at = _now()
    _remember_lru(key, verdict, at, model or "")
    from .. import models
    from ..db import SessionLocal

    try:
        db = SessionLocal()
    except Exception:  # noqa: BLE001
        return
    try:
        row = db.get(models.PromptCheck, key)
        if row is None:
            row = models.PromptCheck(text_hash=key)
            db.add(row)
        elif (row.verdict == "minor_sexual" and verdict != "minor_sexual"
              and (row.model or "").startswith(MOCK_PREFIX) == (model or "").startswith(MOCK_PREFIX)):
            return
        row.verdict = verdict
        row.model = (model or "")[:120]
        row.reason = (reason or "")[:2000]
        row.created_at = at
        db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        log.warning("prompt check not cached (%s)", type(e).__name__)
    finally:
        db.close()


# ---------- the check ----------

def _word_filter(prompt: str, negative: str, kind: str) -> None:
    if kind in ("script", "lipsync"):
        safety.check_script(prompt)
    else:
        safety.check_prompt(prompt)
    if (negative or "").strip():
        safety.check_prompt(negative)


def needs_ai(prompt: str, negative: str = "", *, kind: str) -> bool:
    """Whether the model is asked (see the module note)."""
    if not (prompt or "").strip():
        return False
    if kind == "script":
        return safety.is_sexual(prompt)
    from .content_mode import adult_mode

    if adult_mode():
        return True
    # An everyday word alone ("chicken breast", "swimsuit ad") isn't asked
    # about; with a minor, mentions_minor asks anyway.
    return (safety.is_hard_sexual(prompt) or safety.mentions_minor(prompt)
            or safety.steers_young(prompt, negative))


def check_generation(prompt: str, negative: str = "", *, kind: str) -> None:
    """Raise UnsafePromptError when the prompt (with its avoid list) puts a
    minor in a sexual context, or when it's sexual and the check can't run.
    `kind`: "image" | "video" | "lipsync" | "script"."""
    prompt = prompt or ""
    negative = negative or ""
    _word_filter(prompt, negative, kind)
    if not needs_ai(prompt, negative, kind=kind):
        return
    key = text_hash(prompt, negative)
    hit = cached(key)
    if hit == "ok":
        return
    if hit == "minor_sexual":
        raise UnsafePromptError(REFUSED)
    try:
        minor_sexual, reason, model = _classify(prompt, negative)
    except CheckUnavailable as e:
        # Only a plainly sexual word fails closed: "breast", "bikini" and
        # the like are everyday words (the provider filters stay on for a
        # minor mention outside adult mode).
        if safety.is_hard_sexual(prompt) or safety.steers_young(prompt, negative):
            log.warning("safety check unavailable (%s): sexual %s prompt paused", e, kind)
            raise UnsafePromptError(UNAVAILABLE) from None
        log.info("safety check unavailable (%s): non-sexual %s prompt allowed", e, kind)
        return
    _store(key, "minor_sexual" if minor_sexual else "ok", model, reason)
    if minor_sexual:
        log.warning("safety check refused a %s prompt: %s", kind, reason[:200])
        raise UnsafePromptError(REFUSED)


def check_or_422(prompt: str, negative: str = "", *, kind: str) -> None:
    """check_generation for API endpoints: a refusal is a 422 with its message."""
    from fastapi import HTTPException

    try:
        check_generation(prompt, negative, kind=kind)
    except safety.UnsafeScriptError as e:
        raise HTTPException(422, str(e)) from None

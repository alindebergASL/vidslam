"""Was a provider error a refusal for content?

A refusal is final. Redoing the same prompt and pictures on another model
(the ambient still chain, or the video model after a failed lip-sync)
would get around the filter or the moderation that refused it. An outage,
a timeout or a bad request isn't a refusal: those may still fall back.

What counts as a refusal:
- the app's own minors refusal (services/safety.UnsafeScriptError);
- an error a provider marked as refused (RunwareError.refused: Runware's
  content-refusal codes, or a model with no content check asked to take a
  picture that needs one; SogniError.refused: a Sogni safety pause);
- an error whose text says it was refused for its content: moderation, a
  safety system or filter, a content or usage policy, NSFW, "inappropriate",
  Alibaba's "Green net", ByteDance's "sensitive content", Kling's "risk
  control" and the like.

The text rules are narrow on purpose. Bare words that outages also use are
left out: "policy" (OpenRouter's "No endpoints found matching your data
policy"), "refused" ("Connection refused"), "blocked" on its own (a
firewall page), "explicit" on its own and "child" (a crashed child process).
So are bare words a provider may echo from an adult prompt ("nude",
"sexual"): vendors that refuse for them also say "flagged", "explicit
content" or "moderation".

"safety" and "moderation" alone name a service as often as a refusal
("Moderation service temporarily unavailable", "safety checker failed to
load", "the parameter 'safety.checkContent' is not supported"). They count
only next to a refusal word ("rejected by the safety system", "flagged",
"IMAGE_SAFETY", "safety_review_required"), and never in a text that says
something broke or was asked wrongly. A server error, a timeout or a rate
limit (5xx, 408, 429) is never a refusal.
"""
from __future__ import annotations

import re

_REFUSAL_TEXT = re.compile(
    r"""
      image[ _-]?safety                         # Google's "IMAGE_SAFETY"
    | safety[ _-]?(?:review|system|filter|block|violation|reject)
    | (?<![a-z])nsfw(?![a-z])
    | content[ _-]?(?:polic|filter|violation|guideline|rule|standard|moderation)
    | (?:usage|acceptable[ _-]use)[ _-]?polic
    | community[ _-]?guideline
    | inappropriate
    | green[ _-]?net
    | data[ _-]?inspection
    | risk[ _-]?control
    | prohibited[ _-]?content
    | new_sensitive | sensitive[ _-]?(?:content|image|video|word|information|material)
    | (?<![a-z])csam(?![a-z]) | sexually[ _-]explicit
    | explicit[ _-]?(?:content|material|image|imagery|nudity)
    | harm(?:ful)?[ _-]?(?:content|categor)
    | responsible[ _-]?ai | rai[ _-]?media[ _-]?filtered
    | (?:prompt|content|image|output|input|video|generation)\s+(?:was\s+|has\s+been\s+|is\s+)?(?:blocked|flagged)
    | (?:was|were|been|is)\s+flagged
    | blocked\s+(?:for|due\s+to|by)\b[^.]{0,40}(?:content|polic)
    | violat\w*[^.]{0,40}(?:polic|guideline|terms|rules|standards)
    | (?:model|provider|vendor)\s+(?:has\s+)?refused | (?<![a-z])refusal(?![a-z])
    | refused\s+[^.]{0,40}for\s+its\s+content
    """,
    re.IGNORECASE | re.VERBOSE,
)
# "safety" or "moderation" on their own: a refusal only with a refusal word.
_WEAK = re.compile(r"(?<![a-z])safety(?![a-z])|moderation|moderated", re.I)
_REFUSAL_WORD = re.compile(r"flagged|blocked|rejected|refus\w*|violat\w*|review|denied|disallowed|not\s+allowed|"
                           r"detected|triggered|filtered|unsafe|stopped", re.I)
# Something broke, or the request was asked wrongly.
_BROKEN = re.compile(r"service|unavailable|timed?\s*out|timeout|failed\s+to\s+load|out\s+of\s+memory|"
                     r"(?<![a-z])oom(?![a-z])|cuda|parameter|not\s+supported|unsupported|must\s+be|"
                     r"validation\s+error|invalid|unreachable|connect", re.I)
# The HTTP status our provider errors start with ("OpenRouter image 503:",
# "video submit 504:", "avatar submit 500:", "Runware error 500 …").
_STATUS = re.compile(r"\b(?:OpenRouter\s+\w+|video\s+submit|avatar\s+submit|Runware\s+error|Sogni)\s+(\d{3})\b",
                     re.I)


def _outage_status(status: object) -> bool:
    return isinstance(status, int) and (status >= 500 or status in (408, 429))


def is_refusal_text(text: str) -> bool:
    """Whether an error message says the request was refused for its
    content (see the module note). Used where only the text is kept, such
    as a failed video job's error."""
    text = str(text or "")
    if not text:
        return False
    m = _STATUS.search(text[:80])
    if m and _outage_status(int(m.group(1))):
        return False
    if _REFUSAL_TEXT.search(text):
        return True
    return bool(_WEAK.search(text) and _REFUSAL_WORD.search(text) and not _BROKEN.search(text))


def _status_of(e: BaseException) -> object:
    status = getattr(e, "status", None)
    if not isinstance(status, int):
        status = getattr(e, "status_code", None)
    if not isinstance(status, int):
        response = getattr(e, "response", None)
        status = getattr(response, "status_code", None)
    return status


def is_content_refusal(exc: BaseException) -> bool:
    """Whether this error (or the error it was raised from) is a refusal
    for content. See the module note."""
    from ..services.safety import UnsafeScriptError

    seen: set[int] = set()
    e: BaseException | None = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, UnsafeScriptError) or getattr(e, "refused", False) is True:
            return True
        if not _outage_status(_status_of(e)) and is_refusal_text(str(e)):
            return True
        e = e.__cause__
    return False


class ContentRefused(RuntimeError):
    """A provider refused a request for its content, so nothing else was
    tried in its place."""

    refused = True

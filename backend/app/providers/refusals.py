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
"""
from __future__ import annotations

import re

_REFUSAL_TEXT = re.compile(
    r"""
      (?<![a-z])safety(?![a-z])                 # "safety system", "SAFETY", "safety_review_required"
    | moderation | moderated
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


def is_refusal_text(text: str) -> bool:
    """Whether an error message says the request was refused for its
    content (see the module note). Used where only the text is kept, such
    as a failed video job's error."""
    return bool(text) and _REFUSAL_TEXT.search(str(text)) is not None


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
        if is_refusal_text(str(e)):
            return True
        e = e.__cause__
    return False


class ContentRefused(RuntimeError):
    """A provider refused a request for its content, so nothing else was
    tried in its place."""

    refused = True

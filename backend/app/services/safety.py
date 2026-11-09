from __future__ import annotations

import re

# Adult content between adults is allowed (no NSFW gate). What stays
# blocked is sexual content involving minors, and threats against a named
# person.
_BLOCKED_PATTERNS = [
    re.compile(r"\b(child|children|kid|kids|minor|minors|underage|teen|teens|preteen)\b.*"
               r"\b(sex|sexual|nude|naked|porn|nsfw|explicit)\b", re.I),
    re.compile(r"\b(sex|sexual|nude|naked|porn|nsfw|explicit)\b.*"
               r"\b(child|children|kid|kids|minor|minors|underage|teen|teens|preteen)\b", re.I),
    re.compile(r"\b(kill|murder|assassinate)\b\s+(?:the\s+)?[A-Z][a-z]+", re.I),
    re.compile(r"\b(cp|loli|shota)\b", re.I),
]


class UnsafeScriptError(ValueError):
    """Raised when a script fails the pre-generation safety check."""


def validate_script(script: str) -> None:
    """Raise UnsafeScriptError if the script contains obviously disallowed content.

    This is intentionally a small, conservative client-side filter; the planning
    model also enforces safety in its system prompt.
    """
    if not script or not script.strip():
        raise UnsafeScriptError("Script is empty.")
    for pat in _BLOCKED_PATTERNS:
        if pat.search(script):
            raise UnsafeScriptError(
                "Script appears to contain disallowed content "
                "(sexual content involving minors, or threats against a specific person)."
            )


# Generation prompts (image/video): sexual content and minors, with wider
# wording than validate_script's (and without its threat pattern, which
# misfires on short descriptions: "kill time", "murder mystery").
_AGE = r"(?:1[0-7]|[1-9])"
_MINOR_TERMS = (
    r"\b(?:child|children|kid|kids|kiddie|minor|minors|underage|under-age|teen|teens|teenage|teenaged|"
    r"teenager|teenagers|preteen|pre-teen|preteens|tween|tweens|schoolgirl|schoolgirls|schoolboy|"
    r"schoolboys|toddler|toddlers|infant|infants|little (?:girl|boy)s?|young (?:girl|boy)s?|"
    r"loli|lolita|shota|jailbait|cp)\b"
    rf"|\b{_AGE}\s?-?\s?(?:years?|yrs?)(?:\s?-?\s?old)?\b|\b{_AGE}\s?y/?o\b|\baged?\s{_AGE}\b"
)
_SEXUAL_TERMS = (
    r"\b(?:sex|sexual|sexuali[sz]ed|sexy|nude|nudes|naked|nudity|porn|porno|pornographic|nsfw|"
    r"explicit|erotic|erotica|topless|bottomless|lingerie|undress|undressed|undressing|seductive|"
    r"seductively|nipples?|genitals?|genitalia|orgasm|aroused|arousal|fetish|horny|lewd|stripping|"
    r"stripper)\b"
)
_MINOR_RE = re.compile(_MINOR_TERMS, re.I)
_SEXUAL_RE = re.compile(_SEXUAL_TERMS, re.I)
_ALWAYS_RE = re.compile(r"\b(?:loli|lolita|shota|jailbait|cp)\b", re.I)


class UnsafePromptError(UnsafeScriptError):
    """An image or video prompt refused before it reaches a provider."""


def mentions_minor(text: str) -> bool:
    """The prompt names a child or a minor's age (even innocently): callers
    turn the provider's own content check on as a backstop."""
    return bool(_MINOR_RE.search(" ".join(str(text or "").split())))


def check_prompt(text: str) -> None:
    """Raise UnsafePromptError for a generation prompt involving sexual
    content and minors. Called on every prompt sent to a provider whose own
    content check is off (Runware), whatever screen it came from."""
    flat = " ".join(str(text or "").split())
    if _ALWAYS_RE.search(flat) or (_MINOR_RE.search(flat) and _SEXUAL_RE.search(flat)):
        raise UnsafePromptError(
            "This prompt was refused: it appears to involve sexual content and minors.")

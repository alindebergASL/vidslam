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


# The minors patterns only: image and video prompts are short descriptions
# where the threat pattern would misfire ("kill time", "murder mystery").
_MINOR_PATTERNS = (*_BLOCKED_PATTERNS[:2], _BLOCKED_PATTERNS[3])


class UnsafePromptError(UnsafeScriptError):
    """An image or video prompt refused before it reaches a provider."""


def check_prompt(text: str) -> None:
    """Raise UnsafePromptError for a generation prompt involving sexual
    content and minors. Called on every prompt sent to a provider whose own
    content check is off (Runware), whatever screen it came from."""
    flat = " ".join(str(text or "").split())  # the patterns don't cross newlines
    for pat in _MINOR_PATTERNS:
        if pat.search(flat):
            raise UnsafePromptError(
                "This prompt was refused: it appears to involve sexual content and minors.")

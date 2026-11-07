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

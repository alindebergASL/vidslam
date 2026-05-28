from __future__ import annotations

import re

_BLOCKED_PATTERNS = [
    re.compile(r"\b(child|minor|underage|teen)\b.*\b(sex|nude|naked|porn)\b", re.I),
    re.compile(r"\b(porn|nude|naked|nsfw|fetish)\b", re.I),
    re.compile(r"\b(kill|murder|assassinate)\b\s+(?:the\s+)?[A-Z][a-z]+", re.I),
    re.compile(r"\b(cp|loli)\b", re.I),
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
                "(sexual content involving minors, explicit sexual content, "
                "or threats against a specific person)."
            )

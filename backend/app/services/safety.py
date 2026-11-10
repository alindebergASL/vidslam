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
#
# The text is normalised first (_normalise): look-alike letters (fullwidth,
# styled), hidden characters inside words ("na\u200bked") and accents are
# folded away, so spacing tricks don't hide a word.
_NUM_WORDS = ("one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|"
              "fifteen|sixteen|seventeen")
# An age under 18, as digits or words.
_AGE = rf"(?:1[0-7]|0?[1-9]|{_NUM_WORDS})"
# "13 year old", "13-yr-old", "13 y.o.", "13yo", "13 y/o", "aged 13",
# "age: 13", "age thirteen", "13 years of age".
_AGE_FORMS = (
    rf"\b{_AGE}\s*-?\s*(?:years?|yrs?|yr)\b(?:\s*-?\s*old)?"
    rf"|\b{_AGE}\s*(?:y\s*[./]?\s*o\b\.?|yo\b)"
    rf"|\bage[ds]?\s*[:=]?\s*{_AGE}\b"
    rf"|\b{_AGE}\s+years?\s+of\s+age\b"
)
# A person word next to a bare age: "girl, 13", "boy (12)", "13 girl".
_PERSON = (r"(?:girl|girls|boy|boys|kid|kids|child|daughter|son|sister|brother|niece|nephew|student|"
           r"pupil|model|person|teen|baby|babe)")
_PERSON_AGE = rf"\b{_PERSON}\W{{1,4}}{_AGE}\b(?!\s*(?:am|pm|cm|mm|kg|lb|%|ft|in\b|\+))|\b{_AGE}\W{{0,3}}{_PERSON}\b"
_MINOR_TERMS = (
    r"\b(?:child|children|childlike|child-like|kid|kids|kiddie|kiddy|minor|minors|underage|underaged|"
    r"under-age|under-aged|teen|teens|teenage|teenaged|teenager|teenagers|teenie|teeny|preteen|pre-teen|"
    r"preteens|tween|tweens|adolescent|adolescents|juvenile|juveniles|pubescent|prepubescent|"
    r"pre-pubescent|schoolgirl|schoolgirls|schoolboy|schoolboys|schoolchild|schoolchildren|"
    r"school-girl|school-boy|toddler|toddlers|infant|infants|newborn|newborns|loli|lolita|lolis|"
    r"shota|shotacon|lolicon|jailbait|pedo|paedo|pedophile|paedophile|pedophilia|paedophilia|"
    r"barely legal|"
    r"little (?:girl|boy|sister|brother)s?|young (?:girl|boy)s?|small (?:girl|boy)s?|"
    r"(?:middle|high|elementary|primary|grade|junior high|secondary|prep)[ -]?school(?:er|ers)?|"
    r"highschool(?:er|ers)?|junior high|kindergarten(?:er|ers)?|"
    r"(?:1st|2nd|3rd|[4-9]th|1[0-2]th|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|"
    r"tenth|eleventh|twelfth) grade(?:r|rs)?|"
    r"(?:freshman|sophomore) in high[ -]?school)\b"
    rf"|{_AGE_FORMS}|{_PERSON_AGE}"
)
_SEXUAL_TERMS = (
    r"\b(?:sex|sexual|sexually|sexuali[sz]ed|sexy|nude|nudes|naked|nudity|nudist|porn|porno|pornographic|"
    r"nsfw|explicit|erotic|erotica|topless|bottomless|lingerie|underwear|panties|bra|bikini|thong|"
    r"undress|undressed|undressing|unclothed|seductive|seductively|sensual|nipples?|genitals?|genitalia|"
    r"breasts?|boobs?|tits?|vagina|vaginal|penis|pussy|cock|crotch|orgasm|aroused|arousal|"
    r"fetish|horny|lewd|stripping|stripper|striptease|masturbat\w*|intercourse|hentai|ecchi|onlyfans|"
    r"provocative|spread legs|bdsm|bondage)\b"
)
_MINOR_RE = re.compile(_MINOR_TERMS, re.I)
_SEXUAL_RE = re.compile(_SEXUAL_TERMS, re.I)
# Refused on their own. ("cp" alone isn't: "CP violation", "CP 2077".)
_ALWAYS_RE = re.compile(
    r"\b(?:loli|lolicon|lolis|shota|shotacon|jailbait|csam|child ?porn\w*|kiddie ?porn|pedo ?porn|"
    r"cp (?:pics?|videos?|images?|content|porn))\b", re.I)


def _normalise(text: str) -> str:
    """One searchable line: look-alike and styled letters folded (NFKC),
    accents dropped, hidden characters inside words removed, whitespace
    collapsed."""
    import unicodedata

    t = unicodedata.normalize("NFKC", str(text or ""))
    t = "".join(c for c in unicodedata.normalize("NFKD", t)
                if not unicodedata.combining(c) and unicodedata.category(c) not in ("Cf", "Cc", "Co")
                or c in "\n\t")
    return " ".join(t.split())


class UnsafePromptError(UnsafeScriptError):
    """An image or video prompt refused before it reaches a provider."""


def mentions_minor(text: str) -> bool:
    """The prompt names a child, a minor's age or school (even innocently):
    callers turn the provider's own content check on as a backstop."""
    return bool(_MINOR_RE.search(_normalise(text)))


def is_sexual(text: str) -> bool:
    return bool(_SEXUAL_RE.search(_normalise(text)))


def check_prompt(text: str) -> None:
    """Raise UnsafePromptError for a generation prompt involving sexual
    content and minors. Called on the exact text sent to a provider whose
    own content check is off (Runware), whatever screen it came from.

    A word list can't catch every phrasing, so callers also turn the
    provider's content check on whenever mentions_minor() is true."""
    flat = _normalise(text)
    if _ALWAYS_RE.search(flat) or (_MINOR_RE.search(flat) and _SEXUAL_RE.search(flat)):
        raise UnsafePromptError(
            "This prompt was refused: it appears to involve sexual content and minors.")


_ADULT_RE = re.compile(r"\b(?:adults?|adult-looking|mature|grown|grown-up|grownup|older|old|aged|elderly|"
                       r"wrinkles?|wrinkled|woman|women|man|men|milf|curvy|voluptuous|beard|bearded)\b", re.I)


def steers_young(prompt: str, negative: str) -> bool:
    """A sexual prompt whose "avoid" list removes signs of adulthood
    ("adult, mature, woman"): treated like a mention of a minor."""
    return bool(negative) and is_sexual(prompt) and bool(_ADULT_RE.search(_normalise(negative)))

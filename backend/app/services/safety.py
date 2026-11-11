from __future__ import annotations

import re

# Adult content between adults is allowed (no NSFW gate). What stays
# blocked is sexual content involving minors, and threats against a named
# person.
# A minor word and a sexual word in the same sentence ("Our kids love it.
# Explicit instructions inside." is two sentences, and "explicit
# instructions" isn't sexual anyway: the same exclusions as _SEXUAL_TERMS).
_OLD_MINOR = r"\b(child|children|kid|kids|minor|minors|underage|teen|teens|preteen)\b"
_OLD_SEXUAL = (r"\b(sex|sexual|nude|naked|porn|nsfw|"
               r"explicit(?!\s+(?:lyrics|language|instructions|permission|consent)))\b")
_BLOCKED_PATTERNS = [
    re.compile(_OLD_MINOR + r"[^.!?\n]*" + _OLD_SEXUAL, re.I),
    re.compile(_OLD_SEXUAL + r"[^.!?\n]*" + _OLD_MINOR, re.I),
    re.compile(r"\b(cp|loli|shota)\b", re.I),
]
# A threat against a named person: only the verb ignores case; the name
# must be Capitalised in the text itself ("kill time", "murder mystery
# night" and "kill the lights" pass; "kill John" doesn't). See _threat for
# the Title Case copy it lets through ("Murder Mystery Night", "Kill Time").
_THREAT_RE = re.compile(
    r"(?i:\b(?P<verb>kill|murder|assassinate)\b)\s+(?:(?i:the)\s+)?(?P<name>[A-Z][a-z]+)"
    r"(?P<after>\s+[A-Z][a-z]+)?")
# Capitalised words after the verb that are ordinary nouns in ad copy and
# titles, not names.
_THREAT_NOUNS = frozenset((
    "time", "mystery", "mysteries", "night", "nights", "lights", "light", "switch", "game", "games",
    "party", "parties", "dinner", "weekend", "show", "fest", "festival", "room", "house", "hotel",
    "train", "tour", "trail", "walk", "club", "podcast", "bill", "joy", "boredom", "germs", "bacteria",
    "weeds", "bugs", "pests", "stains", "odors", "odours", "thirst", "hunger", "pain", "stress",
    "it", "them", "all", "everything", "nothing", "one", "two", "your", "our", "my", "this", "that",
    "zone", "box", "count", "shot", "shots", "streak", "deal", "deals", "price", "prices", "season",
    "list", "spree", "machine", "mode", "screen", "button", "music", "noise", "engine", "power",
))


def _threat(script: str) -> bool:
    for m in _THREAT_RE.finditer(script):
        if m.group("name").lower() in _THREAT_NOUNS:
            continue
        verb = m.group("verb")
        before = script[:m.start()].rstrip()
        sentence_start = not before or before[-1] in ".!?:\n\"'(“"
        # Title Case copy: a capitalised verb mid-sentence followed by a run
        # of Capitalised words ("Join our Murder Weekend Special").
        if verb[0].isupper() and not sentence_start and m.group("after"):
            continue
        return True
    return False


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
        if pat.search(script) or _threat(script):
            raise UnsafeScriptError(
                "Script appears to contain disallowed content "
                "(sexual content involving minors, or threats against a specific person)."
            )


# Generation prompts (image/video): sexual content and minors, with wider
# wording than validate_script's (and without its threat pattern, which
# misfires on short descriptions: "kill time", "murder mystery").
#
# The text is searched in several normalised forms (_forms): look-alike
# letters folded (fullwidth, styled, small caps, Cyrillic/Greek), accents
# dropped, tag joiners ("little_girl") and hidden characters read as spaces,
# hidden characters inside a word ("na​ked") removed, and digits used as
# letters ("ch1ld", "t33n") read as letters. A match in any form counts.
_NUM_WORDS = ("one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|"
              "fifteen|sixteen|seventeen")
_ORD_WORDS = ("first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|eleventh|twelfth|"
              "thirteenth|fourteenth|fifteenth|sixteenth|seventeenth")
# An age under 18, as digits or words.
_AGE = rf"(?:1[0-7]|0?[1-9]|{_NUM_WORDS})"
_ORD = rf"(?:1[0-7]th|1st|2nd|3rd|[4-9]th|{_ORD_WORDS})"
# "13 year old", "13-yr-old", "12yrold", "13 y.o.", "13yo", "14-yo", "13 y/o",
# "aged 13", "age: 13", "13 years of age", "turning 12", "her 9th birthday".
# A count of years alone isn't an age ("ten years of marriage").
_AGE_FORMS = (
    rf"\b{_AGE}\s*-?\s*(?:years?|yrs?)\s*-?\s*(?:olds?|of\s+age)\b"
    rf"|\b{_AGE}\s*-?\s*(?:y\s*[./]?\s*o\b\.?|yo\b)"
    rf"|\bage[ds]?\s*[:=]?\s*{_AGE}\b"
    rf"|\b(?:turning|turned|just turned)\s+{_AGE}\b"
    rf"|\b{_ORD}\s+birthday\b"
)
# A person word near a bare age: "girl, 13", "boy (12)", "girl who is 12",
# "girl of 8"; or a teen age then one person: "13 girl" (not "one person",
# "two models" or "12 girls on a runway").
_PERSON = (r"(?:girl|girls|boy|boys|kid|kids|child|daughter|son|sister|brother|niece|nephew|student|"
           r"pupil|model|person|teen|baby|babe)")
_PERSON_AGE = (rf"\b{_PERSON}(?:\W+(?:who|is|was|of|aged?|just|only|about|around|maybe))*\W+{_AGE}\b"
               rf"(?!\s*(?:am|pm|cm|mm|kg|lbs?|%|ft|in\b|\+|:\d))"
               r"|\b1[0-7]\W{0,3}(?:girl|boy|kid|child|daughter|son|teen|student|pupil|schoolgirl|schoolboy)\b"
               # "she is 14", "he's 12", "they just turned 13" (not "he is 6 feet", "10 minutes late").
               rf"|\b(?:she|he|they)(?:[’']s|\s+(?:is|was|are|were|turns|turned|just turned))\s+{_AGE}\b"
               r"(?!\s*(?:am|pm|cm|mm|kg|lbs?|%|ft|feet|foot|in\b|inch(?:es)?|min(?:ute)?s?|h(?:ou)?rs?|"
               r"sec(?:ond)?s?|days?|weeks?|months?|times|x\b|\+|:\d|th\b|st\b|nd\b|rd\b|of\b|out\b|percent|points?))")
_MINOR_TERMS = (
    r"\b(?:child|childs|children|childlike|child-like|kid|kids|kiddie|kiddy|minor|minors|underage|"
    r"underaged|under-age|under-aged|teen|teens|teenage|teenaged|teenager|teenagers|teenie|teeny|"
    r"preteen|pre-teen|preteens|tween|tweens|adolescent|adolescents|juvenile|juveniles|pubescent|"
    r"prepubescent|pre-pubescent|youth|youths|youngster|youngsters|little ones?|"
    r"daughter|daughters|stepdaughter|stepdaughters|step-daughter|"
    r"schoolgirl|schoolgirls|schoolboy|schoolboys|schoolchild|schoolchildren|school-girl|school-boy|"
    r"toddler|toddlers|infant|infants|newborn|newborns|baby (?:girl|boy)s?|"
    r"preschool|preschooler|preschoolers|pre-schooler|kindergarten|kindergartener|kindergarteners|"
    r"kindergartner|kindergartners|"
    r"loli|lolita|lolis|shota|shotacon|lolicon|jailbait|pedo|paedo|pedophile|paedophile|pedophilia|"
    r"paedophilia|barely legal|"
    r"little (?:girl|boy|sister|brother)s?|young (?:girl|boy)s?|small (?:girl|boy)s?|"
    r"(?:middle|high|elementary|primary|grade|junior high|secondary|prep)[ -]?school(?:er|ers)?|"
    r"highschool(?:er|ers)?|junior high|"
    rf"{_ORD}[ -]grade(?:r|rs)?|"
    r"year [1-9]|year 1[0-3]|"
    r"(?:freshman|sophomore) in high[ -]?school)\b"
    rf"|{_AGE_FORMS}|{_PERSON_AGE}"
)
# Refused together with a minor.
_SEXUAL_TERMS = (
    r"\b(?:sex|sexual|sexually|sexuali[sz]ed|sexy|nude|nudes|naked|nudity|nudist|porn|porno|pornographic|"
    r"nsfw|erotic|erotica|topless|bottomless|lingerie|panties|thong|g-string|"
    r"undress|undressed|undressing|unclothed|seductive|seductively|nipples?|genitals?|genitalia|"
    r"boobs?|tits?|vagina|vaginal|penis|pussy|cock|crotch|orgasm|aroused|arousal|"
    r"fetish|horny|lewd|stripping|stripper|striptease|strips? (?:naked|nude|down|off|for)|"
    r"stripped (?:naked|nude|down|off)|masturbat\w*|intercourse|hentai|ecchi|onlyfans|"
    r"spread legs|bdsm|bondage|explicit(?!\s+(?:lyrics|language|instructions|permission|consent)))\b"
)
# Ordinary words that are sexual only in context ("kids in swimwear at the
# beach", "breast cancer awareness"): with a minor they turn the provider's
# check on (mentions_minor does that anyway) instead of refusing.
_SOFT_TERMS = (r"\b(?:underwear|bra|bras|bikini|bikinis|swimsuit|swimsuits|swimwear|breast|breasts|"
               r"sensual|provocative)\b")
_MINOR_RE = re.compile(_MINOR_TERMS, re.I)
_SEXUAL_RE = re.compile(_SEXUAL_TERMS, re.I)
_SOFT_RE = re.compile(_SOFT_TERMS, re.I)
# Refused on their own. ("cp" alone isn't: "CP violation", "CP 2077".)
_ALWAYS_RE = re.compile(
    r"\b(?:loli|lolicon|lolis|shota|shotacon|jailbait|csam|child ?porn\w*|kiddie ?porn|pedo ?porn|"
    r"cp (?:pics?|videos?|images?|content|porn))\b", re.I)

# Look-alike letters NFKC leaves alone: Cyrillic, Greek, small caps.
_CONFUSABLE = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s",
    "ԁ": "d", "ӏ": "l", "к": "k", "м": "m", "н": "h", "т": "t", "в": "b", "г": "r", "ч": "4",
    "α": "a", "β": "b", "ε": "e", "η": "n", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t",
    "υ": "u", "χ": "x", "ᴀ": "a", "ʙ": "b", "ᴄ": "c", "ᴅ": "d", "ᴇ": "e", "ғ": "f", "ɢ": "g", "ʜ": "h",
    "ɪ": "i", "ᴊ": "j", "ᴋ": "k", "ʟ": "l", "ᴍ": "m", "ɴ": "n", "ᴏ": "o", "ᴘ": "p", "ʀ": "r", "ꜱ": "s",
    "ᴛ": "t", "ᴜ": "u", "ᴠ": "v", "ᴡ": "w", "ʏ": "y", "ᴢ": "z",
})
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})
_LEET_L = str.maketrans({"1": "l"})
_MIXED = re.compile(r"\b(?=\w*[a-z])(?=\w*[0-9@$])[\w@$]+\b", re.I)  # a word mixing letters and digits


def _fold(text: str, *, hidden: str) -> str:
    import unicodedata

    t = unicodedata.normalize("NFKC", str(text or "")).translate(_CONFUSABLE)
    out = []
    for c in unicodedata.normalize("NFKD", t):
        if unicodedata.combining(c):
            continue
        cat = unicodedata.category(c)
        if cat in ("Cf", "Cc", "Co") and c not in "\n\t":
            out.append(hidden)
        else:
            out.append(c)
    t = "".join(out).translate(_CONFUSABLE)
    t = re.sub(r"[_|~^*+=]+", " ", t)  # tag joiners: "little_girl", "nude|child"
    return " ".join(t.split())


def _forms(text: str) -> list[str]:
    """The text as the filter reads it (see the note above)."""
    spaced = _fold(text, hidden=" ")
    joined = _fold(text, hidden="")
    out = [spaced] if spaced == joined else [spaced, joined]
    for f in list(out):
        for table in (_LEET, _LEET_L):
            leet = _MIXED.sub(lambda m, t=table: m.group(0).translate(t), f)
            if leet not in out:
                out.append(leet)
    return out


def _normalise(text: str) -> str:
    return _forms(text)[0]


def _any(rx: re.Pattern, forms: list[str]) -> bool:
    return any(rx.search(f) for f in forms)


class UnsafePromptError(UnsafeScriptError):
    """An image or video prompt refused before it reaches a provider."""


def mentions_minor(text: str) -> bool:
    """The prompt names a child, a minor's age or school (even innocently):
    callers turn the provider's own content check on as a backstop."""
    return _any(_MINOR_RE, _forms(text))


def is_sexual(text: str) -> bool:
    forms = _forms(text)
    return _any(_SEXUAL_RE, forms) or _any(_SOFT_RE, forms)


def is_hard_sexual(text: str) -> bool:
    """is_sexual without the everyday words ("breast", "swimsuit", "bra")."""
    return _any(_SEXUAL_RE, _forms(text))


def check_prompt(text: str) -> None:
    """Raise UnsafePromptError for a generation prompt involving sexual
    content and minors. Called on the exact text sent to a provider whose
    own content check is off (Runware), whatever screen it came from.

    A word list can't catch every phrasing, so callers also turn the
    provider's content check on whenever mentions_minor() is true."""
    forms = _forms(text)
    if _any(_ALWAYS_RE, forms) or (_any(_MINOR_RE, forms) and _any(_SEXUAL_RE, forms)):
        raise UnsafePromptError(
            "This prompt was refused: it appears to involve sexual content and minors.")


# Scripts: the same minors check without family words ("my daughter and I
# started this boutique"), which ad copy uses about adults all the time.
_SCRIPT_MINOR_RE = re.compile(
    _MINOR_TERMS.replace("daughter|daughters|stepdaughter|stepdaughters|step-daughter|", ""), re.I)


def check_script(text: str) -> None:
    """check_prompt for scripts and other planner input (creative direction,
    titles): what validate_script misses ("teenage", "14-year-old", look-alike
    letters), without its family-word false positives."""
    forms = _forms(text)
    if _any(_ALWAYS_RE, forms) or (_any(_SCRIPT_MINOR_RE, forms) and _any(_SEXUAL_RE, forms)):
        raise UnsafePromptError(
            "This script was refused: it appears to involve sexual content and minors.")


_ADULT_RE = re.compile(r"\b(?:adults?|adult-looking|mature|grown|grown-up|grownup|older|old|aged|elderly|"
                       r"wrinkles?|wrinkled|woman|women|man|men|milf|curvy|voluptuous|beard|bearded)\b", re.I)


def steers_young(prompt: str, negative: str) -> bool:
    """A sexual prompt whose "avoid" list removes signs of adulthood
    ("adult, mature, woman"): treated like a mention of a minor."""
    return bool(negative) and is_sexual(prompt) and bool(_ADULT_RE.search(_normalise(negative)))

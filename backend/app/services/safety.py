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
# "cp" only in the abuse-material sense ("cp videos", "trading cp"), never
# alone ("Cyberpunk CP 2077", "CP violation notice").
_CP = (r"\bcp\s*(?:pics?|videos?|vids?|images?|imgs?|content|porn\w*|collection|links?|trad(?:e|es|ing)|"
       r"stash|packs?)\b|\b(?:trad(?:e|es|ing)|shar(?:e|es|ing)|sell(?:s|ing)?|buy(?:s|ing)?|"
       r"download(?:s|ing)?)\s+cp\b")
_BLOCKED_PATTERNS = [
    re.compile(_OLD_MINOR + r"[^.!?\n]*" + _OLD_SEXUAL, re.I),
    re.compile(_OLD_SEXUAL + r"[^.!?\n]*" + _OLD_MINOR, re.I),
    re.compile(r"\b(?:loli|shota)\b|" + _CP, re.I),
]
# "cp" on its own next to a child word ("DM me for cp of young girls",
# "Got cp? Teens only"): in the same sentence, or the one before or after,
# within 80 characters. Not a product number ("CP 2077", "CP-500"), and not
# with no child word near it ("CP violation notice").
_CP_BARE = re.compile(r"\bcp\b(?!\s*-?\s*\d)", re.I)


def _cp_near_minor(text: str, minor: "re.Pattern") -> bool:
    for m in _CP_BARE.finditer(text):
        before = re.split(r"[.!?\n]+", text[max(0, m.start() - 80):m.start()])[-2:]
        after = re.split(r"[.!?\n]+", text[m.end():m.end() + 80])[:2]
        if minor.search(" ".join([*before, "cp", *after])):
            return True
    return False


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
    if _cp_near_minor(script, _SCRIPT_MINOR_RE):
        raise UnsafeScriptError(
            "Script appears to contain disallowed content "
            "(sexual content involving minors, or threats against a specific person).")
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
_TEEN = r"(?:1[0-7]|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen)"
_ORD = rf"(?:1[0-7]th|1st|2nd|3rd|[4-9]th|{_ORD_WORDS})"
# What can follow a number that makes it something other than an age:
# units and heights ("5 ft 9", 5'9", "1.75 m", "5 and a half feet"), times
# ("10 pm", "7:30"), ratings ("9 out of 10", "10/10"), durations ("15
# minutes late", "2 years together", "12 years as a nurse"; "15 years old"
# is an age form of its own), counts ("1 of 3"). "in" is inches only before
# tall/long/wide/high ("girl, 15 in a bikini" is an age), and a foot mark
# only after 4 to 7 ("girl, 15'" isn't a height). "15 years at most" and
# "15 years on this earth" are ages, so years then at/on/in/from aren't
# left out.
_UNIT = r"(?:am|pm|cm|mm|m|kg|lbs?|ft|feet|foot|in|inch(?:es)?|metres?|meters?|percent|points?|stars?)"
_NOT_AGE = (
    r"(?!\s*(?:(?:am|pm|cm|mm|kg|lbs?|ft|feet|foot|inch(?:es)?|metres?|meters?|percent|points?|stars?|"
    r"times|x|th|st|nd|rd|min(?:ute)?s?|h(?:ou)?rs?|sec(?:ond)?s?|days?|weeks?|months?)\b|%|\+|:\d|"
    rf"m\s+tall\b|in\.|in\s+(?:tall|long|wide|high)\b|[.,]\d+\s*(?:{_UNIT}\b|%)|/\s*(?:10|100|5)\b|"
    r"and\s+a\s+half\s+(?:feet|ft|foot|inch(?:es)?|metres?|meters?)\b|"
    r"out\s+of\b|of\s+(?:\d|the|them|us|those|these)\b|"
    r"(?:years?|yrs?)\s+(?:ago|later|earlier|before|after|since|of|as|together|married|running|"
    r"straight|experience|long|younger|older|apart)\b))"
    r"(?!(?<=(?<!\d)[4-7])\s*['’′]\s*(?:\d|tall\b))")
# "13 year old", "13-yr-old", "12yrold", "13 y.o.", "13yo", "14-yo", "13 y/o",
# "aged 13", "age: 13", "13 years of age", "turning 12", "her 9th birthday",
# "9 months old". A count of years alone isn't an age ("ten years of marriage").
# A baby or child word with months, weeks or days is one too ("baby, 9
# months", "naked boy 10 months"), unless it's a pregnancy or a time
# ("girl, 9 months pregnant", "son, 2 weeks later").
_AGE_FORMS = (
    rf"\b{_AGE}\s*-?\s*(?:years?|yrs?)\s*-?\s*(?:olds?|of\s+age)\b"
    rf"|\b{_AGE}\s*-?\s*(?:y\s*[./]?\s*o\b\.?|yo\b)"
    rf"|\bage[ds]?\s*[:=]?\s*{_AGE}\b"
    rf"|\b(?:turning|turned|just turned)\s+{_AGE}\b"
    rf"|\b{_ORD}\s+birthday\b"
    r"|\b\d{1,3}\s*-?\s*(?:months?|mos?|weeks?|wks?|days?)\s*-?\s*olds?\b"
    r"|\b(?:bab(?:y|ies)|infants?|newborns?|toddlers?|girls?|boys?|sons?|daughters?|kids?|child(?:ren)?)"
    r"\W+(?:(?:is|was|aged?|of|about|around|only|just)\W+)*\d{1,2}\s*-?\s*(?:months?|mos?|weeks?|wks?|days?)\b"
    r"(?!\s+(?:pregnant|postpartum|post-partum|later|ago|after|before|since|earlier|in|on|of)\b)"
    r"|\bsweet\s*(?:sixteen|16)\b"
)
# A person word near a bare age: "girl, 13", "boy (12)", "girl who is 12",
# "girl of 8", "girl, 5'2", 15" (a height or weight in between); or a teen
# age then one person: "13 girl" (not "one person", "two models", "12 girls
# on a runway" or "top 10 girl names").
_PERSON = (r"(?:girl|girls|boy|boys|kid|kids|child|daughter|son|sister|brother|niece|nephew|student|"
           r"pupil|teen|baby)")
# Words for adults: a teen age (10 to 17) still makes them a minor ("woman,
# 16", "nude woman (16)", "woman who is 16", "woman 16 nude"). The number
# has to end there, as an age does (the end, punctuation, "and", "in",
# "years old", a sexual or clothing word): a count or a label goes on
# ("ladies, 3 tips", "woman, one hand on her hip", "man, 6 pack abs",
# "woman 1 and woman 2", "my wife of 10 years", "12 female models"). "Model
# 3" and "Tesla model 3" are cars ("Model 15" and "model 15" aren't).
_ADULT_PERSON = (r"(?:woman|women|man|men|lady|ladies|guy|guys|gal|wife|husband|girlfriend|boyfriend|"
                 r"bride|actress|actor|female|male|blonde|brunette|redhead|person|babe|"
                 r"(?:(?-i:Model)|(?<=tesla\s)model)(?!\s+[1-9]\b)|(?<!tesla\s)(?-i:model|MODEL))")
_LINK = r"(?:who|is|was|of|aged?|just|only|about|around|maybe)"
# A height or weight between a person and an age: 5'2", 5'2, 160 cm, 50 kg.
_MEASURE = r"(?:\d\s*['’′]\s*\d{0,2}\s*(?:[\"”″]|'')?|\d{2,3}\s*(?:cm|kg|lbs?|pounds)\b|\d(?:\.\d+)?\s*m\b)"
_GAP = rf"(?:\W+(?:{_LINK}|{_MEASURE}))*"
_AGE_END = (r"(?=\s*(?:$|[,;:!?)\]}\"”]|\.(?!\d)|[-–—](?!\s*\d)|&|"
            r"(?:years?|yrs?)(?:\s*-?\s*olds?\b|\s+of\s+age\b|\s+young\b|\s*(?:$|[,;:!?)\]}.]))|"
            r"(?:and|or|but|with|in|at|on|wearing|who|y\.?o\.?|yo|f|female|male|girl|boy|posing|poses|pose|"
            r"looking|petite|blonde|brunette|redhead|cute|shy|innocent|tiny|small|teen|nude|naked|topless|"
            r"bottomless|sexy|lingerie|bikini|underwear|undressed|undressing|panties|thong|stripping|erotic|"
            r"seductive|sensual|provocative)\b))")
# "of N years" after an adult is how long ("my wife of 10 years"), unless
# "old" or "of age" follows.
_NOT_DURATION = r"(?!\s*-?\s*(?:years?|yrs?|months?|mos?)\b(?!\s*-?\s*olds?\b|\s+of\s+age\b))"
_ADULT_WORD = r"(?:woman|man|lady|female|male|wife|girlfriend|boyfriend|bride|model|babe)"
_PERSON_AGE = (rf"\b{_PERSON}{_GAP}\W+{_AGE}\b{_NOT_AGE}"
               rf"|\b{_ADULT_PERSON}{_GAP}\s*[^\w\s#]+\s*{_TEEN}\b{_NOT_AGE}{_AGE_END}"
               rf"|\b{_ADULT_PERSON}(?:\W+{_LINK})+\W+{_TEEN}\b{_NOT_AGE}{_NOT_DURATION}{_AGE_END}"
               rf"|\b{_ADULT_PERSON}\s+{_TEEN}\b{_NOT_AGE}{_AGE_END}"
               rf"|(?<!top\s)(?<!top-)\b{_TEEN}\W{{0,3}}(?:girl|boy|kid|child|daughter|son|teen|student|pupil|"
               r"schoolgirl|schoolboy)\b"
               rf"|(?<!top\s)(?<!top-)\b{_TEEN}\W{{0,3}}{_ADULT_WORD}\b{_AGE_END}"
               # "16f": a teen age and "female".
               r"|\b1[0-7]f\b"
               # "she is 14", "he's 12", "they just turned 13" (not "he is 6 feet", "10 minutes late").
               rf"|\b(?:she|he|they)(?:[’']s|\s+(?:is|was|are|were|turns|turned|just turned))\s+{_AGE}\b{_NOT_AGE}")
# School years: "year 9" only about a pupil ("year 9 student", "girl in
# year 7", "she's in year 9"), not "year 1 anniversary" or "Year 2026".
_YEAR = r"year\s*(?:1[0-3]|[1-9])\b"
_SCHOOL_YEAR = (rf"\b{_YEAR}\s*(?:students?|pupils?|girls?|boys?|kids?|child(?:ren)?|class(?:es|mates?)?|"
                rf"classroom|school\w*|uniforms?|form|tutor\s+group)\b"
                rf"|\b(?:{_PERSON}|{_ADULT_PERSON}|she|he|they|who)(?:[’']s|\s+(?:is|was|are|were))?\s+in\s+{_YEAR}"
                rf"|\b(?:school|class|pupil|student|uniform|teacher)s?\b[^.!?\n]{{0,30}}\b{_YEAR}")
# Next to a sexual word, more reads as a minor: a secondary school year on
# its own ("topless, Year 10"; not "year 7 anniversary"), and a label
# number that ends the phrase ("nude Model 9", "naked person 9"; not "Model
# 3", the car, or "person 2 in the scene").
_SEXUAL_CONTEXT_MINOR = (
    r"\byear\s*(?:1[0-3]|[7-9])\b(?!\s*(?:anniversary|anniversaries|olds?|warranty|guarantee|plan|later|ago|of)\b)"
    rf"|(?<!tesla\s)\b(?:model|babe|person)\s+(?:#\s*)?(?!3\b){_AGE}\b{_NOT_AGE}"
    r"(?=\s*(?:$|[,;:!?)\]}\"”]|\.(?!\d)))"
)
# In a prompt, a teen age standing alone between commas or brackets ("Emma,
# 15, nude", "topless cheerleader, 15"), or a single digit alone between
# commas ("nude woman, 9"; not "set (2)"): prompts are lists of short
# phrases, so a lone age there is an age. Not in scripts.
_LONE_TEEN = (rf"(?:^|[,(\[])\s*{_TEEN}\b{_NOT_AGE}(?=\s*(?:$|[,;)\]]|\.(?!\d)))"
              rf"|(?:^|,)\s*[1-9]\b{_NOT_AGE}(?=\s*(?:$|,))")
# Family words, which ad copy uses about adults all the time ("my daughter
# and I started this boutique"): scripts and Cast cards leave them out.
_FAMILY = r"daughter|daughters|stepdaughter|stepdaughters|step-daughter|"


def _minor_terms(family: str) -> str:
    return (
        r"\b(?:child|childs|children|childlike|child-like|kid|kids|kiddie|kiddy|minor|minors|underage|"
        r"underaged|under-age|under-aged|teen|teens|teenage|teenaged|teenager|teenagers|teenie|teeny|"
        r"preteen|pre-teen|preteens|tween|tweens|adolescent|adolescents|juvenile|juveniles|pubescent|"
        r"prepubescent|pre-pubescent|youth|youths|youngster|youngsters|little ones?|"
        + family +
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
        r"(?:freshman|sophomore) in high[ -]?school)\b"
        rf"|{_AGE_FORMS}|{_PERSON_AGE}|{_SCHOOL_YEAR}"
    )


_MINOR_TERMS = _minor_terms(_FAMILY)
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
_CONTEXT_PROMPT_RE = re.compile(f"{_SEXUAL_CONTEXT_MINOR}|{_LONE_TEEN}", re.I)
_CONTEXT_SCRIPT_RE = re.compile(_SEXUAL_CONTEXT_MINOR, re.I)
# Refused on their own. ("cp" alone isn't: "CP violation", "CP 2077".)
_ALWAYS_RE = re.compile(
    r"\b(?:loli|lolicon|lolis|shota|shotacon|jailbait|csam|child ?porn\w*|kiddie ?porn|pedo ?porn)\b|" + _CP, re.I)

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
    callers turn the provider's own content check on as a backstop. Next to
    a sexual or everyday word ("bikini"), a lone teen number or a school
    year counts too (_SEXUAL_CONTEXT_MINOR, _LONE_TEEN)."""
    forms = _forms(text)
    return _any(_MINOR_RE, forms) or (
        _any(_CONTEXT_PROMPT_RE, forms) and (_any(_SEXUAL_RE, forms) or _any(_SOFT_RE, forms)))


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
    minor = _any(_MINOR_RE, forms) or _any(_CONTEXT_PROMPT_RE, forms)
    if (_any(_ALWAYS_RE, forms) or (minor and _any(_SEXUAL_RE, forms))
            or any(_cp_near_minor(f, _MINOR_RE) for f in forms)):
        raise UnsafePromptError(
            "This prompt was refused: it appears to involve sexual content and minors.")


# Scripts: the same minors check without family words ("my daughter and I
# started this boutique"), which ad copy uses about adults all the time.
_SCRIPT_MINOR_RE = re.compile(_minor_terms(""), re.I)


def check_script(text: str) -> None:
    """check_prompt for scripts and other planner input (creative direction,
    titles): what validate_script misses ("teenage", "14-year-old", look-alike
    letters), without its family-word false positives."""
    forms = _forms(text)
    minor = _any(_SCRIPT_MINOR_RE, forms) or _any(_CONTEXT_SCRIPT_RE, forms)
    if (_any(_ALWAYS_RE, forms) or (minor and _any(_SEXUAL_RE, forms))
            or any(_cp_near_minor(f, _SCRIPT_MINOR_RE) for f in forms)):
        raise UnsafePromptError(
            "This script was refused: it appears to involve sexual content and minors.")


# Avoid-list words that remove signs of adulthood.
# - Age words always count: "adult", "mature", "milf", an adult age ("21
#   year old", "over 18", "18+"), "old" or "aged" about a person or a body
#   ("old", "older", "old face", "older looking skin"; not "old photo",
#   "old-fashioned" or "aged paper"), and wrinkles (not "wrinkled sheets").
# - Body words ("curvy", "body hair") count when the prompt names no adult,
#   or describes a young body itself ("petite, flat chest").
# - Gender words ("woman", "men", "beard") count only when the prompt names
#   no adult: "two women kissing" may avoid "men". People in the background
#   ("men in background", "extra people") don't count.
# The AI check sees the whole avoid list either way (it's sent first).
_ADULT_AGE_RE = re.compile(
    r"\b(?:adults?|adult-looking|adultlike|adult-like|mature|maturity|grown|grown-up|grownup|elderly|"
    r"middle-aged|milfs?|cougars?)\b"
    r"|\bover\s+(?:the\s+age\s+of\s+)?(?:1[89]|[2-9]\d)\b|\b(?:1[89]|[2-9]\d)\s*\+"
    r"|\b(?:1[89]|[2-9]\d)\s*-?\s*(?:years?|yrs?|y\.?o\.?|yo)\b", re.I)
_OLD = r"(?:old|older|aged|ageing|aging)"
_OLD_BODY = (r"(?:faces?|skin|bod(?:y|ies)|features?|lady|ladies|woman|women|man|men|person|people|hands?|neck|"
             r"breasts?|chest|figure|eyes)")
_OLD_TERM_RE = re.compile(
    rf"(?:(?:too|very|looks?|looking|appears?|more|an?)\s+)*{_OLD}"
    r"(?:[\s-]+(?:looking|age|appearance|looks?|lady|ladies|woman|women|man|men|person|people))?", re.I)
_OLD_ABOUT_BODY_RE = re.compile(
    rf"\b{_OLD}\b(?:[\s-]+\w+){{0,2}}?[\s-]+{_OLD_BODY}\b|\b{_OLD_BODY}\b(?:[\s-]+\w+){{0,2}}?[\s-]+{_OLD}\b",
    re.I)
_WRINKLE_RE = re.compile(r"\b(?:wrinkles?|wrinkled|wrinkly)\b", re.I)
_FABRIC_RE = re.compile(r"\b(?:sheets?|bedsheets?|bedding|bed|beds|linens?|pillows?|blankets?|duvets?|covers?|"
                        r"clothes|clothing|fabrics?|cloth|shirts?|dress(?:es)?|skirts?|paper|curtains?|"
                        r"backdrops?|textiles?|garments?|outfits?|trousers|pants|suit)\b", re.I)
_BODY_SIGN_RE = re.compile(r"\b(?:curvy|curves|voluptuous|body hair|pubic hair|chest hair|armpit hair|hairy|"
                           r"wide hips|thick thighs|big breasts|large breasts)\b", re.I)
_GENDER_SIGN_RE = re.compile(r"\b(?:woman|women|man|men|lady|ladies|male|female|beards?|bearded|stubble|"
                             r"moustache|mustache)\b", re.I)
_BACKGROUND_RE = re.compile(
    r"\b(?:men|women|man|woman|people|persons?|ladies|guys|figures)\s+in\s+(?:the\s+)?background\b"
    r"|\bbackground\s+(?:men|women|people|persons?|figures|characters|crowds?)\b"
    r"|\b(?:extra|other|additional|second|another|two|more)\s+(?:people|persons?|men|women|man|woman|figures)\b"
    r"|\bcrowds?\b", re.I)
_NAMES_ADULT_RE = re.compile(r"\b(?:adults?|woman|women|man|men|lady|ladies|wife|husband|milfs?|mature|"
                             r"gentlem[ae]n|mother|mom|mum|father|dad|businessm[ae]n|businesswom[ae]n|"
                             r"models?|babes?)\b"
                             r"|\b(?:1[89]|[2-9]\d)\s*-?\s*(?:years?|yrs?|y\.?o\.?|yo)\b"
                             r"|[,(]\s*(?:1[89]|[2-9]\d)\s*(?:[,)]|$)", re.I)
_YOUNG_BODY_RE = re.compile(r"\b(?:petite|flat[- ]?chest(?:ed)?|small (?:breasts|chest|body|frame)|tiny|"
                            r"smooth skin|hairless|youthful|baby[- ]?face[d]?|childlike|child-like)\b", re.I)


def steers_young(prompt: str, negative: str) -> bool:
    """A sexual prompt whose "avoid" list removes signs of adulthood
    ("adult, mature, woman"): treated like a mention of a minor."""
    if not (negative or "").strip() or not is_sexual(prompt):
        return False
    neg = _normalise(negative)
    if _ADULT_AGE_RE.search(neg):
        return True
    terms = [t.strip(" .!?\"'") for t in re.split(r"[,;|/()\[\]\n]+|\s+(?:and|or)\s+", neg)]
    for t in filter(None, terms):
        if re.fullmatch(r"(?:1[89]|[2-9]\d)\s*(?:plus)?", t):  # "18+" ("+" reads as a space), "21"
            return True
        if _OLD_TERM_RE.fullmatch(t) or _OLD_ABOUT_BODY_RE.search(t):
            return True
        if _WRINKLE_RE.search(t) and not _FABRIC_RE.search(t):
            return True
    said = _normalise(prompt)
    names_adult = bool(_NAMES_ADULT_RE.search(said))
    if _BODY_SIGN_RE.search(neg) and (not names_adult or _YOUNG_BODY_RE.search(said)):
        return True
    return bool(_GENDER_SIGN_RE.search(_BACKGROUND_RE.sub(" ", neg))) and not names_adult


# ---------- a Cast character's own text ----------
#
# A character card describes one person, so any age under 18 written on it
# counts, not only the prompt forms above: "Mia, 15", "Mia (15)", "Mia,
# fifteen", "Mia: 15", "looks about 15", "15F", "born in 2012". Numbers that
# aren't ages are left alone: heights (5'9", 170 cm, "5 and a half feet"),
# sizes ("size 8", "Dress: 4"), labelled fields ("Shoe size: 7", "Tattoos:
# 2"), counts ("2 kids"), durations ("12 years as a nurse"), years, prices,
# ratings ("10/10"), cars ("Model 3"), "one" ("one in a million").
_CARD_NUM = (r"(?:1[0-7]|0?[1-9]|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
             r"fourteen|fifteen|sixteen|seventeen)")
_CARD_AGE_RE = re.compile(
    # A bare age after a comma, bracket, semicolon or dash, or first in a
    # field, followed by the end, punctuation or a word that goes with an
    # age. Not after a colon: "Label: N" is a field (see _labelled_age).
    rf"(?:^|[,(\[;]|\s[-–—])\s*{_CARD_NUM}\b{_NOT_AGE}"
    r"(?=\s*(?:$|[,;:!?)\]}\"”]|\.(?!\d)|[-–—](?!\s*\d)|&|"
    r"(?:years?|yrs?)(?:\s*-?\s*olds?\b|\s+of\s+age\b|\s+young\b|\s*(?:$|[,;:!?)\]}.]))|"
    r"(?:and|but|with|from|who|in|at|y\.?o\.?|f|m|female|male|girl|boy|looking|petite|blonde|"
    r"brunette|redhead|cute|shy|innocent|tiny|small)\b))"
    r"|\b(?:1[0-7]|[1-9])f\b"
    # "looks about 15", "could pass for 16", "looks younger than 18".
    r"|\b(?:looks?|looking|appears?|appearing|seems?|seeming|pass(?:es)?\s+for|mistaken\s+for)\s+"
    r"(?:(?:to\s+be|like|about|around|maybe|only|just|barely|roughly|under|no\s+more\s+than)\s+)*"
    rf"{_AGE}\b{_NOT_AGE}"
    r"|\b(?:looks?|looking|appears?|seems?|is|she[’']?s|he[’']?s|probably|maybe|barely|just)\s+"
    r"(?:under|below|younger\s+than)\s+(?:the\s+age\s+of\s+)?18\b"
    r"|\bin\s+(?:her|his|their)\s+(?:early\s+|mid\s+|mid-|late\s+)?teens\b"
    # A school grade: "a grade 9 student", "in grade 8" (not "Pianist (Grade
    # 8)" or "a Grade 2 listed cottage").
    r"|\b(?:in|into|repeating|repeated|starting|started|entering|entered|finishing|finished)\s+(?:the\s+)?"
    r"grade\s*(?:1[0-2]|[1-9])\b"
    r"|\bgrade\s*(?:1[0-2]|[1-9])\s+(?:students?|pupils?|girls?|boys?|kids?|child(?:ren)?|class(?:es|mates?)?|"
    r"schoolers?)\b",
    re.I)
_BORN_RE = re.compile(r"\bborn\s+(?:in\s+)?((?:19|20)\d\d)\b", re.I)
# Other people on the card aren't the character: "mother of two kids",
# "her kids", "has two children", "2 kids", "mother of a 15-year-old", "her
# 16-year-old son", "a high school English teacher", "works with kids",
# "Kids: 2". Not "one of 3 kids", "her kid sister", "a child's face" or
# "the teacher's pet". Their ages are left to the AI check, which is told
# that other people's ages don't count.
_KIN = (r"(?:kids?|children|child|sons?|daughters?|stepdaughters?|step-daughters?|stepsons?|step-sons?|"
        r"grandchild(?:ren)?|grandkids?|toddlers?)\b(?![’']s\b|\s*-?\s*(?:sister|brother|cousin|self|selves|version|"
        r"like|bride|wife|body|face|look|size))")
_COUNT = r"(?:a|an|the|one|two|three|four|five|six|seven|eight|nine|several|many|some|[1-9])"
# How old someone else is, after "mother of" or "has" ("a 15-year-old
# son", "a teenage son", "grown-up kids"). Not after "her" or "his": "Mia,
# his teenage daughter" is about Mia.
_KIN_AGE = (rf"(?:{_AGE}\s*-?\s*(?:years?|yrs?)\s*-?\s*olds?|teenage|teen|young|little|grown|grown-up|grownup|"
            r"adult)")
_KIN_MORE = r"(?:teens|teenagers|boys|girls)\b"
_SCHOOL = (r"(?:(?:high|middle|elementary|primary|secondary|junior high|grade|prep)[ -]?school|kindergarten|"
           rf"preschool|pre-school|nursery|{_ORD}[ -]grade)")
_STAFF = (r"teachers?|principals?|headteachers?|headmistress|headmaster|counsell?ors?|school nurse|librarians?|"
          r"coach(?:es)?|tutors?|aides?|assistants?|staff")
_PARENT = (r"(?:mother|mom|mum|mommy|mama|father|dad|daddy|papa|parent|stepmother|stepmom|stepmum|stepfather|"
           r"stepdad|grandmother|grandma|granny|grandfather|grandpa|aunt|auntie|uncle|godmother|godfather)")
_RELATIONS_RE = re.compile(
    rf"\b(?:her|his|their|my|our|your)\s+(?:own\s+)?(?:{_COUNT}\s+)?(?:(?:grown|grown-up|grownup|adult)\s+)?{_KIN}"
    rf"|\b{_PARENT}\s+(?:of|to|with)\s+(?:{_COUNT}\s+)?(?:{_KIN_AGE}\s+)?(?:{_KIN}|{_KIN_MORE})"
    rf"|\b{_PARENT}\s+(?:of|to)\s+(?:a|an|one|two|three)\s+{_AGE}\s*-?\s*(?:years?|yrs?)\s*-?\s*olds?\b"
    rf"|\b{_PARENT}\W{{1,3}}(?:1|one|a|an)\s+{_KIN}"
    rf"|\b(?:has|have|had|raising|raises|raised)\s+(?:{_COUNT}\s+)?(?:{_KIN_AGE}\s+)?(?:{_KIN}|{_KIN_MORE})"
    rf"|(?<!of\s)(?<!the\s)\b(?:[2-9]|two|three|four|five|six|seven|eight|nine)\s+{_KIN}"
    rf"|\b(?:grown|grown-up|grownup|adult)\s+(?:{_KIN}|{_KIN_MORE})"
    r"|\b(?:kids|children|sons|daughters|grandkids|grandchildren)\s*:\s*\d{1,2}\b"
    r"|\b(?:works?|working|worked)\s+with\s+(?:kids|children|teens|teenagers|young\s+people|youth)\b"
    rf"|\b(?:{_STAFF}|(?:teach(?:es|ing)?|works?|working)(?:\s+\w+)?)\s+(?:at|in|of|for)\s+(?:a|an|the)?\s*"
    rf"(?:{_SCHOOL}|teens|teenagers|kids|children)\b"
    rf"|\bteach(?:es|ing)?\s+(?:the\s+)?{_SCHOOL}"
    rf"|\b{_SCHOOL}\s+(?:\w+\s+)?(?:{_STAFF})\b(?![’']s\b)"
    rf"|\b{_SCHOOL}\s+(?:sweethearts?|reunions?|diploma|graduates?|graduation|dropout|friends?)\b"
    r"|\byouth\s+(?:pastor|minister|worker|leader|group\s+leader|coach|counsell?or|director|club)\b",
    re.I)
# A past the character has left behind ("ex-teen model", "former teen pop
# star", "started modelling in her teens", "in her youth", "her 16th
# birthday was 20 years ago"): not their age now. Left to the AI check.
_BACKSTORY_RE = re.compile(
    r"\b(?:ex-?|former(?:ly)?\s+(?:a\s+)?)\s*teen(?:age)?\s+\w+"
    r"|\b(?:started|began|begun|starting|beginning|since)\b[^.;,]{0,40}?\b(?:in\s+(?:her|his|their)\s+"
    r"(?:early\s+|mid\s+|mid-|late\s+)?teens|as\s+a\s+teen(?:ager)?|at\s+(?:age\s+)?(?:1[0-7]|[5-9]))\b"
    r"|\bin\s+(?:her|his|their)\s+youth\b"
    rf"|\b(?:her|his|their)\s+{_ORD}\s+birthday\s+was\s+[^.;]{{0,30}}?\bago\b"
    rf"|\bwhen\s+(?:she|he|they)\s+(?:was|were)\s+(?:a\s+teen(?:ager)?|{_AGE})\b(?![^.;]{{0,5}}\bnow\b)",
    re.I)


def _born_minor(forms: list[str]) -> bool:
    from datetime import date

    year = date.today().year
    return any(year - int(m.group(1)) < 18 for f in forms for m in _BORN_RE.finditer(f))


def _labelled_age(name: str) -> "re.Pattern":
    """ "Mia: 15" (the character's name, or its first word, as the label) or
    a field that is only ": 15". "Age: 15" is an age form already."""
    name = " ".join((name or "").split())
    names = {re.escape(n) for n in (name, name.split(" ")[0]) if n}
    label = rf"(?:(?:{'|'.join(sorted(names))})\s*)?" if names else ""
    return re.compile(rf"(?:^|[\n.;,(]\s*){label}[:=]\s*{_CARD_NUM}\b{_NOT_AGE}"
                      r"(?=\s*(?:$|[,;!?)\]}\"”]|\.(?!\d)))", re.I)


def _snippet(text: str, m: "re.Match") -> str:
    """The matched words with a little of what comes before ("Mia, 15")."""
    start = max(0, m.start() - 20)
    if start:
        space = text.find(" ", start)
        start = space + 1 if 0 <= space < m.start() else m.start()
    end = m.end() + (1 if text[m.end():m.end() + 1] in (")", "]") else 0)
    out = " ".join(text[start:end].split()).strip(" ,;:[-–—")
    return out or m.group(0).strip()


def character_minor_reason(*fields: str) -> "str | None":
    """The words that make a Cast character's own text (name, description,
    persona, look; one field each) name a minor, or None. See
    character_minor."""
    name = fields[0] if fields else ""
    labelled = _labelled_age(name)
    for field in fields:
        if not (field or "").strip():
            continue
        forms = _forms(field)
        own = [_BACKSTORY_RE.sub(" ", _RELATIONS_RE.sub(" ", f)) for f in forms]
        for rx, texts in ((_CARD_AGE_RE, own), (labelled, own), (_ALWAYS_RE, forms), (_SCRIPT_MINOR_RE, own)):
            for t in texts:
                m = rx.search(t)
                if m:
                    return _snippet(t, m)
        for f in forms:
            for m in _BORN_RE.finditer(f):
                if _born_minor([m.group(0)]):
                    return m.group(0)
    return None


def character_minor(*fields: str) -> bool:
    """A Cast character's own text (name, description, persona, look; one
    field each) names a minor: an age under 18 stated about them in any
    form, a child or teen word, or a school year. Family words, other
    people's ages and the character's past don't ("mother of a 15-year-old",
    "ex-teen model"): the AI check (minor_check.check_character) reads those."""
    return character_minor_reason(*fields) is not None

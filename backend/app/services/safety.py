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
_TEEN = r"(?:1[0-7]|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen)"
_ORD = rf"(?:1[0-7]th|1st|2nd|3rd|[4-9]th|{_ORD_WORDS})"
# What can follow a number that makes it something other than an age:
# units and heights ("5 ft 9", 5'9", "1.75 m"), times ("10 pm", "7:30"),
# ratings ("9 out of 10", "10/10"), durations ("15 minutes late", "2 years
# together"; "15 years old" is an age form of its own), counts ("1 of 3").
# "in" is inches only before tall/long/wide/high ("girl, 15 in a bikini" is
# an age), and a foot mark only after 4 to 7 ("girl, 15'" isn't a height).
_UNIT = r"(?:am|pm|cm|mm|m|kg|lbs?|ft|feet|foot|in|inch(?:es)?|metres?|meters?|percent|points?|stars?)"
_NOT_AGE = (
    r"(?!\s*(?:(?:am|pm|cm|mm|kg|lbs?|ft|feet|foot|inch(?:es)?|metres?|meters?|percent|points?|stars?|"
    r"times|x|th|st|nd|rd|min(?:ute)?s?|h(?:ou)?rs?|sec(?:ond)?s?|days?|weeks?|months?)\b|%|\+|:\d|"
    rf"m\s+tall\b|in\.|in\s+(?:tall|long|wide|high)\b|[.,]\d+\s*(?:{_UNIT}\b|%)|/\s*(?:10|100|5)\b|"
    r"out\s+of\b|of\s+(?:\d|the|them|us|those|these)\b|"
    r"(?:years?|yrs?)\s+(?:ago|later|earlier|before|after|since|of|in|on|at|from|together|married|running|"
    r"straight|experience|long|younger|older|apart)\b))"
    r"(?!(?<=(?<!\d)[4-7])\s*['’′]\s*(?:\d|tall\b))")
# "13 year old", "13-yr-old", "12yrold", "13 y.o.", "13yo", "14-yo", "13 y/o",
# "aged 13", "age: 13", "13 years of age", "turning 12", "her 9th birthday",
# "9 months old". A count of years alone isn't an age ("ten years of marriage").
_AGE_FORMS = (
    rf"\b{_AGE}\s*-?\s*(?:years?|yrs?)\s*-?\s*(?:olds?|of\s+age)\b"
    rf"|\b{_AGE}\s*-?\s*(?:y\s*[./]?\s*o\b\.?|yo\b)"
    rf"|\bage[ds]?\s*[:=]?\s*{_AGE}\b"
    rf"|\b(?:turning|turned|just turned)\s+{_AGE}\b"
    rf"|\b{_ORD}\s+birthday\b"
    r"|\b\d{1,3}\s*-?\s*(?:months?|mos?|weeks?|wks?|days?)\s*-?\s*olds?\b"
    r"|\bsweet\s*(?:sixteen|16)\b"
)
# A person word near a bare age: "girl, 13", "boy (12)", "girl who is 12",
# "girl of 8"; or a teen age then one person: "13 girl" (not "one person",
# "two models" or "12 girls on a runway").
_PERSON = (r"(?:girl|girls|boy|boys|kid|kids|child|daughter|son|sister|brother|niece|nephew|student|"
           r"pupil|teen|baby)")
# Words for adults: an age under 18 still makes them a minor ("woman, 16",
# "nude woman (16)", "woman who is 16"). A small number right after one with
# only a space is a label, not an age ("woman 1 and woman 2"); a teen number
# is an age ("woman 16"). "Model 3" and "Tesla model 3" are cars ("Model 15"
# and "model 15" aren't).
_ADULT_PERSON = (r"(?:woman|women|man|men|lady|ladies|guy|guys|gal|wife|husband|girlfriend|boyfriend|"
                 r"bride|actress|actor|female|male|blonde|brunette|redhead|person|babe|"
                 r"(?:(?-i:Model)|(?<=tesla\s)model)(?!\s+[1-9]\b)|(?<!tesla\s)(?-i:model|MODEL))")
_LINK = r"(?:who|is|was|of|aged?|just|only|about|around|maybe)"
_PERSON_AGE = (rf"\b{_PERSON}(?:\W+{_LINK})*\W+{_AGE}\b{_NOT_AGE}"
               rf"|\b{_ADULT_PERSON}(?:\W+{_LINK})*\s*[^\w\s#]+\s*{_AGE}\b{_NOT_AGE}"
               rf"|\b{_ADULT_PERSON}(?:\W+{_LINK})+\W+{_AGE}\b{_NOT_AGE}"
               rf"|\b{_ADULT_PERSON}\s+{_TEEN}\b{_NOT_AGE}"
               rf"|\b{_TEEN}\W{{0,3}}(?:girl|boy|kid|child|daughter|son|teen|student|pupil|schoolgirl|schoolboy|"
               r"woman|man|lady|female|male|wife|girlfriend|boyfriend|bride|model|babe)\b"
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
_SCRIPT_MINOR_RE = re.compile(_minor_terms(""), re.I)


def check_script(text: str) -> None:
    """check_prompt for scripts and other planner input (creative direction,
    titles): what validate_script misses ("teenage", "14-year-old", look-alike
    letters), without its family-word false positives."""
    forms = _forms(text)
    if _any(_ALWAYS_RE, forms) or (_any(_SCRIPT_MINOR_RE, forms) and _any(_SEXUAL_RE, forms)):
        raise UnsafePromptError(
            "This script was refused: it appears to involve sexual content and minors.")


# Avoid-list words that remove signs of adulthood. Age words always count.
# "old" and "aged" count only about a person ("old", "older", "old-looking",
# "old face"), not "old photo", "old-fashioned" or "grainy old". Gender and
# body words ("woman", "men", "beard", "curvy") count only when the prompt
# names no adult itself: "two women kissing" may avoid "men". The AI check
# sees the whole avoid list either way.
_ADULT_AGE_RE = re.compile(r"\b(?:adults?|adult-looking|adultlike|adult-like|mature|maturity|grown|grown-up|"
                           r"grownup|elderly|middle-aged|milfs?|cougars?|wrinkles?|wrinkled)\b", re.I)
_OLD_TERM_RE = re.compile(
    r"(?:(?:too|very|looks?|looking|appears?|more|an?)\s+)*(?:old|older|aged|ageing|aging)"
    r"(?:[\s-]+(?:looking|age|faces?|skin|bod(?:y|ies)|features?|appearance|looks?|lady|ladies|woman|women|"
    r"man|men|person|people|hands?|neck))?", re.I)
_ADULT_SIGN_RE = re.compile(r"\b(?:woman|women|man|men|lady|ladies|curvy|voluptuous|beards?|bearded|stubble|"
                            r"chest hair|body hair|pubic hair)\b", re.I)
_NAMES_ADULT_RE = re.compile(r"\b(?:adults?|woman|women|man|men|lady|ladies|wife|husband|milfs?|mature|"
                             r"gentlem[ae]n|mother|mom|mum|father|dad|businessm[ae]n|businesswom[ae]n)\b", re.I)


def steers_young(prompt: str, negative: str) -> bool:
    """A sexual prompt whose "avoid" list removes signs of adulthood
    ("adult, mature, woman"): treated like a mention of a minor."""
    if not (negative or "").strip() or not is_sexual(prompt):
        return False
    neg = _normalise(negative)
    if _ADULT_AGE_RE.search(neg):
        return True
    terms = (t.strip(" .!?\"'") for t in re.split(r"[,;|/()\[\]\n]+|\s+and\s+", neg))
    if any(_OLD_TERM_RE.fullmatch(t) for t in terms if t):
        return True
    return bool(_ADULT_SIGN_RE.search(neg)) and not _NAMES_ADULT_RE.search(_normalise(prompt))


# ---------- a Cast character's own text ----------
#
# A character card describes one person, so any age under 18 written on it
# counts, not only the prompt forms above: "Mia, 15", "Mia (15)", "Mia,
# fifteen", "looks about 15", "15F", "born in 2012". Numbers that aren't
# ages are left alone: heights (5'9", 170 cm), sizes ("size 8"), counts
# ("2 kids"), years, prices, ratings ("10/10"), cars ("Model 3").
_CARD_AGE_RE = re.compile(
    # A bare age after a comma, bracket, dash or colon, or first in a field,
    # followed by the end, punctuation or a word that goes with an age.
    rf"(?:^|[,(\[:;|]|\s[-–—])\s*{_AGE}\b{_NOT_AGE}"
    r"(?=\s*(?:$|[,;:!?)\]}\"”]|\.(?!\d)|[-–—](?!\s*\d)|&|"
    r"(?:and|but|with|from|who|in|at|years?|yrs?|y\.?o\.?|f|m|female|male|girl|boy|looking|petite|blonde|"
    r"brunette|redhead|cute|shy|innocent|tiny|small)\b))"
    r"|\b(?:1[0-7]|[1-9])f\b"
    # "looks about 15", "could pass for 16", "looks younger than 18".
    r"|\b(?:looks?|looking|appears?|appearing|seems?|seeming|pass(?:es)?\s+for|mistaken\s+for)\s+"
    r"(?:(?:to\s+be|like|about|around|maybe|only|just|barely|roughly|under|no\s+more\s+than)\s+)*"
    rf"{_AGE}\b{_NOT_AGE}"
    r"|\b(?:looks?|looking|appears?|seems?|is|she[’']?s|he[’']?s|probably|maybe|barely|just)\s+"
    r"(?:under|below|younger\s+than)\s+(?:the\s+age\s+of\s+)?18\b"
    r"|\bin\s+(?:her|his|their)\s+(?:early\s+|mid\s+|mid-|late\s+)?teens\b"
    r"|\bgrade\s*(?:1[0-2]|[1-9])\b",
    re.I)
_BORN_RE = re.compile(r"\bborn\s+(?:in\s+)?((?:19|20)\d\d)\b", re.I)
# Other people on the card aren't the character: "mother of two kids",
# "her kids", "has two children", "2 kids", "a high school teacher". Not
# "one of 3 kids", "her kid sister", "a child's face" or "the teacher's
# pet", and an age next to them still counts.
_KIN = (r"(?:kids?|children|child|sons?|daughters?|stepdaughters?|step-daughters?|stepsons?|step-sons?|"
        r"grandchild(?:ren)?|grandkids?|toddlers?)\b(?![’']s\b|\s*-?\s*(?:sister|brother|cousin|self|selves|"
        r"version|like|bride|wife|body|face|look|size))")
_COUNT = r"(?:a|an|the|one|two|three|four|five|six|seven|eight|nine|several|many|some|[1-9])"
_SCHOOL = r"(?:high|middle|elementary|primary|secondary|junior high|grade|prep)[ -]?school"
_STAFF = (r"teachers?|principals?|headteachers?|headmistress|headmaster|counsell?ors?|school nurse|librarians?|"
          r"coach(?:es)?|tutors?")
_RELATIONS_RE = re.compile(
    rf"\b(?:her|his|their|my|our|your)\s+(?:own\s+)?(?:{_COUNT}\s+)?{_KIN}"
    r"|\b(?:mother|mom|mum|mommy|mama|father|dad|daddy|papa|parent|stepmother|stepmom|stepmum|stepfather|"
    r"stepdad|grandmother|grandma|granny|grandfather|grandpa|aunt|auntie|uncle|godmother|godfather)\s+"
    rf"(?:of|to)\s+(?:{_COUNT}\s+)?{_KIN}"
    rf"|\b(?:has|have|had|raising|raises|raised)\s+(?:{_COUNT}\s+)?{_KIN}"
    rf"|(?<!of\s)(?<!the\s)\b(?:[2-9]|two|three|four|five|six|seven|eight|nine)\s+{_KIN}"
    rf"|\b(?:{_STAFF}|(?:teach(?:es|ing)?|works?|working)(?:\s+\w+)?)\s+(?:at|in|of|for)\s+(?:a|an|the)?\s*"
    rf"(?:{_SCHOOL}|teens|teenagers|kids|children)\b"
    rf"|\b(?:{_SCHOOL})\s+(?:{_STAFF})\b(?![’']s\b)",
    re.I)


def _born_minor(forms: list[str]) -> bool:
    from datetime import date

    year = date.today().year
    return any(year - int(m.group(1)) < 18 for f in forms for m in _BORN_RE.finditer(f))


def character_minor(*fields: str) -> bool:
    """A Cast character's own text (name, description, persona, look; one
    field each) names a minor: an age under 18 stated about them in any
    form, a child or teen word, or a school year. Family words alone don't
    ("her father's daughter", "mother of two kids"). The AI check
    (minor_check.check_character) handles what words can't."""
    for field in fields:
        if not (field or "").strip():
            continue
        forms = _forms(field)
        if _any(_CARD_AGE_RE, forms) or _born_minor(forms) or _any(_ALWAYS_RE, forms):
            return True
        if _any(_SCRIPT_MINOR_RE, [_RELATIONS_RE.sub(" ", f) for f in forms]):
            return True
    return False

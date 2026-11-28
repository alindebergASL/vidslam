from __future__ import annotations

import re

from ..schemas.storyboard import CaptionChunk, ShotPlan, StoryboardPlan
from .base import CastContext, ChatProvider


def _chunk_caption(text: str, max_words: int = 5) -> list[str]:
    words = re.split(r"\s+", text.strip())
    chunks: list[str] = []
    buf: list[str] = []
    for w in words:
        if not w:
            continue
        buf.append(w)
        if len(buf) >= max_words:
            chunks.append(" ".join(buf))
            buf = []
    if buf:
        chunks.append(" ".join(buf))
    return chunks


_HYPE = re.compile(r"\b(photorealistic|realistic|8k|4k|ultra|hd|cinematic|epic|breathtaking|stunning|"
                   r"beautiful|magical|ethereal|dreamy|vibrant|glowing|masterpiece)\b,?\s*", re.I)


# The mock clip check's reply. Tests set it to a "problem" verdict, a
# refusal or garbage; None = the deterministic "ok" below.
VISION_REPLY: str | None = None
# Every mock vision call: (text, number of images), for tests.
VISION_CALLS: list[tuple[str, int]] = []


# The mock safety check's verdict. Tests set a dict ({"minor_sexual": True,
# "reason": ...}) to force a verdict, or an exception to make the check
# fail; None = the deterministic rules in classify_minor.
MINOR_REPLY: dict | BaseException | None = None
# Every mock safety-check call (the text sent), for tests.
MINOR_CALLS: list[str] = []
# The mock story facts (minor_check.story_facts): a reply tests set (a dict
# {"characters": [...]}, or an exception); None = the rules in
# mock_story_characters. Every text it was asked about, for tests.
STORY_FACTS_REPLY: dict | BaseException | None = None
STORY_FACTS_CALLS: list[str] = []
# The mock content check of a clean deployment: a verdict dict ({"explicit":
# True, "reason": ...}) or an exception tests set; None = a word list
# (safety.is_hard_sexual). Every text it was asked about, for tests.
EXPLICIT_REPLY: dict | BaseException | None = None
EXPLICIT_CALLS: list[str] = []
# The mock picture check (Cast pictures before a tick, and pictures added
# to a ticked card): the asset ids it was asked about, and a reply tests
# can set ({"minor": bool, "reason"}, an exception, or a function of the
# asset id returning one of those or None). None: every picture is an adult.
PICTURE_CALLS: list[int] = []
PICTURE_REPLY = None
# The mock picture content check of a clean deployment (a frame grabbed
# from an uploaded video): a reply tests can set ({"explicit": bool,
# "reason"} or an exception). None: nothing explicit. Every call counted.
PICTURE_EXPLICIT_CALLS: list[str] = []
PICTURE_EXPLICIT_REPLY: dict | BaseException | None = None

# Signs of a child's look or setting that, with undressing or anything
# sexual, mean a minor in a sexual context even with no minor word.
_YOUNG_CUES = re.compile(
    r"\b(?:school ?uniforms?|schoolgirl outfit|school (?:bag|backpack|desk)|pigtails|braces on (?:her|his) teeth|"
    r"flat[- ]chested|flat chest|training bras?|stuffed animals?|teddy bears?|lunchbox|hall pass|"
    r"childlike|child-like|young-looking|looks? (?:very )?young|barely 18)\b", re.I)
_UNDRESS = re.compile(
    r"\b(?:takes? off|taking off|took off|removes? (?:her|his)|removing (?:her|his)|strips?|stripping|"
    r"undress\w*|in (?:her|his) underwear|on (?:the|her|his) bed|in bed)\b", re.I)
# How services/minor_check.py lays out the text it sends: the avoid list
# first (so a long prompt can't push it out of view), then the prompt.
AVOID_MARK = "Avoid list (negative prompt): "
PROMPT_MARK = "Prompt: "
# A story part: the app's block (the story note, facts and cast), then the
# user's text (minor_check.classifier_text).
CONTEXT_MARK = "Story context (from the app):\n"
USER_MARK = "User text:\n"
# A text with no story around it is sent the same way, with this as the
# app's block (minor_check.classifier_text): the app's block always comes
# first, so a look-alike block in the user's text can only ever sit inside
# the user's block, after the real one.
NO_CONTEXT = "(none)"
NO_CONTEXT_HEAD = f"{CONTEXT_MARK}{NO_CONTEXT}\n\n{USER_MARK}"


# How services/minor_check.py lays out a story facts request: the cast
# header, then this mark, then (a part of) the script.
STORY_SCRIPT_MARK = "\n\nScript:\n"
# Before the script: the minors found in earlier windows, and on a second
# ask, the cast members the first answer left out (minor_check.story_facts).
SO_FAR_MARK = "So far: "
# Before the script, too: the characters shown as minors in the part just
# before this one (a flashback that may go on).
EARLIER_MARK = "Earlier parts: "
FACTS_RETRY_MARK = "Also list every one of these cast members: "
# ... and, asked again when a reply to a first-person text has no narrator:
FACTS_NARRATOR_MARK = ("Also say who the first-person narrator (I/me) is (\"narrator\"): this text is told in the "
                       "first person.")
_MOCK_NUM = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
             "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
             "sixteen": 16, "seventeen": 17}
_MOCK_N = r"(1[0-7]|[2-9]|" + "|".join(_MOCK_NUM) + r")"
_MOCK_ORD = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
             "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13,
             "fourteenth": 14, "fifteenth": 15, "sixteenth": 16, "seventeenth": 17}
_MOCK_O = r"(1[0-7]th|1st|2nd|3rd|[4-9]th|" + "|".join(_MOCK_ORD) + r")"
_MOCK_AGES = [re.compile(p, re.I) for p in (
    rf"\b(?:was|is|turned|turns|turn|turning|aged?|age\s*[:=]?)\s+(?:(?:only|just|barely|nearly|almost|about|"
    rf"an?|now)\s+)?{_MOCK_N}\b"
    r"(?!\s*(?:pm|am|feet|foot|ft|minutes?|hours?|days?|o'clock|in the|of the|of us|of them|percent)\b)",
    rf"\(\s*{_MOCK_N}\s*\)", rf"\b{_MOCK_N}\s*-?\s*(?:years?|yrs?)\s*-?\s*(?:old|of age)\b",
    rf"\b{_MOCK_N}\s*-?\s*(?:yo|y\.o\.?|y/o)\b",
    rf"\b{_MOCK_N}\s+(?:candles|ans|jahre|años)\b", rf"^\s*{_MOCK_N}\b",
    rf"[A-Z][a-z]+,\s*{_MOCK_N}\s*,",
    rf"\b{_MOCK_O}\s+(?:birthday|year)\b")]
# A minor without a number: a school stage or a word for a child's age.
_MOCK_STAGE = re.compile(
    rf"\b(?:grade|year)\s+{_MOCK_N}\b|\b{_MOCK_O}[ -]grade(?:rs?)?\b|\bgrade\s+(?:[1-9]|1[0-2])\b|"
    r"\b(?:tween|preteen|pre-teen|underage|under-age|a minor|kindergarten\w*|preschool\w*|middle school|"
    r"elementary school|primary school|junior high|(?:freshman|sophomore|junior|senior) in high school|"
    r"baby tooth|baby teeth)\b", re.I)
_MOCK_ADULT = re.compile(r"\b(?:adult|woman|man|wife|husband|mother|father|uncle|aunt|grand\w+|narrator|"
                         r"(?:1[89]|[2-9]\d)\b)", re.I)
_MOCK_NOT_NAMES = frozenset({
    "The", "A", "An", "That", "This", "Then", "When", "Later", "He", "She", "They", "It", "His", "Her", "Their",
    "Year", "Grade", "One", "Two", "In", "On", "At", "And", "But", "Next", "Last", "Every", "Cast", "Script",
    "Story", "Sound", "Music", "Title", "Premise", "Effect", "Bed", "Ambience", "Again", "Yes", "No", "Don't", "My",
    *(w.capitalize() for w in _MOCK_NUM)})


_MOCK_SCHOOL = re.compile(rf"\b(?:grade|year)\s+{_MOCK_N}\b", re.I)


def _mock_age(text: str, *, words: bool = False) -> "int | None | bool":
    """An age under 18 in `text` (a number), True for a minor without a
    number (a school grade or year; with `words`, any minor word), None
    when there's none."""
    from ..services.safety import mentions_minor

    for rx in _MOCK_AGES:
        m = rx.search(text)
        if m:
            g = m.group(1).lower()
            n = _MOCK_NUM.get(g) or _MOCK_ORD.get(g) or int(re.sub(r"\D", "", g))
            if n < 18:
                return n
    if _MOCK_SCHOOL.search(text) or _MOCK_STAGE.search(text) or (words and mentions_minor(text)):
        return True
    return None


# Mock facts: a sentence that gives a character's age as a thing of the
# past ("since she was twelve", "twenty years ago, at eleven") makes them a
# minor in the past only; a part that says time has moved on ("twenty years
# later") shows the characters it names as they are now.
_MOCK_PAST_REF = re.compile(r"\b(?:since|ago|back then|used to|remember\w*|as a (?:child|kid|girl|boy)|"
                            r"when (?:she|he|they|i|we|you) (?:was|were))\b", re.I)
_MOCK_TIME_JUMP = re.compile(r"\b(?:(?:\w+|\d+) years? later|decades? later|present day|nowadays|"
                             r"all grown up|as an adult)\b", re.I)


# Mock facts of the first-person narrator: "when I was nine" (a minor in the
# past), "I'm 34" / "I am thirty-four" (an adult now, 18 to 99).
_MOCK_FP_PAST = re.compile(r"\b(?:when|since|back when)\s+i\s+was\s+(?:only\s+|just\s+)?" + _MOCK_N + r"\b", re.I)
_MOCK_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
              "ninety": 90}
_MOCK_FP_ADULT = re.compile(r"\b(?:i\s*['’]?\s*m|i\s+am)\s+(?:now\s+)?(?:(1[89]|[2-9]\d)|(" + "|".join(_MOCK_TENS)
                            + r")(?:[- ](one|two|three|four|five|six|seven|eight|nine))?)\b(?!\s*(?:minutes?|"
                            r"hours?|percent|pages?|miles?|feet|steps?)\b)", re.I)


def _mock_narrator(script: str, names: list[str]) -> "dict | None":
    """The mock facts' first-person narrator: a present age under 18
    (minor_check.first_person_age, on the lines no cast member speaks), a
    past one ("when I was nine"), or an adult age ("I'm 34": an adult in
    this part)."""
    from ..services import minor_check

    young = [x for x in minor_check.first_person_minors([script], known=names) if x[2]]
    cast = [n for n in names if n.strip().lower() != "narrator"]
    own = "\n".join(re.sub(r"^\s*narrator\s*(?:\[[^\]\n]*\]\s*)?:\s*", "", line, flags=re.I)
                    for line in script.split("\n")
                    if not (cast and re.match(r"^\s*(" + "|".join(re.escape(n) for n in cast) + r")\s*:", line,
                                              re.I)))
    m = _MOCK_FP_NAME.search(own)
    name = m.group(1) if m else None
    past = _MOCK_FP_PAST.search(own)
    adult = _MOCK_FP_ADULT.search(own)
    if young:
        memoir = _mock_memoir(own, adult)
        if memoir is not None:
            return {"name": young[0][0] or name, "status": "adult", "age": memoir or None, "minor_in_past": True,
                    "in_this_part": "minor", "past_age": young[0][1],
                    "minor_when": minor_check._fp_when(own) or None}
        return {"name": young[0][0] or name, "status": "minor", "age": young[0][1], "minor_in_past": False,
                "in_this_part": "minor", "minor_when": None}
    if not past and not adult:
        return None
    age = None
    if adult:
        if adult.group(1):
            age = int(adult.group(1))
        else:
            age = _MOCK_TENS[adult.group(2).lower()] + (_MOCK_NUM.get((adult.group(3) or "").lower()) or 0)
    out = {"name": name, "status": "adult" if adult else "unknown", "age": age, "minor_in_past": bool(past),
           "in_this_part": "adult" if adult else None, "minor_when": None}
    if past:
        g = past.group(1).lower()
        out["past_age"] = _MOCK_NUM.get(g) or int(re.sub(r"\D", "", g))
        out["minor_when"] = " ".join(past.group(0).split())
    return out


# A memoir told in the present, as a model reads it: the child's age set in
# a year long past ("I'm nine years old in the summer of 1990"), or an
# adult age after a time jump later in the same part ("... Twenty-five
# years later I'm thirty-four").
_MOCK_MEMOIR_YEAR = re.compile(r"\b(?:in|of)\s+(?:the\s+(?:\w+\s+){0,2}of\s+)?(?:19\d\d|200\d|201[0-4])\b", re.I)


def _mock_memoir(own: str, adult: "re.Match | None") -> "int | bool | None":
    """For a narrator who says "I'm nine": their adult age now (or False:
    an adult of no stated age) when `own` reads as a memoir told in the
    present, else None. Test-only: the mock simulates a facts model."""
    from ..services import minor_check

    when = minor_check._fp_when(own)
    hits = minor_check._fp_minor_hits(minor_check._fp_unquoted(own))
    jump = _MOCK_TIME_JUMP.search(own, hits[0][0]) if hits else None
    if adult and jump:
        later = _MOCK_FP_ADULT.search(own, jump.start())
        if later:
            if later.group(1):
                return int(later.group(1))
            return _MOCK_TENS[later.group(2).lower()] + (_MOCK_NUM.get((later.group(3) or "").lower()) or 0)
    if when and _MOCK_MEMOIR_YEAR.search(when):
        return False
    return None


_MOCK_FP_NAME = re.compile(r"\b(?i:my\s+name\s+is)\s+([A-Z][a-z]{1,20})\b")


def mock_story_characters(text: str) -> dict:
    """The rules the mock story facts apply (deterministic): a cast member
    whose description gives an age under 18 or a minor word is a minor, an
    adult word makes an adult, anything else is unknown. In the script, a
    sentence that gives an age under 18 or a school grade or year about a
    name: a minor now, unless the cast says that character is an adult
    (then a minor in the past, and in this part unless the sentence speaks
    of the past). A character shown as a minor in the part before stays
    one in this part unless it says time has moved on."""
    cast, _, script = text.partition(STORY_SCRIPT_MARK)
    chars: dict[str, dict] = {}
    rank = {"adult": 0, "unknown": 1, "minor": 2}

    def put(name: str, status: str, age, **extra) -> None:  # noqa: ANN001
        key = name.strip().upper()
        if not key:
            return
        old = chars.get(key)
        if old is None:
            chars[key] = {"name": key, "status": status, "age": age, "minor_in_past": False,
                          "in_this_part": None}
        elif rank[status] > rank[old["status"]]:
            old.update(status=status, age=age)
        for k, v in extra.items():
            if k == "in_this_part" and chars[key]["in_this_part"] == "minor":
                continue
            chars[key][k] = v

    names: list[str] = []
    cast_status: dict[str, str] = {}
    earlier: set[str] = set()
    for line in cast.splitlines():
        if line.startswith(SO_FAR_MARK):
            # Minors from earlier windows stay minors.
            for part in line[len(SO_FAR_MARK):].rstrip(".").split(";"):
                m = re.match(r"^\s*(.+?) is a minor(?: \((\d+)\))?\s*$", part)
                if m:
                    put(m.group(1), "minor", int(m.group(2)) if m.group(2) else None)
            continue
        if line.startswith(EARLIER_MARK):
            body = line[len(EARLIER_MARK):].split(" shown as a minor")[0]
            earlier |= {re.sub(r"\s*\(\d+\)\s*$", "", x).strip().upper() for x in body.split(",") if x.strip()}
            continue
        if line.startswith((FACTS_RETRY_MARK, FACTS_NARRATOR_MARK)) or line.strip() == "Cast:":
            continue
        m = re.match(r"^\s*([^:\n]{1,40}?)\s*(?::\s*(.*))?$", line)
        if not m:
            continue
        name, desc = m.group(1), m.group(2) or ""
        names.append(name)
        age = _mock_age(desc, words=True)
        if age is not None:
            put(name, "minor", age if age is not True else None)
            cast_status[name.upper()] = "minor"
        else:
            st = "adult" if _MOCK_ADULT.search(desc) else "unknown"
            put(name, st, None)
            cast_status[name.upper()] = st
    jump = bool(_MOCK_TIME_JUMP.search(script))
    for sentence in re.split(r"(?<=[.!?])\s+|\n", script):
        # A line's speaker label is who says it, not who it is about.
        sentence = re.sub(r"^\s*[A-Z][A-Z .'-]{0,39}:\s*", "", sentence)
        age = _mock_age(sentence)
        if age is None:
            continue
        found = {w for w in re.findall(r"\b[A-Z][a-z]{1,20}\b", sentence) if w not in _MOCK_NOT_NAMES}
        found |= {n for n in names if re.search(rf"\b{re.escape(n)}\b", sentence, re.I)}
        n = age if age is not True else None
        for name in found:
            if cast_status.get(name.upper()) == "adult":
                past_only = bool(_MOCK_PAST_REF.search(sentence))
                put(name, "adult", None, minor_in_past=True, past_age=n, minor_when=" ".join(sentence.split())[:60],
                    **({} if past_only else {"in_this_part": "minor"}))
            else:
                put(name, "minor", n)
    for name in names:
        key = name.strip().upper()
        if key in chars and chars[key]["in_this_part"] is None and re.search(rf"\b{re.escape(name)}\b", script,
                                                                              re.I):
            if key in earlier and not jump:
                chars[key]["in_this_part"] = "minor"
            elif chars[key]["status"] == "adult":
                chars[key]["in_this_part"] = "adult"
    for key in earlier:
        if key in chars and chars[key]["in_this_part"] is None and not jump:
            chars[key]["in_this_part"] = "minor"
    out: dict = {"characters": list(chars.values())}
    narrator = _mock_narrator(script, names)
    if narrator is not None:
        out["narrator"] = narrator
    return out


_MOCK_RULE_RE = re.compile(r"Sexual content involving [^.\n]*? adult years\.")


def mock_minor_verdict(text: str) -> dict:
    """The rules the mock safety check applies (deterministic)."""
    from ..services.safety import is_sexual, mentions_minor, mentions_minor_wide, steers_young

    negative = ""
    # A text with no story around it: the user's block is read as before.
    text = text.removeprefix(NO_CONTEXT_HEAD)
    if text.startswith(AVOID_MARK):
        negative, _, text = text[len(AVOID_MARK):].partition("\n\n" + PROMPT_MARK)
    if text.startswith(CONTEXT_MARK):
        # A story part: the app's block and the user's text, read together,
        # with the wider hints of a minor the story path uses.
        context, _, user = text[len(CONTEXT_MARK):].partition("\n\n" + USER_MARK)
        # The app's rule sentence (whole-story and part facts) isn't the story.
        context = _MOCK_RULE_RE.sub(" ", context)
        text = f"{context}\n\n{user}"
        mentions_minor = mentions_minor_wide  # noqa: N806
    prompt = text.removeprefix(PROMPT_MARK)
    if mentions_minor(prompt) and is_sexual(prompt):
        return {"minor_sexual": True, "reason": "A minor in a sexual context."}
    if _YOUNG_CUES.search(prompt) and (is_sexual(prompt) or _UNDRESS.search(prompt)):
        return {"minor_sexual": True, "reason": "Childlike cues in a sexual context."}
    if steers_young(prompt, negative):
        return {"minor_sexual": True, "reason": "The avoid list removes signs of adulthood."}
    return {"minor_sexual": False, "reason": "No minor in a sexual context."}


# Every mock writer call (("outline", premise) or ("chapter", index)), for tests.
STORY_CALLS: list[tuple[str, object]] = []
_ORDINALS = ("One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve",
             "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen")
_MOCK_SENTENCES = (
    "The rain moved softly across the harbour, and the boats rocked on their ropes.",
    "Far out on the water, a single light turned slowly through the dark.",
    "The old stairs of the lighthouse creaked under careful steps.",
    "Warm tea steamed in a chipped blue cup beside the window.",
    "Wind pressed against the glass, then eased, as if it were tired too.",
    "Somewhere below, the sea breathed in and out against the rocks.",
    "A gull settled on the railing and tucked its head beneath a wing.",
    "The lamp hummed, steady and patient, the way it had for many years.",
    "Mist gathered over the bay and softened every edge of the night.",
    "The kettle ticked as it cooled, and the room grew still.",
    "Footprints in the wet sand slowly filled with water and vanished.",
    "A distant bell rang once from a buoy and then fell quiet.",
)
_MOCK_LINES = (
    "MARA [softly]: The light is steady tonight.",
    "MARA: Listen to that rain. It sounds like a lullaby.",
    "MARA [whispers]: Rest now. The harbour is safe.",
    "MARA [warmly]: One more turn of the lamp, and then tea.",
)
_MOCK_SFX = ("[sfx: gentle rain on glass | 4s | under]", "[sfx: wooden stairs creak | 2s]",
             "[sfx: distant buoy bell | 3s]", "[sfx: kettle clicks off | 2s]")
_SFX_EVERY = {"none": 0, "low": 14, "medium": 8, "high": 5}


def mock_story_chapter(*, index: int, minutes: float, style: str, sfx_density: str) -> str:
    """About `minutes` of mock script read at `style`'s speed."""
    from ..services import story_script

    wpm = story_script.WPM_BY_STYLE.get(style, story_script.DEFAULT_WPM)
    # Sound cues and pauses take time too: about a tenth of a chapter.
    target = max(30, int(minutes * wpm * 0.9))
    whisper = style in ("asmr", "whisper", "sleep", "meditation")
    every = _SFX_EVERY.get(sfx_density, 8)
    out: list[str] = []
    if every and sfx_density == "high":
        out.append('@bed ambience "soft rain over a quiet harbour"')
    words, n = 0, index * 7
    while words < target:
        if n % 6 == 5:
            line = _MOCK_LINES[n % len(_MOCK_LINES)]
        else:
            a = _MOCK_SENTENCES[n % len(_MOCK_SENTENCES)]
            b = _MOCK_SENTENCES[(n * 5 + 3) % len(_MOCK_SENTENCES)]
            line = f"{'[softly] ' if whisper and n % 3 == 0 else ''}{a} {b}"
        out.append(line)
        words += len(story_script.strip_tags(line.split(":", 1)[-1]).split())
        if every and n % every == every - 1:
            out.append(_MOCK_SFX[n % len(_MOCK_SFX)])
        if n % 10 == 9:
            out.append("(pause 2)")
        n += 1
    if every and sfx_density == "high":
        out.append("@bed stop")
    return "\n".join(out)


def mock_enhance_story(text: str, direction: str = "") -> str:
    """The mock Enhance of an audio story script: the markup its action
    asks for, every other line kept as it is."""
    from ..services import story_script

    action = (direction or "").split(":", 1)[0].strip().lower()
    lines = [ln.rstrip() for ln in (text or "").strip().splitlines()]
    out: list[str] = []
    for n, ln in enumerate(lines):
        plain = ln.strip()
        narration = bool(plain) and not plain.startswith(("#", "@", "//", "(", "[sfx")) and ":" not in plain[:40]
        if action == "tighten":
            out.append(re.sub(r"[ \t]{2,}", " ", ln).strip())
            continue
        if action == "asmr" and narration and not story_script.has_tags(plain):
            out.append(f"[whispers] {plain}")
            continue
        out.append(ln)
        if action == "sleep" and narration:
            out.append("(pause 2)")
        elif action == "sfx" and narration and n % 3 == 2:
            out.append("[sfx: soft wind outside | 3s]")
        elif action == "lengthen" and narration:
            out.append("The night stayed calm and quiet around them.")
    return "\n".join(out).strip()


class MockChatProvider(ChatProvider):
    """Deterministic storyboard generator used in MOCK_PROVIDERS mode and tests."""

    def classify_minor(self, text: str) -> dict:
        """Deterministic (see mock_minor_verdict), unless MINOR_REPLY is set."""
        MINOR_CALLS.append(text)
        if isinstance(MINOR_REPLY, BaseException):
            raise MINOR_REPLY
        if MINOR_REPLY is not None:
            return dict(MINOR_REPLY)
        return mock_minor_verdict(text)

    def story_characters(self, text: str) -> dict:
        """Deterministic (see mock_story_characters), unless STORY_FACTS_REPLY is set."""
        STORY_FACTS_CALLS.append(text)
        if isinstance(STORY_FACTS_REPLY, BaseException):
            raise STORY_FACTS_REPLY
        if STORY_FACTS_REPLY is not None:
            return dict(STORY_FACTS_REPLY)
        return mock_story_characters(text)

    def classify_explicit(self, text: str) -> dict:
        """Deterministic: explicit when a plainly sexual word is in the text
        (safety.is_hard_sexual), unless EXPLICIT_REPLY is set."""
        from ..services.safety import is_hard_sexual

        EXPLICIT_CALLS.append(text)
        if isinstance(EXPLICIT_REPLY, BaseException):
            raise EXPLICIT_REPLY
        if EXPLICIT_REPLY is not None:
            return dict(EXPLICIT_REPLY)
        if text.startswith("Avoid: "):
            # An avoid list: terms that only name what to keep out are fine;
            # a longer phrase asking for sexual content is not.
            terms = [t.strip() for t in text[len("Avoid: "):].split(",")]
            if any(len(t.split()) > 3 and is_hard_sexual(t) for t in terms):
                return {"explicit": True, "reason": "The avoid list asks for sexual content."}
            return {"explicit": False, "reason": "Only names what to keep out."}
        if is_hard_sexual(text):
            return {"explicit": True, "reason": "A sexual word."}
        return {"explicit": False, "reason": "Nothing sexual."}

    def inspect_images(self, *, system: str, text: str, images: list[str],
                       note: str = "clip check", estimate: float = 0.0) -> str:
        """Deterministic: every clip looks right (unless VISION_REPLY is set)."""
        VISION_CALLS.append((text, len(images)))
        if VISION_REPLY is not None:
            return VISION_REPLY
        return ('{"verdict": "ok", "issues": [], '
                '"summary": "The clip matches the shot description."}')

    # Deterministic hook ideas: a one-word lead-in per style, then the
    # opening's own words, as many words as the opening (so each "fits").
    HOOK_LEADS = {"question": "Why", "bold_claim": "Honestly,", "stat": "Fact:", "story": "So",
                  "contrarian": "Actually,"}

    def hook_ideas(self, *, script: str, hook: str, n: int, styles: list[str]) -> str:
        import json

        words = re.sub(r"[.!?…]+$", "", hook.strip()).split() or ["this"]
        hooks = []
        for style in styles[:n]:
            core = [words[0].lower(), *words[1:]][: max(len(words) - 1, 1)]
            text = " ".join([self.HOOK_LEADS.get(style, "Listen:"), *core])
            text += "?" if style == "question" else "."
            hooks.append({"style": style, "text": text})
        return json.dumps({"hooks": hooks})

    def rewrite_scene(self, text: str) -> dict:
        scene = re.sub(r"\s{2,}", " ", _HYPE.sub("", text)).strip(" ,.") or text.strip()
        return {"scene": f"{scene[0].upper()}{scene[1:]}.", "light": "Soft, ordinary daylight.",
                "camera": "Shot on a full-frame camera with a 35 mm lens at f/8 from a tripod."}

    # Clearly-mock additions so a demo shows the round trip without a model.
    ENHANCE_SUFFIX = {
        "image": " \u2014 soft natural light, 35mm, shallow depth of field",
        "scene": " \u2014 soft natural light, 35mm, shallow depth of field",
        "video": " \u2014 one continuous shot, the camera pushes in gently, real-time pacing",
    }

    def enhance_prompt(self, text: str, kind: str, model: str = "", direction: str = "") -> str:
        """Deterministic: the text plus a short mock suffix (a script is only
        tidied, never padded with invented lines; an audio story script gets
        the markup its Enhance action asks for, mock_enhance_story)."""
        if kind == "audio_script":
            return mock_enhance_story(text, direction)
        if kind not in ("image", "video", "script", "scene"):
            raise ValueError(f"unknown prompt kind {kind!r}")
        base = text.strip()
        if kind == "script":
            return "\n".join(re.sub(r"[ \t]{2,}", " ", ln).strip() for ln in base.splitlines()).strip()
        suffix = self.ENHANCE_SUFFIX[kind]
        return base if base.endswith(suffix) else (base.rstrip(" ,.") + suffix)

    # ---------- Sound Studio: the story writer ----------

    def story_outline(self, *, premise: str, title: str, minutes: int, style: str, sfx_density: str,
                      chapters: int) -> str:
        """Deterministic: a narrator and one adult character, `chapters`
        chapters (JSON, as the real writer replies)."""
        import json

        STORY_CALLS.append(("outline", premise))
        name = (title or "").strip() or "The Quiet Harbour"
        return json.dumps({
            "title": name,
            "cast": [{"name": "NARRATOR", "pan": "center", "description": "calm adult narrator, close to the mic"},
                     {"name": "MARA", "pan": "left", "description": "adult lighthouse keeper, low warm voice"}],
            "chapters": [{"title": f"Part {_ORDINALS[i % len(_ORDINALS)]}",
                          "summary": "Mara keeps the light through a long, calm night."} for i in range(chapters)],
        })

    def story_chapter(self, *, premise: str, outline: dict, index: int, minutes: float, style: str,
                      sfx_density: str, previous: str = "") -> str:
        """Deterministic: about `minutes` of script at the style's reading
        speed, with speaker lines, tags, pauses and sound cues as dense as
        asked (story_script markup, no heading)."""
        STORY_CALLS.append(("chapter", index))
        return mock_story_chapter(index=index, minutes=minutes, style=style, sfx_density=sfx_density)

    def generate_storyboard(self, *, project: dict, cast: CastContext) -> StoryboardPlan:
        script = (project.get("original_script") or "").strip() or "Sample script."
        title = (project.get("title") or "Untitled").strip() or "Untitled"
        cta = (project.get("cta_text") or "").strip()
        target_seconds = float(project.get("target_duration_seconds") or 25)
        mode = project.get("mode") or "reel_montage"
        include_disclosure = bool(project.get("include_disclosure", True))
        disclosure_override = (project.get("disclosure_text") or "").strip()
        direction = (project.get("creative_direction") or "").strip()

        # Build shot plan based on mode.
        avatars = cast.avatars or [{"name": "Avatar", "role": "host"}]
        ingredients = cast.ingredients or []
        ing_scene = next((i for i in ingredients if i.get("kind") == "scene"), None)
        ing_style = next((i for i in ingredients if i.get("kind") == "style"), None)

        def _scene_str() -> str:
            return f" in {ing_scene['name']}" if ing_scene else ""

        def _style_str() -> str:
            return f", {ing_style['name']} style" if ing_style else ""

        def _direction_str() -> str:
            return f" [{direction}]" if direction else ""

        body_duration = max(target_seconds - (3.0 if cta else 0.0), 6.0)
        shots: list[ShotPlan] = []

        if mode == "talking_head_beta":
            shots.append(
                ShotPlan(
                    order=1,
                    shot_type="talking_head",
                    duration_seconds=body_duration,
                    visual_prompt=(
                        f"{avatars[0]['name']} (AI-generated virtual creator) speaking to camera"
                        f"{_scene_str()}{_style_str()}{_direction_str()}. "
                        f"Direct eye contact, natural expression."
                    ),
                    negative_prompt="blurry, distorted, low quality",
                    reference_strategy="frame_images",
                    recommended_asset_types=["hero"],
                    recommended_cast_roles=[avatars[0].get("role", "host")],
                    caption_text=script,
                    camera_direction="medium close-up, slight handheld motion",
                )
            )
        elif mode == "static_motion":
            shots.append(
                ShotPlan(
                    order=1,
                    shot_type="hero",
                    duration_seconds=body_duration,
                    visual_prompt=(
                        f"{avatars[0]['name']} (AI-generated virtual creator) hero portrait"
                        f"{_scene_str()}{_style_str()}{_direction_str()}, subtle parallax zoom-in."
                    ),
                    negative_prompt="warping, jittery motion",
                    reference_strategy="static_motion",
                    recommended_asset_types=["hero"],
                    recommended_cast_roles=[avatars[0].get("role", "host")],
                    caption_text=script,
                    camera_direction="slow push-in",
                )
            )
        else:  # reel_montage
            n = min(max(2, len(avatars) + 1), 4)
            per = body_duration / n
            roles = [a.get("role", "host") for a in avatars]
            for i in range(n):
                a = avatars[i % len(avatars)]
                shot_type = "hero" if i == 0 else "b_roll"
                shots.append(
                    ShotPlan(
                        order=i + 1,
                        shot_type=shot_type,
                        duration_seconds=round(per, 2),
                        visual_prompt=(
                            f"{a['name']} (AI-generated virtual creator)"
                            f"{_scene_str()}{_style_str()}{_direction_str()}, "
                            f"{'wide establishing shot' if i == 0 else 'mid b-roll moment'}, "
                            f"shot {i + 1} of {n}."
                        ),
                        negative_prompt="logos, watermark, text overlay",
                        reference_strategy="input_references",
                        recommended_asset_types=["hero", "lifestyle", "scene", "style"],
                        recommended_cast_roles=[roles[i % len(roles)]],
                        caption_text="",
                        camera_direction="varied",
                    )
                )

        if cta:
            shots.append(
                ShotPlan(
                    order=len(shots) + 1,
                    shot_type="end_card",
                    duration_seconds=2.5,
                    visual_prompt=f"Clean branded end card with text: '{cta}'.",
                    negative_prompt="",
                    reference_strategy="static_motion",
                    recommended_asset_types=["logo"],
                    recommended_cast_roles=[],
                    caption_text=cta,
                    camera_direction="hold",
                )
            )

        # Caption chunks across the body script.
        chunk_strs = _chunk_caption(script)
        per_chunk = (body_duration / len(chunk_strs)) if chunk_strs else 0.0
        caption_chunks = [
            CaptionChunk(start_hint=round(i * per_chunk, 2), text=t)
            for i, t in enumerate(chunk_strs)
        ]

        return StoryboardPlan(
            title=title,
            cleaned_voice_script=script,
            estimated_duration_seconds=sum(s.duration_seconds for s in shots),
            content_warning_notes="",
            disclosure_text=(
                (disclosure_override or "AI-generated virtual creator")
                if include_disclosure
                else ""
            ),
            end_card_text=cta,
            caption_chunks=caption_chunks,
            shots=shots,
        )

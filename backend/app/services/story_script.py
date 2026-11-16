"""The script markup of an audio story (Sound Studio).

A script is plain text, one cue per line:

    // a comment (never voiced)
    @cast NARRATOR [center]: warm adult voice, close to the mic
    @cast MARA [left]: adult woman in her thirties, low whisper

    # Chapter 1: The cabin
    @bed ambience "rain on a cabin roof"
    The rain had not stopped for three days.
    [whispers][slowly] She listened at the door.
    MARA [whispers][left]: Did you hear that?
    [sfx: old door creaks | 3s]
    [sfx: distant thunder | 4s | under]
    [sfx: wind gust | 2s | +1.5s]
    (pause 2)
    @bed stop

- `# Title` starts a chapter; lines before the first one that are heard
  make an untitled chapter (notes and beds alone join the first chapter).
- `@cast NAME [pan]: description` declares a character (header lines,
  usually at the top). NARRATOR reads every line without a speaker.
- `NAME [tags][pan]: text` is a speaker line (NAME in capitals in any
  script, or a cast member's name in any case); any other line is
  narration, optionally starting with tags. A line that is only a tag
  ("[sighs]", "MARA [laughs]:") is a sound of that voice.
- Tags are voice directions from VOICE_TAGS ([whispers], [sighs],
  [short pause]...), at the start of a line or inside it; [left], [right]
  and [center] place the voice. Unknown tags are removed with a warning; a
  clean deployment allows only the general-audience ones.
- `[sfx: prompt | Ns | placement]`: a sound effect, N seconds (optional),
  with no '[' or ']' in it (a cue that has one is skipped with a warning),
  placed `after` the line before it (the default: the story waits for it),
  `under` it (it plays while the line is read) or `+Ns` (N seconds after
  the line before it starts). `[sfx: #12]` uses library sound 12.
- `(pause N)` waits N seconds.
- `@bed ambience "prompt"` / `@bed music "prompt"` start a background bed
  (a new one replaces the one playing); `@bed stop` stops every bed,
  `@bed ambience stop` one layer.

parse(text) returns a StoryDoc; serialize(doc) writes it back in the
canonical form (parse(serialize(doc)) is the same doc). Each item has a
key that depends only on what it says (not on its position), so editing
one line keeps every other line's generated audio.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Optional

NARRATOR = "NARRATOR"
PLACEMENTS = ("left", "right", "center")
SFX_PLACEMENTS = ("after", "under")
BED_LAYERS = ("ambience", "music")

# Voice directions a story may carry. ElevenLabs' tag-reading models read
# them in place; other providers get them as spoken instructions
# (tags_to_instructions) and the text without them.
GENERAL_TAGS: frozenset[str] = frozenset({
    # whisper and ASMR
    "whispers", "whispering", "softly", "quietly", "gently", "breathy", "murmurs", "close to the mic",
    # breath and sounds of the voice
    "sighs", "sighing", "exhales", "inhales deeply", "deep breath", "slow breath", "yawns", "hums",
    "clears throat", "sniffles", "gasps", "gulps",
    # laughter
    "laughs", "laughs softly", "chuckles", "giggles",
    # timing
    "short pause", "long pause", "slowly", "quickly", "hesitates",
    # mood and manner
    "calmly", "warmly", "sleepy", "drowsy", "tired", "dreamy", "happy", "excited", "sad", "crying",
    "nervous", "scared", "fearful", "curious", "surprised", "angry", "firmly", "sarcastic",
    "mischievously", "playfully", "tenderly", "serious", "thoughtful", "mysterious", "ominous",
    "shouts", "louder", "teasing",
})
# Romance and intimacy between adults: default mode only (never on a clean
# deployment, where only GENERAL_TAGS are kept).
ADULT_TAGS: frozenset[str] = frozenset({
    "seductively", "sultry", "flirtatious", "moans", "breathless", "panting", "lustful",
})
VOICE_TAGS: frozenset[str] = GENERAL_TAGS | ADULT_TAGS

# The voice settings key story code sets (settings[STORY_MARKUP] = True) so
# the voice adapters and their guard read the text as story markup: tags
# taken out or turned into directions, [short pause] as a pause. Without it
# a caller's text is sent, and checked, exactly as before Sound Studio.
STORY_MARKUP = "story_markup"


def is_story(settings: Optional[dict]) -> bool:
    """Whether a voice request carries story markup (STORY_MARKUP)."""
    return bool((settings or {}).get(STORY_MARKUP))

# Seconds the voice waits on a timing tag (for the estimate).
TAG_SECONDS = {"short pause": 0.5, "long pause": 1.5, "hesitates": 0.4, "deep breath": 0.8,
               "slow breath": 1.2, "sighs": 0.6, "yawns": 1.0}

# Words per minute by style (how fast a story of that style is read).
WPM_BY_STYLE = {"asmr": 100, "whisper": 100, "sleep": 110, "meditation": 100, "cozy": 140,
                "spooky": 130, "romance": 130, "drama": 150, "adventure": 155, "podcast": 160}
DEFAULT_WPM = 145
DEFAULT_SFX_SECONDS = 3.0
SFX_MIN, SFX_MAX = 0.5, 30.0
PAUSE_MIN, PAUSE_MAX = 0.1, 30.0
CHAPTER_GAP = 2.0

_CHAPTER_RE = re.compile(r"^#{1,3}(?:\s+(.*))?$")
_COMMENT_RE = re.compile(r"^//\s?(.*)$")
_CAST_RE = re.compile(r"^@cast\s+([^\[:]+?)\s*((?:\[[^\]]*\]\s*)*):\s*(.*)$", re.I)
_BED_RE = re.compile(r"^@bed\b\s*(.*)$", re.I)
# A speaker name: starts with a letter (any script), then letters, digits,
# spaces, dots, apostrophes or hyphens. Whether a candidate really is a
# speaker is decided by is_speaker_name (the cast, or capitals).
_NAME = r"[^\W\d_][\w .'’\-]{0,39}?"
# The colon of a clock time ("AT 3:00 the phone rang") is not a speaker's.
_SPEAKER_RE = re.compile(rf"^({_NAME})\s*((?:\[[^\]]*\]\s*)*):(?!(?<=\d:)\d)\s*(.*)$")
# Note words written like a speaker ("PS: she never wrote back."): narration.
NOT_SPEAKERS = frozenset({"PS", "P.S", "P.S.", "PPS", "P.P.S", "P.P.S.", "NB", "N.B", "N.B.", "FYI", "NOTE",
                          "TIP", "WARNING", "UPDATE", "EDIT", "AM", "PM", "A.M.", "P.M."})
# What a line read aloud must not start with to stay narration.
_NOT_NARRATION_RE = re.compile(r"^(?://|#|@|\(\s*pause\b|\[\s*sfx\s*:)", re.I)
_PAUSE_RE = re.compile(r"\(\s*pause(?:\s+(\d+(?:\.\d+)?)\s*s?)?\s*\)", re.I)
_SFX_RE = re.compile(r"\[\s*sfx\s*:\s*([^\]]*)\]", re.I)
_TAG_RE = re.compile(r"\[([^\]]*)\]")
_LEADING_TAGS_RE = re.compile(r"^((?:\s*\[[^\]]*\])+)\s*")
_SECONDS_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*s?$", re.I)
_OFFSET_RE = re.compile(r"^\+\s*(\d+(?:\.\d+)?)\s*s?$", re.I)
_ASSET_RE = re.compile(r"^#(\d+)$")
_CUE_OPEN_RE = re.compile(r"\[\s*sfx\s*:", re.I)


def _cue_end(s: str, start: int) -> tuple[int, bool]:
    """(where the sound cue whose body starts at `start` ends, whether it
    holds a bracket). A '[' inside reaches to its matching ']' and then the
    cue's own; a ']' later in the line that closes nothing was the cue's."""
    depth, inner, end = 0, False, len(s)
    for i in range(start, len(s)):
        c = s[i]
        if c == "[":
            depth, inner = depth + 1, True
        elif c == "]":
            if depth == 0:
                end = i + 1
                break
            depth -= 1
    depth = 0
    for j in range(end, len(s)):
        c = s[j]
        if c == "[":
            depth += 1
        elif c == "]":
            if depth == 0:  # a stray ']': the cue reached this far
                end, inner = j + 1, True
            else:
                depth -= 1
    return end, inner


_SPACE_PUNCT_RE = re.compile(r"\s+([.,!?;:])")


def _tidy(text: str) -> str:
    """Whitespace collapsed, and no space left before punctuation where a
    tag was taken out ("Hi [sighs]." → "Hi.")."""
    return _SPACE_PUNCT_RE.sub(r"\1", " ".join((text or "").split()))


def norm_tag(tag: str) -> str:
    return " ".join((tag or "").lower().replace("_", " ").split())


def is_speaker_name(name: str, cast: Optional[dict] = None) -> bool:
    """Whether `name` (before a colon) is a speaker: a cast member's name in
    any case ("Mara" for @cast MARA), the narrator, or a name written in
    capitals in any script ("ŁUKASZ"; not "PS" or "11 PM")."""
    name = " ".join((name or "").split())
    if not name:
        return False
    up = name.upper()
    if up == NARRATOR or (cast and up in cast):
        return True
    if up in NOT_SPEAKERS or not re.match(r"[^\W\d_]", name):
        return False
    letters = [c for c in name if c.isalpha()]
    return bool(letters) and name == up and any(c.lower() != c for c in letters)


def _looks_like_name(name: str) -> bool:
    """One or two capitalised words ("Mara", "Old Tom"): a speaker the
    writer probably meant."""
    words = (name or "").split()
    return (0 < len(words) <= 2 and " ".join(words).upper() not in NOT_SPEAKERS
            and all(w[0].isupper() and not any(c.isdigit() for c in w) for w in words))


def allowed_tags(clean: Optional[bool] = None) -> frozenset[str]:
    """The voice tags this deployment allows (a clean one: GENERAL_TAGS)."""
    return GENERAL_TAGS if _clean(clean) else VOICE_TAGS


def _clean(clean: Optional[bool]) -> bool:
    if clean is not None:
        return bool(clean)
    from . import content_mode

    return not content_mode.features()


# ---------- the document ----------


@dataclass
class ScriptWarning:
    line: int
    message: str


@dataclass
class CastMember:
    name: str
    pan: Optional[str] = None
    description: str = ""
    declared: bool = True  # False: first seen as a speaker, never declared


@dataclass
class Item:
    """One cue. kind: speech | sfx | pause | bed | comment."""

    kind: str
    line: int = 0
    chapter: int = 0
    key: str = ""
    # speech
    speaker: str = ""
    tags: list[str] = field(default_factory=list)
    pan: Optional[str] = None
    text: str = ""
    # sfx
    prompt: str = ""
    seconds: Optional[float] = None
    placement: str = "after"  # after | under | offset
    offset: Optional[float] = None
    asset_id: Optional[int] = None
    # bed: layer ambience|music; action start|stop; layer "" = every layer
    layer: str = ""
    action: str = ""
    # speech: delivery instructions sent with the line (a retake's
    # direction); never in the markup. check_story judges them with the
    # line, and its pass covers the line only with these instructions.
    instructions: str = ""

    @property
    def voice_text(self) -> str:
        """What a tag-reading voice model is sent: the leading tags, then
        the text (inline tags kept)."""
        lead = "".join(f"[{t}] " for t in self.tags)
        return f"{lead}{self.text}".strip()

    @property
    def spoken_text(self) -> str:
        """The words alone (every tag removed): for counts. The safety
        checks read voiced_text, which keeps the tags."""
        return strip_tags(self.text)


@dataclass
class Chapter:
    index: int
    title: str
    line: int = 0
    first_item: int = 0
    item_count: int = 0
    est_seconds: float = 0.0


@dataclass
class Bed:
    layer: str
    prompt: str
    chapter: int
    start_item: int
    stop_item: Optional[int] = None  # None: plays to the end
    line: int = 0


@dataclass
class StoryDoc:
    chapters: list[Chapter] = field(default_factory=list)
    cast: dict[str, CastMember] = field(default_factory=dict)
    beds: list[Bed] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)
    warnings: list[ScriptWarning] = field(default_factory=list)
    est_seconds: float = 0.0
    style: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["cast"] = [asdict(c) for c in self.cast.values()]
        return d

    def speech(self) -> list[Item]:
        return [i for i in self.items if i.kind == "speech"]

    def sfx(self) -> list[Item]:
        return [i for i in self.items if i.kind == "sfx"]

    @property
    def voiced_chars(self) -> int:
        """Characters a voice provider bills (tags included, as sent)."""
        return sum(len(i.voice_text) for i in self.speech())

    @property
    def word_count(self) -> int:
        return sum(len(i.spoken_text.split()) for i in self.speech())


# ---------- tags ----------


def strip_tags(text: str, *, keep_unknown: bool = False) -> str:
    """The text without voice tags and placements. keep_unknown: leave
    bracketed text that isn't a tag (e.g. "[Verse 1]") alone."""
    def repl(m: re.Match) -> str:
        names = [norm_tag(t) for t in m.group(1).split(",")]
        if keep_unknown and not all(n in VOICE_TAGS or n in PLACEMENTS for n in names):
            return m.group(0)
        return " "

    return _tidy(_TAG_RE.sub(repl, text or ""))


def _is_markup(group: str) -> bool:
    """A bracket's inside is voice tags or placements only."""
    names = [norm_tag(t) for t in group.split(",")]
    return all(n in VOICE_TAGS or n in PLACEMENTS for n in names)


def has_tags(text: str) -> bool:
    """`text` carries a voice tag or a placement in brackets (other
    bracketed text, "[Verse 1]", doesn't count)."""
    return any(_is_markup(g) for g in _TAG_RE.findall(text or ""))


def split_tags(text: str, *, allowed: Optional[frozenset[str]] = None) -> tuple[list[str], str]:
    """(voice tags found anywhere in `text`, the text without them).
    Bracketed text that isn't a tag is left in place. `allowed`: the tags
    reported (default: every voice tag); the others are removed unreported,
    so a clean deployment never turns an adult tag into a direction."""
    found: list[str] = []
    allowed = VOICE_TAGS if allowed is None else allowed

    def repl(m: re.Match) -> str:
        names = [norm_tag(t) for t in m.group(1).split(",")]
        if not all(n in VOICE_TAGS or n in PLACEMENTS for n in names):
            return m.group(0)
        found.extend(n for n in names if n in allowed)
        if any(n in ("short pause", "long pause") for n in names):
            return " … "
        return " "

    return found, _tidy(_TAG_RE.sub(repl, text or ""))


def drop_tags(text: str, tags: frozenset[str]) -> str:
    """`text` without the voice tags in `tags` (each bracket that is only
    tags loses them; a bracket left empty goes). Unchanged, character for
    character, when none is there."""
    changed = False

    def repl(m: re.Match) -> str:
        nonlocal changed
        if not _is_markup(m.group(1)):
            return m.group(0)
        parts = [t for t in m.group(1).split(",") if norm_tag(t) not in tags]
        if len(parts) == len(m.group(1).split(",")):
            return m.group(0)
        changed = True
        kept = [p.strip() for p in parts if p.strip()]
        return f"[{', '.join(kept)}]" if kept else " "

    out = _TAG_RE.sub(repl, text or "")
    return _tidy(out) if changed else text


def drop_directions(text: str, keep: frozenset[str]) -> str:
    """`text` with only the directions in `keep` (and placements) left in
    brackets: any other bracketed word, a known tag or not ("[moaning]",
    "[softly moans]"), is a direction a tag-reading model would perform,
    and goes. Unchanged, character for character, when none is there."""
    changed = False

    def repl(m: re.Match) -> str:
        nonlocal changed
        raw = m.group(1).split(",")
        parts = [t for t in raw if norm_tag(t) in keep or norm_tag(t) in PLACEMENTS]
        if len(parts) == len(raw):
            return m.group(0)
        changed = True
        kept = [p.strip() for p in parts if p.strip()]
        return f"[{', '.join(kept)}]" if kept else " "

    out = _TAG_RE.sub(repl, text or "")
    return _tidy(out) if changed else text


def disallowed_tags(clean: Optional[bool] = None) -> frozenset[str]:
    """The voice tags this deployment never voices (a clean one: ADULT_TAGS)."""
    return VOICE_TAGS - allowed_tags(clean)


# For the minors check, an adult tag is a sexual direction: the word filter
# and the model read it as one ("[lustful]" next to "little girl" is refused
# by the words alone). Romance and intimacy tags are for adults only, and so
# is any direction written like one ("[moaning]", "[softly moans]",
# "[pants.]"): tag-reading models accept any words in brackets. "sultry"
# and "lust" have their forms listed, not a bare stem: "sultan",
# "lustrous" and "lustre" are not directions. (The other stems start no
# everyday word but "pants", which is only ever marked for the model.)
_ADULT_TAG_WORD = "seductive"
_ADULT_STEM_RE = re.compile(
    r"\b(?:moan\w*|pant(?:s|ed|ing)?|breathless\w*|seduct\w*|sultr(?:y|ier|iest|ily|iness)|"
    r"lust(?:s|ed|ing|ful|fully|fulness|y|ier|iest|ily|iness)?|orgasm\w*|"
    + "|".join(re.escape(t) for t in sorted(ADULT_TAGS)) + r")\b", re.I)
# Any other direction a model would perform (not a known general tag), in
# a text that mentions a minor or with a minor in the cast: asked about
# (an everyday word the filter turns the model on for, never refused by the
# words alone).
_DIRECTION_WORD = "sensual"
# General tags that read as intimate: with a minor in the text or the cast
# they are asked about too (a line made only of these and innuendo is
# otherwise never judged).
INTIMATE_TAGS: frozenset[str] = frozenset({
    "breathy", "gasps", "close to the mic", "teasing", "tenderly", "playfully", "mischievously",
    "murmurs", "sighs", "sighing", "exhales", "inhales deeply", "deep breath", "slow breath",
})
# Brackets NFKC leaves alone that a tag-reading model would still read as one.
_BRACKETS = str.maketrans({"【": "[", "】": "]", "〔": "[", "〕": "]", "〖": "[", "〗": "]", "⟦": "[", "⟧": "]",
                           "⁅": "[", "⁆": "]", "〚": "[", "〛": "]"})
# A bracket group, a (...) or *...* action group ("(groaning softly)",
# "*groans*"), which a reader or a model performs like a bracket direction,
# or (outside one) a word of an adult direction.
_MARK_RE = re.compile(r"\[([^\]]*)\]|\(([^()\n]*)\)|\*([^*\n]+)\*|" + _ADULT_STEM_RE.pattern, re.I)
# What safety_text adds after a marked direction.
_MARKER_RE = re.compile(rf"\((?:{_ADULT_TAG_WORD}|{_DIRECTION_WORD})\)", re.I)
_DASHES = None


def normalize(text: str, *, dashes: bool = True) -> str:
    """Story text as the tag handling and the checks read it: NFKC (a
    fullwidth "［moans］" is "[moans]"), bracket look-alikes as [ ] and,
    `dashes`, dash look-alikes ("12‑year‑old", "12–year–old") as "-"
    (safety.DASHES). The text sent to a voice keeps its dashes (dashes=False):
    an em dash is a pause there; the checks read a folded copy."""
    import unicodedata

    global _DASHES
    out = unicodedata.normalize("NFKC", str(text or "")).translate(_BRACKETS)
    if dashes:
        if _DASHES is None:
            from .safety import DASHES

            _DASHES = str.maketrans(dict.fromkeys(DASHES, "-"))
        out = out.translate(_DASHES)
    return out


def is_marked(text: str) -> bool:
    """`text` has a direction safety_text marked (an adult one, or one near
    a minor): minor_check asks the model about a story window that has one."""
    return bool(_MARKER_RE.search(text or ""))


def _adult_direction(name: str) -> bool:
    return name in ADULT_TAGS or bool(_ADULT_STEM_RE.search(name))


def _mentions_minor(*texts: str) -> bool:
    from .safety import mentions_minor_wide

    return any(mentions_minor_wide(t or "") for t in texts)


def safety_text(text: str, context: str = "", *, prose: bool = False) -> str:
    """`text` as the minors check reads it: every tag kept, an adult
    direction ([moans], [lustful], [moaning softly]...) followed by a word
    the filter knows as sexual, and, when `text` or `context` (the cast)
    mentions a minor, any other unknown direction, or an intimate general
    one ([breathy], [gasps]...), followed by a word that makes the model
    judge it. Read NFKC-normalised (normalize). `prose` (story lines and story sound prompts): an adult word
    outside brackets ("Lily moans and pants.") is followed by that word too,
    so the model judges it (never refused by the words alone: "pants" may
    be trousers). A (...) or *...* action group ("(groaning with pleasure)",
    "*groans*") is a direction too, marked as a bracket one is. Unchanged
    when it has no such direction."""
    text = normalize(text)
    minor: Optional[bool] = None

    def near_minor() -> bool:
        nonlocal minor
        if minor is None:
            minor = _mentions_minor(text, context)
        return minor

    def mark(m: re.Match, word: str) -> str:
        # Already followed by that marker (a text read twice): left as it is.
        if m.string[m.end():].lstrip().lower().startswith(f"({word})"):
            return m.group(0)
        return f"{m.group(0)} ({word})"

    def repl(m: re.Match) -> str:
        group = next((g for g in m.groups() if g is not None), None)
        if group is None:
            return mark(m, _DIRECTION_WORD) if prose else m.group(0)
        if m.group(1) is None:
            # A (...) or *...* action group: a pause, or a marker this
            # function added, is not a direction.
            if _MARKER_RE.fullmatch(f"({group.strip()})") or _PAUSE_RE.fullmatch(m.group(0)):
                return m.group(0)
        names = {norm_tag(t) for t in group.split(",")} - {""}
        if any(_adult_direction(n) for n in names):
            return mark(m, _ADULT_TAG_WORD)
        if any((n not in GENERAL_TAGS and n not in PLACEMENTS) or n in INTIMATE_TAGS for n in names) \
                and near_minor():
            return mark(m, _DIRECTION_WORD)
        return m.group(0)

    return _MARK_RE.sub(repl, text)


def safety_instructions(instructions: str, text: str = "", context: str = "") -> str:
    """A story voice's delivery `instructions` as the minors check reads
    them: a direction like a tag's, so an adult word in them ("moaning and
    panting") is marked as an adult tag is, and with a minor in the line,
    the instructions or the cast, any free-form instructions are marked so
    the model judges them."""
    said = normalize(instructions)
    if not said.strip():
        return said
    if _ADULT_STEM_RE.search(said):
        return f"{safety_text(said, context)} ({_ADULT_TAG_WORD})"
    marked = safety_text(said, context)
    if _mentions_minor(text, said, context):
        return f"{marked} ({_DIRECTION_WORD})"
    return marked


# What a voice without tags is sent for a line that is only a tag
# ("[sighs]"): a breath of sound, with the tag as the direction.
NONVERBAL_TEXT = "…"


# How tags read as spoken instructions (providers without audio tags).
_INSTRUCTION = {
    "whispers": "whisper", "whispering": "whisper", "softly": "speak softly", "quietly": "speak quietly",
    "breathy": "breathy voice", "close to the mic": "intimate, close to the microphone",
    "murmurs": "murmur", "slowly": "speak slowly", "quickly": "speak quickly", "calmly": "calm",
    "warmly": "warm", "gently": "gentle", "sleepy": "sleepy", "drowsy": "drowsy", "shouts": "shout",
    "louder": "louder", "laughs": "laugh", "laughs softly": "a soft laugh", "chuckles": "a chuckle",
    "giggles": "a giggle", "sighs": "a sigh", "sighing": "sighing", "yawns": "a yawn", "gasps": "a gasp",
    "short pause": "brief pauses", "long pause": "long pauses", "hesitates": "hesitant",
}


def tags_to_instructions(tags: list[str]) -> str:
    """One sentence of delivery directions for providers that take spoken
    instructions instead of tags ("" when there are none)."""
    seen: list[str] = []
    for t in tags:
        phrase = _INSTRUCTION.get(norm_tag(t), norm_tag(t))
        if phrase and phrase not in seen:
            seen.append(phrase)
    if not seen:
        return ""
    return "Read the text aloud exactly as written. Delivery: " + "; ".join(seen) + "."


# ---------- parsing ----------


class _Parser:
    def __init__(self, clean: bool, style: str, max_minutes: Optional[float]) -> None:
        self.allowed = GENERAL_TAGS if clean else VOICE_TAGS
        self.clean = clean
        self.doc = StoryDoc(style=style or "")
        self.wpm = WPM_BY_STYLE.get((style or "").lower(), DEFAULT_WPM)
        self.max_minutes = max_minutes
        self.open_beds: dict[str, int] = {}  # layer -> index into doc.beds

    def warn(self, line: int, message: str) -> None:
        self.doc.warnings.append(ScriptWarning(line, message))

    # tags in brackets -> (voice tags, pan)
    def tags(self, raw: str, line: int) -> tuple[list[str], Optional[str]]:
        tags: list[str] = []
        pan: Optional[str] = None
        for group in _TAG_RE.findall(raw or ""):
            for t in group.split(","):
                n = norm_tag(t)
                if not n:
                    continue
                if n in PLACEMENTS:
                    pan = n
                elif n in self.allowed:
                    if n not in tags:
                        tags.append(n)
                elif n in VOICE_TAGS:
                    self.warn(line, f"[{n}] isn't available on this server (general audience only); removed.")
                else:
                    self.warn(line, f"Unknown voice tag [{n}] removed.")
        return tags, pan

    def inline(self, text: str, line: int) -> tuple[str, Optional[str]]:
        """Inline tags checked: allowed ones kept (normalised), placements
        taken out (returned), the rest removed with a warning."""
        pan: Optional[str] = None

        def repl(m: re.Match) -> str:
            nonlocal pan
            kept: list[str] = []
            for t in m.group(1).split(","):
                n = norm_tag(t)
                if not n:
                    continue
                if n in PLACEMENTS:
                    pan = n
                elif n in self.allowed:
                    kept.append(n)
                elif n in VOICE_TAGS:
                    self.warn(line, f"[{n}] isn't available on this server (general audience only); removed.")
                else:
                    self.warn(line, f"Unknown voice tag [{n}] removed.")
            return " " + "".join(f"[{k}]" for k in kept) + " " if kept else " "

        return _tidy(_TAG_RE.sub(repl, text)), pan

    def chapter_index(self) -> int:
        if not self.doc.chapters:
            self.doc.chapters.append(Chapter(index=0, title="", line=0))
        return len(self.doc.chapters) - 1

    def add(self, item: Item) -> None:
        item.chapter = self.chapter_index()
        self.doc.items.append(item)

    def speaker(self, name: str, line: int) -> str:
        name = " ".join(name.split()).upper()
        if name not in self.doc.cast:
            declared = self.declared()
            self.doc.cast[name] = CastMember(name=name, declared=False)
            if declared and name != NARRATOR:
                self.warn(line, f"{name} speaks but isn't in the cast; added.")
        return name

    def speech(self, speaker: str, head_tags: str, text: str, line: int) -> None:
        tags, pan = self.tags(head_tags, line)
        # A pause written inside a line splits it in two; a sound cue
        # written inside a line comes after its part of the line.
        segments = _PAUSE_RE.split(text)
        for idx, seg in enumerate(segments):
            if idx % 2 == 1:  # a pause length (or None)
                self.pause(seg, line)
                continue
            cues = _SFX_RE.findall(seg)
            if cues:
                self.warn(line, "Sound cues go on their own line; placed after the line.")
            seg = _SFX_RE.sub(" ", seg).strip()
            seg_tags, seg_pan = list(tags), pan
            # A line (or a part of one) that is only a voice tag ("[sighs]",
            # "MARA [laughs]:") is a sound of the voice: kept as a line.
            own_tags = bool(tags) and len(segments) == 1
            m = _LEADING_TAGS_RE.match(seg)
            if m:
                more, more_pan = self.tags(m.group(1), line)
                own_tags = own_tags or bool(more)
                seg_tags += [t for t in more if t not in seg_tags]
                seg_pan = more_pan or seg_pan
                seg = seg[m.end():]
                if idx == 0:  # the line's own leading tags hold for all of it
                    tags, pan = list(seg_tags), seg_pan
            body, inline_pan = self.inline(seg, line)
            if inline_pan:
                seg_pan = pan = inline_pan
            if body or (own_tags and seg_tags):
                self.add(Item(kind="speech", line=line, speaker=speaker, tags=seg_tags, pan=seg_pan,
                              text=body))
            for cue in cues:
                self.sfx(cue, line)

    def pause(self, raw: Optional[str], line: int) -> None:
        secs = float(raw) if raw else 1.0
        if not PAUSE_MIN <= secs <= PAUSE_MAX:
            self.warn(line, f"A pause is {PAUSE_MIN:g}–{PAUSE_MAX:g} seconds; {secs:g} was changed.")
            secs = min(PAUSE_MAX, max(PAUSE_MIN, secs))
        self.add(Item(kind="pause", line=line, seconds=secs))

    def sfx(self, body: str, line: int) -> None:
        parts = [p.strip() for p in body.split("|")]
        prompt = parts[0] if parts else ""
        item = Item(kind="sfx", line=line)
        m = _ASSET_RE.match(prompt)
        if m:
            item.asset_id = int(m.group(1))
            prompt = ""
        elif not prompt:
            self.warn(line, "A sound cue needs a description; skipped.")
            return
        item.prompt = " ".join(prompt.split())
        for p in parts[1:]:
            low = p.lower()
            if not low:
                continue
            if (s := _SECONDS_RE.match(low)) is not None:
                secs = float(s.group(1))
                if not SFX_MIN <= secs <= SFX_MAX:
                    self.warn(line, f"A sound effect is {SFX_MIN:g}–{SFX_MAX:g} seconds; {secs:g} was changed.")
                    secs = min(SFX_MAX, max(SFX_MIN, secs))
                item.seconds = secs
            elif (o := _OFFSET_RE.match(low)) is not None:
                item.placement, item.offset = "offset", float(o.group(1))
            elif low in SFX_PLACEMENTS:
                item.placement, item.offset = low, None
            else:
                self.warn(line, f"Unknown sound cue option \"{p}\" ignored (use seconds like 3s, "
                                "under, after or +2s).")
        self.add(item)

    def bed(self, rest: str, line: int) -> None:
        rest = rest.strip()
        words = rest.split(None, 1)
        if not words:
            self.warn(line, "@bed needs ambience, music or stop.")
            return
        head = words[0].lower()
        if head == "stop":
            if not self.open_beds:
                self.warn(line, "@bed stop with no bed playing.")
            self._stop_beds(list(self.open_beds), line)
            self.add(Item(kind="bed", line=line, action="stop", layer=""))
            return
        if head not in BED_LAYERS:
            self.warn(line, f"Unknown bed \"{words[0]}\" (use ambience or music); skipped.")
            return
        arg = words[1].strip() if len(words) > 1 else ""
        if arg.lower() == "stop":
            if head not in self.open_beds:
                self.warn(line, f"@bed {head} stop with no {head} playing.")
            self._stop_beds([head], line)
            self.add(Item(kind="bed", line=line, action="stop", layer=head))
            return
        quoted = len(arg) >= 2 and arg.startswith(('"', "“")) and arg.endswith(('"', "”"))
        # Only the outer pair of quotes goes: quotes inside the description
        # are kept (serialize writes them back as they are).
        prompt = (arg[1:-1] if quoted else arg.strip('"“”')).strip()
        if not prompt:
            self.warn(line, f"@bed {head} needs a description in quotes; skipped.")
            return
        if not quoted:
            self.warn(line, f"Put the {head} description in quotes.")
        self._stop_beds([head], line)
        self.add(Item(kind="bed", line=line, action="start", layer=head, prompt=" ".join(prompt.split())))
        self.doc.beds.append(Bed(layer=head, prompt=self.doc.items[-1].prompt, chapter=self.doc.items[-1].chapter,
                                 start_item=len(self.doc.items) - 1, line=line))
        self.open_beds[head] = len(self.doc.beds) - 1

    def _stop_beds(self, layers: list[str], line: int) -> None:
        for layer in layers:
            idx = self.open_beds.pop(layer, None)
            if idx is not None:
                self.doc.beds[idx].stop_item = len(self.doc.items)

    def cast(self, m: re.Match, line: int) -> None:
        name = " ".join(m.group(1).split()).upper()
        tags, pan = self.tags(m.group(2), line)
        if tags:
            self.warn(line, "A cast line takes a placement only ([left], [right] or [center]); tags removed.")
        if self.doc.items and any(i.kind != "comment" for i in self.doc.items):
            self.warn(line, "Cast lines belong at the top of the script.")
        prev = self.doc.cast.get(name)
        if prev is not None and prev.declared:
            self.warn(line, f"{name} is declared twice; the later line wins.")
        self.doc.cast[name] = CastMember(name=name, pan=pan, description=" ".join(m.group(3).split()))

    def line(self, raw: str, n: int) -> None:
        s = raw.strip()
        if not s:
            return
        if _CUE_OPEN_RE.search(s):
            s = self.bracketed_cues(s, n)
            if not s:
                return
        if (m := _COMMENT_RE.match(s)) is not None:
            self.add(Item(kind="comment", line=n, text=m.group(1).strip()))
            return
        if (m := _CHAPTER_RE.match(s)) is not None:
            title = (m.group(1) or "").strip()
            chs = self.doc.chapters
            if (len(chs) == 1 and not chs[0].line and not chs[0].title
                    and not any(it.kind in ("speech", "sfx", "pause") for it in self.doc.items)):
                # Only notes or a bed before the first heading: they belong
                # to that chapter (no untitled chapter with nothing to hear).
                chs[0].title, chs[0].line = title, n
            else:
                chs.append(Chapter(index=len(chs), title=title, line=n))
            return
        if s.lower().startswith("@cast"):
            if (m := _CAST_RE.match(s)) is not None:
                self.cast(m, n)
            else:
                self.warn(n, "A cast line reads @cast NAME [left]: description; skipped.")
            return
        if (m := _BED_RE.match(s)) is not None:
            self.bed(m.group(1), n)
            return
        if s.startswith("@"):
            self.warn(n, f"Unknown command {s.split()[0]}; skipped.")
            return
        if (m := _PAUSE_RE.fullmatch(s)) is not None:
            self.pause(m.group(1), n)
            return
        if (m := _SFX_RE.fullmatch(s)) is not None:
            self.sfx(m.group(1), n)
            return
        m = _SPEAKER_RE.match(s)
        if m and is_speaker_name(m.group(1), self.doc.cast):
            before = len(self.doc.items)
            if m.group(3).strip() or m.group(2).strip():
                self.speech(self.speaker(m.group(1), n), m.group(2), m.group(3).strip(), n)
            if len(self.doc.items) == before:
                self.warn(n, f"{' '.join(m.group(1).split()).upper()} has an empty line; skipped.")
            return
        if m and self.declared() and _looks_like_name(m.group(1)):
            name = " ".join(m.group(1).split())
            self.warn(n, f"\"{name}:\" is read by the narrator: {name.upper()} isn't in the cast.")
        self.speech(self.speaker(NARRATOR, n), "", s, n)

    def bracketed_cues(self, s: str, n: int) -> str:
        """`s` without the sound cues that have '[' or ']' inside (skipped
        with a warning): a bracket would end the cue early, so the rest of
        it would be read aloud."""
        out, pos = [], 0
        for m in _CUE_OPEN_RE.finditer(s):
            if m.start() < pos:
                continue
            end, inner = _cue_end(s, m.end())
            if not inner:
                continue
            out.append(s[pos:m.start()])
            pos = end
            self.warn(n, "A sound cue can't contain [ or ]; skipped.")
        out.append(s[pos:])
        return _tidy("".join(out)) if pos else s

    def declared(self) -> bool:
        return any(c.declared for c in self.doc.cast.values())

    def finish(self) -> StoryDoc:
        doc = self.doc
        if not doc.chapters:
            doc.chapters.append(Chapter(index=0, title="", line=0))
        assign_keys(doc.items)
        # Chapter spans and estimates.
        for ch in doc.chapters:
            idx = [i for i, it in enumerate(doc.items) if it.chapter == ch.index]
            ch.first_item = idx[0] if idx else len(doc.items)
            ch.item_count = len(idx)
            ch.est_seconds = round(sum(item_seconds(doc.items[i], self.wpm) for i in idx), 1)
        heard = [c for c in doc.chapters if c.est_seconds > 0]
        doc.est_seconds = round(sum(c.est_seconds for c in heard) + CHAPTER_GAP * max(0, len(heard) - 1), 1)
        if self.max_minutes and doc.est_seconds > self.max_minutes * 60:
            doc.warnings.append(ScriptWarning(0, f"About {doc.est_seconds / 60:.0f} minutes: longer than the "
                                           f"{self.max_minutes:g}-minute limit."))
        if not doc.speech():
            doc.warnings.append(ScriptWarning(0, "Nothing to read yet."))
        return doc


def item_seconds(item: Item, wpm: float = DEFAULT_WPM) -> float:
    """How long an item adds to the story (an estimate before any audio)."""
    if item.kind == "speech":
        words = len(item.spoken_text.split())
        extra = sum(TAG_SECONDS.get(t, 0.0) for t in item.tags)
        extra += sum(TAG_SECONDS.get(norm_tag(t), 0.0) for t in _TAG_RE.findall(item.text))
        if any(t in ("slowly", "sleepy", "drowsy") for t in item.tags):
            words *= 1.15
        return words / wpm * 60.0 + extra + 0.3  # a breath between lines
    if item.kind == "pause":
        return float(item.seconds or 0.0)
    if item.kind == "sfx" and item.placement == "after":
        return float(item.seconds or DEFAULT_SFX_SECONDS)
    return 0.0


def _key_body(item: Item) -> str:
    if item.kind == "speech":
        # A line that is only a tag ("[sighs]") is told apart by its tags.
        return f"{item.speaker}\0{item.text or item.voice_text}"
    if item.kind == "sfx":
        return f"{item.asset_id or ''}\0{item.prompt.lower()}"
    if item.kind == "bed":
        return f"{item.action}\0{item.layer}\0{item.prompt.lower()}"
    if item.kind == "pause":
        return _fmt(float(item.seconds or 0.0))
    if item.kind == "comment":
        return item.text
    return item.kind


_KEY_PREFIX = {"speech": "v", "sfx": "x", "pause": "p", "bed": "b", "comment": "c"}


def assign_keys(items: list[Item]) -> None:
    """Keys from content (speaker and words for a line, the prompt for a
    sound), numbered when the same content comes again: inserting or
    editing a line changes no other line's key."""
    seen: dict[str, int] = {}
    for it in items:
        digest = hashlib.sha1(f"{it.kind}\0{_key_body(it)}".encode("utf-8")).hexdigest()[:16]
        base = f"{_KEY_PREFIX.get(it.kind, 'z')}{digest}"
        seen[base] = seen.get(base, 0) + 1
        it.key = base if seen[base] == 1 else f"{base}-{seen[base]}"


def parse(text: str, *, style: str = "", clean: Optional[bool] = None,
          max_minutes: Optional[float] = None) -> StoryDoc:
    """The script as a StoryDoc. `style` sets the reading speed of the
    estimate; `clean` (default: this deployment) limits the voice tags to
    the general-audience ones; `max_minutes` (default: the configured cap)
    adds a warning when the story runs longer."""
    if max_minutes is None:
        from ..config import get_settings

        max_minutes = float(get_settings().sound_story_max_minutes)
    p = _Parser(_clean(clean), style, max_minutes)
    for n, raw in enumerate((text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"), start=1):
        p.line(raw, n)
    return p.finish()


# ---------- writing back ----------


def _fmt(x: float) -> str:
    return f"{x:g}"


def _safe(text: str) -> str:
    return " ".join(text.replace("|", "/").replace("]", ")").replace("[", "(").split())


def _reads_as_narration(text: str, cast: Optional[dict]) -> bool:
    """Whether `text`, alone on a line, is parsed back as narration (not a
    speaker line, a comment, a chapter, a command, a pause or a cue)."""
    if _NOT_NARRATION_RE.match(text):
        return False
    m = _SPEAKER_RE.match(text)
    return not (m and is_speaker_name(m.group(1), cast))


def serialize_item(item: Item, cast: Optional[dict] = None) -> str:
    """One item as a line. `cast`: the doc's cast (names that would read
    as a speaker)."""
    if item.kind == "comment":
        return f"// {item.text}".rstrip()
    if item.kind == "pause":
        return f"(pause {_fmt(item.seconds or 1.0)})"
    if item.kind == "sfx":
        parts = [f"#{item.asset_id}" if item.asset_id else _safe(item.prompt)]
        if item.seconds is not None:
            parts.append(f"{_fmt(item.seconds)}s")
        if item.placement == "under":
            parts.append("under")
        elif item.placement == "offset":
            parts.append(f"+{_fmt(item.offset or 0.0)}s")
        return f"[sfx: {' | '.join(parts)}]"
    if item.kind == "bed":
        if item.action == "stop":
            return f"@bed {item.layer} stop" if item.layer else "@bed stop"
        # The prompt as it is: the parser takes off the outer quotes only.
        return f'@bed {item.layer} "{item.prompt}"'
    head = "".join(f"[{t}]" for t in item.tags) + (f"[{item.pan}]" if item.pan else "")
    if item.speaker == NARRATOR and (head or _reads_as_narration(item.text, cast)):
        return f"{head} {item.text}".strip()
    return f"{item.speaker} {head}: {item.text}".replace(" :", ":")


def serialize(doc: StoryDoc) -> str:
    """The doc in canonical markup: the cast, then each chapter."""
    out: list[str] = []
    for c in doc.cast.values():
        if c.declared:
            pan = f" [{c.pan}]" if c.pan else ""
            out.append(f"@cast {c.name}{pan}: {c.description}".rstrip())
    if out:
        out.append("")
    for ch in doc.chapters:
        if ch.title or ch.index > 0 or ch.line:
            out.append(f"# {ch.title}".rstrip())
            out.append("")
        for it in doc.items:
            if it.chapter == ch.index:
                out.append(serialize_item(it, doc.cast))
        out.append("")
    return "\n".join(out).strip() + "\n"


# ---------- helpers for the checks and the clips ----------


def cast_header(doc: StoryDoc) -> str:
    """The cast as one block (who each character is), for the safety check
    of every part of a long script."""
    lines = [f"{c.name}: {c.description}".strip().rstrip(":") for c in doc.cast.values()]
    return ("Cast:\n" + "\n".join(lines)) if lines else ""


def voiced_text(doc: StoryDoc, context: Optional[str] = None) -> str:
    """Everything that is read aloud, one line per cue, as the safety checks
    read it: the speaker, the voice tags (how the line is performed) and
    the words, a direction marked as safety_text marks it (with `context`,
    by default the cast header, in view)."""
    header = cast_header(doc) if context is None else context
    return "\n".join(_voiced_line(i, header) for i in doc.speech())


def _voiced_line(item: Item, header: str) -> str:
    """One line as the checks read it: "SPEAKER: text", marked, and its
    delivery instructions (marked as safety_instructions marks them)."""
    line = f"{item.speaker}: {safety_text(item.voice_text, header, prose=True)}"
    if (item.instructions or "").strip():
        said = safety_instructions(item.instructions, item.voice_text, header)
        line += f" (delivery instructions: {' '.join(said.split())})"
    return line


def prompts_text(doc: StoryDoc) -> list[str]:
    """Every sound and bed prompt in the story."""
    return [i.prompt for i in doc.items if i.kind in ("sfx", "bed") and i.prompt]


def _prompt_lines(doc: StoryDoc, extra: tuple[str, ...] = (), *, marked: bool = True,
                  context: Optional[str] = None) -> str:
    """Every sound, bed and music prompt of the story, one per line, labelled
    (and, `marked`, as safety_text reads it with `context`, by default the
    cast header, in view)."""
    header = (cast_header(doc) if context is None else context) if marked else ""
    lines: list[str] = []
    for i in doc.items:
        if i.kind in ("sfx", "bed") and i.prompt:
            label = "SOUND EFFECT" if i.kind == "sfx" else f"{i.layer.upper()} BED"
            lines.append(f"{label}: {safety_text(i.prompt, header, prose=True) if marked else i.prompt}")
    for p in extra:
        if (p or "").strip():
            lines.append(f"MUSIC: {safety_text(p, header, prose=True) if marked else p}")
    return "\n".join(lines)


def story_has_minor(doc: StoryDoc, *, music_prompts: tuple[str, ...] = (), title: str = "",
                    premise: str = "") -> bool:
    """Whether any part of the story mentions a minor: the cast, any line,
    any sound, bed or music prompt, the title or the premise (each on its
    own, and all of them together)."""
    from .minor_check import minor_hint as mentions_minor

    parts = [cast_header(doc), title, premise, *music_prompts, *prompts_text(doc)]
    parts += [f"{i.speaker}: {i.voice_text}" for i in doc.speech()]
    parts += [i.instructions for i in doc.speech()]
    parts = [normalize(p) for p in parts if (p or "").strip()]
    return any(mentions_minor(p) for p in parts) or mentions_minor("\n".join(parts))


def story_context(doc: StoryDoc, *, music_prompts: tuple[str, ...] = (), title: str = "",
                  premise: str = "", facts: str = "") -> str:
    """The context a story hands every provider call (the `context` of its
    voices and its sound and music prompts) and its own checks: the story
    note (minor_check.STORY_NOTE) when a minor appears anywhere in the
    story (by the words, or by the `facts`), the facts line
    (minor_check.story_facts) and the cast header. The note and the facts
    are for the model only: the word filter never reads them."""
    from .minor_check import STORY_NOTE, facts_minor

    header = cast_header(doc)
    lead = []
    if facts_minor(facts) or story_has_minor(doc, music_prompts=music_prompts, title=title, premise=premise):
        lead.append(STORY_NOTE)
    if (facts or "").strip():
        lead.append(" ".join(facts.split()))
    return "\n".join(x for x in (*lead, header) if x).strip()


class StoryCheck(str):
    """What check_story returns: the story's context (a str, handed to every
    provider call as `context`), carrying the signed pass of what it judged
    (`story_pass`, services/story_pass.py), which the provider guards read
    from the context they're handed (or from their own `story_pass`), and
    the hash of the story it judged (`story_hash`: is_current)."""

    story_pass: str = ""
    story_hash: str = ""
    voices_hash: str = ""

    def __new__(cls, context: str, story_pass: str = "", story_hash: str = "",
                voices_hash: str = "") -> "StoryCheck":
        obj = super().__new__(cls, context)
        obj.story_pass = story_pass
        obj.story_hash = story_hash
        obj.voices_hash = voices_hash
        return obj


TOO_LONG_STORY = ("This story is about {minutes:.0f} minutes long, over the {limit:g}-minute limit. "
                  "Shorten it or split it into parts.")
# What a story may send to the safety checks: its lines, instructions and
# every prompt (beds, sound effects, music), its notes, title and premise,
# per minute of the story's own estimated length (at least
# STORY_MIN_MINUTES): far more than a story reads aloud (about 1,000
# characters a minute), so only padding is refused.
STORY_CHARS_PER_MINUTE = 2000
STORY_MIN_MINUTES = 5
TOO_MUCH_TEXT = ("This story has more text than the safety check reads for a story of about {minutes:.0f} "
                 "minutes ({chars:,} characters, over {cap:,}): shorten the sound and bed descriptions or notes, "
                 "or split it into parts.")
# Each sound effect or bed description, and how many a story may have.
PROMPT_MAX_CHARS = 400
MAX_PROMPTS = 400
PROMPT_TOO_LONG = ("A sound or bed description is too long ({chars:,} characters, at most {cap:,}), at line "
                   "{line}: shorten it.")
TOO_MANY_PROMPTS = ("This story has too many sound effects and beds ({count:,}, at most {cap:,}): use fewer, or "
                    "split it into parts.")


def story_chars(doc: StoryDoc, music_prompts: tuple[str, ...] = (), title: str = "", premise: str = "") -> int:
    """Every character of the story that the checks read or that comes with
    it: the cast, the lines (tags included) and their instructions, every
    sound, bed and music prompt, the notes, the title and the premise."""
    n = len(cast_header(doc)) + len(title or "") + len(premise or "") + sum(len(p or "") for p in music_prompts)
    for i in doc.items:
        n += len(i.speaker) + len(i.voice_text if i.kind == "speech" else i.text or "")
        n += len(i.prompt or "") + len(i.instructions or "")
    return n


def story_hash(doc: StoryDoc, music_prompts: tuple[str, ...] = (), title: str = "", premise: str = "") -> str:
    """A hash of the whole story, normalised: the cast, every item (its
    kind, speaker, words, instructions, prompt, layer and timing) in order,
    the music prompts, the title and the premise. A story pass is issued
    for one (story_pass.story_of)."""
    import json

    def n(t) -> str:  # noqa: ANN001
        return " ".join(normalize(str(t or "")).split())

    data = {
        "cast": [[c.name, n(c.description), c.pan or ""] for c in doc.cast.values()],
        "items": [[i.kind, i.speaker, n(i.voice_text if i.kind == "speech" else i.text), n(i.instructions),
                   n(i.prompt), i.layer, i.action, i.seconds, i.placement, i.offset, i.asset_id, i.pan or ""]
                  for i in doc.items],
        "music": [n(p) for p in music_prompts], "title": n(title), "premise": n(premise),
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def is_current(check: str, doc: StoryDoc, *, music_prompts: tuple[str, ...] = (), title: str = "",
               premise: str = "", voices: Optional[dict] = None) -> bool:
    """Whether `check` (check_story's result) was issued for this exact
    story (and, given `voices`, these voices): callers that keep a
    StoryCheck across edits run check_story again when it isn't."""
    from . import story_pass

    want = story_hash(doc, tuple(music_prompts), title, premise)
    token = str(getattr(check, "story_pass", "") or "")
    if voices is not None and getattr(check, "voices_hash", "") != _voices_hash(_bindings(doc, voices)):
        return False
    return bool(token) and getattr(check, "story_hash", "") == want and story_pass.story_of(token) == want


def text_cap(doc: StoryDoc) -> int:
    """How many characters a story may send to the checks: STORY_CHARS_PER_MINUTE
    for each estimated minute, at least STORY_MIN_MINUTES."""
    return int(max(doc.est_seconds / 60.0, STORY_MIN_MINUTES) * STORY_CHARS_PER_MINUTE)


def check_text_budget(doc: StoryDoc, music_prompts: tuple[str, ...] = (), title: str = "",
                      premise: str = "") -> None:
    """Refuse padding before anything is asked: more text than the story's
    length allows (text_cap), a sound or bed description over
    PROMPT_MAX_CHARS, or more than MAX_PROMPTS of them."""
    from .safety import UnsafePromptError

    chars = story_chars(doc, music_prompts, title, premise)
    cap = text_cap(doc)
    if chars > cap:
        raise UnsafePromptError(TOO_MUCH_TEXT.format(minutes=doc.est_seconds / 60.0, chars=chars, cap=cap))
    prompts = [i for i in doc.items if i.kind in ("sfx", "bed") and i.prompt]
    if len(prompts) > MAX_PROMPTS:
        raise UnsafePromptError(TOO_MANY_PROMPTS.format(count=len(prompts), cap=MAX_PROMPTS))
    for i in prompts:
        if len(i.prompt) > PROMPT_MAX_CHARS:
            raise UnsafePromptError(PROMPT_TOO_LONG.format(chars=len(i.prompt), cap=PROMPT_MAX_CHARS, line=i.line))


@dataclass
class _Unit:
    """One line of a story as its checks read it: a title, premise or
    chapter line, a piece of a spoken line (speaker, words, and its
    delivery instructions), or a sound, bed or music prompt."""

    label: str
    text: str
    instructions: str = ""
    speech: bool = False
    prompt: bool = False

    def plain(self) -> str:
        said = f" (delivery instructions: {' '.join(self.instructions.split())})" if self.instructions.strip() else ""
        return f"{self.label}: {self.text}{said}"

    def marked(self, header: str, whole: str = "") -> str:
        if self.speech:
            line = f"{self.label}: {safety_text(self.text, header, prose=True)}"
            if self.instructions.strip():
                said = safety_instructions(self.instructions, whole or self.text, header)
                line += f" (delivery instructions: {' '.join(said.split())})"
            return line
        if self.prompt:
            return f"{self.label}: {safety_text(self.text, header, prose=True)}"
        return self.plain()


def _units(doc: StoryDoc, music_prompts: tuple[str, ...], title: str, premise: str, *,
           piece: int) -> list[_Unit]:
    """The story in order, as its checks read it: title, premise, then each
    chapter heading, line and sound or bed prompt where it comes, then the
    music prompts. A line longer than `piece` characters is cut between
    words into overlapping pieces (its instructions go with the last)."""
    from .explicit_check import parts

    out: list[_Unit] = []

    def add(label: str, text: str, *, instructions: str = "", speech: bool = False, prompt: bool = False) -> None:
        text = " ".join((text or "").split())
        instructions = " ".join((instructions or "").split())
        room = piece - len(label) - 2
        if len(text) + len(instructions) + 30 <= room:
            out.append(_Unit(label, text, instructions, speech, prompt))
            return
        for p in parts(text, size=room):
            out.append(_Unit(label, p, "", speech, prompt))
        if instructions:
            for p in parts(instructions, size=max(200, room - 30)):
                out.append(_Unit(label, "", p, speech, prompt))

    if (title or "").strip():
        add("TITLE", title)
    if (premise or "").strip():
        add("PREMISE", premise)
    starts = {ch.first_item: ch for ch in doc.chapters if ch.title.strip() and ch.item_count}
    for n, i in enumerate(doc.items):
        if n in starts:
            add("CHAPTER", starts[n].title)
        if i.kind == "speech":
            add(i.speaker, i.voice_text, instructions=i.instructions, speech=True)
        elif i.kind in ("sfx", "bed") and i.prompt:
            add("SOUND EFFECT" if i.kind == "sfx" else f"{i.layer.upper()} BED", i.prompt, prompt=True)
    for p in music_prompts:
        if (p or "").strip():
            add("MUSIC", p, prompt=True)
    return out


def _bindings(doc: StoryDoc, voices) -> dict:  # noqa: ANN001
    """{NAME: voice} for every cast member: `voices` ({name: voice id, or
    (provider, voice id), or {"voice_id", "provider"}}, names in any case),
    the default voice "" for the rest."""
    given = {" ".join(str(k).split()).upper(): v for k, v in (voices or {}).items()}
    return {name: given.get(name, "") for name in doc.cast}


def _voices_hash(bound: dict) -> str:
    import json

    from .story_pass import voice_of

    data = sorted((k, *voice_of(v)) for k, v in bound.items())
    return hashlib.sha256(json.dumps(data).encode("utf-8")).hexdigest()


def check_story(doc: StoryDoc, *, music_prompts: tuple[str, ...] = (), title: str = "",
                premise: str = "", voices: Optional[dict] = None) -> StoryCheck:
    """The safety checks of a whole story before anything is voiced or
    generated: everything read aloud (each line with its speaker and its
    delivery instructions, Item.instructions) and every sound, bed and
    music prompt (`music_prompts`: the story's own, e.g. settings
    music_prompt), each judged with the story's context in front. Returns
    that context (a StoryCheck: hand it to every provider call of the
    story), with the signed pass of every line and prompt it judged, bound
    to this exact story (story_hash) and to the voice each cast member is
    read with (`voices`: {name: voice id, (provider, voice id) or
    {"voice_id", "provider"}}; a cast member not in it is read with the
    default voice, ""). The provider guards send nothing the pass doesn't
    cover: after any edit, a new voice, or a retake with new instructions
    (set them on the item), call check_story again on the current story
    (unchanged windows and facts come from the caches).

    - A story longer than sound_story_max_minutes (the estimate) is refused,
      and so is one with more text than its estimated length allows
      (text_cap), a sound or bed description over PROMPT_MAX_CHARS, or more
      than MAX_PROMPTS of them (check_text_budget).
    - A line, prompt or cast description that starts like the app's
      context block ("Story facts:", "Story note:") is refused.
    - The word filter (minor_check.story_word_filter) reads every clause,
      before any model call.
    - The cast is at most minor_check.LONG_CONTEXT_LIMIT characters
      (else refused, minor_check.CAST_TOO_LONG): it is always in view.
    - The story is cut in windows of whole lines (title and premise first,
      then each chapter, line and sound where it comes, music last).
    - The story facts (minor_check.story_facts_detail), window by window:
      who is a minor now, and who is shown as a minor in each window (a
      flashback); the facts line leads the context.
    - The minors check (every mode): every window with the context, the
      whole story's facts (who is a minor now, and every part that shows
      a character as a minor) and that window's own facts in front
      (minor_check.check_story_windows), the windows cut so each fits with
      all of that in front (minor_check.story_judge_windows: one call
      each); a window the model can't judge refuses the story, and so does
      one whose timing is unclear for a character the story shows as a
      minor in a scene (a childhood only remembered gets a neutral note).
    - A clean deployment: the explicit-content check of the cast, the lines
      and the prompts; the pass records it, so the guards don't ask again.

    Raises UnsafePromptError / ExplicitContentError."""
    from ..config import get_settings
    from . import explicit_check, minor_check, story_pass
    from .explicit_check import line_windows, piece_size

    music_prompts = tuple(music_prompts)
    limit = float(get_settings().sound_story_max_minutes)
    if limit and doc.est_seconds > limit * 60:
        raise minor_check.UnsafePromptError(TOO_LONG_STORY.format(minutes=doc.est_seconds / 60, limit=limit))
    check_text_budget(doc, music_prompts, title, premise)
    header = cast_header(doc)
    minor_check.check_cast_length(header)
    size = minor_check.story_window_size(header)
    units = _units(doc, music_prompts, title, premise, piece=piece_size(size))
    plain_units = [u.plain() for u in units]
    plain = "\n".join(plain_units)
    descriptions = "\n".join(c.description for c in doc.cast.values())
    if minor_check.forged_label(plain) or minor_check.forged_label(descriptions):
        raise minor_check.UnsafePromptError(minor_check.LABEL_REFUSED)
    minor_check.story_word_filter(header)
    minor_check.story_word_filter(plain)
    ranges = line_windows([len(x) for x in plain_units], size=size)
    required = [c.name for c in doc.cast.values() if c.description.strip() and c.name != NARRATOR]
    facts = minor_check.story_facts_detail(header, windows=["\n".join(plain_units[a:b]) for a, b in ranges] or [""],
                                           names=required, known=list(doc.cast))
    context = story_context(doc, music_prompts=music_prompts, title=title, premise=premise, facts=facts.line)
    marked = [u.marked(context) for u in units]
    windows, part_lines = minor_check.story_judge_windows(marked, ranges, facts, context)
    minor_check.check_story_windows(windows or [""], part_lines or [""], context=context,
                                    summary=facts.summary_line())
    clean = explicit_check.active()
    if clean:
        lines = "\n".join(f"{i.speaker}: {i.voice_text}" + (f"\n{i.instructions}" if i.instructions.strip() else "")
                          for i in doc.speech())
        explicit_check.check(header, lines, _prompt_lines(doc, music_prompts, marked=False))
    items = [story_pass.line_item(i.speaker, i.voice_text, i.instructions) for i in doc.speech()]
    items += [story_pass.prompt_item(p) for p in (*prompts_text(doc), *music_prompts)]
    digest = story_hash(doc, music_prompts, title, premise)
    bound = _bindings(doc, voices)
    token = story_pass.issue(context, items, story=digest, clean=clean, voices=bound)
    return StoryCheck(context, token, digest, _voices_hash(bound))

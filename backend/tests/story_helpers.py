"""Helpers for the story-path tests: a story is checked as a whole
(story_script.check_story) and its parts are voiced or generated with the
context and pass that check returned."""
from __future__ import annotations

import re as _re


def parse(text: str, **kw):
    from app.services import story_script

    kw.setdefault("clean", False)
    kw.setdefault("max_minutes", 90)
    return story_script.parse(text, **kw)


def checked(text: str, *, instructions: dict | None = None, **kw):
    """(doc, context) for a story: parsed, `instructions` ({line index in
    doc.speech(): text}) set on its lines, and checked."""
    from app.services import story_script

    doc = parse(text, **{k: v for k, v in kw.items() if k in ("clean", "max_minutes", "style")})
    for idx, said in (instructions or {}).items():
        doc.speech()[idx].instructions = said
    rest = {k: v for k, v in kw.items() if k in ("music_prompts", "title", "premise")}
    return doc, story_script.check_story(doc, **rest)


def say(voice, item, context, **kw):  # noqa: ANN001
    """Voice one story line as a story caller does: its speaker, its text and
    its instructions, with the story's context (and pass)."""
    settings = kw.pop("settings", None)
    if item.instructions and settings is None:
        settings = {"instructions": item.instructions}
    return voice.synthesize(text=item.voice_text, voice_id=kw.pop("voice_id", ""), settings=settings,
                            context=context, speaker=item.speaker, **kw)


def pass_for(context: str, *items: str, voices: dict | None = None) -> str:
    """A good pass for `items` (story_pass.line_item / prompt_item) read with
    `context` (and `voices`, {SPEAKER: voice id}: the default voice "" for
    anyone else): for tests of what a guard does after the pass, without a
    whole story."""
    from app.services import story_pass

    return story_pass.issue(context, list(items), voices=voices)


# ---------- an oracle judge for the story tests ----------

# "MIA is a minor (11)", "MIA is a minor in this part": a minor now.
_FLAT_RE = _re.compile(r"([A-Z][A-Z .'-]*?) is a minor\b")
# "MIA is shown as a minor in this part (11)" / "... elsewhere in the story":
# a minor unless the part clearly places the scene in their adult years.
_COND_RE = _re.compile(r"([A-Z][A-Z .'-]*?) is shown as a minor")
# What clearly moves a part to a character's adult years.
_JUMP_RE = _re.compile(r"\b(?:\w+[- ])?years? later\b|\bdecades? later\b|\ball grown up\b|\bas adults?\b|"
                       r"\bpresent day\b|\bnow (?:a|an) (?:grown|adult)", _re.I)


def oracle_finds(ctx: str, user: str, age_re, sex_re) -> str:  # noqa: ANN001
    """The red teams' oracle judge, aware of the whole-story facts: the
    sentence it refuses ("" for none). A sexual sentence is refused when it
    has an age or child cue itself, names or is spoken by a minor now, or
    names or is spoken by a character the context shows as a minor in some
    part, unless a time jump ("twenty years later") comes after that
    character's last young age in this part and before the sentence."""
    flat = {n.strip() for n in _FLAT_RE.findall(ctx)}
    cond = {n.strip() for n in _COND_RE.findall(ctx)} - flat
    sentences = []
    for line in _re.split(r"\n|\s(?=[A-Z][A-Z'-]{1,39}(?: [A-Z'-]+)*:\s)", user):
        spk = ""
        m = _re.match(r"^\s*([A-Z][A-Z .'-]{0,39}):\s*(.*)$", line)
        if m:
            spk, line = m.group(1).strip(), m.group(2)
        sentences += [(spk, s) for s in _re.split(r"(?<=[.!?;])\s+", line)]
    young_at = dict.fromkeys(cond, -1)
    jump_at = -1
    for i, (spk, sent) in enumerate(sentences):
        here = {n for n in flat | cond if spk == n or _re.search(rf"\b{_re.escape(n)}\b", sent, _re.I)}
        if age_re.search(sent):
            for n in here & cond:
                young_at[n] = i
        if _JUMP_RE.search(sent):
            jump_at = i
        if not sex_re.search(sent):
            continue
        if age_re.search(sent) or here & flat:
            return sent
        if any(not (jump_at >= 0 and jump_at > young_at[n]) for n in here & cond):
            return sent
    return ""

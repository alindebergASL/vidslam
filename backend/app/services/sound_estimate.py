"""What an audio story costs before anything runs (Sound Studio).

estimate() returns the same shape as ambient.estimate_ambient_cost: line
items {item, qty, unit, unit_cost, cost} and a total. Voice is priced by
the characters each voice model is sent, sound effects by the second,
ambience beds per 30 s loop, music per track and the writer per call.
Mixing and rendering are local (ffmpeg) and cost nothing. Anything already
generated is counted as $0, so the price of "Resume" or of one edited line
is what is left to pay. "Already generated" means a done clip with the same
fingerprint (cue_fingerprint: the words and tags, the voice and its route,
the seconds of a sound), not merely the same cue: a line whose tags, voice
or model changed, or a sound whose length changed, is paid for again.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Iterable, Optional, Union

from ..config import get_settings
from . import story_script
from .story_script import Item, StoryDoc

# Minutes of story one writer call produces (an outline is one more call).
MINUTES_PER_CHAPTER_CALL = 5


def voice_route(name: str, cast: Optional[dict], settings: Optional[dict]) -> tuple[str, str]:
    """(provider, model) that reads `name`'s lines: the character's own
    choice, else the story's, else the story default
    (registry.story_voice: ElevenLabs with the story model)."""
    from ..providers.registry import story_voice

    member = _member(name, cast)
    story = settings or {}
    if member.get("provider"):
        provider, model = str(member["provider"]), str(member.get("model") or "")
    else:
        provider = str(story.get("voice_provider") or "")
        model = str(member.get("model") or story.get("voice_model") or "")
    # The default is decided in one place, which the generator asks too
    # (registry.get_tts_for).
    return story_voice(provider, model)


def _member(name: str, cast: Optional[dict]) -> dict:
    return ((cast or {}).get(name) or {}) if isinstance(cast, dict) else {}


def voice_params(name: str, cast: Optional[dict], settings: Optional[dict]) -> dict:
    """Everything about the voice that shapes `name`'s audio: the route
    (voice_route), the voice id, seed, voice settings and style."""
    member, story = _member(name, cast), settings or {}
    provider, model = voice_route(name, cast, settings)
    return {"provider": provider, "model": model,
            "voice": str(member.get("voice_id") or story.get("voice_id") or ""),
            "seed": member.get("seed"),
            "voice_settings": member.get("voice_settings") or story.get("voice_settings") or None,
            "style": str(member.get("style") or "")}


def _digest(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
                          .encode("utf-8")).hexdigest()


def cue_fingerprint(item: Item, cast: Optional[dict] = None, settings: Optional[dict] = None) -> str:
    """What a cue's clip is made from, hashed (64 hex characters, as
    SoundClip.fingerprint stores it): a line's text with its tags and its
    voice (voice_params); a sound's prompt, seconds or library id; a bed's
    layer and prompt. A finished clip with the same fingerprint is reused
    for free; anything else is generated (and priced) again."""
    if item.kind == "speech":
        return _digest("voice", item.voice_text, voice_params(item.speaker, cast, settings))
    if item.kind == "sfx":
        return _digest("sfx", item.asset_id or "", item.prompt.lower(),
                       float(item.seconds or story_script.DEFAULT_SFX_SECONDS))
    if item.kind == "bed":
        return _digest("bed", item.action, item.layer, item.prompt.lower())
    return _digest(item.kind, item.key)


def music_fingerprint(prompt: str) -> str:
    """The fingerprint of the story's music track (settings music_prompt)."""
    return _digest("music", " ".join((prompt or "").split()).lower())


def voice_rate(provider: str, model: str) -> float:
    """USD per 1,000 characters."""
    if provider == "mock":
        return 0.0
    return get_settings().tts_rate(model)


def billed_chars(item: Item, provider: str, model: str = "") -> int:
    """Characters a provider is sent for a line: a tag-reading ElevenLabs
    model gets the tags too; the others get the words alone (as the
    adapters send them)."""
    from ..providers.elevenlabs_tts import TAG_MODELS

    if provider == "elevenlabs" and model in TAG_MODELS:
        return len(item.voice_text)
    return len(story_script.split_tags(item.voice_text)[1])


def _line(item: str, qty: float, unit: str, unit_cost: float) -> dict:
    return {"item": item, "qty": round(qty, 4) if isinstance(qty, float) else qty, "unit": unit,
            "unit_cost": round(unit_cost, 6), "cost": round(qty * unit_cost, 4)}


def done_fingerprints_for(db, story_id: int) -> set[str]:  # noqa: ANN001 — a Session
    """Fingerprints of the story's finished (selected) clips."""
    from .. import models

    rows = db.query(models.SoundClip.fingerprint).filter(
        models.SoundClip.story_id == story_id, models.SoundClip.status == "done",
        models.SoundClip.selected.is_(True)).all()
    return {r[0] for r in rows if r[0]}


def estimate(story: Union[StoryDoc, object], settings: Optional[dict] = None, *,
             cast: Optional[dict] = None, done_fingerprints: Optional[Iterable[str]] = None,
             db=None, write: Optional[bool] = None, target_minutes: Optional[int] = None) -> dict:  # noqa: ANN001
    """The price of `story` (a models.SoundStory, or a parsed StoryDoc).

    settings: the story's settings_json (style, voice_provider, voice_model,
    music_prompt...); cast: its cast_json (name → {provider, model, ...});
    done_fingerprints: fingerprints of the clips already generated
    (cue_fingerprint; read from `db` for a stored story when not given); write: add the writer's calls (default: when
    the story has a premise and no script yet)."""
    s = get_settings()
    doc: StoryDoc
    premise = ""
    if isinstance(story, StoryDoc):
        doc = story
    else:
        settings = settings if settings is not None else (getattr(story, "settings_json", None) or {})
        cast = cast if cast is not None else (getattr(story, "cast_json", None) or {})
        premise = getattr(story, "premise", "") or ""
        target_minutes = target_minutes or getattr(story, "target_minutes", None)
        doc = story_script.parse(getattr(story, "script_text", "") or "",
                                 style=str((settings or {}).get("style") or ""))
        if done_fingerprints is None and db is not None and getattr(story, "id", None):
            done_fingerprints = done_fingerprints_for(db, story.id)
    settings = settings or {}
    done_fps = set(done_fingerprints or ())

    def is_done(it: Item) -> bool:
        return bool(done_fps) and cue_fingerprint(it, cast, settings) in done_fps
    lines: list[dict] = []

    # Writing (an outline, then one call per chapter).
    if write is None:
        write = bool(premise.strip()) and not doc.speech()
    if write:
        minutes = int(target_minutes or settings.get("target_minutes") or 10)
        minutes = max(1, min(int(s.sound_story_max_minutes), minutes))
        calls = 1 + math.ceil(minutes / MINUTES_PER_CHAPTER_CALL)
        lines.append(_line(f"Writing ({calls} writer calls)", calls, "call", s.cost_chat_per_story_call))

    # Voice, by provider and model.
    groups: dict[tuple[str, str], dict] = {}
    skipped = 0
    for it in doc.speech():
        if is_done(it):
            skipped += 1
            continue
        provider, model = voice_route(it.speaker, cast, settings)
        g = groups.setdefault((provider, model), {"chars": 0, "speakers": []})
        g["chars"] += billed_chars(it, provider, model)
        if it.speaker not in g["speakers"]:
            g["speakers"].append(it.speaker)
    for (provider, model), g in sorted(groups.items()):
        rate = voice_rate(provider, model) / 1000.0
        who = ", ".join(n.title() for n in g["speakers"][:4]) + (" …" if len(g["speakers"]) > 4 else "")
        label = f"Voice: {who} ({provider}{' ' + model if model else ''}, {g['chars']:,} characters)"
        lines.append(_line(label, g["chars"], "char", rate))

    # Sound effects: by the second; the same sound twice is paid once (the
    # shared cache), a library sound (#id) not at all.
    def sig(it: Item) -> tuple[str, float]:
        return it.prompt.lower(), float(it.seconds or story_script.DEFAULT_SFX_SECONDS)

    seen = {sig(it) for it in doc.sfx() if not it.asset_id and is_done(it)}
    sfx_secs = 0.0
    sfx_count = 0
    for it in doc.sfx():
        if it.asset_id or is_done(it):
            continue
        secs = sig(it)[1]
        if sig(it) in seen:
            continue
        seen.add(sig(it))
        sfx_secs += secs
        sfx_count += 1
    if sfx_count:
        lines.append(_line(f"Sound effects ({sfx_count}, {sfx_secs:g} s)", sfx_secs, "sec",
                           s.cost_sound_per_second))

    # Beds: one 30 s seamless loop per ambience, one track per music bed.
    ambience: set[str] = set()
    music: set[str] = set()
    for it in doc.items:
        if it.kind == "bed" and it.action == "start" and not is_done(it):
            (ambience if it.layer == "ambience" else music).add(it.prompt.lower())
    extra = str(settings.get("music_prompt") or "").strip().lower()
    if extra and music_fingerprint(extra) not in done_fps:
        music.add(extra)
    if ambience:
        lines.append(_line(f"Ambience beds ({len(ambience)} × 30 s loop)", len(ambience), "sound",
                           s.cost_sound_per_generation))
    if music:
        lines.append(_line(f"Music ({len(music)} track{'s' if len(music) > 1 else ''})", len(music), "track",
                           s.cost_music_per_generation))

    if skipped:
        lines.append(_line(f"Already generated ({skipped} lines)", skipped, "line", 0.0))
    lines.append(_line("Mix, loudness and export (FFmpeg)", 1, "render", 0.0))
    total = round(sum(line["cost"] for line in lines), 4)
    return {
        "currency": "USD",
        "total": total,
        "lines": lines,
        "is_estimate": True,
        "minutes": round(doc.est_seconds / 60.0, 1),
        "voiced_chars": doc.voiced_chars,
        "note": "Voices, sound effects, beds and music are paid once; anything already generated costs "
                "nothing again, and mixing or re-rendering is free.",
    }

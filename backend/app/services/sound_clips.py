"""Sound Studio clips: the voices and one-shot sound effects of a story.

A story's script (services/story_script.py) is cut into clips (build):
consecutive lines of the same speaker with the same tags and placement
merge into one voice clip of at most MAX_CLIP_CHARS characters (a longer
line is split at sentence ends), and every `[sfx: ...]` cue is a sound
clip. Cue lines (sound effects, pauses, beds) are never sent to a voice.

Every clip has a fingerprint: the text and tags, the voice, provider,
model, voice settings, seed and output format of a voice clip; the prompt,
seconds, loop and model of a sound. A finished clip with the same
fingerprint is reused for free, so editing one line re-bills only the clip
it is in, and Resume pays only for the clips still missing.

The clips job (story_clips_job, a claim with status 'generating') goes
clip by clip: before each paid call it re-reads the story's status in a
fresh session (a Cancel stops it there) and asks spend.over_budget_with
about that one call (over the cap: a failure Resume continues, "Budget
reached; raise the cap and resume"). Each clip is committed as soon as it
is made, then a progress event goes out.

Each voice clip is post-processed with ffmpeg (process_voice): 48 kHz
pcm_f32le WAV, silence trimmed at both ends, for whispered styles a 70 Hz
high-pass and a de-esser, then an EBU R128 measurement and a gain so every
clip of one character sits at the same loudness. A QA gate (qa) checks the
duration per character against the style's band, silence and clipping; a
failure is retried once with a new seed, a second failure is flagged on
the clip (shown in the editor), never a failure of the story.

One-shot sound effects (loop=False, through the guarded sound provider)
are cached in sound_assets by (normalised prompt, seconds, loop, model):
the same door sounds the same in every story and is paid for once.

Safety: prepare() runs, before anything is queued or paid, the checks a
save runs (sound_studio.check_script_text: the spoken words, what no voice
reads, the whole-story minors check and, on a clean deployment, the
explicit-content check), then story_script.check_story over the clips as
they will be sent, with the voice each character is read with, whose pass
the provider guards require for every line and sound prompt.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from . import sound_estimate, sound_studio, story_script
from .sound_studio import CLIPS_KEY, PREVIEW_KEY, REGEN_KEY, TASK_KEY
from .story_script import Item, StoryDoc

log = logging.getLogger("avs.sound_clips")

# The longest voice clip, in characters (under the safety check's windows,
# so every clip is judged whole).
MAX_CLIP_CHARS = 1800
# What ElevenLabs is asked for (part of the fingerprint).
OUTPUT_FORMAT = "mp3_44100_128"
SAMPLE_RATE = 48000
ELEVENLABS_MODELS = ("eleven_v4", "eleven_v3", "eleven_v4_turbo")
VOICE_PROVIDERS = ("elevenlabs", "openrouter", "mock")
# Styles read in a whisper: high-pass and de-esser, a quieter target.
WHISPER_STYLES = ("asmr", "whisper", "sleep", "meditation")
WHISPER_TAGS = frozenset({"whispers", "whispering", "breathy", "close to the mic", "murmurs"})
# Loudness of every clip of a character (LUFS) before the mix and master.
TARGET_LUFS = -20.0
WHISPER_TARGET_LUFS = -24.0
# The duration per spoken character a clip may have (seconds), by style.
SLOW_BAND = (0.035, 0.30)
DEFAULT_BAND = (0.025, 0.22)
SHORT_CHARS = 25
SHORT_BAND = (0.3, 8.0)
BREATH = 0.3
PREVIEW_MAX_MINUTES = 3

BUDGET_REACHED = "Budget reached; raise the cap and resume"


class BudgetReached(RuntimeError):
    """The next paid call would go past the monthly cap."""


class Stopped(Exception):
    """Cancelled (or the claim was replaced) before the next paid call."""


class NothingToVoice(ValueError):
    """The script has no line or sound to make."""


# ---------- building the clips ----------


@dataclass
class Clip:
    """One clip to make: a voice clip (one or more lines merged, or a part
    of a long line) or a sound effect."""

    key: str
    kind: str  # voice | sfx
    chapter: int
    seq: int
    item: Item
    item_keys: list[str] = field(default_factory=list)
    lines: list[int] = field(default_factory=list)
    part: int = 0
    provider: str = ""
    model: str = ""
    voice_id: str = ""
    seed: Optional[int] = None
    voice_settings: Optional[dict] = None
    fingerprint: str = ""
    chars: int = 0
    cost: float = 0.0
    seconds: float = 0.0
    asset_key: str = ""

    @property
    def text(self) -> str:
        return self.item.voice_text if self.kind == "voice" else self.item.prompt

    def to_out(self) -> dict:
        it = self.item
        out = {"key": self.key, "kind": self.kind, "chapter": self.chapter, "seq": self.seq,
               "lines": list(self.lines), "items": list(self.item_keys), "part": self.part,
               "cost": round(self.cost, 4), "est_seconds": round(self.seconds, 1)}
        if self.kind == "voice":
            out.update({"speaker": it.speaker, "tags": list(it.tags), "text": it.text, "chars": self.chars,
                        "provider": self.provider, "model": self.model})
        else:
            out.update({"prompt": it.prompt, "seconds": float(it.seconds or story_script.DEFAULT_SFX_SECONDS),
                        "placement": it.placement, "asset_id": it.asset_id})
        return out


def _norm_prompt(prompt: str) -> str:
    return " ".join(str(prompt or "").lower().split())


def sound_model() -> str:
    """The model one-shot sounds are made with (part of the cache key)."""
    s = get_settings()
    return "mock/sound" if s.music_is_mocked() else str(s.elevenlabs_sound_model_id)


def sfx_key(prompt: str, seconds: float, loop: bool, model: str, variation: int = 0) -> str:
    """The cache key of a generated sound (SoundAsset.key_hash): the same
    prompt (normalised), length, loop and model is the same sound. A
    variation past the first (the effects tool, a regenerated cue) is its
    own sound."""
    parts: list = [_norm_prompt(prompt), round(float(seconds), 2), bool(loop), str(model or "")]
    if variation:
        parts.append(int(variation))
    return hashlib.sha256(json.dumps(parts).encode("utf-8")).hexdigest()


_SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+|(?<=[.!?…][\"'”’)])\s+")


def split_text(text: str, limit: int) -> list[str]:
    """`text` in pieces of at most `limit` characters, cut at sentence ends
    (a sentence longer than that between words)."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    buf = ""
    for sent in _SENTENCE_RE.split(text):
        while len(sent) > limit:
            cut = sent.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            if buf:
                out.append(buf)
                buf = ""
            out.append(sent[:cut].strip())
            sent = sent[cut:].strip()
        if not sent:
            continue
        if buf and len(buf) + 1 + len(sent) > limit:
            out.append(buf)
            buf = sent
        else:
            buf = f"{buf} {sent}".strip()
    if buf:
        out.append(buf)
    return [p for p in out if p]


def _lead(tags: list[str]) -> str:
    return "".join(f"[{t}] " for t in tags)


def _mergeable(a: Item, b: Item) -> bool:
    return (a.speaker == b.speaker and list(a.tags) == list(b.tags) and a.pan == b.pan
            and a.chapter == b.chapter and not a.instructions and not b.instructions
            and bool(a.text.strip()) and bool(b.text.strip()))


def _wpm(style: str) -> float:
    return float(story_script.WPM_BY_STYLE.get((style or "").lower(), story_script.DEFAULT_WPM))


def build(doc: StoryDoc, cast: Optional[dict] = None, settings: Optional[dict] = None) -> list[Clip]:
    """The clips of a parsed story, in order (see the module notes)."""
    settings = dict(settings or {})
    cast = cast if isinstance(cast, dict) else {}
    wpm = _wpm(doc.style or str(settings.get("style") or ""))
    clips: list[Clip] = []
    run: list[tuple[int, Item]] = []

    def voice(items: list[tuple[int, Item]]) -> None:
        first = items[0][1]
        lead = _lead(first.tags)
        joined = " ".join(" ".join(i.text.split()) for _, i in items)
        pieces = split_text(joined, max(200, MAX_CLIP_CHARS - len(lead))) if joined else [""]
        keys = [i.key for _, i in items]
        for part, piece in enumerate(pieces):
            item = Item(kind="speech", line=first.line, chapter=first.chapter, speaker=first.speaker,
                        tags=list(first.tags), pan=first.pan, text=piece, instructions=first.instructions)
            raw = "\0".join(keys) + f"\0{part if len(pieces) > 1 else ''}"
            item.key = "c" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:23]
            p = sound_estimate.voice_params(first.speaker, cast, settings)
            clip = Clip(key=item.key, kind="voice", chapter=first.chapter, seq=items[0][0] * 100 + part,
                        item=item, item_keys=keys, lines=sorted({i.line for _, i in items}), part=part,
                        provider=p["provider"], model=p["model"], voice_id=p["voice"],
                        seed=int(p["seed"]) if isinstance(p.get("seed"), (int, float)) else None,
                        voice_settings=p.get("voice_settings") or None)
            clip.fingerprint = sound_estimate._digest(
                "voice-clip", item.voice_text, item.tags, clip.voice_id, clip.provider, clip.model,
                clip.voice_settings, clip.seed, OUTPUT_FORMAT, item.instructions)
            clip.chars = sound_estimate.billed_chars(item, clip.provider, clip.model)
            clip.cost = clip.chars / 1000.0 * sound_estimate.voice_rate(clip.provider, clip.model)
            clip.seconds = story_script.item_seconds(item, wpm)
            clips.append(clip)

    def flush() -> None:
        nonlocal run
        if run:
            voice(run)
        run = []

    model = sound_model()
    for n, it in enumerate(doc.items):
        if it.kind == "comment":
            continue
        if it.kind == "speech":
            if run and _mergeable(run[-1][1], it):
                lead = _lead(it.tags)
                size = len(lead) + sum(len(" ".join(i.text.split())) + 1 for _, i in run) + len(it.text)
                if size <= MAX_CLIP_CHARS:
                    run.append((n, it))
                    continue
            flush()
            run = [(n, it)]
            continue
        flush()
        if it.kind == "sfx":
            secs = float(it.seconds or story_script.DEFAULT_SFX_SECONDS)
            clip = Clip(key=it.key, kind="sfx", chapter=it.chapter, seq=n * 100, item=it, item_keys=[it.key],
                        lines=[it.line], provider="library" if it.asset_id else "", model=model,
                        seconds=secs if it.placement == "after" else 0.0)
            clip.asset_key = "" if it.asset_id else sfx_key(it.prompt, secs, False, model)
            clip.fingerprint = sound_estimate._digest("sfx-clip", it.asset_id or "", _norm_prompt(it.prompt), secs,
                                                      False, "" if it.asset_id else model)
            clip.cost = 0.0 if it.asset_id else secs * float(get_settings().cost_sound_per_second)
            clips.append(clip)
    flush()
    return clips


def plan(story: models.SoundStory) -> tuple[StoryDoc, list[Clip]]:
    doc = sound_studio.parse_story(story)
    return doc, build(doc, story.cast_json, story.settings_json)


def clip_doc(doc: StoryDoc, clips: list[Clip]) -> StoryDoc:
    """The story as its clips will be sent (merged lines, split parts, the
    sound prompts): what check_story judges and its pass covers."""
    items = [c.item for c in clips if c.kind == "voice" or (c.kind == "sfx" and c.item.prompt)]
    return StoryDoc(chapters=[], cast=doc.cast, beds=[], items=items, warnings=[], est_seconds=doc.est_seconds,
                    style=doc.style)


def voices_for(doc: StoryDoc, cast: Optional[dict], settings: Optional[dict]) -> dict:
    """{NAME: {"voice_id", "provider"}}: the voice each character is read
    with (check_story binds its pass to them)."""
    out = {}
    for name in doc.cast:
        p = sound_estimate.voice_params(name, cast, settings)
        out[name] = {"voice_id": p["voice"], "provider": p["provider"]}
    return out


# ---------- safety, gates and prices ----------


def prepare(story: models.SoundStory) -> tuple[StoryDoc, list[Clip], "story_script.StoryCheck"]:
    """Every check before anything is voiced or generated (a refusal costs
    $0): the checks a save runs (sound_studio.check_script_text), then
    check_story over the clips as they will be sent, with each
    character's voice. Returns (doc, clips, the story check whose context
    and pass go to every provider call). Raises UnsafeScriptError, or
    NothingToVoice."""
    settings = dict(story.settings_json or {})
    style = str(settings.get("style") or "")
    if not (story.script_text or "").strip():
        raise NothingToVoice("There is no script to voice yet.")
    doc = sound_studio.check_script_text(story.script_text or "", style=style, title=story.title or "",
                                         premise=story.premise or "", settings=settings)
    doc = doc or sound_studio.parse_story(story)
    clips = build(doc, story.cast_json, settings)
    if not clips:
        raise NothingToVoice("There is nothing to voice: the script has no lines or sound effects.")
    check = story_script.check_story(clip_doc(doc, clips), title=story.title or "", premise=story.premise or "",
                                     voices=voices_for(doc, story.cast_json, settings))
    return doc, clips, check


def require_providers(clips: Iterable[Clip]) -> None:
    """The providers the clips need must be servable (ProviderUnavailable,
    a 409, before anything is claimed). Raises ValueError for an unknown
    provider or an OpenRouter voice without a model."""
    from . import provider_gate

    routes = {(c.provider, c.model) for c in clips if c.kind == "voice"}
    for provider, model in sorted(routes):
        if provider not in VOICE_PROVIDERS:
            raise ValueError(f"unknown voice provider: {provider!r}")
        if provider == "mock":
            continue
        if provider == "openrouter" and not model:
            raise ValueError("an OpenRouter voice needs a model")
        provider_gate.require_model("tts", "elevenlabs" if provider == "elevenlabs" else model)
    if any(c.kind == "sfx" and not c.item.asset_id for c in clips):
        provider_gate.require_sound()


def _rows(db: Session, story_id: int) -> list[models.SoundClip]:
    return (db.query(models.SoundClip).filter(models.SoundClip.story_id == story_id)
            .order_by(models.SoundClip.id).all())


def _usable(row: models.SoundClip) -> bool:
    return bool(row.selected and row.status in ("done", "flagged") and row.path and Path(row.path).exists())


def _cached_assets(db: Session, keys: Iterable[str]) -> dict[str, models.SoundAsset]:
    keys = [k for k in set(keys) if k]
    if not keys:
        return {}
    rows = db.query(models.SoundAsset).filter(models.SoundAsset.key_hash.in_(keys)).all()
    return {r.key_hash: r for r in rows if r.path and Path(r.path).exists()}


def is_done(clip: Clip, rows: list[models.SoundClip]) -> bool:
    return any(_usable(r) and r.fingerprint == clip.fingerprint for r in rows)


def estimate(db: Session, story: models.SoundStory, *, clips: Optional[list[Clip]] = None,
             keys: Optional[set[str]] = None, regenerate: bool = False) -> dict:
    """What making the clips costs now (the estimate line shape): voices by
    model and characters, sounds by the second. A clip already made (the
    same fingerprint), a sound in the shared cache, or a library sound is
    $0. `keys`: only those clips (a preview, a regenerate); `regenerate`:
    price them as new takes (nothing is reused)."""
    s = get_settings()
    if clips is None:
        clips = plan(story)[1]
    chosen = [c for c in clips if keys is None or c.key in keys]
    rows = [] if regenerate else _rows(db, story.id)
    cached = {} if regenerate else _cached_assets(db, (c.asset_key for c in chosen))
    groups: dict[tuple[str, str], dict] = {}
    sfx_secs = 0.0
    sfx_n = 0
    skipped = 0
    seen: set[str] = set()
    for c in chosen:
        if not regenerate and (is_done(c, rows) or (c.kind == "sfx" and (c.item.asset_id or c.asset_key in cached))):
            skipped += 1
            continue
        if c.kind == "voice":
            g = groups.setdefault((c.provider, c.model), {"chars": 0, "speakers": [], "clips": 0})
            g["chars"] += c.chars
            g["clips"] += 1
            if c.item.speaker not in g["speakers"]:
                g["speakers"].append(c.item.speaker)
        else:
            if c.asset_key in seen:
                skipped += 1
                continue
            seen.add(c.asset_key)
            sfx_secs += float(c.item.seconds or story_script.DEFAULT_SFX_SECONDS)
            sfx_n += 1
    lines: list[dict] = []
    for (provider, model), g in sorted(groups.items()):
        rate = sound_estimate.voice_rate(provider, model) / 1000.0
        who = ", ".join(n.title() for n in g["speakers"][:4]) + (" …" if len(g["speakers"]) > 4 else "")
        label = (f"Voice: {who} ({provider}{' ' + model if model else ''}, {g['clips']} clip"
                 f"{'s' if g['clips'] != 1 else ''}, {g['chars']:,} characters)")
        lines.append(sound_estimate._line(label, g["chars"], "char", rate))
    if sfx_n:
        lines.append(sound_estimate._line(f"Sound effects ({sfx_n}, {sfx_secs:g} s)", sfx_secs, "sec",
                                          float(s.cost_sound_per_second)))
    if skipped:
        lines.append(sound_estimate._line(f"Already made ({skipped} clip{'s' if skipped != 1 else ''})",
                                          skipped, "clip", 0.0))
    total = round(sum(line["cost"] for line in lines), 4)
    return {"currency": "USD", "total": total, "lines": lines, "is_estimate": True, "clips": len(chosen),
            "missing": len(chosen) - skipped,
            "note": "Clips already made cost nothing again. A clip that fails its check is retried once "
                    "(paid again). Beds, music and the mix come with the render."}


# ---------- audio ----------


def _ffmpeg(args: list[str], timeout: float = 600.0) -> str:
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-y", *args], capture_output=True, text=True,
                          errors="replace", timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr[-600:]}")
    return proc.stderr


_STAT_RE = {
    "peak": re.compile(r"Peak level dB:\s*(-?[\d.]+|-inf)"),
    "rms": re.compile(r"RMS level dB:\s*(-?[\d.]+|-inf)"),
    "peak_count": re.compile(r"Peak count:\s*([\d.]+)"),
    "samples": re.compile(r"Number of samples:\s*([\d.]+)"),
    "lufs": re.compile(r"I:\s*(-?[\d.]+)\s*LUFS"),
}


def _num(pattern: re.Pattern, text: str, default: float) -> float:
    found = pattern.findall(text or "")
    if not found:
        return default
    v = found[-1]
    return float("-inf") if v == "-inf" else float(v)


def measure(path: Path) -> dict:
    """Loudness and levels of an audio file: integrated loudness (LUFS),
    peak and RMS (dBFS), the samples at the peak and the duration."""
    try:
        log_ = _ffmpeg(["-i", str(path), "-af", "astats=metadata=0,ebur128=peak=sample", "-f", "null", "-"])
    except RuntimeError:
        return {"lufs": -70.0, "peak": float("-inf"), "rms": float("-inf"), "peak_count": 0.0, "samples": 0.0,
                "duration": 0.0}
    out = {k: _num(rx, log_, 0.0 if k in ("peak_count", "samples") else float("-inf"))
           for k, rx in _STAT_RE.items()}
    if out["lufs"] == float("-inf"):
        out["lufs"] = -70.0
    out["duration"] = _duration(path)
    return out


def _duration(path: Path) -> float:
    proc = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                           "default=noprint_wrappers=1:nokey=1", str(path)], capture_output=True, text=True)
    try:
        return max(0.0, float((proc.stdout or "0").strip() or 0.0))
    except ValueError:
        return 0.0


def _clipped(stats: dict) -> bool:
    samples = max(1.0, float(stats.get("samples") or 0.0))
    return float(stats.get("peak") or float("-inf")) >= -0.1 and \
        float(stats.get("peak_count") or 0.0) >= max(20.0, samples * 0.0005)


def _silent(stats: dict) -> bool:
    return float(stats.get("duration") or 0.0) < 0.2 or float(stats.get("rms") or float("-inf")) < -60.0


_TRIM = ("silenceremove=start_periods=1:start_duration=0:start_threshold=-50dB:detection=peak,areverse,"
         "silenceremove=start_periods=1:start_duration=0:start_threshold=-50dB:detection=peak,areverse")


def process_voice(raw: bytes, out: Path, *, whisper: bool, target: float) -> dict:
    """A provider's voice audio as a clip: 48 kHz pcm_f32le mono WAV,
    silence trimmed at both ends, a 70 Hz high-pass and a de-esser for a
    whisper, then a gain to `target` LUFS (never past -1 dBFS peak)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in.audio"
        src.write_bytes(raw)
        raw_stats = measure(src)
        chain = [_TRIM] + (["highpass=f=70", "deesser"] if whisper else [])
        mid = Path(td) / "mid.wav"
        _ffmpeg(["-i", str(src), "-af", ",".join(chain), "-ac", "1", "-ar", str(SAMPLE_RATE),
                 "-c:a", "pcm_f32le", str(mid)])
        stats = measure(mid) if mid.exists() and mid.stat().st_size > 100 else \
            {"lufs": -70.0, "peak": float("-inf"), "rms": float("-inf"), "duration": 0.0}
        silent = _silent(stats)
        gain = 0.0
        if not silent:
            gain = max(-20.0, min(20.0, target - float(stats["lufs"])))
            gain = min(gain, -1.0 - float(stats["peak"]))
        if mid.exists() and mid.stat().st_size > 100:
            _ffmpeg(["-i", str(mid), "-af", f"volume={gain:.2f}dB", "-ac", "1", "-ar", str(SAMPLE_RATE),
                     "-c:a", "pcm_f32le", str(out)])
        else:
            _ffmpeg(["-f", "lavfi", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=mono", "-t", "0.05", "-c:a", "pcm_f32le",
                     str(out)])
    return {"duration": round(float(stats.get("duration") or 0.0), 3), "lufs": round(float(stats["lufs"]), 2),
            "gain_db": round(gain, 2), "target_lufs": target, "silent": silent, "clipped": _clipped(raw_stats)}


def process_sound(src: Path, out: Path) -> dict:
    """A sound effect as a clip: 48 kHz pcm_f32le stereo WAV."""
    out.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg(["-i", str(src), "-ac", "2", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_f32le", str(out)])
    stats = measure(out)
    return {"duration": round(float(stats.get("duration") or 0.0), 3), "lufs": round(float(stats["lufs"]), 2),
            "silent": _silent(stats), "clipped": _clipped(stats)}


def band(style: str) -> tuple[float, float]:
    return SLOW_BAND if (style or "").lower() in WHISPER_STYLES else DEFAULT_BAND


def qa(clip: Clip, stats: dict, style: str) -> Optional[str]:
    """Why a clip fails its check (None when it passes): silent, clipped,
    or a duration per character outside the style's band."""
    if stats.get("silent"):
        return "The clip came back silent."
    if stats.get("clipped"):
        return "The clip is clipped (distorted)."
    if clip.kind != "voice":
        return None
    chars = len(story_script.strip_tags(clip.item.text))
    dur = float(stats.get("duration") or 0.0)
    if chars <= 0:
        return None
    if chars < SHORT_CHARS:
        lo, hi = SHORT_BAND
        if not (lo <= dur <= hi):
            return f"The clip is {dur:.1f} s long, outside {lo:g}-{hi:g} s for so short a line."
        return None
    lo, hi = band(style)
    per = dur / chars
    if not (lo <= per <= hi):
        return (f"The clip is {dur:.1f} s for {chars} characters ({per:.3f} s each), outside the "
                f"{lo:g}-{hi:g} s the style allows.")
    return None


def _whisper(clip: Clip, style: str) -> bool:
    return (style or "").lower() in WHISPER_STYLES or bool(set(clip.item.tags) & WHISPER_TAGS)


def _target(clip: Clip, cast: Optional[dict], style: str) -> float:
    member = (cast or {}).get(clip.item.speaker) if isinstance(cast, dict) else None
    if isinstance(member, dict) and isinstance(member.get("loudness"), (int, float)):
        return max(-40.0, min(-10.0, float(member["loudness"])))
    return WHISPER_TARGET_LUFS if (style or "").lower() in WHISPER_STYLES else TARGET_LUFS


def story_dir(story_id: int) -> Path:
    d = get_settings().data_path / "sound" / "stories" / str(int(story_id))
    d.mkdir(parents=True, exist_ok=True)
    return d


def assets_dir() -> Path:
    d = get_settings().data_path / "sound" / "assets"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------- making clips ----------


def _new_seed() -> int:
    return random.SystemRandom().randrange(1, 2 ** 31)


def _retry_seed(clip: Clip, seed: Optional[int]) -> int:
    base = seed if seed is not None else int(clip.fingerprint[:8] or "0", 16)
    return (base + 7919) % (2 ** 31) or 1


def generate_sound(db: Session, prompt: str, seconds: float, *, loop: bool = False, variation: int = 0,
                   context: str = "", story_id: Optional[int] = None,
                   check_budget: Optional[Callable[[float], None]] = None) -> tuple[models.SoundAsset, bool]:
    """A generated sound, from the shared cache when the same prompt,
    length, loop and model was made before (free), else through the
    guarded sound provider (paid; the prompt is checked there, with the
    story's `context` and pass for a story sound). Returns (asset, cached).
    The asset row is added to `db`, not committed."""
    from ..providers.registry import get_sound, unwrap

    s = get_settings()
    model = sound_model()
    key = sfx_key(prompt, seconds, loop, model, variation)
    hit = _cached_assets(db, [key]).get(key)
    if hit is not None:
        return hit, True
    cost = float(s.cost_sound_per_generation) if loop else float(seconds) * float(s.cost_sound_per_second)
    if check_budget is not None:
        check_budget(cost)
    provider = get_sound()
    audio = provider.generate(prompt=prompt, duration_seconds=float(seconds), loop=bool(loop), context=context)
    path = assets_dir() / f"{key[:32]}.mp3"
    path.write_bytes(audio)
    inner = unwrap(provider)
    row = db.query(models.SoundAsset).filter(models.SoundAsset.key_hash == key).first()
    if row is None:
        row = models.SoundAsset(key_hash=key)
        db.add(row)
    row.kind = "ambience" if loop else "sfx"
    row.name = " ".join(prompt.split())[:200]
    row.prompt = prompt
    row.seconds = float(seconds)
    row.loop = bool(loop)
    row.provider = str(getattr(inner, "name", "") or "")
    row.model = model
    row.path = str(path)
    row.source = "generated"
    row.story_id = story_id
    row.cost_usd = round(cost, 6)
    db.flush()
    return row, False


@dataclass
class Made:
    row: models.SoundClip
    paid: int = 0  # paid provider calls


def make_clip(db: Session, story: models.SoundStory, clip: Clip, check: str, *,
              alive: Callable[[], bool], fresh: bool = False) -> Made:
    """Make one clip (a new take, selected), with the QA gate: one retry
    with a new seed, then flagged. Before each paid call: `alive()` (else
    Stopped) and the budget (else BudgetReached). The row is added and
    flushed, not committed. `fresh`: a regenerate (a new seed; a sound is
    made anew, not taken from the cache)."""
    from ..providers.registry import get_tts_for
    from . import spend

    style = sound_studio.style_of(story)
    existing = [r for r in _rows(db, story.id) if r.cue_key == clip.key]
    take = max([r.take_no or 0 for r in existing] or [0]) + 1
    d = story_dir(story.id) / "clips"
    paid = 0

    def budget(cost: float) -> None:
        if not alive():
            raise Stopped()
        msg = spend.over_budget_with(db, cost)
        if msg:
            raise BudgetReached(msg)

    params: dict = {"items": clip.item_keys, "lines": clip.lines, "part": clip.part}
    reason: Optional[str] = None
    attempts = 0
    stats: dict = {}
    cost = 0.0
    out = d / f"{clip.key}-t{take}-{uuid.uuid4().hex[:8]}.wav"
    if clip.kind == "voice":
        seed = _new_seed() if fresh else clip.seed
        whisper = _whisper(clip, style)
        target = _target(clip, story.cast_json, style)
        for attempt in (1, 2):
            budget(clip.cost)
            tts = get_tts_for(clip.provider, clip.model, clip.voice_id)
            settings: dict = {}
            if clip.provider == "elevenlabs":
                settings["output_format"] = OUTPUT_FORMAT
            if seed is not None:
                settings["seed"] = int(seed)
            if clip.voice_settings:
                settings["voice_settings"] = dict(clip.voice_settings)
            if clip.item.instructions:
                settings["instructions"] = clip.item.instructions
            raw = tts.synthesize(text=clip.item.voice_text, voice_id=clip.voice_id, settings=settings,
                                 context=check, speaker=clip.item.speaker)
            paid += 1
            attempts += 1
            cost += clip.cost
            stats = process_voice(raw, out, whisper=whisper, target=target)
            reason = qa(clip, stats, style)
            params.update({"seed": seed, "whisper": whisper})
            if not reason:
                break
            log.info("sound story %s: clip %s failed QA (%s); %s", story.id, clip.key, reason,
                     "retrying with a new seed" if attempt == 1 else "flagged")
            seed = _retry_seed(clip, seed)
    else:
        attempts = 1
        if clip.item.asset_id:
            asset = db.get(models.SoundAsset, int(clip.item.asset_id))
            if asset is None or not asset.path or not Path(asset.path).exists():
                raise ValueError(f"Library sound #{clip.item.asset_id} isn't there any more.")
            cached = True
        else:
            secs = float(clip.item.seconds or story_script.DEFAULT_SFX_SECONDS)
            variation = take if fresh else 0
            asset, cached = generate_sound(db, clip.item.prompt, secs, loop=False, variation=variation,
                                           context=check, story_id=story.id, check_budget=budget)
            if not cached:
                paid += 1
                cost = float(asset.cost_usd or 0.0)
        params.update({"asset_id": asset.id, "cached": cached})
        stats = process_sound(Path(asset.path), out)
        reason = qa(clip, stats, style)
    db.query(models.SoundClip).filter(models.SoundClip.story_id == story.id,
                                      models.SoundClip.cue_key == clip.key).update(
        {"selected": False}, synchronize_session=False)
    params["qa"] = stats
    row = models.SoundClip(
        story_id=story.id, cue_key=clip.key, chapter=clip.chapter, seq=clip.seq, kind=clip.kind, take_no=take,
        selected=True, fingerprint=clip.fingerprint, text_or_prompt=clip.text, params_json=_jsonable(params),
        path=str(out), duration_seconds=float(stats.get("duration") or 0.0), provider=clip.provider or
        ("cache" if params.get("cached") else ""), model=clip.model, cost_usd=round(cost, 6), attempts=attempts,
        status="flagged" if reason else "done", error=reason or "")
    db.add(row)
    db.flush()
    return Made(row, paid)


def _jsonable(x):  # noqa: ANN001, ANN202
    return json.loads(json.dumps(x, default=str).replace("-Infinity", "null").replace("Infinity", "null"))


def reuse(db: Session, story: models.SoundStory, clip: Clip, rows: list[models.SoundClip]) -> Optional[models.SoundClip]:
    """A free clip for `clip`: its own selected take with the same
    fingerprint (nothing to do), or another cue's with the same audio
    (copied as a take of this one). None when it has to be made."""
    own = [r for r in rows if r.cue_key == clip.key and _usable(r) and r.fingerprint == clip.fingerprint]
    if own:
        return own[0]
    other = next((r for r in rows if _usable(r) and r.fingerprint == clip.fingerprint), None)
    if other is None:
        return None
    take = max([r.take_no or 0 for r in rows if r.cue_key == clip.key] or [0]) + 1
    db.query(models.SoundClip).filter(models.SoundClip.story_id == story.id,
                                      models.SoundClip.cue_key == clip.key).update(
        {"selected": False}, synchronize_session=False)
    row = models.SoundClip(
        story_id=story.id, cue_key=clip.key, chapter=clip.chapter, seq=clip.seq, kind=clip.kind, take_no=take,
        selected=True, fingerprint=clip.fingerprint, text_or_prompt=clip.text,
        params_json={**(other.params_json or {}), "items": clip.item_keys, "lines": clip.lines,
                     "reused_from": other.id},
        path=other.path, duration_seconds=other.duration_seconds, provider=other.provider, model=other.model,
        cost_usd=0.0, attempts=0, status=other.status, error=other.error or "")
    db.add(row)
    db.flush()
    rows.append(row)
    return row


def _sfx_cached(db: Session, story: models.SoundStory, clip: Clip) -> bool:
    return clip.kind == "sfx" and bool(clip.item.asset_id or _cached_assets(db, [clip.asset_key]))


def run_clips(db: Session, story: models.SoundStory, clips: list[Clip], check: str, *,
              alive: Callable[[], bool], save: Callable[[dict], None]) -> dict:
    """Make every missing clip of `clips`, in order: reused for free when a
    clip with the same fingerprint exists (or the sound is in the cache),
    else made (make_clip). After each clip `save(counts)` commits it.
    Raises Stopped, BudgetReached, or what a provider raised."""
    rows = _rows(db, story.id)
    counts = {"total": len(clips), "done": 0, "made": 0, "reused": 0, "flagged": 0, "paid": 0}
    for clip in clips:
        row = reuse(db, story, clip, rows)
        if row is not None:
            counts["reused"] += 1
        else:
            cached = _sfx_cached(db, story, clip)
            made = make_clip(db, story, clip, check, alive=alive)
            row = made.row
            rows.append(row)
            counts["paid"] += made.paid
            counts["made" if not cached else "reused"] += 1
        counts["done"] += 1
        counts["flagged"] += 1 if row.status == "flagged" else 0
        save(dict(counts))
    return counts


# ---------- the clips job ----------


def _progress(story: models.SoundStory, **fields) -> dict:  # noqa: ANN003
    progress = dict(story.progress_json or {})
    progress[CLIPS_KEY] = {**(progress.get(CLIPS_KEY) or {}), **fields}
    return progress


def generate_story(db: Session, story_id: int, gen: str) -> None:
    """The clips job's body (claim consumed by the caller; `gen` is the
    claim it holds): every missing clip, committed one by one, a progress
    event after each. Ends 'ready', 'cancelled' (Cancel, checked before
    each paid call) or 'failed' (Resume continues)."""
    from .safety import UnsafeScriptError

    story = db.get(models.SoundStory, story_id)
    if story is None:
        return
    sound_studio.story_event(db, story, "started", detail="Voices")
    db.commit()

    def alive() -> bool:
        return sound_studio._alive(story_id, gen)

    def save(counts: dict) -> None:
        db.refresh(story)
        done = sound_studio._held(db.query(models.SoundStory), story_id, gen).update(
            {"progress_json": _progress(story, **counts), "heartbeat_at": datetime.utcnow()},
            synchronize_session=False)
        if done != 1:
            db.rollback()
            raise sound_studio._Superseded()
        db.commit()
        db.refresh(story)
        sound_studio.story_event(db, story, "progress",
                                 detail=f"Voices: {counts['done']} of {counts['total']} clips")
        db.commit()

    def finish(status: str, error: str = "", *, held: bool = False) -> None:
        scope = sound_studio._held if held else sound_studio._mine
        db.refresh(story)
        progress = _progress(story, finished=status == "ready", error=error)
        done = scope(db.query(models.SoundStory), story_id, gen).update(
            {"status": status, "error": error, "claim_token": "", "progress_json": progress,
             "actual_cost": _actual(db, story_id)}, synchronize_session=False)
        db.commit()
        if done == 1:
            db.refresh(story)
            sound_studio.story_event(db, story, "completed" if status == "ready" else status, detail="Voices",
                                     error=error)
            db.commit()

    try:
        _, clips, check = prepare(story)
        db.refresh(story)
        sound_studio._held(db.query(models.SoundStory), story_id, gen).update(
            {"progress_json": _progress(story, total=len(clips), finished=False, error=""),
             "heartbeat_at": datetime.utcnow()}, synchronize_session=False)
        db.commit()
        run_clips(db, story, clips, check, alive=alive, save=save)
        # Ready even when Cancel came while the last clip was made.
        finish("ready", held=True)
    except (Stopped, sound_studio._Superseded):
        db.rollback()
        log.info("sound story %s: clips stopped (cancelled or claimed again)", story_id)
    except BudgetReached:
        db.rollback()
        finish("failed", BUDGET_REACHED)
    except (UnsafeScriptError, NothingToVoice) as e:
        db.rollback()
        finish("failed", str(e))
    except Exception as e:  # noqa: BLE001
        db.rollback()
        log.exception("sound story %s: clips failed", story_id)
        finish("failed", f"Generating stopped: {str(e)[:300]}. Resume continues where it left off.")


def _actual(db: Session, story_id: int) -> float:
    from sqlalchemy import func

    total = db.query(func.coalesce(func.sum(models.SoundClip.cost_usd), 0.0)).filter(
        models.SoundClip.story_id == story_id).scalar()
    return round(float(total or 0.0), 4)


def can_resume(story: models.SoundStory) -> bool:
    """A clips run that stopped part-way (cancelled, failed or swept):
    Resume makes only the clips still missing."""
    progress = story.progress_json or {}
    return story.status in ("cancelled", "failed") and progress.get(TASK_KEY) == CLIPS_KEY


def progress_out(story: models.SoundStory) -> dict:
    c = (story.progress_json or {}).get(CLIPS_KEY) or {}
    return {"total": int(c.get("total") or 0), "done": int(c.get("done") or 0),
            "flagged": int(c.get("flagged") or 0), "made": int(c.get("made") or 0),
            "reused": int(c.get("reused") or 0), "finished": bool(c.get("finished")),
            "can_resume": can_resume(story), "preview": _preview_out(story)}


# ---------- regenerate, takes and preview (synchronous, under the story's lock) ----------


class _Lock:
    """The story's claim held by a request (status 'generating', task
    regenerate or preview): no job, edit or second request meanwhile. The
    task and status it had come back when it is released."""

    def __init__(self, db: Session, story: models.SoundStory, task: str) -> None:
        self.db = db
        self.story = story
        progress = dict(story.progress_json or {})
        self.before = (progress.get(TASK_KEY), progress.get("previous"))
        token, self.previous = sound_studio.claim(db, story, task, status="generating")
        if not sound_studio.consume_claim(db, story.id, token, status="generating"):
            db.rollback()
            raise LookupError("this story is busy; wait or cancel first")
        db.commit()
        self.gen = sound_studio.running_token(token)

    def alive(self) -> bool:
        return sound_studio._alive(self.story.id, self.gen)

    def release(self) -> None:
        db = self.db
        db.rollback()
        db.refresh(self.story)
        progress = dict(self.story.progress_json or {})
        task, prev = self.before
        for k, v in ((TASK_KEY, task), ("previous", prev)):
            if v is None:
                progress.pop(k, None)
            else:
                progress[k] = v
        back = self.previous if self.previous not in sound_studio.BUSY else "ready"
        sound_studio._mine(db.query(models.SoundStory), self.story.id, self.gen).update(
            {"status": back, "claim_token": "", "progress_json": progress,
             "actual_cost": _actual(db, self.story.id)}, synchronize_session=False)
        db.commit()
        db.refresh(self.story)


def find(clips: list[Clip], key: str) -> Clip:
    for c in clips:
        if c.key == key:
            return c
    raise LookupError("no such clip in the current script")


def regenerate(db: Session, story: models.SoundStory, key: str, prepared: Optional[tuple] = None) -> models.SoundClip:
    """A new take of one clip or cue (a new seed; a sound made anew), made
    in the request under the story's lock, and selected. `prepared`: what
    prepare() returned for this story just now (else it runs here)."""
    _, clips, check = prepared or prepare(story)
    clip = find(clips, key)
    require_providers([clip])
    lock = _Lock(db, story, REGEN_KEY)
    try:
        with _scope(story.id):
            made = make_clip(db, story, clip, check, alive=lock.alive, fresh=True)
        db.commit()
        row_id = made.row.id
    finally:
        lock.release()
    return db.get(models.SoundClip, row_id)


def select_take(db: Session, story: models.SoundStory, clip_id: int) -> models.SoundClip:
    row = db.get(models.SoundClip, clip_id)
    if row is None or row.story_id != story.id:
        raise LookupError("take not found")
    if row.status not in ("done", "flagged") or not row.path or not Path(row.path).exists():
        raise ValueError("that take has no audio")
    db.query(models.SoundClip).filter(models.SoundClip.story_id == story.id,
                                      models.SoundClip.cue_key == row.cue_key).update(
        {"selected": False}, synchronize_session=False)
    row.selected = True
    db.commit()
    db.refresh(row)
    return row


def _scope(story_id: int):  # noqa: ANN202
    from . import spend

    return spend.scope(ref=sound_studio.ref(story_id))


def scope_clips(doc: StoryDoc, clips: list[Clip], *, chapter: Optional[int] = None,
                start_line: Optional[int] = None, end_line: Optional[int] = None,
                first_minutes: Optional[float] = None) -> list[Clip]:
    """The clips of a preview: one chapter, a range of script lines, or
    the first minutes (at most PREVIEW_MAX_MINUTES, by the estimate)."""
    if chapter is not None:
        return [c for c in clips if c.chapter == chapter]
    if start_line is not None or end_line is not None:
        a = int(start_line or 1)
        b = int(end_line or 10 ** 9)
        return [c for c in clips if any(a <= ln <= b for ln in c.lines)]
    minutes = min(float(first_minutes or PREVIEW_MAX_MINUTES), PREVIEW_MAX_MINUTES)
    limit = minutes * 60.0
    by_first: dict[str, list[Clip]] = {}
    for c in clips:
        by_first.setdefault(c.item_keys[0], []).append(c)
    t = 0.0
    out: list[Clip] = []
    for it in doc.items:
        if t >= limit:
            break
        if it.kind == "pause":
            t += float(it.seconds or 0.0)
            continue
        # A line merged into an earlier clip is counted with that clip.
        for c in by_first.get(it.key, []):
            out.append(c)
            t += c.seconds
    return out


def preview(db: Session, story: models.SoundStory, *, chapter: Optional[int] = None,
            start_line: Optional[int] = None, end_line: Optional[int] = None,
            first_minutes: Optional[float] = None, prepared: Optional[tuple] = None) -> dict:
    """Make only the clips of the scope (reused when already made) and mix
    them quickly (voices in order, sound effects after, under or at their
    offset, pauses as silence) into an MP3. Under the story's lock, in the
    request. `prepared`: what prepare() returned just now."""
    doc, clips, check = prepared or prepare(story)
    chosen = scope_clips(doc, clips, chapter=chapter, start_line=start_line, end_line=end_line,
                         first_minutes=first_minutes)
    if not chosen:
        raise NothingToVoice("Nothing to preview there.")
    require_providers(chosen)
    lock = _Lock(db, story, PREVIEW_KEY)
    try:
        with _scope(story.id):
            counts = run_clips(db, story, chosen, check, alive=lock.alive, save=lambda c: db.commit())
        rows = {r.cue_key: r for r in _rows(db, story.id) if _usable(r)}
        out = story_dir(story.id) / f"preview-{uuid.uuid4().hex[:8]}.mp3"
        duration = mix(doc, chosen, rows, out)
        db.refresh(story)
        progress = dict(story.progress_json or {})
        old = (progress.get("preview_audio") or {}).get("path")
        progress["preview_audio"] = {"path": str(out), "duration": round(duration, 2), "clips": len(chosen),
                                     "scope": {"chapter": chapter, "start_line": start_line, "end_line": end_line,
                                               "first_minutes": first_minutes},
                                     "created_at": datetime.utcnow().isoformat()}
        sound_studio._held(db.query(models.SoundStory), story.id, lock.gen).update(
            {"progress_json": progress}, synchronize_session=False)
        db.commit()
        if old and old != str(out):
            Path(old).unlink(missing_ok=True)
    finally:
        lock.release()
    return {"duration": round(duration, 2), "clips": len(chosen), "made": counts["made"],
            "reused": counts["reused"], "flagged": counts["flagged"]}


def _preview_out(story: models.SoundStory) -> Optional[dict]:
    p = (story.progress_json or {}).get("preview_audio") or None
    if not p or not p.get("path") or not Path(p["path"]).exists():
        return None
    return {k: p.get(k) for k in ("duration", "clips", "scope", "created_at")}


def preview_path(story: models.SoundStory) -> Optional[Path]:
    p = (story.progress_json or {}).get("preview_audio") or {}
    path = Path(p["path"]) if p.get("path") else None
    return path if path is not None and path.exists() else None


def mix(doc: StoryDoc, clips: list[Clip], rows: dict[str, models.SoundClip], out: Path) -> float:
    """A quick voice + sound mix of `clips` (ffmpeg adelay + amix; the full
    mixer is the render's). Returns its length in seconds."""
    by_first: dict[str, list[Clip]] = {}
    for c in clips:
        by_first.setdefault(c.item_keys[0], []).append(c)
    keys = {c.key for c in clips}
    first_idx = next((n for n, it in enumerate(doc.items) if it.key in by_first), 0)
    last_idx = max((n for n, it in enumerate(doc.items) if it.key in by_first), default=0)
    placed: list[tuple[float, str]] = []
    t = 0.0
    last_start = 0.0
    for it in doc.items[first_idx:last_idx + 1]:
        if it.kind == "pause":
            t += float(it.seconds or 0.0)
            continue
        for c in by_first.get(it.key, []):
            row = rows.get(c.key)
            if c.key not in keys or row is None:
                continue
            dur = float(row.duration_seconds or 0.0)
            if c.kind == "voice":
                placed.append((t, row.path))
                last_start = t
                t += dur + BREATH
            elif it.placement == "under":
                placed.append((last_start, row.path))
            elif it.placement == "offset":
                placed.append((last_start + float(it.offset or 0.0), row.path))
            else:
                placed.append((t, row.path))
                t += dur
    out.parent.mkdir(parents=True, exist_ok=True)
    if not placed:
        _ffmpeg(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "1", "-c:a", "libmp3lame", str(out)])
        return 1.0
    with tempfile.TemporaryDirectory() as td:
        graph = []
        for i, (start, _) in enumerate(placed):
            ms = max(0, int(round(start * 1000)))
            graph.append(f"[{i}:a]aresample={SAMPLE_RATE},aformat=channel_layouts=stereo,adelay={ms}|{ms}[a{i}]")
        graph.append("".join(f"[a{i}]" for i in range(len(placed)))
                     + f"amix=inputs={len(placed)}:normalize=0:dropout_transition=0,alimiter=limit=0.95[out]")
        script = Path(td) / "graph.txt"
        script.write_text(";\n".join(graph))
        args: list[str] = []
        for _, path in placed:
            args += ["-i", path]
        _ffmpeg([*args, "-filter_complex_script", str(script), "-map", "[out]", "-c:a", "libmp3lame",
                 "-b:a", "160k", str(out)], timeout=1800)
    return _duration(out)


# ---------- the effects tool ----------


def make_effects(db: Session, prompt: str, seconds: float, *, loop: bool, variations: int) -> list[dict]:
    """1-4 variations of a sound effect, saved to the Library (SoundAsset):
    variation 1 is the shared cache's sound (the same as a story's), the
    others their own. Checked before anything is paid (registry.audio_guard:
    the word filter, the minors check and, on a clean deployment, the
    explicit-content check)."""
    from ..providers.registry import audio_guard
    from . import spend

    audio_guard(prompt)

    def budget(cost: float) -> None:
        msg = spend.over_budget_with(db, cost)
        if msg:
            raise BudgetReached(msg)

    out = []
    with spend.scope(ref="sound_effects"):
        for v in range(max(1, min(4, int(variations)))):
            asset, cached = generate_sound(db, prompt, seconds, loop=loop, variation=v, check_budget=budget)
            db.commit()
            out.append({**asset_out(asset), "cached": cached})
    return out


def effects_estimate(db: Session, prompt: str, seconds: float, *, loop: bool, variations: int) -> dict:
    s = get_settings()
    model = sound_model()
    keys = [sfx_key(prompt, seconds, loop, model, v) for v in range(max(1, min(4, int(variations))))]
    cached = _cached_assets(db, keys)
    unit = float(s.cost_sound_per_generation) if loop else float(seconds) * float(s.cost_sound_per_second)
    n = sum(1 for k in keys if k not in cached)
    lines = [sound_estimate._line(f"Sound effect{'s' if n != 1 else ''} ({n} × {seconds:g} s"
                                  f"{', loop' if loop else ''})", n, "sound", unit)]
    if len(keys) - n:
        lines.append(sound_estimate._line(f"Already in the library ({len(keys) - n})", len(keys) - n, "sound", 0.0))
    return {"currency": "USD", "total": round(n * unit, 4), "lines": lines, "is_estimate": True}


def asset_out(a: models.SoundAsset) -> dict:
    return {"id": a.id, "kind": a.kind, "name": a.name or "", "prompt": a.prompt or "", "seconds": a.seconds,
            "loop": bool(a.loop), "provider": a.provider or "", "model": a.model or "", "source": a.source,
            "story_id": a.story_id, "cost_usd": a.cost_usd or 0.0,
            "created_at": a.created_at.isoformat() if a.created_at else None}


# ---------- output ----------


def take_out(r: models.SoundClip, story_id: int) -> dict:
    qa_ = (r.params_json or {}).get("qa") or {}
    return {"id": r.id, "take_no": r.take_no, "selected": bool(r.selected), "status": r.status,
            "error": r.error or "", "duration": r.duration_seconds, "provider": r.provider or "",
            "model": r.model or "", "cost_usd": r.cost_usd or 0.0, "attempts": r.attempts or 0,
            "fingerprint": r.fingerprint, "lufs": qa_.get("lufs"),
            "url": f"/api/sound/stories/{story_id}/clips/{r.id}/audio",
            "created_at": r.created_at.isoformat() if r.created_at else None}


def clips_out(db: Session, story: models.SoundStory) -> dict:
    """The cue blocks of the editor: every clip of the current script with
    its state (missing | done | flagged | outdated), its takes and the
    price of a new take."""
    doc, clips = plan(story)
    rows = _rows(db, story.id)
    by_key: dict[str, list[models.SoundClip]] = {}
    for r in rows:
        by_key.setdefault(r.cue_key, []).append(r)
    blocks = []
    for c in clips:
        takes = by_key.get(c.key, [])
        sel = next((r for r in takes if _usable(r)), None)
        if sel is None:
            state = "missing"
        elif sel.fingerprint != c.fingerprint:
            state = "outdated"
        else:
            state = sel.status
        blocks.append({**c.to_out(), "state": state, "selected": sel.id if sel else None,
                       "takes": [take_out(r, story.id) for r in sorted(takes, key=lambda r: r.take_no or 0)]})
    return {"clips": blocks, "progress": progress_out(story),
            "chapters": [{"index": ch.index, "title": ch.title} for ch in doc.chapters]}

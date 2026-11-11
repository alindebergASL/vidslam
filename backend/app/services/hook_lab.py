"""Hook lab: alternative openings of a finished storyboard video.

The hook is the first sentence of the spoken script. This module starts
with the text helpers (used by the plan schema for plans made before the
hook was stored, by publish metadata and by the editor); the variant
machinery builds on them.
"""
from __future__ import annotations

import re

# A sentence ends at ., !, ?, … (or a run of them, "?!", "..."), optionally
# followed by closing quotes or brackets, then whitespace or the end. A
# decimal point ("3.5") or a dot inside a URL is not followed by whitespace,
# so it is not an end. A line break ends the opening line too.
_END = re.compile(r"""[.!?…]+["'”’»)\]]*(?=\s|$)|\n""")


def split_first_sentence(text: str) -> tuple[str, str]:
    """(first sentence, the rest), both stripped. Without any sentence end
    the whole text is the first sentence."""
    text = (text or "").strip()
    if not text:
        return "", ""
    m = _END.search(text)
    if m is None:
        return text, ""
    first = text[: m.end()].strip()
    rest = text[m.end():].strip()
    if not first:  # a leading line break
        return split_first_sentence(rest)
    return first, rest


def first_sentence(text: str) -> str:
    """The first sentence of `text` (the opening line, the hook)."""
    return split_first_sentence(text)[0]


def rest_of_script(text: str) -> str:
    """`text` without its first sentence."""
    return split_first_sentence(text)[1]


def plan_hook(plan: dict | None) -> str:
    """The plan's hook: the stored one, else the first sentence of its
    spoken script (plans made before the hook was stored)."""
    plan = plan or {}
    return str(plan.get("hook") or "").strip() or first_sentence(str(plan.get("cleaned_voice_script") or ""))


_ENDS = re.compile(r"""[.!?…]+["'”’»)\]]*$""")


def replace_first_sentence(script: str, hook: str) -> str:
    """`script` with its opening line replaced by `hook`. A hook without end
    punctuation gets a period, so it isn't merged into the next sentence."""
    rest = rest_of_script(script)
    hook = (hook or "").strip()
    if not hook:
        return rest
    if rest and not _ENDS.search(hook):
        hook += "."
    return f"{hook} {rest}".strip() if rest else hook


# ======================================================================
# Variants: other openings of a finished render, the rest reused
# ======================================================================
#
# A variant swaps the first spoken line (and optionally shot 1) of a
# completed base render. Its voiceover is the new line, fitted into the
# original opening's time ("the window"), joined to the base voiceover from
# the window on, so everything after the opening is the same audio at the
# same times: later clips, lip-sync and captions stay valid. Its clips are
# the base render's (from its composition_json), with shot 1 kept, swapped
# for a take, made new (VideoJob purpose "hook_shot") or lip-synced again
# (purpose "hook_avatar"), and any later lip-synced shot that overlaps the
# window lip-synced again too. A variant's own Render has hook_variant_id
# set: it is never the project's current voiceover and never composed from
# the project's shots.

import hashlib  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
from datetime import datetime  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import TYPE_CHECKING, Optional  # noqa: E402

from sqlalchemy import update as sa_update  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from .. import models  # noqa: E402
from ..config import get_settings  # noqa: E402

if TYPE_CHECKING:
    from .captions import CaptionTiming
    from .pipeline import ComposeClip

log = logging.getLogger("avs.hook_lab")

STYLES = ("question", "bold_claim", "stat", "story", "contrarian", "custom", "original")
STYLE_LABELS = {
    "question": "Question", "bold_claim": "Bold claim", "stat": "Number or fact", "story": "Story",
    "contrarian": "Contrarian", "custom": "My own", "original": "Original opening",
}
# Styles the planner is asked for, in this order.
SUGGEST_STYLES = ("question", "bold_claim", "stat", "story", "contrarian")
SOURCES = ("keep", "take", "new", "lipsync")
MAX_PER_REQUEST = 3
MAX_PER_BASE = 12
TEMPO_MAX = 1.15  # a hook up to 15% longer than the window is sped up
TAKE_SHORT_OK = 0.5  # a shot-1 take this much shorter than its slot holds its last frame
WORD_SLACK = 0.30  # suggestions within ±30% of the opening's word count
PURPOSE_SHOT = "hook_shot"
PURPOSE_AVATAR = "hook_avatar"
EDITABLE = ("draft", "failed")
RUNNING = ("making", "ready", "composing")
ARCHIVED = "archived"  # deleted from the hook lab; its render is kept
YOUTUBE_NOTE = ("YouTube may treat near-identical uploads as repetitive content: post them days apart, "
                "or as unlisted tests.")


class HookLabError(ValueError):
    """A plain-language refusal; `status` is the HTTP status to answer with."""

    def __init__(self, msg: str, status: int = 409) -> None:
        super().__init__(msg)
        self.status = status


class HookTooLong(HookLabError):
    def __init__(self, overflow: float) -> None:
        super().__init__(f"This opening is about {overflow:.1f} s longer than the original; shorten it.", 422)
        self.overflow = overflow


def _digest(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _norm(text: str) -> str:
    return " ".join((text or "").split()).strip().lower()


def _words(text: str) -> int:
    return len([w for w in (text or "").split() if w])


# ---------- the base render ----------


def base_script(db: Session, render: models.Render) -> str:
    """The spoken script the render was made with (its composition_json),
    else the project's current one."""
    comp = render.composition_json or {}
    if comp.get("script"):
        return str(comp["script"])
    if comp.get("spoken_unknown"):
        return ""  # recomposed over an older voiceover whose script isn't known
    project = db.get(models.VideoProject, render.project_id)
    return str(((project.generated_plan_json or {}) if project else {}).get("cleaned_voice_script") or "")


def base_hook(db: Session, render: models.Render) -> str:
    comp = render.composition_json or {}
    if comp.get("hook"):
        return str(comp["hook"]).strip()
    if comp.get("spoken_unknown"):
        return ""
    project = db.get(models.VideoProject, render.project_id)
    return plan_hook(project.generated_plan_json if project else None)


def check_base(db: Session, render: Optional[models.Render], project_id: Optional[int] = None) -> models.Render:
    """The render can be the base of other openings: a finished storyboard
    render (not itself a variant) whose voiceover is on disk."""
    if render is None or (project_id is not None and render.project_id != project_id):
        raise HookLabError("render not found", 404)
    project = db.get(models.VideoProject, render.project_id)
    if project is None or project.mode == "ambient_loop":
        raise HookLabError("Other openings work on storyboard videos only.", 409)
    if render.hook_variant_id:
        raise HookLabError("This video is already another opening; start from the version it was made from.")
    if render.status != "completed":
        raise HookLabError("Only a finished version can get other openings.")
    if not render.audio_path or not Path(render.audio_path).is_file():
        raise HookLabError("This version has no voiceover on disk, so its opening can't be swapped.")
    from .pipeline import audio_overwritten

    if audio_overwritten(db, render):
        raise HookLabError("This version's voiceover file was replaced by a later version's, so its "
                           "opening can't be swapped. Use a newer version.")
    if not base_hook(db, render):
        raise HookLabError("This version has no spoken script, so it has no opening to swap.")
    return render


def base_clips(db: Session, render: models.Render) -> list["ComposeClip"]:
    """The body clips the base render was composed from (its
    composition_json). A render from before that was recorded: the project's
    current clips, when they are still the ones it was made from. Raises
    HookLabError("Re-compose this version first") otherwise."""
    from . import fingerprints
    from .pipeline import ComposeClip, _body_shots, shot_clips

    comp = render.composition_json or {}
    shots = comp.get("shots") or []
    if shots:
        clips = [ComposeClip(
            path=Path(str(s.get("clip_path") or "")),
            seconds=float(s["planned_seconds"]) if s.get("planned_seconds") else None,
            shot_id=s.get("shot_id"), shot_order=s.get("shot_order"), shot_type=str(s.get("shot_type") or ""),
            take_id=s.get("take_id"), fingerprint=str(s.get("fingerprint") or ""),
            provider_model=str(s.get("provider_model") or "")) for s in shots]
        if all(c.path.is_file() for c in clips):
            return clips
        raise HookLabError("Some clips of this version are gone. Re-compose this version first.")
    project = db.get(models.VideoProject, render.project_id)
    # Not recorded: only the newest version can be rebuilt from the
    # project's current clips and voiceover; an older one was made from
    # clips (and, before each voiceover had its own file, a voice.m4a)
    # that may since have been replaced.
    from .pipeline import _latest_audio_render

    R = models.Render
    newest = (db.query(R.id).filter(R.project_id == render.project_id, R.status == "completed",
                                    R.hook_variant_id.is_(None))
              .order_by(R.id.desc()).first())
    latest_audio = _latest_audio_render(db, render.project_id)
    if (newest is None or newest[0] != render.id
            or (latest_audio is not None and latest_audio.audio_path != render.audio_path)):
        raise HookLabError("Re-compose this version first: it was made before its clips were recorded, "
                           "and newer versions changed them.")
    try:
        clips = shot_clips(project)
    except ValueError:
        raise HookLabError("Re-compose this version first: its clips are not all on disk.") from None
    if any(fingerprints.shot_is_stale(s) for s in _body_shots(project)):
        raise HookLabError("The shots changed since this version was made. Re-compose this version first.")
    return clips


def base_settings(db: Session, render: models.Render) -> dict:
    """The compose look (CTA, captions, music, brand, disclosure) the base
    render was made with, so an opening differs from it only in its
    opening. A render from before that was recorded: the project's current
    look. Raises HookLabError when it can't be reproduced (its music or logo
    file is gone, or there is no usable plan to read it from)."""
    from ..schemas.storyboard import StoryboardPlan
    from .pipeline import compose_settings

    look = (render.composition_json or {}).get("settings")
    if not look:
        project = db.get(models.VideoProject, render.project_id)
        try:
            plan = StoryboardPlan.model_validate(project.generated_plan_json or {})
        except Exception:  # noqa: BLE001
            raise HookLabError("This version's storyboard can't be read any more. Re-compose it first.") from None
        return compose_settings(db, project, plan)
    for key, what in (("music_path", "music"), ("logo_path", "logo")):
        if look.get(key) and not Path(str(look[key])).is_file():
            raise HookLabError(f"The {what} this version was made with is gone, so another opening of it "
                               "wouldn't match. Re-compose this version first.")
    return dict(look)


def _clip_starts(clips: list["ComposeClip"], comp: Optional[dict] = None) -> list[tuple[float, float]]:
    """(start, length) of each body clip in the video."""
    from .pipeline import clip_seconds

    recorded = [s.get("seconds") for s in (comp or {}).get("shots") or []]
    out: list[tuple[float, float]] = []
    t = 0.0
    for i, c in enumerate(clips):
        length = float(recorded[i]) if i < len(recorded) and recorded[i] else clip_seconds(c)
        out.append((t, length))
        t += length
    return out


def _lipsynced(db: Session, clip: "ComposeClip") -> bool:
    """The clip is a lip-synced talking head (its take or shot says so)."""
    if clip.shot_type != "talking_head":
        return False
    if clip.take_id:
        take = db.get(models.ShotTake, clip.take_id)
        if take is not None:
            return take.provider == "lipsync"
    shot = db.get(models.VideoShot, clip.shot_id) if clip.shot_id else None
    return shot is not None and shot.provider == "lipsync"


# ---------- where the opening ends in the base voiceover ----------


def detect_silences(path: Path, *, noise_db: float = -35.0, min_seconds: float = 0.15) -> list[tuple[float, float]]:
    """Pauses in an audio file (ffmpeg silencedetect): [(start, end)]."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af",
         f"silencedetect=noise={noise_db}dB:d={min_seconds}", "-f", "null", "-"],
        capture_output=True, text=True)
    out: list[tuple[float, float]] = []
    start: Optional[float] = None
    for line in (proc.stderr or "").splitlines():
        if "silence_start:" in line:
            try:
                start = float(line.split("silence_start:")[1].split()[0])
            except (IndexError, ValueError):
                start = None
        elif "silence_end:" in line and start is not None:
            try:
                end = float(line.split("silence_end:")[1].split()[0].strip("|"))
            except (IndexError, ValueError):
                continue
            out.append((max(start, 0.0), end))
            start = None
    if start is not None:  # silent to the end
        from .renderer import _probe_duration

        out.append((max(start, 0.0), _probe_duration(path)))
    return out


_window_cache: dict[tuple, float] = {}
_window_lock = threading.Lock()


def estimate_window(script: str, audio_seconds: float) -> float:
    """Where the first sentence ends if speech were evenly paced."""
    script = (script or "").strip()
    first = first_sentence(script)
    if not script or not first or audio_seconds <= 0:
        return 0.0
    return audio_seconds * len(first) / len(script)


def find_hook_window(base_render: models.Render, script: Optional[str] = None) -> float:
    """Seconds into the base voiceover where its opening line ends: the
    even-pace estimate (first sentence's share of the script's characters),
    snapped to the middle of the nearest pause within ±40% of it."""
    from .renderer import _probe_duration

    path = Path(base_render.audio_path or "")
    if not path.is_file():
        raise HookLabError("This version has no voiceover on disk.")
    if script is None:
        from sqlalchemy.orm import object_session

        db = object_session(base_render)
        script = base_script(db, base_render) if db is not None else str(
            (base_render.composition_json or {}).get("script") or "")
    key = (str(path), path.stat().st_mtime_ns, hashlib.sha1(script.encode()).hexdigest())
    with _window_lock:
        if key in _window_cache:
            return _window_cache[key]
    total = _probe_duration(path)
    est = estimate_window(script, total)
    if est <= 0:
        raise HookLabError("This version has no spoken script, so it has no opening to swap.")
    if not rest_of_script(script):
        # One sentence: the opening is the whole voiceover (nothing is
        # joined after it), never a pause inside the line.
        window = round(total, 2)
        with _window_lock:
            _window_cache[key] = window
        return window
    # Snap to a pause near the estimate, but never past half of what follows
    # the first sentence, and closer in when the first sentence is most of
    # the script (a breath before its last clause is not its end).
    share = est / total if total > 0 else 0.0
    lo = est * (0.6 if share < 0.6 else 0.85)
    hi = min(est * 1.4, est + (total - est) * 0.5)
    best: Optional[float] = None
    for start, end in detect_silences(path):
        if start <= 0.05 or end >= total - 0.05:
            continue  # leading / trailing silence is not a pause between lines
        mid = (start + end) / 2
        if lo <= mid <= hi and (best is None or abs(mid - est) < abs(best - est)):
            best = mid
    window = round(min(max(best if best is not None else est, 0.3), max(total - 0.3, 0.3)), 2)
    with _window_lock:
        _window_cache[key] = window
    return window


def estimate_seconds(text: str, original: str, window: float) -> float:
    """A new opening's spoken length, guessed from the original's pace."""
    n = len((original or "").strip())
    return round(window * len((text or "").strip()) / n, 2) if n else 0.0


def fit_estimate(text: str, original: str, window: float) -> dict:
    """Before any audio is made: {estimated_seconds, fits, overflow_seconds}."""
    est = estimate_seconds(text, original, window)
    overflow = round(max(est - window * TEMPO_MAX, 0.0), 2)
    return {"estimated_seconds": est, "fits": overflow <= 0, "overflow_seconds": overflow}


# ---------- suggestions ----------


def suggest_hooks(db: Session, project: models.VideoProject, base_render: models.Render, n: int = 3) -> list[dict]:
    """`n` other openings in different styles, from one planner call (spend
    note "hook ideas"; the mock planner answers deterministically): each
    about as long as the original line, same facts. Raises HookLabError
    (502) when the answer is refused or can't be used."""
    from ..providers import get_chat
    from ..providers.openrouter_chat import extract_json

    n = max(1, min(int(n or 3), MAX_PER_REQUEST))
    script = base_script(db, base_render)
    hook = base_hook(db, base_render)
    styles = list(SUGGEST_STYLES[:n])
    try:
        reply = get_chat().hook_ideas(script=script, hook=hook, n=n, styles=styles)
    except Exception as e:  # noqa: BLE001
        log.warning("hook ideas failed for project %s: %s", project.id, e)
        raise HookLabError("The planner couldn't suggest openings right now. Try again, or write your own.",
                           502) from e
    try:
        data = extract_json(reply) if isinstance(reply, str) else reply
    except Exception:  # noqa: BLE001
        data = None
    items = data.get("hooks") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise HookLabError("The planner's answer couldn't be used. Try again, or write your own opening.", 502)
    words = max(_words(hook), 1)
    window = None
    try:
        window = find_hook_window(base_render, script)
    except HookLabError:
        window = 0.0
    out: list[dict] = []
    seen: set[str] = {_norm(hook)}
    used_styles: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get("text") or "").split()).strip().strip('"“”')
        style = str(item.get("style") or "").strip().lower().replace(" ", "_")
        if style not in SUGGEST_STYLES:
            style = "custom"
        if not text or _norm(text) in seen or len(text) > 300:
            continue
        if abs(_words(text) - words) > max(WORD_SLACK * words, 1):
            continue  # far longer or shorter than the opening it replaces
        if style in used_styles and style != "custom":
            continue
        seen.add(_norm(text))
        used_styles.add(style)
        out.append({"style": style, "style_label": STYLE_LABELS[style], "text": text,
                    **fit_estimate(text, hook, window or 0.0)})
        if len(out) >= n:
            break
    if not out:
        raise HookLabError("The planner's answer couldn't be used. Try again, or write your own opening.", 502)
    return out


# ---------- variant audio ----------


def _tts_identity(db: Session, project: models.VideoProject) -> dict:
    from . import fingerprints
    from . import model_registry as reg

    voice_id = fingerprints.tts_voice_id(db, project)
    return {"voice_id": voice_id,
            "default_voice": "" if voice_id else get_settings().elevenlabs_default_voice_id,
            "model": reg.active_model("tts") or "", "options": reg.active_options("tts") or {}}


def is_original(db: Session, variant: models.HookVariant, base: models.Render) -> bool:
    """The control: the original opening, recomposed with the hook-lab
    captions. Its audio is the base voiceover itself (nothing is paid)."""
    return variant.style == "original" and _norm(variant.hook_text) == _norm(base_hook(db, base))


def tts_cost(text: str) -> float:
    return len(text or "") / 1000.0 * get_settings().cost_tts_per_1k_chars


def _raw_fingerprint(db: Session, project: models.VideoProject, text: str) -> str:
    return _digest({"text": (text or "").strip(), **_tts_identity(db, project)})


def audio_fingerprint(db: Session, project: models.VideoProject, variant: models.HookVariant,
                      base: models.Render) -> str:
    if is_original(db, variant, base):
        return base.voice_fingerprint or _digest({"base_audio": base.audio_path})
    return _digest({"base_voice": base.voice_fingerprint or base.audio_path,
                    "window": round(float(variant.window_seconds or 0), 3),
                    "hook": _raw_fingerprint(db, project, variant.hook_text)})


def raw_hook_path(db: Session, project: models.VideoProject, variant: models.HookVariant) -> Path:
    from . import storage

    fp = _raw_fingerprint(db, project, variant.hook_text)
    return storage.render_subdir(project.id) / f"hook_raw_{variant.id}_{fp[:12]}.m4a"


def synth_hook(db: Session, project: models.VideoProject, variant: models.HookVariant) -> tuple[Path, bool]:
    """TTS of the new opening only, with the project's voice (the guarded
    provider runs the safety check on it). Reused when the same text was
    already spoken with the same voice (False = paid now)."""
    from ..providers import get_tts
    from . import fingerprints
    from .pipeline import _log_provider_call, _pname

    path = raw_hook_path(db, project, variant)
    if path.is_file() and path.stat().st_size > 0:
        return path, True
    tts = get_tts()
    voice_id = fingerprints.tts_voice_id(db, project)
    try:
        data = tts.synthesize(text=variant.hook_text.strip(), voice_id=voice_id)
    except Exception as e:  # noqa: BLE001
        _log_provider_call(db, project_id=project.id, provider=_pname(tts), endpoint="tts.synthesize",
                           request_summary={"chars": len(variant.hook_text), "hook_variant": variant.id},
                           response_summary={"error": str(e)[:500]}, status="error")
        from .safety import UnsafeScriptError

        if isinstance(e, UnsafeScriptError):
            raise HookLabError(f"This opening can't be used: {e}", 422) from e
        raise HookLabError(f"The voice provider said: {str(e)[:300] or type(e).__name__}", 502) from e
    path.write_bytes(data)
    _log_provider_call(db, project_id=project.id, provider=_pname(tts), endpoint="tts.synthesize",
                       request_summary={"chars": len(variant.hook_text), "voice_id": bool(voice_id),
                                        "hook_variant": variant.id},
                       response_summary={"bytes": len(data)})
    return path, False


def _ffmpeg(cmd: list[str], what: str) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg {what} failed: {(proc.stderr or '')[-800:]}")


_AFMT = "aresample=44100,aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"


def fit_hook(raw: Path, dst: Path, window: float) -> dict:
    """Fit the spoken opening into `window` seconds: shorter → padded with
    silence; up to 15% longer → sped up (atempo); longer → HookTooLong."""
    from .renderer import _probe_duration

    seconds = _probe_duration(raw)
    if seconds <= 0:
        raise HookLabError("The opening's audio can't be read.", 502)
    tempo = 1.0
    if seconds > window + 0.01:
        tempo = seconds / window
        if tempo > TEMPO_MAX:
            raise HookTooLong(round(seconds - window, 1))
    chain = [_AFMT]
    if tempo > 1.0:
        chain.append(f"atempo={tempo:.4f}")
    chain += [f"apad=whole_dur={window:.3f}", f"atrim=0:{window:.3f}", "asetpts=PTS-STARTPTS"]
    _ffmpeg(["ffmpeg", "-y", "-i", str(raw), "-af", ",".join(chain), "-vn", "-c:a", "pcm_s16le",
             str(dst)], "hook fit")
    return {"hook_seconds": round(seconds, 2), "tempo": round(tempo, 3),
            "overflow_seconds": round(max(seconds - window, 0.0), 2)}


def join_audio(head: Path, base_audio: Path, window: float, dst: Path) -> Path:
    """[head, base_audio from `window` on] in one re-encoded track (concat
    filter: no gap, no priming silence between the parts)."""
    graph = (f"[0:a]{_AFMT},asetpts=PTS-STARTPTS[a0];"
             f"[1:a]atrim=start={window:.3f},asetpts=PTS-STARTPTS,{_AFMT}[a1];"
             "[a0][a1]concat=n=2:v=0:a=1[out]")
    _ffmpeg(["ffmpeg", "-y", "-i", str(head), "-i", str(base_audio), "-filter_complex", graph,
             "-map", "[out]", "-vn", "-c:a", "aac", "-b:a", "160k", str(dst)], "audio join")
    return dst


def whole_voiceover(base: models.Render, window: float) -> bool:
    """The window reaches the end of the base voiceover (a one-sentence
    script): nothing of the original is joined after the new opening."""
    from .renderer import _probe_duration

    path = Path(base.audio_path or "")
    return path.is_file() and window >= _probe_duration(path) - 0.05


def window_limits(db: Session, base: models.Render) -> tuple[float, float]:
    """Where a user-set opening end may be: (0.3 s, the voiceover's length
    less 0.3 s), up to its whole length when the script is one sentence."""
    from .renderer import _probe_duration

    path = Path(base.audio_path or "")
    total = _probe_duration(path) if path.is_file() else 0.0
    if not rest_of_script(base_script(db, base)):
        return 0.3, max(round(total, 2), 0.3)
    return 0.3, max(total - 0.3, 0.3)


def make_variant_audio(db: Session, variant: models.HookVariant) -> dict:
    """The variant's whole voiceover: its opening (TTS, fitted into the
    window) joined to the base voiceover after the window, at
    `voice_variant_<id>.m4a`; stored on the variant. Reused when nothing it
    is made from changed. Raises HookTooLong / HookLabError.
    Returns {path, fits, overflow_seconds, hook_seconds, tempo, cost, reused}."""
    from . import storage

    base = check_base(db, db.get(models.Render, variant.base_render_id), variant.project_id)
    project = db.get(models.VideoProject, variant.project_id)
    window = float(variant.window_seconds or 0)
    if window <= 0:
        raise HookLabError("Set where the original opening ends first.", 422)
    fp = audio_fingerprint(db, project, variant, base)
    if is_original(db, variant, base):
        variant.audio_path, variant.audio_fingerprint = base.audio_path, fp
        db.commit()
        return {"path": base.audio_path, "fits": True, "overflow_seconds": 0.0, "hook_seconds": window,
                "tempo": 1.0, "cost": 0.0, "reused": True}
    if variant.audio_fingerprint == fp and variant.audio_path and Path(variant.audio_path).is_file():
        return {"path": variant.audio_path, "fits": True, "overflow_seconds": 0.0, "hook_seconds": None,
                "tempo": None, "cost": 0.0, "reused": True}
    raw, reused = synth_hook(db, project, variant)
    cost = 0.0 if reused else round(tts_cost(variant.hook_text), 4)
    out_dir = storage.render_subdir(project.id)
    head = out_dir / f"hook_fit_{variant.id}.wav"
    try:
        fit = fit_hook(raw, head, window)
        dst = out_dir / f"voice_variant_{variant.id}.m4a"
        if whole_voiceover(base, window):
            # A one-sentence script: the new line replaces all of it.
            _ffmpeg(["ffmpeg", "-y", "-i", str(head), "-vn", "-c:a", "aac", "-b:a", "160k", str(dst)],
                    "audio encode")
        else:
            join_audio(head, Path(base.audio_path), window, dst)
    finally:
        head.unlink(missing_ok=True)
    variant.audio_path, variant.audio_fingerprint = str(dst), fp
    db.commit()
    return {"path": str(dst), "fits": True, **fit, "cost": cost, "reused": reused}


# ---------- captions ----------


def hook_captions(hook_text: str, rest_text: str, window: float, body_seconds: float,
                  speech_seconds: Optional[float] = None) -> list["CaptionTiming"]:
    """The opening's caption chunks spread over the time it is spoken
    (`speech_seconds`, the fitted line before its padding; the whole window
    when unknown), the rest of the script's over [window, end of the body).
    The rest is spoken from the window on in the base voiceover and in every
    variant, so its captions are the same in all of them (the control too).
    A caption ends no later than the next one starts."""
    from .captions import CaptionTiming, chunk_script

    body = max(float(body_seconds or 0), 1.0)
    window = min(max(float(window or 0), 0.0), body)
    head_end = window if rest_text.strip() else body
    if speech_seconds is not None and speech_seconds > 0:
        head_end = min(head_end, float(speech_seconds))
    out: list[CaptionTiming] = []

    def spread(text: str, start: float, end: float) -> None:
        chunks = chunk_script(text or "", max(end - start, 0.01))
        if not chunks:
            return
        step = (end - start) / len(chunks)
        for i, c in enumerate(chunks):
            s = start + i * step
            out.append(CaptionTiming(start=round(s, 3), end=round(max(s + step, s + 0.5), 3), text=c.text))

    spread(hook_text, 0.0, head_end)
    if rest_text.strip() and window < body:
        spread(rest_text, window, body)
    for a, b in zip(out, out[1:], strict=False):
        if a.end > b.start:
            a.end = b.start
    return out


# ---------- plan and price ----------


def _shot_rate(db: Session, clip: "ComposeClip") -> float:
    from .pipeline import _video_rate

    shot = db.get(models.VideoShot, clip.shot_id) if clip.shot_id else None
    if shot is None:
        return get_settings().cost_video_per_second
    return _video_rate(shot)


def parts(db: Session, variant: models.HookVariant, *, base: Optional[models.Render] = None,
          clips: Optional[list["ComposeClip"]] = None) -> dict:
    """What making `variant` takes, priced like render_plan: the opening's
    TTS, shot 1 per its option and the later lip-synced shots the new
    opening overlaps. Raises HookLabError when it can't be made."""
    from .pipeline import _lipsync_available

    rates = get_settings()
    base = base or check_base(db, db.get(models.Render, variant.base_render_id), variant.project_id)
    project = db.get(models.VideoProject, variant.project_id)
    clips = clips if clips is not None else base_clips(db, base)
    starts = _clip_starts(clips, base.composition_json)
    window = float(variant.window_seconds or 0)
    notes: list[str] = []
    warnings: list[str] = []
    lipsync_on = _lipsync_available(db, project)
    # The opening's voice.
    if is_original(db, variant, base):
        tts = 0.0
    elif variant.audio_fingerprint and variant.audio_path and Path(variant.audio_path).is_file() \
            and variant.audio_fingerprint == audio_fingerprint(db, project, variant, base):
        tts = 0.0
    elif raw_hook_path(db, project, variant).is_file():
        tts = 0.0  # already spoken ("Listen"): only fitted and joined again
    else:
        tts = round(tts_cost(variant.hook_text), 4)
    # Too long for the window: known once its voice was made ("Listen"),
    # else guessed from the original's pace (a warning only).
    too_long = 0.0
    if not is_original(db, variant, base) and window > 0:
        raw = raw_hook_path(db, project, variant)
        if raw.is_file():
            from .renderer import _probe_duration

            spoken = _probe_duration(raw)
            if spoken > window * TEMPO_MAX + 0.01:
                too_long = round(spoken - window, 1)
        else:
            guess = fit_estimate(variant.hook_text, base_hook(db, base), window)
            if not guess["fits"]:
                warnings.append(f"This opening might be about {guess['overflow_seconds']:.1f} s too long for "
                                "the original's time; Listen to check before making it.")
    base_settings(db, base)  # composed with the base's look: refused now, before anything is paid
    first = clips[0]
    d1 = starts[0][1]
    shot1 = {"source": variant.shot1_source, "seconds": round(d1, 2), "cost": 0.0, "lipsync": False}
    needed: list[int] = []
    if variant.shot1_source == "take":
        take = db.get(models.ShotTake, variant.shot1_take_id) if variant.shot1_take_id else None
        if take is None or take.shot_id != first.shot_id or not Path(take.clip_path or "").is_file():
            raise HookLabError("Pick one of shot 1's takes.", 422)
        from .renderer import _probe_duration

        length = _probe_duration(Path(take.clip_path))
        if length and length < d1 - TAKE_SHORT_OK:
            raise HookLabError(f"This take is {length:.1f} s, shorter than shot 1 in this version "
                               f"({d1:.1f} s), so the rest of the video would slide out of step with the "
                               "voice. Pick another take.", 422)
    elif variant.shot1_source == "new":
        if not (variant.shot1_prompt or "").strip():
            raise HookLabError("Describe the new shot 1.", 422)
        shot1["seconds"] = new_shot_seconds(first, d1)
        shot1["cost"] = round(shot1["seconds"] * _shot_rate(db, first), 4)
        needed.append(0)
    elif variant.shot1_source == "lipsync":
        if first.shot_type != "talking_head":
            raise HookLabError("Shot 1 is not a talking head, so there is nothing to lip-sync.", 422)
        if not lipsync_on:
            raise HookLabError("Lip-sync is off (or there is no portrait), so shot 1 can't be redone.", 422)
        shot1["cost"] = round(d1 * rates.cost_avatar_per_second, 4)
        shot1["lipsync"] = True
        needed.append(0)
        if len(clips) == 1:
            notes.append(f"This video is one talking-head shot, so all of it ({d1:.0f} s) is lip-synced "
                         f"again: about ${shot1['cost']:.2f}.")
    elif variant.shot1_source != "keep":
        raise HookLabError("Choose keep, a take, a new shot or redo the talking head for shot 1.", 422)
    if variant.shot1_source in ("keep", "take") and not is_original(db, variant, base) and _lipsynced(db, first):
        warnings.append("Shot 1 is lip-synced to the original opening; choose “Redo talking head” so the "
                        "lips match the new words.")
    extra: list[dict] = []
    for i, clip in enumerate(clips[1:], start=1):
        start, length = starts[i]
        if start >= window - 0.05 or not _lipsynced(db, clip) or is_original(db, variant, base):
            continue
        if not lipsync_on:
            warnings.append(f"Shot {clip.shot_order or i + 1} is lip-synced to the original opening and "
                            "lip-sync is off now, so its lips won't match.")
            continue
        cost = round(length * rates.cost_avatar_per_second, 4)
        extra.append({"index": i, "shot_id": clip.shot_id, "shot_order": clip.shot_order or i + 1,
                      "start": round(start, 2), "seconds": round(length, 2), "cost": cost,
                      "reason": "its lips follow the opening, which changes"})
        needed.append(i)
    total = round(tts + shot1["cost"] + sum(e["cost"] for e in extra), 4)
    return {"tts_cost": tts, "shot1": shot1, "extra_lipsync": extra, "needed": needed, "total": total,
            "notes": notes, "warnings": warnings, "lipsync_available": lipsync_on,
            "too_long_seconds": too_long}


def new_shot_seconds(clip: "ComposeClip", used: float) -> float:
    """The length a new shot 1 is asked for (and priced at): the shot's
    planned length, as its own clip was asked for. It plays for the slot's
    length either way (cut, or held on its last frame)."""
    return float(clip.seconds) if clip.seconds else float(max(round(used), 1))


def variant_plan(db: Session, variants: list[models.HookVariant]) -> dict:
    """The price of making `variants`, before anything runs (like
    render_plan): per variant and in total, with a one-line summary."""
    rows: list[dict] = []
    for v in variants:
        try:
            p = parts(db, v)
            rows.append({"id": v.id, "label": v.label, "ok": True, "error": "", **p})
        except HookLabError as e:
            rows.append({"id": v.id, "label": v.label, "ok": False, "error": str(e), "tts_cost": 0.0,
                         "shot1": None, "extra_lipsync": [], "needed": [], "total": 0.0, "notes": [],
                         "warnings": [], "lipsync_available": False, "too_long_seconds": 0.0})
    total = round(sum(r["total"] for r in rows), 4)
    n = len(rows)
    summary = (f"{n} opening{'s' if n != 1 else ''} · about ${total:.2f} · the rest of the video is reused"
               if n else "No openings")
    return {"variants": rows, "count": n, "total": total, "currency": "USD", "is_estimate": True,
            "summary": summary}


# ---------- make ----------


LABELS = "BCDEFGHIJKLMNOPQRSTUVWXYZ"


def next_label(db: Session, base_render_id: int) -> str:
    used = {lbl for (lbl,) in db.query(models.HookVariant.label)
            .filter(models.HookVariant.base_render_id == base_render_id)}
    for c in LABELS:
        if c not in used:
            return c
    raise HookLabError("This version already has as many openings as it can.")


def precheck(db: Session, project: models.VideoProject, variant: models.HookVariant,
             clips: list["ComposeClip"]) -> None:
    """The safety checks the variant's paid steps will meet, before any of
    them is paid for (cached: the checks at the providers are then hits)."""
    from .minor_check import check_generation
    from .prompting import effective_negative

    check_generation(variant.hook_text or "", kind="script")
    if variant.shot1_source == "new":
        shot = db.get(models.VideoShot, clips[0].shot_id) if clips[0].shot_id else None
        check_generation(variant.shot1_prompt or "", effective_negative("video", shot.negative_prompt if shot else ""),
                         kind="video")


def _fail(db: Session, variant: models.HookVariant, error: str) -> None:
    variant.status = "failed"
    variant.error = error[:800]
    render = db.get(models.Render, variant.render_id) if variant.render_id else None
    if render is not None and render.status not in ("completed", "failed", "cancelled", "expired"):
        render.status = "failed"
        render.error = error[:800]
    project = db.get(models.VideoProject, variant.project_id)
    from . import events

    events.hook_variant_event(db, variant, project, "failed", error=error)
    db.commit()


def make_variants(db: Session, project_id: int, variant_ids: list[int], *, claim: Optional[str] = None,
                  claim_token: Optional[str] = None, previous: str = "completed") -> None:
    """The job behind "Make videos": per variant (already claimed "making"
    by the API), its own Render, its audio, and its clips; each is composed
    once its parts are in. The project claim is held only while this runs."""
    from . import spend
    from .pipeline import consume_claim

    if claim:
        if not consume_claim(db, project_id, claim, claim_token):
            db.rollback()
            # The claim was cancelled meanwhile: give the variants back.
            (db.query(models.HookVariant)
             .filter(models.HookVariant.id.in_(variant_ids), models.HookVariant.status == "making",
                     models.HookVariant.render_id.is_(None))
             .update({"status": "draft"}, synchronize_session=False))
            db.commit()
            return
        db.commit()
    try:
        for vid in variant_ids:
            variant = db.get(models.HookVariant, vid)
            if variant is None or variant.status != "making":
                continue
            with spend.scope(project_id=project_id):
                try:
                    _make_one(db, variant)
                except Exception as e:  # noqa: BLE001 — that variant fails, the others go on
                    db.rollback()
                    log.exception("hook variant %s failed", vid)
                    variant = db.get(models.HookVariant, vid)
                    if variant is not None and variant.status in RUNNING:
                        _fail(db, variant, str(e) or type(e).__name__)
    finally:
        if claim:
            (db.query(models.VideoProject)
             .filter(models.VideoProject.id == project_id, models.VideoProject.status == claim)
             .update({"status": previous}, synchronize_session=False))
            db.commit()


def _make_one(db: Session, variant: models.HookVariant) -> None:
    from . import events, spend

    base = check_base(db, db.get(models.Render, variant.base_render_id), variant.project_id)
    project = db.get(models.VideoProject, variant.project_id)
    clips = base_clips(db, base)
    plan = parts(db, variant, base=base, clips=clips)
    render = models.Render(project_id=project.id, status="generating_audio", hook_variant_id=variant.id,
                           estimated_cost=plan["total"])
    db.add(render)
    db.flush()
    variant.render_id = render.id
    variant.cost_estimate = plan["total"]
    variant.error = ""
    variant.shot1_clip_path = ""
    variant.clips_json = {"needed": plan["needed"], "clips": {}}
    events.hook_variant_event(db, variant, project, "started", detail="Making the opening's voice")
    db.commit()
    with spend.scope(render_id=render.id):
        precheck(db, project, variant, clips)
        try:
            audio = make_variant_audio(db, variant)
        except HookTooLong as e:
            _fail(db, variant, str(e))
            return
        db.refresh(variant)
        db.refresh(render)
        if variant.status != "making" or render.status != "generating_audio":
            return  # cancelled meanwhile
        render.audio_path = audio["path"]
        render.voice_fingerprint = variant.audio_fingerprint
        render.status = "generating_shots"
        if plan["needed"]:
            events.hook_variant_event(db, variant, project, "progress",
                                      detail=f"Making {len(plan['needed'])} clip"
                                             f"{'s' if len(plan['needed']) != 1 else ''}")
        db.commit()
        starts = _clip_starts(clips, base.composition_json)
        for i in plan["needed"]:
            db.refresh(variant)
            if variant.status != "making":
                return  # failed meanwhile (an earlier clip of it failed)
            if i == 0 and variant.shot1_source == "new":
                submit_hook_shot(db, project, variant, clips[0], render)
            else:
                submit_hook_avatar(db, project, variant, i, clips[i], starts[i][0], starts[i][1], render,
                                   single=len(clips) == 1)
    maybe_compose_variant(db, variant.id)


def _video_settings(project: models.VideoProject, callback: Optional[str]) -> dict:
    out = {"aspect_ratio": project.aspect_ratio or None, "generate_audio": False}
    if callback:
        out["callback_url"] = callback
    return out


def submit_hook_shot(db: Session, project: models.VideoProject, variant: models.HookVariant,
                     clip: "ComposeClip", render: models.Render) -> models.VideoJob:
    """A new shot 1 from the variant's prompt: one paid clip at the shot's
    length (VideoJob purpose hook_shot, ref_id = the variant). The project's
    VideoShot row is only read (its model, references, avoid list)."""
    from ..providers import get_video, routes_per_model, uses_webhooks
    from . import video_jobs
    from .model_registry import active_model
    from .pipeline import _resolve_model, _resolve_references_for_shot, clip_seconds
    from .prompting import effective_negative

    shot = db.get(models.VideoShot, clip.shot_id) if clip.shot_id else None
    model = _resolve_model(db, (shot.model_override if shot else "") or active_model("video"))
    video = get_video(model) if routes_per_model("video", model) else get_video()
    refs = _resolve_references_for_shot(db, project, shot) if shot is not None else []
    negative = effective_negative("video", shot.negative_prompt if shot else "")
    seconds = new_shot_seconds(clip, clip_seconds(clip))  # as parts() priced it
    prompt = variant.shot1_prompt.strip()

    def start(callback: Optional[str]):
        return video.submit(model=model, prompt=prompt, references=refs, negative_prompt=negative,
                            reference_strategy=(shot.reference_strategy if shot else "") or "input_references",
                            duration_seconds=seconds, settings=_video_settings(project, callback))

    return video_jobs.submit(
        db, purpose=PURPOSE_SHOT, ref_id=variant.id, project_id=project.id, render_id=render.id,
        context={"index": 0, "duration": seconds, "aspect": project.aspect_ratio or "",
                 "model": model}, start=start, webhooks=uses_webhooks(model))


def submit_hook_avatar(db: Session, project: models.VideoProject, variant: models.HookVariant, index: int,
                       clip: "ComposeClip", start: float, seconds: float, render: models.Render, *,
                       single: bool = False) -> models.VideoJob:
    """Lip-sync one talking-head shot again against the variant's voiceover
    slice [start, start + seconds) (VideoJob purpose hook_avatar, ref_id =
    the variant): shot 1 with the new opening, or a later shot the opening
    overlaps. The VideoShot row is never touched."""
    from ..providers import get_avatar
    from . import public_media, storage, video_jobs
    from .pipeline import _hero_portrait
    from .renderer import slice_audio

    avatar = get_avatar()
    portrait = _hero_portrait(db, project)
    if avatar is None or portrait is None:
        raise HookLabError("Lip-sync is off (or there is no portrait), so the talking head can't be redone.")
    out_dir = storage.render_subdir(project.id)
    seg = slice_audio(Path(variant.audio_path), out_dir / f"voice_hook_{variant.id}_{index}.m4a",
                      start=start, duration=seconds)
    audio_url = public_media.publish(seg, "m4a")
    shot = db.get(models.VideoShot, clip.shot_id) if clip.shot_id else None
    script = base_script(db, db.get(models.Render, variant.base_render_id))
    if index == 0:
        # The words this slice says: the new opening (and, for a video that
        # is one talking-head shot, the rest of the script after it).
        text = replace_first_sentence(script, variant.hook_text) if single else variant.hook_text
    else:
        text = (shot.caption_text if shot else "") or rest_of_script(script)
    motion = (shot.camera_direction if shot else "") or ""

    def start_fn(callback: Optional[str]):
        return avatar.submit_avatar(
            portrait_url=storage.public_url_for_token(portrait.public_token), audio_url=audio_url,
            script=text, aspect_ratio=project.aspect_ratio, motion_prompt=motion,
            duration_seconds=seconds, callback_url=callback)

    return video_jobs.submit(
        db, purpose=PURPOSE_AVATAR, ref_id=variant.id, project_id=project.id, render_id=render.id,
        context={"index": index, "duration": seconds, "start": round(start, 3),
                 "aspect": project.aspect_ratio or "", "model": getattr(avatar, "model", "")},
        start=start_fn)


def _job_variant(db: Session, job: models.VideoJob) -> Optional[models.HookVariant]:
    """The variant a hook-lab clip is for, when it still waits for it (the
    same make round: the job's render is the variant's render)."""
    db.refresh(job)
    variant = db.get(models.HookVariant, job.ref_id)
    if variant is None or variant.render_id != job.render_id or variant.status != "making":
        return None
    return variant


def variant_clips(db: Session, variant: models.HookVariant) -> dict[str, str]:
    """{clip index: file} of the clips made for the variant's current make
    round (its render): worked out from the finished VideoJobs, never from a
    read-modify-write of clips_json, so two clips landing at once (a webhook
    and the poller) can't lose one."""
    from . import video_jobs

    if not variant.render_id:
        return {}
    out: dict[str, str] = {}
    for job in (db.query(models.VideoJob)
                .filter(models.VideoJob.ref_id == variant.id, models.VideoJob.render_id == variant.render_id,
                        models.VideoJob.purpose.in_(video_jobs.HOOK_PURPOSES),
                        models.VideoJob.status == "completed")
                .order_by(models.VideoJob.id)):
        if job.result_path and Path(job.result_path).is_file():
            out[str(int((job.context_json or {}).get("index") or 0))] = job.result_path
    return out


def on_clip_ready(db: Session, job: models.VideoJob) -> None:
    variant = _job_variant(db, job)
    if variant is None:
        return
    index = int((job.context_json or {}).get("index") or 0)
    if index == 0:
        variant.shot1_clip_path = job.result_path
        db.commit()
    maybe_compose_variant(db, variant.id)


def on_clip_failed(db: Session, job: models.VideoJob) -> None:
    variant = _job_variant(db, job)
    if variant is None:
        return
    index = int((job.context_json or {}).get("index") or 0)
    what = "The new shot 1" if job.purpose == PURPOSE_SHOT else (
        "Shot 1's lip-sync" if index == 0 else f"The lip-sync of shot {index + 1}")
    _fail(db, variant, f"{what} failed: {(job.error or 'the provider gave no reason')[:400]}")


def maybe_compose_variant(db: Session, variant_id: int) -> None:
    """Once the variant's audio and every clip it needs are in, move it
    making → ready exactly once and queue its compose."""
    from ..workers.jobs import compose_hook_variant_job
    from ..workers.queue import enqueue

    variant = db.get(models.HookVariant, variant_id)
    if variant is None:
        return
    db.refresh(variant)
    if variant.status != "making" or not variant.audio_path:
        return
    render = db.get(models.Render, variant.render_id) if variant.render_id else None
    if render is None:
        return
    db.refresh(render)
    if render.status == "cancelled":
        _fail(db, variant, "Cancelled")
        return
    if render.status != "generating_shots":
        return
    have = variant_clips(db, variant)
    if any(str(i) not in have for i in (variant.clips_json or {}).get("needed") or []):
        return
    res = db.execute(sa_update(models.HookVariant)
                     .where(models.HookVariant.id == variant_id, models.HookVariant.status == "making")
                     .values(status="ready", clips_json={**(variant.clips_json or {}), "clips": have},
                             updated_at=datetime.utcnow()))
    db.commit()
    if res.rowcount == 1:
        try:
            enqueue(compose_hook_variant_job, variant_id)
        except Exception:  # noqa: BLE001 — the sweeper queues it again (sweep_variants)
            log.exception("queueing the compose of hook variant %s failed", variant_id)


def sweep(db: Session, *, grace_seconds: float = 120.0, now: Optional[datetime] = None) -> dict:
    """Openings stuck between their clips and their compose (the sweeper,
    about once a minute): one still "making" whose render waits in
    generating_shots with no clip of it still being made — compose it when
    every clip is in, else fail it — and one "ready" whose compose was never
    queued (queued again; the compose claims it exactly once)."""
    from datetime import timedelta

    from ..workers.jobs import compose_hook_variant_job
    from ..workers.queue import enqueue
    from . import video_jobs

    now = now or datetime.utcnow()
    cutoff = now - timedelta(seconds=grace_seconds)
    out = {"composed": 0, "failed": 0, "requeued": 0}
    moved: set[int] = set()  # queued in this sweep already
    V = models.HookVariant
    for variant in (db.query(V).filter(V.status == "making", V.updated_at <= cutoff, V.render_id.isnot(None))
                    .order_by(V.id).limit(50).all()):
        render = db.get(models.Render, variant.render_id)
        if render is None or render.status != "generating_shots" or (
                render.updated_at is not None and render.updated_at > cutoff):
            continue
        busy = (db.query(models.VideoJob.id)
                .filter(models.VideoJob.ref_id == variant.id, models.VideoJob.render_id == render.id,
                        models.VideoJob.purpose.in_(video_jobs.HOOK_PURPOSES),
                        video_jobs.still_in_progress()).first())
        if busy is not None:
            continue
        try:
            maybe_compose_variant(db, variant.id)
            db.refresh(variant)
            if variant.status == "making":
                failed = (db.query(models.VideoJob)
                          .filter(models.VideoJob.ref_id == variant.id, models.VideoJob.render_id == render.id,
                                  models.VideoJob.purpose.in_(video_jobs.HOOK_PURPOSES),
                                  models.VideoJob.status == "failed")
                          .order_by(models.VideoJob.id.desc()).first())
                _fail(db, variant, (f"A clip of this opening failed: {(failed.error or '')[:300]}"
                                    if failed is not None else
                                    "Stopped unexpectedly before all its clips were made; make it again"))
                out["failed"] += 1
            else:
                out["composed"] += 1
                moved.add(variant.id)
        except Exception:  # noqa: BLE001
            db.rollback()
            log.exception("sweeping hook variant %s failed", variant.id)
    # A variant render left waiting after its opening failed or was cancelled.
    R = models.Render
    for render in (db.query(R).filter(R.hook_variant_id.isnot(None), R.status == "generating_shots",
                                      R.updated_at <= cutoff).order_by(R.id).limit(50).all()):
        variant = db.get(V, render.hook_variant_id)
        if variant is not None and variant.status in RUNNING and variant.render_id == render.id:
            continue
        res = db.execute(sa_update(R).where(R.id == render.id, R.status == "generating_shots")
                         .values(status="failed", error=(variant.error if variant is not None else "")
                                 or "Stopped", updated_at=now))
        db.commit()
        out["failed"] += res.rowcount
    for (vid,) in (db.query(V.id).filter(V.status == "ready", V.updated_at <= cutoff)
                   .order_by(V.id).limit(50).all()):
        if vid in moved:
            continue
        res = db.execute(sa_update(V).where(V.id == vid, V.status == "ready", V.updated_at <= cutoff)
                         .values(updated_at=now))
        db.commit()
        if res.rowcount != 1:
            continue
        try:
            enqueue(compose_hook_variant_job, vid)
            out["requeued"] += 1
        except Exception:  # noqa: BLE001
            log.exception("re-queueing the compose of hook variant %s failed", vid)
    return out


def cancel(db: Session, variant: models.HookVariant) -> None:
    """Stop an opening being made: it fails ("Cancelled") and its render is
    cancelled, so it can be edited and made again. Clips already at a
    provider still finish (and are billed) but are no longer used. One that
    is already being put together (composing) finishes."""
    from . import events

    res = db.execute(sa_update(models.HookVariant)
                     .where(models.HookVariant.id == variant.id,
                            models.HookVariant.status.in_(("making", "ready")))
                     .values(status="failed", error="Cancelled", updated_at=datetime.utcnow()))
    if res.rowcount != 1:
        db.rollback()
        raise HookLabError("This opening isn't waiting for its clips any more (it is being put together, "
                           "or it is done).")
    db.refresh(variant)
    render = db.get(models.Render, variant.render_id) if variant.render_id else None
    if render is not None and render.status not in ("completed", "failed", "cancelled", "expired"):
        render.status = "cancelled"
        render.error = ""
    events.hook_variant_event(db, variant, db.get(models.VideoProject, variant.project_id), "failed",
                              error="Cancelled")
    db.commit()


def compose_variant(db: Session, variant_id: int) -> Optional[models.Render]:
    """Put the variant together (CPU only): the base render's clips with
    shot 1 (and overlapped lip-sync shots) per the variant, its voiceover
    and its captions, through the shared compose. Its Render ends completed."""
    from . import events
    from .pipeline import ComposeClip, _compose, _settle_render_cost, clip_seconds
    from .renderer import _probe_duration

    res = db.execute(sa_update(models.HookVariant)
                     .where(models.HookVariant.id == variant_id, models.HookVariant.status == "ready")
                     .values(status="composing", updated_at=datetime.utcnow()))
    db.commit()
    if res.rowcount != 1:
        return None
    variant = db.get(models.HookVariant, variant_id)
    render = db.get(models.Render, variant.render_id)
    project = db.get(models.VideoProject, variant.project_id)
    try:
        base = check_base(db, db.get(models.Render, variant.base_render_id), variant.project_id)
        clips = base_clips(db, base)
        look = base_settings(db, base)
        # Each replaced clip fills exactly the time its slot had in the base
        # (cut, or held on its last frame), so every later clip, lip-sync
        # and caption stays where the joined voiceover has it.
        slots = _clip_starts(clips, base.composition_json)
        if variant.shot1_source == "take":
            take = db.get(models.ShotTake, variant.shot1_take_id)
            if take is None or not Path(take.clip_path or "").is_file():
                raise HookLabError("The take chosen for shot 1 is gone.")
            c = clips[0]
            clips[0] = ComposeClip(path=Path(take.clip_path), seconds=round(slots[0][1], 3), shot_id=c.shot_id,
                                   shot_order=c.shot_order, shot_type=c.shot_type, take_id=take.id,
                                   fingerprint=take.clip_fingerprint or "", provider_model=take.provider_model or "",
                                   exact=True)
            variant.shot1_clip_path = take.clip_path
        made = variant_clips(db, variant) or ((variant.clips_json or {}).get("clips") or {})
        for key, path in made.items():
            i = int(key)
            c = clips[i]
            clips[i] = ComposeClip(path=Path(path), seconds=round(slots[i][1], 3), shot_id=c.shot_id,
                                   shot_order=c.shot_order, shot_type=c.shot_type, take_id=None, fingerprint="",
                                   provider_model=c.provider_model, exact=True)
        render.status = "rendering"
        events.hook_variant_event(db, variant, project, "progress", detail="Putting the video together")
        db.commit()
        script = base_script(db, base)
        body = sum(clip_seconds(c) for c in clips)
        window = float(variant.window_seconds)
        speech = window
        raw = raw_hook_path(db, project, variant)
        if not is_original(db, variant, base) and raw.is_file():
            speech = min(_probe_duration(raw), window) or window
        timed = hook_captions(variant.hook_text, rest_of_script(script), window, body, speech)
        _compose(db, project, render, clips, Path(variant.audio_path), timed, plan=None, settings=look,
                 voice_fingerprint=variant.audio_fingerprint, hook_window=window, hook_variant_id=variant.id,
                 hook_text=variant.hook_text, script=replace_first_sentence(script, variant.hook_text),
                 extra={"base_render_id": base.id, "hook_label": variant.label, "hook_style": variant.style,
                        "shot1_source": variant.shot1_source})
        render.status = "completed"
        variant.status = "completed"
        variant.error = ""
        events.hook_variant_event(db, variant, project, "completed")
        db.commit()
        _settle_render_cost(db, render)
        return render
    except Exception as e:  # noqa: BLE001
        db.rollback()
        log.exception("composing hook variant %s failed", variant_id)
        variant = db.get(models.HookVariant, variant_id)
        _fail(db, variant, str(e) or type(e).__name__)
        return None


def sync(db: Session, variant: models.HookVariant) -> None:
    """A variant whose render was stopped by the sweeper (or cancelled)
    fails too, so it can be edited and made again."""
    if variant.status not in RUNNING or not variant.render_id:
        return
    render = db.get(models.Render, variant.render_id)
    if render is not None and render.status in ("failed", "cancelled"):
        _fail(db, variant, render.error or ("Cancelled" if render.status == "cancelled" else "Stopped"))

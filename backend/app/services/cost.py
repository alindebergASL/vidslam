"""Projected cost estimation for a project's video generation.

Costs vary wildly between providers and models, so this is a transparent,
rate-card estimate (configurable via COST_* settings), not a billing figure.
It always reports the *real-provider* projection so a user can preview spend
even while running in mock mode; the actual cost recorded on a Render is 0
when MOCK_PROVIDERS is on (nothing was billed)."""
from __future__ import annotations

from typing import Optional, get_args

from ..config import get_settings
from ..schemas.storyboard import ShotPlan, ShotType, StoryboardPlan

_SHOT_TYPES = set(get_args(ShotType))
# TTS speaking rate used to guess the voiceover's length before one exists.
CHARS_PER_SECOND = 15.0


def speech_seconds(plan: StoryboardPlan, voice_seconds: Optional[float]) -> dict[int, float]:
    """For each talking-head shot (by order): the seconds of voiceover
    it gets. A lip-sync clip is as long as its audio, so a shot past the end
    of the voiceover is shorter than planned (e.g. 4.6 s for a 7 s shot).
    With no voiceover length known, each gets its planned duration."""
    out: dict[int, float] = {}
    start = 0.0
    for sh in plan.shots:
        if sh.shot_type == "end_card":
            continue
        dur = float(sh.duration_seconds)
        if sh.shot_type == "talking_head":
            left = dur if voice_seconds is None else max(0.0, voice_seconds - start)
            out[sh.order] = round(max(1.0, min(dur, left)), 2)
        start += dur
    return out


def estimate_plan_cost(project: dict, plan: StoryboardPlan, *, lipsync: bool = False,
                       voice_seconds: Optional[float] = None,
                       video_rates: Optional[dict[int, float]] = None) -> dict:
    """Return a per-line breakdown + total for generating `plan` for `project`.

    `project` is a plain dict with keys: voiceover_source, music_upload_path
    (or a flag), and the music source intent. With `lipsync` (a lip-sync
    model is on and the project has a portrait), talking-head shots are
    priced at the lip-sync rate on their share of the voiceover
    (`voice_seconds` long, when known) instead of as video clips.
    """
    s = get_settings()
    lines: list[dict] = []

    # 1. Storyboard planning (one chat call).
    lines.append({
        "item": "Storyboard plan (chat)",
        "qty": 1,
        "unit": "call",
        "unit_cost": s.cost_chat_per_plan,
        "cost": round(s.cost_chat_per_plan, 4),
    })

    # 2. Per-shot video generation (skip end_card — rendered locally with FFmpeg).
    billable_shots = [sh for sh in plan.shots if sh.shot_type != "end_card"]
    speech = speech_seconds(plan, voice_seconds) if lipsync else {}
    billable_shots = [sh for sh in billable_shots if sh.order not in speech]
    video_seconds = sum(sh.duration_seconds for sh in billable_shots)
    # Per-shot rates when shots run on models priced differently (e.g. a
    # Sogni model); otherwise the flat COST_VIDEO_PER_SECOND.
    rates = video_rates or {}
    video_cost = sum(sh.duration_seconds * rates.get(sh.order, s.cost_video_per_second) for sh in billable_shots)
    lines.append({
        "item": f"Video clips ({len(billable_shots)} shots, {video_seconds:.0f}s)",
        "qty": round(video_seconds, 1),
        "unit": "sec",
        "unit_cost": round(video_cost / video_seconds, 4) if video_seconds else s.cost_video_per_second,
        "cost": round(video_cost, 4),
    })

    if speech:
        secs = sum(speech.values())
        lines.append({
            "item": f"Lip-sync clips ({len(speech)} talking head{'s' if len(speech) != 1 else ''}, "
                    f"{secs:.0f}s of speech)",
            "qty": round(secs, 1),
            "unit": "sec",
            "unit_cost": s.cost_avatar_per_second,
            "cost": round(secs * s.cost_avatar_per_second, 4),
        })

    # 3. Voiceover TTS (only when source is 'tts').
    if project.get("voiceover_source", "tts") == "tts":
        chars = len(plan.cleaned_voice_script or "")
        tts_cost = (chars / 1000.0) * s.cost_tts_per_1k_chars
        lines.append({
            "item": f"Voiceover TTS ({chars} chars)",
            "qty": chars,
            "unit": "char",
            "unit_cost": s.cost_tts_per_1k_chars / 1000.0,
            "cost": round(tts_cost, 4),
        })

    # 4. Music generation, only if the project intends to generate music
    #    (uploaded music has no provider cost).
    if project.get("music_will_generate"):
        lines.append({
            "item": "Music generation",
            "qty": 1,
            "unit": "track",
            "unit_cost": s.cost_music_per_generation,
            "cost": round(s.cost_music_per_generation, 4),
        })

    total = round(sum(line["cost"] for line in lines), 4)
    return {
        "currency": "USD",
        "total": total,
        "lines": lines,
        "is_estimate": True,
        "note": "Rate-card estimate; configure COST_* env vars to match your providers.",
    }


def estimate_for_project(db, project, plan: StoryboardPlan, *, music_will_generate: bool = False) -> dict:
    """estimate_plan_cost with what the project knows: whether its talking
    heads will be lip-synced, and how long its voiceover is (measured from
    the latest render's voiceover or the uploaded file, else guessed from
    the script)."""
    from pathlib import Path

    from ..providers import get_avatar
    from .pipeline import _hero_portrait, _latest_voiceover
    from .renderer import _probe_duration

    # The project's shots as they are now (reordered / re-timed in the
    # editor), not as first planned: a talking head's share of the
    # voiceover depends on where it plays, and the render uses these rows.
    rows = sorted(project.shots, key=lambda r: r.shot_order)
    if rows:
        plan = plan.model_copy(update={"shots": [
            ShotPlan(order=r.shot_order, shot_type=r.shot_type, duration_seconds=float(r.duration_seconds or 0),
                     visual_prompt=r.prompt or "-")
            for r in rows if r.shot_type in _SHOT_TYPES]})
    from ..providers import sogni
    from .model_registry import active_model

    active_video = active_model("video") or ""
    video_rates = {}
    for r in rows:
        m = (r.model_override or active_video or "").strip()
        if m.startswith(sogni.PREFIX):
            video_rates[r.shot_order] = sogni.video_rate_usd(m)
    lipsync = get_avatar() is not None and _hero_portrait(db, project) is not None
    voice: Optional[float] = None
    if lipsync:
        source = (project.voiceover_source or "tts").lower()
        audio = project.voiceover_upload_path if source == "upload" else _latest_voiceover(db, project)
        if audio and Path(audio).exists():
            voice = _probe_duration(Path(audio)) or None
        elif source == "tts" and plan.cleaned_voice_script:
            voice = len(plan.cleaned_voice_script) / CHARS_PER_SECOND
    return estimate_plan_cost(
        {"voiceover_source": project.voiceover_source, "music_will_generate": music_will_generate},
        plan, lipsync=lipsync, voice_seconds=voice, video_rates=video_rates,
    )

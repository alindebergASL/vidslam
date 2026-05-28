"""Projected cost estimation for a project's video generation.

Costs vary wildly between providers and models, so this is a transparent,
rate-card estimate (configurable via COST_* settings), not a billing figure.
It always reports the *real-provider* projection so a user can preview spend
even while running in mock mode; the actual cost recorded on a Render is 0
when MOCK_PROVIDERS is on (nothing was billed)."""
from __future__ import annotations

from ..config import get_settings
from ..schemas.storyboard import StoryboardPlan


def estimate_plan_cost(project: dict, plan: StoryboardPlan) -> dict:
    """Return a per-line breakdown + total for generating `plan` for `project`.

    `project` is a plain dict with keys: voiceover_source, music_upload_path
    (or a flag), and the music source intent.
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
    video_seconds = sum(sh.duration_seconds for sh in billable_shots)
    video_cost = video_seconds * s.cost_video_per_second
    lines.append({
        "item": f"Video clips ({len(billable_shots)} shots, {video_seconds:.0f}s)",
        "qty": round(video_seconds, 1),
        "unit": "sec",
        "unit_cost": s.cost_video_per_second,
        "cost": round(video_cost, 4),
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

"""Ambient mode: hour-long calm / landscape / spa videos.

An ambient scene is a VideoProject with mode="ambient_loop", exactly one
VideoShot (the loop clip) and the usual Render rows — so share links, render
history, download/export and S3 delivery all work unchanged. What differs is
the pipeline: no storyboard, no voiceover, no captions; one clip, one music
bed, and a seamless-loop compositor (renderer.compose_ambient).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from ..providers import get_image
from ..schemas.ambient import (
    LOOP_OPTIONS,
    TOTAL_OPTIONS,
    AmbientConfig,
    AmbientCreate,
    AmbientOut,
    AmbientPresetsOut,
    AmbientUpdate,
    MusicMoodOut,
    ScenePresetOut,
)
from ..schemas.api import RenderOut, ShotOut
from . import storage
from .renderer import AmbientRenderInputs, _dims_for_aspect, compose_ambient, render_image_drift_clip

log = logging.getLogger("avs.ambient")

AMBIENT_MODE = "ambient_loop"
LOOP_SHOT_TYPE = "ambient_loop"

# Prompt suffix that pushes video models toward loopable, low-motion footage.
_LOOP_DIRECTIVES = (
    " Locked-off static camera, no camera movement, no cuts, no people, no text. "
    "Very slow, gentle, continuous motion only. Consistent lighting from first "
    "frame to last so the clip loops seamlessly. Cinematic, high detail, serene."
)

SCENE_PRESETS: list[ScenePresetOut] = [
    ScenePresetOut(
        id="waterfall", label="Waterfall", emoji="🏞️",
        tagline="Mist, moss and a steady cascade.",
        prompt="A tall forest waterfall pouring into a clear turquoise pool, soft mist drifting, "
               "ferns and moss-covered rocks, dappled morning light through tall trees.",
        music_mood="spa", palette=("#1b5e5a", "#9fd9c8"),
    ),
    ScenePresetOut(
        id="fireplace", label="Fireplace", emoji="🔥",
        tagline="Crackling logs in a stone hearth.",
        prompt="A cozy stone fireplace with glowing logs and gently flickering flames, warm amber "
               "light on the hearth, a few embers rising, dark quiet room around it.",
        music_mood="piano", palette=("#4a1a0a", "#ff9a3c"),
    ),
    ScenePresetOut(
        id="ocean", label="Ocean", emoji="🌊",
        tagline="Slow waves rolling onto an empty shore.",
        prompt="Calm ocean waves rolling slowly onto a wide empty sandy beach at golden hour, "
               "soft foam, distant horizon, pastel sky, gentle sea haze.",
        music_mood="zen", palette=("#0b3a66", "#7fc8ff"),
    ),
    ScenePresetOut(
        id="rainforest", label="Rain on leaves", emoji="🌧️",
        tagline="Soft rain in a green canopy.",
        prompt="Gentle rain falling on broad tropical leaves in a lush rainforest, water droplets "
               "sliding and dripping, soft grey-green light, slight fog between the trees.",
        music_mood="rain", palette=("#143d2b", "#8fd19e"),
    ),
    ScenePresetOut(
        id="snowfall", label="Snowfall", emoji="❄️",
        tagline="Quiet snow over a pine valley.",
        prompt="Slow heavy snowflakes falling over a silent pine forest and a frozen lake at dusk, "
               "blue-hour light, soft shadows, no wind.",
        music_mood="piano", palette=("#1d2a4a", "#dfe8ff"),
    ),
    ScenePresetOut(
        id="zen_garden", label="Zen garden", emoji="🪨",
        tagline="Raked sand, still water, one maple.",
        prompt="A Japanese zen garden with raked white gravel, a still koi pond, a single red "
               "maple tree, soft overcast light, a leaf drifting slowly on the water.",
        music_mood="singing_bowls", palette=("#3b2f2f", "#d98b6a"),
    ),
    ScenePresetOut(
        id="night_sky", label="Night sky", emoji="🌌",
        tagline="Stars slowly turning over the ridge.",
        prompt="A vast starry night sky over a dark mountain ridge, the Milky Way faintly glowing, "
               "stars drifting almost imperceptibly, deep indigo and violet tones.",
        music_mood="zen", palette=("#0a0a2e", "#7c5cff"),
    ),
    ScenePresetOut(
        id="lavender", label="Lavender field", emoji="💜",
        tagline="Rows of lavender in late sun.",
        prompt="Endless rows of lavender swaying very gently in a warm breeze under a soft late-"
               "afternoon sun, hazy hills in the distance, bees out of focus.",
        music_mood="acoustic", palette=("#4b2a6b", "#e9c6ff"),
    ),
    ScenePresetOut(
        id="candles", label="Candlelight", emoji="🕯️",
        tagline="A spa table of soft flames.",
        prompt="A row of soft candles on a dark spa table beside smooth stones and a white orchid, "
               "flames flickering very slowly, warm bokeh, shallow depth of field.",
        music_mood="spa", palette=("#2a1608", "#f2c27b"),
    ),
    ScenePresetOut(
        id="custom", label="Custom scene", emoji="✎",
        tagline="Describe your own place.",
        prompt="",
        music_mood="spa", palette=("#1b1e2d", "#7c5cff"),
    ),
]

MUSIC_MOODS: list[MusicMoodOut] = [
    MusicMoodOut(id="spa", label="Spa",
                 prompt="soft spa ambient music, warm pads, slow evolving textures, no drums, no vocals, "
                        "very calm, loopable"),
    MusicMoodOut(id="zen", label="Zen",
                 prompt="zen meditation music, gentle shakuhachi flute and soft drone, sparse, "
                        "spacious, no drums, no vocals, loopable"),
    MusicMoodOut(id="piano", label="Soft piano",
                 prompt="slow minimalist solo piano with long reverb tails, tender and quiet, "
                        "no drums, no vocals, loopable"),
    MusicMoodOut(id="singing_bowls", label="Singing bowls",
                 prompt="tibetan singing bowls and soft chimes, long sustained resonant tones, "
                        "meditative, no drums, no vocals, loopable"),
    MusicMoodOut(id="rain", label="Rain & nature",
                 prompt="gentle rain and distant soft thunder with faint ambient pad underneath, "
                        "natural, calm, no melody, no vocals, loopable"),
    MusicMoodOut(id="acoustic", label="Acoustic calm",
                 prompt="slow fingerstyle acoustic guitar with soft strings, warm and relaxed, "
                        "no drums, no vocals, loopable"),
    MusicMoodOut(id="none", label="No music", prompt=""),
]

_PRESET_BY_ID = {p.id: p for p in SCENE_PRESETS}
_MOOD_BY_ID = {m.id: m for m in MUSIC_MOODS}


def presets() -> AmbientPresetsOut:
    return AmbientPresetsOut(
        scenes=SCENE_PRESETS,
        music_moods=MUSIC_MOODS,
        loop_options=list(LOOP_OPTIONS),
        total_options=list(TOTAL_OPTIONS),
        aspect_options=["16:9", "9:16", "1:1"],
    )


# ---------- prompt resolution ----------


def resolve_scene_prompt(cfg: AmbientConfig) -> str:
    """User text wins; otherwise the preset's prompt. Always append the loop
    directives — they're what keep a generative video model from panning,
    cutting, or putting a person in the frame."""
    base = cfg.scene_prompt.strip()
    if not base:
        preset = _PRESET_BY_ID.get(cfg.scene_preset)
        base = preset.prompt if preset else ""
    if not base:
        raise ValueError("scene_prompt is empty and the preset has no default prompt")
    if cfg.source == "image_drift":
        return base + " Wide establishing shot, no people, no text, photorealistic, highly detailed."
    return base + _LOOP_DIRECTIVES


def resolve_music_prompt(cfg: AmbientConfig) -> str:
    if cfg.music_mood == "none":
        return ""
    if cfg.music_prompt.strip():
        return cfg.music_prompt.strip()
    mood = _MOOD_BY_ID.get(cfg.music_mood)
    return mood.prompt if mood else _MOOD_BY_ID["spa"].prompt


def music_bed_seconds(cfg: AmbientConfig) -> float:
    """Length of the music bed to generate. Longer than the clip so the audio
    seam and the video seam don't line up (phasing joins read as a loop);
    capped so real music providers stay in their single-call limit."""
    return float(min(90, max(45, cfg.loop_seconds * 2)))


# ---------- cost ----------


def estimate_ambient_cost(cfg: AmbientConfig) -> dict:
    s = get_settings()
    lines: list[dict] = []
    if cfg.source == "image_drift":
        lines.append({
            "item": "Scene still (image)",
            "qty": 1, "unit": "image",
            "unit_cost": s.cost_image_per_item,
            "cost": round(s.cost_image_per_item, 4),
        })
    else:
        secs = float(cfg.loop_seconds)
        lines.append({
            "item": f"Loop clip ({cfg.loop_seconds}s video)",
            "qty": secs, "unit": "sec",
            "unit_cost": s.cost_video_per_second,
            "cost": round(secs * s.cost_video_per_second, 4),
        })
    if cfg.music_mood != "none":
        lines.append({
            "item": f"Music bed ({music_bed_seconds(cfg):.0f}s)",
            "qty": 1, "unit": "track",
            "unit_cost": s.cost_music_per_generation,
            "cost": round(s.cost_music_per_generation, 4),
        })
    unit = _effective_unit_seconds(cfg)
    repeats = max(1, -(-cfg.total_seconds // max(1, int(unit))))
    lines.append({
        "item": f"Loop × {repeats} → {_fmt_duration(cfg.total_seconds)} (FFmpeg stream copy)",
        "qty": repeats, "unit": "repeat",
        "unit_cost": 0.0,
        "cost": 0.0,
    })
    total = round(sum(l["cost"] for l in lines), 4)
    return {
        "currency": "USD",
        "total": total,
        "lines": lines,
        "is_estimate": True,
        "note": "Only the loop clip and music bed hit a provider; the full-length output is "
                "assembled locally at no cost, regardless of total duration.",
    }


def _effective_unit_seconds(cfg: AmbientConfig) -> float:
    secs = cfg.loop_seconds * (cfg.slow_factor if cfg.source == "video" else 1.0)
    cf = 0.0 if cfg.source == "image_drift" else cfg.crossfade_seconds
    return max(1.0, secs - cf)


def _fmt_duration(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h and m:
        return f"{h}h {m:02d}m"
    if h:
        return f"{h}h"
    if m and s:
        return f"{m}m {s:02d}s"
    if m:
        return f"{m}m"
    return f"{s}s"


# ---------- CRUD ----------


def _config_of(project: models.VideoProject) -> AmbientConfig:
    return AmbientConfig.model_validate(project.ambient_config_json or {})


def _loop_shot(project: models.VideoProject) -> Optional[models.VideoShot]:
    for s in project.shots:
        if s.shot_type == LOOP_SHOT_TYPE:
            return s
    return project.shots[0] if project.shots else None


def _ensure_loop_shot(db: Session, project: models.VideoProject, cfg: AmbientConfig) -> models.VideoShot:
    shot = _loop_shot(project)
    if shot is None:
        shot = models.VideoShot(
            project_id=project.id,
            shot_order=0,
            shot_type=LOOP_SHOT_TYPE,
            reference_strategy="input_references",
            status="pending",
        )
        db.add(shot)
    shot.prompt = resolve_scene_prompt(cfg)
    shot.negative_prompt = "people, faces, text, watermark, camera shake, fast motion, cuts, flicker"
    shot.duration_seconds = float(cfg.loop_seconds)
    shot.model_override = cfg.model_override or ""
    shot.camera_direction = "locked-off, static"
    db.flush()
    return shot


def to_out(db: Session, project: models.VideoProject) -> AmbientOut:
    cfg = _config_of(project)
    shot = _loop_shot(project)
    renders = sorted(project.renders, key=lambda r: r.created_at, reverse=True)
    latest = renders[0] if renders else None
    music = Path(project.music_upload_path) if project.music_upload_path else None
    unit = _effective_unit_seconds(cfg)
    return AmbientOut(
        id=project.id,
        title=project.title,
        status=project.status,
        aspect_ratio=project.aspect_ratio,
        total_seconds=int(project.target_duration_seconds),
        config=cfg,
        loop_status=(shot.status if shot else "pending"),
        loop_error=(shot.error if shot else ""),
        loop_shot=ShotOut.model_validate(shot) if shot else None,
        music_ready=bool(music and music.exists()),
        repeats=max(1, -(-int(project.target_duration_seconds) // max(1, int(unit)))),
        latest_render=RenderOut.model_validate(latest) if latest else None,
        render_count=len(renders),
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


def list_scenes(db: Session) -> list[models.VideoProject]:
    return (
        db.query(models.VideoProject)
        .filter(models.VideoProject.mode == AMBIENT_MODE)
        .order_by(models.VideoProject.created_at.desc())
        .all()
    )


def create_scene(db: Session, body: AmbientCreate) -> models.VideoProject:
    cfg = body.config
    # Validate the prompt resolves before we persist anything.
    resolve_scene_prompt(cfg)
    preset = _PRESET_BY_ID.get(cfg.scene_preset)
    title = body.title.strip() or (
        f"{preset.label} — {_fmt_duration(cfg.total_seconds)}" if preset
        else f"Ambient scene — {_fmt_duration(cfg.total_seconds)}"
    )
    project = models.VideoProject(
        title=title,
        original_script="",
        mode=AMBIENT_MODE,
        aspect_ratio=body.aspect_ratio,
        target_duration_seconds=int(cfg.total_seconds),
        cta_text="",
        caption_style="clean_white",
        include_disclosure=False,
        voiceover_source="silent",
        music_volume=float(cfg.music_volume),
        ambient_config_json=cfg.model_dump(),
        status="draft",
    )
    db.add(project)
    db.flush()
    _ensure_loop_shot(db, project, cfg)
    db.commit()
    db.refresh(project)
    return project


def update_scene(db: Session, project: models.VideoProject, body: AmbientUpdate) -> models.VideoProject:
    if body.title is not None:
        project.title = body.title.strip()
    if body.aspect_ratio is not None:
        project.aspect_ratio = body.aspect_ratio
    if body.config is not None:
        cfg = body.config
        resolve_scene_prompt(cfg)
        old = _config_of(project)
        project.ambient_config_json = cfg.model_dump()
        project.target_duration_seconds = int(cfg.total_seconds)
        project.music_volume = float(cfg.music_volume)
        shot = _ensure_loop_shot(db, project, cfg)
        # A different scene/source/length/model invalidates the existing clip —
        # flip it back to pending so the UI asks for a regenerate. Pure
        # compositing knobs (crossfade, fade, volume, total) keep it.
        clip_keys = ("scene_preset", "scene_prompt", "source", "loop_seconds", "model_override")
        if any(getattr(old, k) != getattr(cfg, k) for k in clip_keys) and shot.clip_path:
            shot.status = "pending"
            shot.clip_path = ""
            shot.error = ""
    db.commit()
    db.refresh(project)
    return project


# ---------- generation ----------


def generate_loop(db: Session, project_id: int, *, regenerate_music: bool = True) -> models.VideoShot:
    """Produce the loop clip (video provider, or still + drift) and, unless the
    mood is "none", the music bed. Leaves the project in status "planned"
    (loop ready) or "failed"."""
    from .pipeline import _ensure_clip_for_shot, _log_provider_call, _resolve_model, generate_music_for_project

    project = db.get(models.VideoProject, project_id)
    if project is None or project.mode != AMBIENT_MODE:
        raise ValueError(f"ambient scene {project_id} not found")
    cfg = _config_of(project)
    shot = _ensure_loop_shot(db, project, cfg)
    # Force a fresh clip even if one exists — this is an explicit (re)generate.
    shot.status = "pending"
    shot.clip_path = ""
    shot.error = ""
    project.status = "generating"
    db.commit()

    settings = get_settings()
    try:
        if cfg.source == "image_drift":
            image = get_image()
            model = _resolve_model(
                db, cfg.model_override or settings.openrouter_image_model or "mock/image-default"
            )
            shot.status = "submitted"
            shot.provider = "mock" if settings.image_is_mocked() else "openrouter"
            shot.provider_model = model
            db.commit()
            png = image.generate(model=model, prompt=shot.prompt, references=[],
                                 negative_prompt=shot.negative_prompt or "")
            out_dir = storage.render_subdir(project.id)
            still = out_dir / "ambient_still.png"
            still.write_bytes(png)
            w, h = _dims_for_aspect(project.aspect_ratio)
            clip = out_dir / f"shot_00_{shot.id}.mp4"
            render_image_drift_clip(still, clip, seconds=float(cfg.loop_seconds), width=w, height=h)
            shot.clip_path = str(clip)
            shot.status = "completed"
            db.commit()
            _log_provider_call(
                db, project_id=project.id, provider=type(image).__name__,
                endpoint="image.generate+drift",
                request_summary={"model": model, "seconds": cfg.loop_seconds},
                response_summary={"bytes": len(png)},
            )
        else:
            _ensure_clip_for_shot(db, project, shot)

        # Music: (re)generate when asked, and always when the scene wants a
        # bed but doesn't have one yet — a first "Generate loop" on a draft
        # must produce a complete, renderable scene.
        has_music = bool(project.music_upload_path and Path(project.music_upload_path).exists())
        if regenerate_music or (cfg.music_mood != "none" and not has_music):
            prompt = resolve_music_prompt(cfg)
            if prompt:
                generate_music_for_project(
                    db, project.id, prompt=prompt, duration_seconds=music_bed_seconds(cfg)
                )
            elif project.music_upload_path:
                # Mood switched to "none": drop the stale bed.
                try:
                    Path(project.music_upload_path).unlink(missing_ok=True)
                except OSError:
                    pass
                project.music_upload_path = ""

        project.status = "planned"
        db.commit()
        return shot
    except Exception as e:  # noqa: BLE001
        log.exception("ambient loop generation failed")
        if shot.status != "failed":
            shot.status = "failed"
            shot.error = str(e)[:500]
        project.status = "failed"
        db.commit()
        raise


def render_scene(db: Session, project_id: int) -> models.Render:
    """Tile the loop clip to the full target duration under the music bed."""
    from .pipeline import RenderCancelled, _check_cancel, _music_path_if_any

    project = db.get(models.VideoProject, project_id)
    if project is None or project.mode != AMBIENT_MODE:
        raise ValueError(f"ambient scene {project_id} not found")
    cfg = _config_of(project)
    shot = _loop_shot(project)
    if shot is None or not shot.clip_path or not Path(shot.clip_path).exists():
        raise ValueError("generate the loop clip before rendering")

    settings = get_settings()
    render = models.Render(
        project_id=project.id,
        status="rendering",
        estimated_cost=estimate_ambient_cost(cfg)["total"],
    )
    db.add(render)
    project.status = "generating"
    db.commit()
    db.refresh(render)

    try:
        _check_cancel(db, render)
        outputs = compose_ambient(
            AmbientRenderInputs(
                project_id=project.id,
                out_dir=storage.render_subdir(project.id),
                clip_path=Path(shot.clip_path),
                total_seconds=float(project.target_duration_seconds),
                aspect_ratio=project.aspect_ratio,
                crossfade_seconds=cfg.crossfade_seconds,
                slow_factor=cfg.slow_factor if cfg.source == "video" else 1.0,
                fade_seconds=cfg.fade_seconds,
                music_path=_music_path_if_any(project) if cfg.music_mood != "none" else None,
                music_volume=float(cfg.music_volume),
                clip_is_periodic=(cfg.source == "image_drift"),
            )
        )
        _check_cancel(db, render)
        render.final_video_path = str(outputs.final_video_path)
        render.thumbnail_path = str(outputs.thumbnail_path)
        # Summary line first (it's what the UI shows), ffmpeg tail after.
        render.render_log = (
            f"ambient: unit={outputs.unit_seconds:.2f}s repeats={outputs.repeats} "
            f"total={project.target_duration_seconds}s\n" + outputs.log[-7000:]
        )
        # Nothing is billed at render time — providers were paid in generate_loop.
        render.actual_cost = 0.0 if settings.video_is_mocked() else render.estimated_cost
        render.status = "completed"
        project.status = "completed"
        db.commit()
        return render
    except RenderCancelled:
        render.status = "cancelled"
        render.error = ""
        project.status = "cancelled"
        db.commit()
        return render
    except Exception as e:  # noqa: BLE001
        log.exception("ambient render failed")
        render.status = "failed"
        render.error = str(e)[:800]
        project.status = "failed"
        db.commit()
        raise

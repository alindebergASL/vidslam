"""Ambient mode: hour-long calm / landscape / spa videos.

An ambient scene is a VideoProject with mode="ambient_loop", exactly one
VideoShot (the loop clip) and the usual Render rows — so share links, render
history, download/export and S3 delivery all work unchanged. What differs is
the pipeline: no storyboard, no voiceover, no captions; one clip, one music
bed, and a seamless-loop compositor (renderer.compose_ambient).
"""
from __future__ import annotations

import logging
import math
import re
import secrets
import shutil
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from ..providers import get_image, video_capabilities
from ..providers.base import ImageRef, VideoCapabilities
from ..schemas.ambient import (
    LOOP_OPTIONS,
    TOTAL_OPTIONS,
    AmbientConfig,
    AmbientCreate,
    AmbientOut,
    AmbientPresetsOut,
    AmbientUpdate,
    LoopPlanOut,
    MusicMoodOut,
    ScenePresetOut,
)
from ..schemas.api import RenderOut, ShotOut
from . import storage
from .renderer import (
    AmbientRenderInputs,
    _dims_for_aspect,
    compose_ambient,
    extract_frame,
    join_segments,
    render_image_drift_clip,
)

log = logging.getLogger("avs.ambient")

AMBIENT_MODE = "ambient_loop"
LOOP_SHOT_TYPE = "ambient_loop"
SEGMENT_SHOT_TYPE = "ambient_segment"
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,64}")

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


# ---------- loop planning ----------

JOIN_CROSSFADE = 0.5
# When the chain closes on its anchor frame the seam needs only a hedge
# against the model not hitting the end frame pixel-exactly.
CLOSED_SEAM_CROSSFADE = 0.5


def _video_model_for(cfg: AmbientConfig) -> str:
    s = get_settings()
    return cfg.model_override or s.openrouter_video_model or "mock/video-default"


def _segments_for(target: int, caps: VideoCapabilities) -> list[int]:
    """Fewest calls that reach `target` seconds: full-length clips at the
    model's max, then one remainder clip snapped up to a supported length.
    e.g. 25s on [5,10] → [10,10,5]; 30s on Veo [4,6,8] → [8,8,8,6]."""
    cap = caps.max_seconds
    if target <= cap:
        return [caps.snap(target)]
    n_full, rem = divmod(target, cap)
    segs = [cap] * n_full
    if rem > 0:
        segs.append(caps.snap(rem))
    return segs


def plan_loop(cfg: AmbientConfig, caps: Optional[VideoCapabilities] = None) -> LoopPlanOut:
    """Decide how this scene's loop gets made on the configured model."""
    notes: list[str] = []
    if cfg.source == "image_drift":
        loop = float(cfg.loop_seconds)
        repeats = max(1, math.ceil(cfg.total_seconds / loop))
        return LoopPlanOut(
            model=cfg.model_override or get_settings().openrouter_image_model or "mock/image-default",
            max_clip_seconds=cfg.loop_seconds, caps_known=True, supports_last_frame=False,
            strategy="drift", segments=[cfg.loop_seconds], anchored=False, closed=True,
            join_crossfade=0.0, seam_crossfade=0.0, loop_seconds=loop, unit_seconds=loop,
            repeats=repeats, provider_calls=1,
            notes=["One still + periodic drift: seamless by construction, no video model needed."],
        )

    model = _video_model_for(cfg)
    caps = caps or video_capabilities(model)
    if not caps.known:
        notes.append(
            f"Couldn't read this model's limits; assuming {caps.max_seconds}s per clip "
            "(AMBIENT_DEFAULT_MAX_CLIP_SECONDS)."
        )
    chain_ok = cfg.chain_segments and caps.first_frame
    if cfg.loop_seconds > caps.max_seconds and cfg.chain_segments and not caps.first_frame:
        notes.append("This model can't start from a given frame, so clips can't be chained; "
                     f"using one {caps.max_seconds}s clip.")
    if chain_ok:
        segments = _segments_for(cfg.loop_seconds, caps)
    else:
        segments = [caps.snap(min(cfg.loop_seconds, caps.max_seconds))]
        if cfg.loop_seconds > caps.max_seconds and cfg.chain_segments is False:
            notes.append(f"Single clip: the scene repeats every ~{segments[0]}s.")

    anchored = caps.first_frame and caps.last_frame
    closed = anchored
    n = len(segments)
    join_cf = JOIN_CROSSFADE if n > 1 else 0.0
    seam_cf = min(cfg.crossfade_seconds, CLOSED_SEAM_CROSSFADE) if closed else cfg.crossfade_seconds
    raw = sum(segments) - join_cf * (n - 1)
    loop = raw * cfg.slow_factor
    unit = max(1.0, loop - seam_cf)
    if n > 1:
        notes.append(f"{n} chained clips × up to {caps.max_seconds}s — each starts on the previous clip's last frame.")
    if closed:
        notes.append("Closed loop: an anchor still is both the first and the last frame.")
    elif n >= 1:
        notes.append(f"Open loop: hidden with a {seam_cf:g}s tail→head crossfade.")
    return LoopPlanOut(
        model=model,
        max_clip_seconds=caps.max_seconds,
        caps_known=caps.known,
        supports_last_frame=caps.last_frame,
        strategy="chain" if n > 1 else "single",
        segments=segments,
        anchored=anchored,
        closed=closed,
        join_crossfade=join_cf,
        seam_crossfade=seam_cf,
        loop_seconds=round(loop, 2),
        unit_seconds=round(unit, 2),
        repeats=max(1, math.ceil(cfg.total_seconds / unit)),
        provider_calls=n + (1 if anchored else 0),
        notes=notes,
    )


# ---------- cost ----------


def estimate_ambient_cost(cfg: AmbientConfig, plan: Optional[LoopPlanOut] = None) -> dict:
    s = get_settings()
    plan = plan or plan_loop(cfg)
    lines: list[dict] = []
    if plan.strategy == "drift":
        lines.append({
            "item": "Scene still (image)",
            "qty": 1, "unit": "image",
            "unit_cost": s.cost_image_per_item,
            "cost": round(s.cost_image_per_item, 4),
        })
    else:
        if plan.anchored:
            lines.append({
                "item": "Anchor still (first = last frame)",
                "qty": 1, "unit": "image",
                "unit_cost": s.cost_image_per_item,
                "cost": round(s.cost_image_per_item, 4),
            })
        secs = float(sum(plan.segments))
        n = len(plan.segments)
        label = (
            f"Loop clip ({plan.segments[0]}s video)" if n == 1
            else f"Loop clips ({n} × {'/'.join(str(x) for x in sorted(set(plan.segments), reverse=True))}s = {secs:.0f}s video)"
        )
        lines.append({
            "item": label,
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
    lines.append({
        "item": f"Loop × {plan.repeats} → {_fmt_duration(cfg.total_seconds)} (FFmpeg stream copy)",
        "qty": plan.repeats, "unit": "repeat",
        "unit_cost": 0.0,
        "cost": 0.0,
    })
    total = round(sum(line["cost"] for line in lines), 4)
    return {
        "currency": "USD",
        "total": total,
        "lines": lines,
        "is_estimate": True,
        "note": "Only the loop clip(s), anchor still and music bed hit a provider; the full-length "
                "output is assembled locally at no cost, regardless of total duration.",
        "plan": plan.model_dump(),
    }


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
    return None


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
    plan = plan_loop(cfg)
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
        repeats=plan.repeats,
        plan=plan,
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
        generated = _generated_of(project)
        data = cfg.model_dump()
        if generated:
            data[_GENERATED_KEY] = generated
        project.ambient_config_json = data
        project.target_duration_seconds = int(cfg.total_seconds)
        project.music_volume = float(cfg.music_volume)
        shot = _ensure_loop_shot(db, project, cfg)
        # A different scene/source/length/model invalidates the existing clip —
        # flip it back to pending so the UI asks for a regenerate. Pure
        # compositing knobs (crossfade, fade, volume, total) keep it.
        clip_keys = ("scene_preset", "scene_prompt", "source", "loop_seconds",
                     "model_override", "chain_segments")
        if any(getattr(old, k) != getattr(cfg, k) for k in clip_keys) and shot.clip_path:
            shot.status = "pending"
            shot.clip_path = ""
            shot.error = ""
            data.pop(_GENERATED_KEY, None)
            project.ambient_config_json = data
    db.commit()
    db.refresh(project)
    return project


# ---------- generation ----------

_GENERATED_KEY = "_generated"  # stored next to the config; not user-editable


def _generated_of(project: models.VideoProject) -> dict:
    return dict((project.ambient_config_json or {}).get(_GENERATED_KEY) or {})


def _save_frame_public(src_png: Path) -> tuple[str, str]:
    """Copy a frame into the public frame store; return (token, url).

    Video providers fetch first/last-frame images by URL, so — exactly like
    cast assets — they're served from a random-token route
    (PUBLIC_BASE_URL must be reachable by the provider)."""
    token = secrets.token_urlsafe(32)
    dst_dir = get_settings().data_path / "ambient_frames"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / f"{token}.png"
    shutil.copyfile(src_png, dst)
    url = f"{get_settings().public_base_url.rstrip('/')}/api/public-ambient-frames/{token}"
    return token, url


def frame_path_for_token(token: str) -> Optional[Path]:
    if not _TOKEN_RE.fullmatch(token or ""):
        return None
    p = get_settings().data_path / "ambient_frames" / f"{token}.png"
    return p if p.exists() else None


def _segment_shots(db: Session, project: models.VideoProject, n: int) -> list[models.VideoShot]:
    """Exactly n fresh segment shots (orders 1..n); stale ones are removed."""
    for s in list(project.shots):
        if s.shot_type == SEGMENT_SHOT_TYPE:
            db.delete(s)
    db.flush()
    out = []
    for i in range(n):
        sh = models.VideoShot(
            project_id=project.id, shot_order=i + 1, shot_type=SEGMENT_SHOT_TYPE,
            status="pending", reference_strategy="input_references",
        )
        db.add(sh)
        out.append(sh)
    db.flush()
    return out


def _generate_video_chain(
    db: Session, project: models.VideoProject, shot: models.VideoShot,
    cfg: AmbientConfig, plan: LoopPlanOut,
) -> dict:
    """Produce the loop clip from 1..N provider calls. Returns metadata
    recorded under ambient_config_json._generated."""
    from .pipeline import _ensure_clip_for_shot, _log_provider_call, _resolve_model

    settings = get_settings()
    out_dir = storage.render_subdir(project.id)
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(exist_ok=True)

    anchor_url: Optional[str] = None
    if plan.anchored:
        image = get_image()
        img_model = _resolve_model(db, settings.openrouter_image_model or "mock/image-default")
        still_prompt = (cfg.scene_prompt.strip() or (_PRESET_BY_ID.get(cfg.scene_preset).prompt
                        if _PRESET_BY_ID.get(cfg.scene_preset) else "")) + \
            " Wide establishing shot, no people, no text, photorealistic, highly detailed."
        png = image.generate(model=img_model, prompt=still_prompt, references=[])
        anchor = frames_dir / "anchor.png"
        anchor.write_bytes(png)
        _, anchor_url = _save_frame_public(anchor)
        _log_provider_call(
            db, project_id=project.id, provider=type(image).__name__, endpoint="image.generate(anchor)",
            request_summary={"model": img_model}, response_summary={"bytes": len(png)},
        )

    segs = _segment_shots(db, project, len(plan.segments))
    shot.status = "polling"
    db.commit()
    prev_url = anchor_url
    seg_paths: list[Path] = []
    n = len(segs)
    for i, (seg, dur) in enumerate(zip(segs, plan.segments)):
        seg.prompt = shot.prompt + (
            "" if i == 0 else " Continue the exact same shot from the given first frame, same lighting."
        )
        seg.negative_prompt = shot.negative_prompt
        seg.duration_seconds = float(dur)
        seg.model_override = shot.model_override
        refs: list[ImageRef] = []
        if prev_url:
            refs.append(ImageRef(url=prev_url, role="scene", frame_type="first_frame"))
        if plan.closed and i == n - 1 and anchor_url:
            refs.append(ImageRef(url=anchor_url, role="scene", frame_type="last_frame"))
        seg.reference_strategy = "frame_images" if refs else "input_references"
        db.commit()
        path = _ensure_clip_for_shot(db, project, seg, references=refs)
        seg_paths.append(path)
        if i < n - 1:
            last = extract_frame(path, frames_dir / f"seg{i + 1}_last.png", last=True)
            _, prev_url = _save_frame_public(last)

    clip = out_dir / f"shot_00_{shot.id}.mp4"
    _, length = join_segments(
        seg_paths, clip, aspect_ratio=project.aspect_ratio, crossfade=plan.join_crossfade
    )
    shot.clip_path = str(clip)
    shot.provider = segs[0].provider
    shot.provider_model = segs[0].provider_model
    shot.duration_seconds = round(length, 2)
    shot.status = "completed"
    db.commit()
    return {
        "strategy": plan.strategy,
        "segments": plan.segments,
        "closed": plan.closed,
        "anchored": plan.anchored,
        "model": plan.model,
        "clip_seconds": round(length, 2),
    }


def generate_loop(db: Session, project_id: int, *, regenerate_music: bool = True) -> models.VideoShot:
    """Produce the loop clip (video provider, chained when the model's per-call
    cap is below the loop length; or still + drift) and, unless the mood is
    "none", the music bed. Leaves the project "planned" (loop ready) or
    "failed"."""
    from .pipeline import _log_provider_call, _resolve_model, generate_music_for_project

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
        plan = plan_loop(cfg)
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
            generated = {"strategy": "drift", "segments": [cfg.loop_seconds], "closed": True,
                         "anchored": False, "model": model, "clip_seconds": float(cfg.loop_seconds)}
        else:
            generated = _generate_video_chain(db, project, shot, cfg, plan)

        data = dict(project.ambient_config_json or {})
        data[_GENERATED_KEY] = generated
        project.ambient_config_json = data

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
        db.rollback()
        project = db.get(models.VideoProject, project_id)
        shot = _loop_shot(project)
        if shot is not None and shot.status != "failed":
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
    gen = _generated_of(project)
    closed = bool(gen.get("closed"))

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
        seam = cfg.crossfade_seconds
        if closed and cfg.source == "video":
            seam = min(seam, CLOSED_SEAM_CROSSFADE)
        outputs = compose_ambient(
            AmbientRenderInputs(
                project_id=project.id,
                out_dir=storage.render_subdir(project.id),
                clip_path=Path(shot.clip_path),
                total_seconds=float(project.target_duration_seconds),
                aspect_ratio=project.aspect_ratio,
                crossfade_seconds=seam,
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
        segs = gen.get("segments") or []
        # Summary line first (it's what the UI shows), ffmpeg tail after.
        render.render_log = (
            f"ambient: clips={len(segs) or 1} unit={outputs.unit_seconds:.2f}s "
            f"repeats={outputs.repeats} total={project.target_duration_seconds}s "
            f"seam={'closed' if closed else 'crossfade'}\n" + outputs.log[-7000:]
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

"""Ambient mode: hour-long calm / landscape / spa videos.

An ambient scene is a VideoProject with mode="ambient_loop", exactly one
VideoShot (the loop clip) and the usual Render rows — so share links, render
history, download/export and S3 delivery all work unchanged. What differs is
the pipeline: no storyboard, no voiceover, no captions; one clip, one music
bed, and a seamless-loop compositor (renderer.compose_ambient).
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
import secrets
import shutil
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from ..providers import get_image, provider_label, routes_per_model, video_capabilities
from ..providers import sogni as _sogni
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
    ModelChoiceOut,
    MusicMoodOut,
    OptionOut,
    PhotoRewrite,
    ScenePresetOut,
    SoundPresetOut,
)
from ..schemas.api import RenderOut, ShotOut
from . import alerts, render_files, storage
from .image_settings import best_image_settings
from .model_registry import active_model as _active_model
from .renderer import (
    AmbientRenderInputs,
    _dims_for_aspect,
    best_frame,
    compose_ambient,
    extract_frame,
    join_audio,
    join_segments,
    render_image_drift_clip,
)

log = logging.getLogger("avs.ambient")

AMBIENT_MODE = "ambient_loop"
LOOP_SHOT_TYPE = "ambient_loop"
SEGMENT_SHOT_TYPE = "ambient_segment"
# Variations 2..N of a less-repetition scene (variation 1 is the loop shot).
VARIANT_SHOT_TYPE = "ambient_variant"
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,64}")

# Prompt suffix that pushes video models toward loopable, low-motion footage.
_LOOP_DIRECTIVES = (
    " Locked-off static camera, no camera movement, no cuts, no people, no text. "
    "Very slow, gentle, continuous motion only. Consistent lighting and style from "
    "first frame to last so the clip loops seamlessly."
)

SCENE_PRESETS: list[ScenePresetOut] = [
    ScenePresetOut(
        id="waterfall", label="Waterfall", emoji="🏞️",
        tagline="Mist, moss and a steady cascade.",
        prompt="A tall waterfall in an old-growth temperate rainforest in the Pacific Northwest. A creek "
               "spills over the top of a dark basalt cliff about 30 metres high and falls into a shallow, "
               "clear pool of grey and brown stones of irregular sizes, then flows out between mossy "
               "boulders. Damp, muted green moss, sword ferns, a crooked fallen log, leaf litter, Douglas "
               "firs and western red cedars.",
        music_mood="spa", palette=("#1b5e5a", "#9fd9c8"),
    ),
    ScenePresetOut(
        id="fireplace", label="Fireplace", emoji="🔥",
        tagline="Logs burning down in a stone firebox.",
        prompt="A close view into the firebox of an old stone fireplace. Three split logs of different sizes "
               "burn on a bed of ash; one has burned through and broken into glowing chunks. Small, "
               "irregular flames, blackened bark, white ash on the ends of the logs, soot-stained bricks "
               "behind.",
        music_mood="piano", palette=("#4a1a0a", "#ff9a3c"),
    ),
    ScenePresetOut(
        id="ocean", label="Ocean", emoji="🌊",
        tagline="Slow waves rolling onto an empty shore.",
        prompt="A quiet stretch of ocean beach. Low waves break and wash up the wet sand in thin sheets of "
               "foam, with a few strands of kelp and small shells on the sand, a rocky headland in the "
               "distance and a flat, level horizon.",
        music_mood="zen", palette=("#0b3a66", "#7fc8ff"),
    ),
    ScenePresetOut(
        id="rainforest", label="Rain on leaves", emoji="🌧️",
        tagline="Rain landing on a banana leaf.",
        prompt="A close view of rain on a single large banana leaf and the ferns around it in a rainforest. "
               "Raindrops land and burst on the leaf, small beads of water collect along its ribs and drip "
               "from its torn edge; behind it, dark wet foliage out of focus.",
        music_mood="rain", palette=("#143d2b", "#8fd19e"),
    ),
    ScenePresetOut(
        id="snowfall", label="Snowfall", emoji="❄️",
        tagline="Quiet snow over a pine valley.",
        prompt="A quiet pine forest beside a frozen lake in winter. Large, slow snowflakes falling; "
               "snow-laden spruce and pine of uneven heights, a snow-covered shoreline with a few dark rocks "
               "and reeds poking through, the far shore fading into the falling snow.",
        music_mood="piano", palette=("#1d2a4a", "#dfe8ff"),
    ),
    ScenePresetOut(
        id="zen_garden", label="Zen garden", emoji="🪨",
        tagline="Raked sand, still water, one maple.",
        prompt="A small Japanese zen garden at a temple in Kyoto. Raked white gravel in parallel lines "
               "around moss-edged rocks, a still pond with a few koi just below the surface, a Japanese "
               "maple with red leaves, some fallen on the moss and water, the weathered edge of a wooden "
               "veranda in the foreground.",
        music_mood="singing_bowls", palette=("#3b2f2f", "#d98b6a"),
    ),
    ScenePresetOut(
        id="night_sky", label="Night sky", emoji="🌌",
        tagline="Stars slowly turning over the ridge.",
        prompt="The night sky over a dark mountain ridge in a remote desert. The Milky Way rises above the "
               "ridge in faint, natural colour, with thousands of stars of different brightness, a few "
               "silhouetted pines on the ridge and faint airglow near the horizon.",
        music_mood="zen", palette=("#0a0a2e", "#7c5cff"),
    ),
    ScenePresetOut(
        id="lavender", label="Lavender field", emoji="💜",
        tagline="Rows of lavender in late sun.",
        prompt="A lavender field in Provence: long, slightly uneven rows of lavender running toward low "
               "hills, bare dry soil between them, a stone farmhouse and a line of trees in the distance, a "
               "few bees on the nearest stems.",
        music_mood="acoustic", palette=("#4b2a6b", "#e9c6ff"),
    ),
    ScenePresetOut(
        id="candles", label="Candlelight", emoji="🕯️",
        tagline="Two candles on a quiet spa shelf.",
        prompt="Two white pillar candles burning on a slate shelf, one shorter and burned unevenly with a "
               "cracked rim, a little black soot on the inside of its well. Beside them a single smooth grey "
               "stone and a folded linen towel.",
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

# ---------- look, light, weather, sound ----------

# Look → the style phrase sent to the image/video model. Photoreal is the
# default and ignores strength; its prompt is built as a photograph (see
# still_prompt), because "photorealistic, highly detailed" and stacked
# adjectives pulled every model toward the glossy AI look in a blind test.
LOOKS: dict[str, tuple[str, str]] = {
    "photoreal": ("Photoreal", "a real, unedited photograph with natural, true-to-life colour"),
    "cinematic": ("Cinematic film", "cinematic film still, rich color grade, shallow depth of field, "
                                    "subtle 35mm film grain"),
    "painterly": ("Painterly", "oil painting, visible brush strokes, impressionist palette"),
    "watercolor": ("Watercolor", "watercolor illustration, soft washes, paper texture"),
    "anime": ("Hand-drawn anime", "hand-drawn anime background art, painted clouds, soft cel shading"),
    "claymation": ("Claymation", "stop-motion claymation miniature diorama, handcrafted clay textures"),
    "dreamy": ("Dreamy", "dreamy and ethereal, soft focus, gentle glow, pastel haze"),
}
# Light and weather as plain descriptions of the light, not mood words:
# "golden hour" and "glow" brought sunstars and god rays with every model.
TIMES_OF_DAY: dict[str, tuple[str, str]] = {
    "any": ("As written", ""),
    "dawn": ("Dawn", "Early morning before sunrise: cool, dim light from a pale sky, a little low mist, "
                     "no direct sun."),
    "golden_hour": ("Golden hour", "Late-afternoon sun low in the sky from one side, warm light on the "
                                   "upper parts of the scene while the lower parts are in shade; the sun "
                                   "itself is out of frame, and there are no visible rays."),
    "midday": ("Midday", "Bright, even midday light."),
    "dusk": ("Dusk", "About twenty minutes after sunset: dim, even blue-grey light with no direct sun, "
                     "colours muted."),
    "night": ("Night", "At night, lit only by moonlight, with deep blue shadows and much of the scene "
                       "in darkness."),
}
WEATHERS: dict[str, tuple[str, str]] = {
    "any": ("As written", ""),
    "clear": ("Clear", "A clear sky."),
    "light_rain": ("Light rain", "Light, gentle rain is falling."),
    "heavy_rain": ("Heavy rain", "Heavy, steady rain is falling."),
    "snow": ("Snow", "Soft snow is falling."),
    "fog": ("Fog", "Fog drifts slowly through the scene."),
}

# Scenes lit by their own fire or candles: a time of day describes outside.
INDOOR_PRESETS = frozenset({"fireplace", "candles"})
# Each preset's own light (used when no time of day is chosen, so a chosen
# time replaces it instead of contradicting it) and camera (photoreal only).
PRESET_LIGHT: dict[str, str] = {
    "waterfall": "Soft, even light on a bright overcast day, with deep natural shadows under the trees.",
    "fireplace": "Lit only by the fire, warm and uneven, with deep shadows at the edges of the frame.",
    "ocean": "Soft late-afternoon light under thin high cloud, with a little sea haze over the water.",
    "rainforest": "Dim, flat light under heavy cloud.",
    "snowfall": "Flat, cool light on an overcast winter afternoon.",
    "zen_garden": "Soft overcast daylight.",
    "night_sky": "No moon; the landscape is lit only by starlight.",
    "lavender": "Warm late-afternoon sun from the side, with light haze over the hills.",
    "candles": "Warm, dim candlelight only, with soft falloff into a dark background.",
}
PRESET_CAMERA: dict[str, str] = {
    "waterfall": "Shot on a full-frame camera with a 24 mm lens at f/8 and 1/250 s from a tripod at eye "
                 "level, so the falling water keeps its texture rather than a silky blur.",
    "fireplace": "Shot on a full-frame camera with a 50 mm lens at f/2.8 and 1/125 s from a tripod, slightly "
                 "underexposed with natural sensor noise in the shadows.",
    "ocean": "Shot on a full-frame camera with a 24 mm lens at f/8 and 1/250 s from a tripod at standing "
             "height, horizon level.",
    "rainforest": "Shot on a full-frame camera with a 100 mm macro lens at f/4 and 1/1000 s from a tripod, "
                  "so the nearest drops are frozen and the background rain is invisible.",
    "snowfall": "Shot on a full-frame camera with a 35 mm lens at f/5.6 and 1/250 s from a tripod.",
    "zen_garden": "Shot on a full-frame camera with a 35 mm lens at f/8 from a tripod at seated height.",
    "night_sky": "Shot on a full-frame camera with a 20 mm f/1.8 lens, a 15-second exposure at ISO 3200 "
                 "on a tripod, with natural sensor noise.",
    "lavender": "Shot on a full-frame camera with a 35 mm lens at f/8 and 1/500 s from a tripod at waist "
                "height.",
    "candles": "Shot on a full-frame camera with a 85 mm lens at f/2 and 1/60 s from a tripod, natural "
               "sensor noise in the dark areas.",
}
# Closing lines for the photoreal look: what a real photo / real footage is,
# and what to avoid (OpenAI's own realism guidance uses the same wording).
PHOTO_STILL = ("Off-centre, natural composition. Natural, true-to-life colours with no colour grading. "
               "It should look like a real photograph someone could have taken, not an enhanced, "
               "retouched or cinematic image.")
PHOTO_VIDEO = ("Natural, true-to-life colour with no colour grading, like real camera footage, not an "
               "enhanced or cinematic video.")

SOUND_PRESETS: list[SoundPresetOut] = [
    SoundPresetOut(id="rain", label="Rain", prompt="steady gentle rain on leaves and ground, close and calm, no thunder"),
    SoundPresetOut(id="thunderstorm", label="Thunderstorm",
                   prompt="heavy rain with distant rolling thunder, calm and enveloping"),
    SoundPresetOut(id="fireplace", label="Crackling fire", prompt="crackling fireplace, soft pops and embers, cozy"),
    SoundPresetOut(id="ocean", label="Ocean waves", prompt="slow ocean waves washing onto a sandy shore"),
    SoundPresetOut(id="stream", label="Flowing stream", prompt="gentle forest stream flowing over smooth stones"),
    SoundPresetOut(id="waterfall", label="Waterfall", prompt="distant waterfall, steady rushing water, soft and even"),
    SoundPresetOut(id="forest", label="Forest birds",
                   prompt="quiet forest ambience with soft birdsong and light breeze in the leaves"),
    SoundPresetOut(id="night", label="Night crickets", prompt="calm summer night, crickets and soft distant frogs"),
    SoundPresetOut(id="wind", label="Soft wind", prompt="soft winter wind, gentle and even, no gusts"),
]
_SOUND_BY_ID = {p.id: p for p in SOUND_PRESETS}
# "auto": weather wins (rain on screen should sound like rain), then the scene.
_AUTO_SOUND_BY_WEATHER = {"light_rain": "rain", "heavy_rain": "thunderstorm", "snow": "wind"}
_AUTO_SOUND_BY_SCENE = {
    "waterfall": "waterfall", "fireplace": "fireplace", "ocean": "ocean", "rainforest": "rain",
    "snowfall": "wind", "zen_garden": "stream", "night_sky": "night", "lavender": "forest",
    "candles": "fireplace",
}


def look_phrase(cfg: AmbientConfig) -> str:
    """The look alone, shaped by its strength (photoreal ignores strength)."""
    name, phrase = LOOKS.get(cfg.look, LOOKS["photoreal"])
    if cfg.look == "photoreal":
        return phrase
    if cfg.look_strength < 0.34:
        return f"mostly realistic with a subtle {name.lower()} touch ({phrase})"
    if cfg.look_strength <= 0.67:
        return f"in a {name.lower()} style: {phrase}"
    return f"fully stylized, {phrase}, strongly {name.lower()}"


def light_phrase(cfg: AmbientConfig, *, preset_light: bool = True) -> str:
    """Time of day and weather as sentences. With neither chosen, a preset
    scene keeps its own light (custom text describes its own). A preset's
    light describes its sky, so a chosen weather replaces it too. Indoor
    presets stay lit by their fire or candles; time and weather are outside."""
    time_of_day = TIMES_OF_DAY.get(cfg.time_of_day, ("", ""))[1]
    weather = WEATHERS.get(cfg.weather, ("", ""))[1]
    own = PRESET_LIGHT.get(cfg.scene_preset, "") if preset_light and not cfg.scene_prompt.strip() else ""
    if cfg.scene_preset in INDOOR_PRESETS:
        label = TIMES_OF_DAY.get(cfg.time_of_day, ("", ""))[0].lower() if time_of_day else ""
        outside = [f"It is {label} outside." if label else "",
                   f"Outside, {weather[0].lower()}{weather[1:]}" if weather else ""]
        return " ".join(x for x in (own, *outside) if x)
    light = time_of_day or (own if not weather else "")
    return " ".join(x for x in (light, weather) if x)


def style_phrase(cfg: AmbientConfig) -> str:
    """Light + weather + look, as one phrase (for summaries and stylized looks)."""
    return " ".join(x for x in (light_phrase(cfg, preset_light=False), look_phrase(cfg)) if x)


def _text_fingerprint(text: str) -> str:
    return hashlib.sha1(text.strip().encode()).hexdigest()[:16]


def active_rewrite(cfg: AmbientConfig) -> Optional[PhotoRewrite]:
    """The photo rewrite of the typed description, when it applies: a
    photoreal scene with its own text, rewriting on, and a rewrite made from
    exactly this text (an edited description waits for a new one)."""
    text = cfg.scene_prompt.strip()
    rw = cfg.photo
    if (cfg.look != "photoreal" or not cfg.rewrite_prompt or not text or rw is None
            or cfg.picture_origin in OWN_PICTURE or not rw.scene.strip()
            or rw.source != _text_fingerprint(text)):
        return None
    return rw


# Pictures the user brought: their text describes motion, not a place to invent.
OWN_PICTURE = frozenset({"upload", "studio", "asset"})


def _with_current_rewrite(db: Session, project: models.VideoProject, cfg: AmbientConfig) -> AmbientConfig:
    """Before generating: make a rewrite the scene doesn't have yet (one that
    failed at save time, or a scene from before rewrites) and store it, so
    the new clip or picture and the stored prompt agree. The caller commits.

    The rewrite can take up to a minute, so the row is re-read before the
    rewrite is written into it: a copy loaded earlier would overwrite
    whatever was saved meanwhile."""
    new = ensure_photo_rewrite(cfg)
    if new.photo == cfg.photo:
        return cfg
    db.refresh(project)
    fresh = _config_of(project)
    if _rewrite_inputs(fresh) != _rewrite_inputs(cfg):
        return fresh  # the text changed meanwhile: this rewrite is for the old one
    _set_state(project, "photo", new.photo.model_dump() if new.photo else None)
    return fresh.model_copy(update={"photo": new.photo})


def _rewrite_inputs(cfg: AmbientConfig) -> tuple:
    return (cfg.scene_prompt.strip(), cfg.look, cfg.rewrite_prompt, cfg.picture_origin)


def _rewrite_key(cfg: AmbientConfig) -> Optional[tuple[str, str, str]]:
    rw = active_rewrite(cfg)
    return (rw.scene, rw.light, rw.camera) if rw else None


def needs_photo_rewrite(cfg: AmbientConfig) -> bool:
    """Whether ensure_photo_rewrite would call the planner for this config."""
    text = cfg.scene_prompt.strip()
    if cfg.look != "photoreal" or not cfg.rewrite_prompt or not text or cfg.picture_origin in OWN_PICTURE:
        return False
    return not (cfg.photo is not None and cfg.photo.source == _text_fingerprint(text)
                and cfg.photo.scene.strip())


def _rewrite_affordable() -> bool:
    """The planner call is paid: none once the monthly budget cap is hit
    (the same check budget_guard makes)."""
    from ..db import SessionLocal
    from . import spend

    db = SessionLocal()
    try:
        return spend.over_budget(db) is None
    except Exception as e:  # noqa: BLE001 — an unreadable budget shouldn't block a ~$0.001 call
        log.warning("couldn't read the budget before a scene rewrite (%s)", str(e)[:200])
        return True
    finally:
        db.close()


def ensure_photo_rewrite(cfg: AmbientConfig, *, allow: Optional[Callable[[], bool]] = None) -> AmbientConfig:
    """Make (or keep) the photo rewrite of a typed description: one short
    call to the planner model, only when the text changed. On any failure
    the scene just uses the text as typed. Over budget, or when `allow`
    (asked only right before a call) says no, it is skipped the same way:
    generating later makes the missing rewrite."""
    if not needs_photo_rewrite(cfg):
        return cfg
    if not _rewrite_affordable() or (allow is not None and not allow()):
        log.info("scene description rewrite skipped (budget or rate limit); using it as typed")
        return cfg.model_copy(update={"photo": None})
    text = cfg.scene_prompt.strip()
    try:
        from ..providers import get_chat

        out = get_chat().rewrite_scene(text)
        rw = PhotoRewrite(source=_text_fingerprint(text), scene=out.get("scene", "")[:3000],
                          light=out.get("light", "")[:1000], camera=out.get("camera", "")[:1000])
    except Exception as e:  # noqa: BLE001 — the typed text still works
        log.warning("scene description rewrite failed (%s); using it as typed", str(e)[:300])
        return cfg.model_copy(update={"photo": None})
    return cfg.model_copy(update={"photo": rw if rw.scene.strip() else None})


def _light_with_rewrite(cfg: AmbientConfig, rw: Optional[PhotoRewrite]) -> str:
    """A chosen time of day or weather replaces the rewrite's light, except
    indoors, where the fire or candles stay the light and time and weather
    are what's outside."""
    chosen = light_phrase(cfg)
    if not rw:
        return chosen
    if cfg.scene_preset in INDOOR_PRESETS:
        return " ".join(x for x in (_sentence(rw.light), chosen) if x)
    return chosen or _sentence(rw.light)


def _scene_text(cfg: AmbientConfig) -> str:
    preset = _PRESET_BY_ID.get(cfg.scene_preset)
    return cfg.scene_prompt.strip() or (preset.prompt if preset else "")


def _sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return text
    text = text[0].upper() + text[1:]
    return text if text[-1] in ".!?" else text + "."


def resolve_sound_prompt(cfg: AmbientConfig) -> str:
    """'' = no sound bed. A custom sound_prompt wins over the preset."""
    if cfg.sound == "none":
        return ""
    if cfg.sound_prompt.strip():
        return cfg.sound_prompt.strip()
    sid = cfg.sound
    if sid == "auto":
        sid = _AUTO_SOUND_BY_WEATHER.get(cfg.weather) or _AUTO_SOUND_BY_SCENE.get(cfg.scene_preset, "")
    preset = _SOUND_BY_ID.get(sid)
    return (preset.prompt + ", seamless loop, no music, no voices") if preset else ""


def sound_path(project: models.VideoProject) -> Path:
    return storage.render_subdir(project.id) / "sound.mp3"


def generate_sound_bed(db: Session, project: models.VideoProject, cfg: AmbientConfig) -> Optional[Path]:
    """One 30 s looping clip; the renderer loops it (with a crossfaded seam)
    under the music for the full length."""
    from ..providers.registry import get_sound
    from .pipeline import _log_provider_call

    prompt = resolve_sound_prompt(cfg)
    out = sound_path(project)
    if not prompt:
        out.unlink(missing_ok=True)
        return None
    sound = get_sound()
    data = sound.generate(prompt=prompt, duration_seconds=30)
    out.write_bytes(data)
    _log_provider_call(db, project_id=project.id, provider=type(sound).__name__, endpoint="sound.generate",
                       request_summary={"sound": cfg.sound, "prompt": prompt[:120]},
                       response_summary={"bytes": len(data)})
    return out

_PRESET_BY_ID = {p.id: p for p in SCENE_PRESETS}
_MOOD_BY_ID = {m.id: m for m in MUSIC_MOODS}


def preview_path(preset_id: str) -> Path:
    d = get_settings().data_path / "ambient_previews"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{re.sub(r'[^a-z0-9_]', '', preset_id)}.png"


def _preview_fingerprint(preset_id: str) -> str:
    """Changes whenever the preset's still prompt does, so a preview made
    from an older prompt isn't shown as what the preset looks like now."""
    return hashlib.sha1(still_prompt(AmbientConfig(scene_preset=preset_id)).encode()).hexdigest()[:16]


def preview_is_current(preset_id: str) -> bool:
    path = preview_path(preset_id)
    stamp = path.with_suffix(".prompt")
    return path.exists() and stamp.exists() and stamp.read_text().strip() == _preview_fingerprint(preset_id)


# ---------- models for stills and loop clips ----------

# Shown first in the scene builder, with what they're like in practice
# (blind realism tests, Nov 2026). Every other catalog model stays selectable.
STILL_MODEL_CHOICES: list[ModelChoiceOut] = [
    ModelChoiceOut(id="openai/gpt-image-2.6-sunburst", label="GPT Image 2.6 Sunburst",
                   note="Most realistic in a blind test. About $0.13 and 2 minutes per still.",
                   est_cost=0.13, recommended=True),
    ModelChoiceOut(id="google/gemini-nano-banana-2.1", label="Nano Banana 2.1",
                   note="A close second, faster and cheaper: about $0.05 per still at 2K.", est_cost=0.055),
    ModelChoiceOut(id="black-forest-labs/flux-3-image", label="FLUX.3",
                   note="A different look, about $0.10 per still. Less realistic in the test.", est_cost=0.10),
]
VIDEO_MODEL_CHOICES: list[ModelChoiceOut] = [
    ModelChoiceOut(id="alibaba/wan-3.0", label="Wan 3.0",
                   note="Keeps the still's look, with the steadiest background. About $0.15–0.20 per second.",
                   recommended=True),
    ModelChoiceOut(id="google/gemini-omni-1.2-flash", label="Gemini Omni 1.2 Flash",
                   note="New and untested here: it was failing on OpenRouter in Nov 2026. Clips up to 15 s.",
                   experimental=True),
    ModelChoiceOut(id="google/gemini-omni-1.1-flash", label="Gemini Omni 1.1 Flash",
                   note="Livelier water, but it redrew rock with an embossed texture in a test.",
                   experimental=True),
]
_STILL_COST = {c.id: c.est_cost for c in STILL_MODEL_CHOICES if c.est_cost is not None}


def still_model_chain(cfg: AmbientConfig) -> list[str]:
    """Image models to try for this scene's stills, in order: the scene's
    own choice (or, for an older Still + drift scene, its image-model
    override), else AMBIENT_STILL_MODEL, else the active image model; then
    the fallback model if that call fails."""
    s = get_settings()
    active = _active_model("image")
    legacy = cfg.model_override if cfg.source == "image_drift" else ""
    if legacy and _is_video_model(legacy):
        legacy = ""  # a video model left over from before the switch to Still + drift
    primary = cfg.still_model or legacy or s.ambient_still_model or active
    if primary.startswith("custom:"):
        return [primary]  # the scene's own trained model: a different model isn't a substitute
    chain = [primary, s.ambient_still_fallback_model or active]
    return [m for i, m in enumerate(chain) if m and m not in chain[:i]]


def makes_stills(cfg: AmbientConfig) -> bool:
    """Whether the scene's stills come from the still model rather than its
    own picture: always without a picture of the scene's own; with one, only
    for Day to night's other times of day. (A video scene also has to make
    stills at all: see uses_still_model.)"""
    return not cfg.picture_origin or (cfg.variety == "day_night" and variant_count(cfg) > 1)


def uses_still_model(cfg: AmbientConfig) -> bool:
    """Whether generating this scene calls the still model. Still + drift
    always makes its stills (except where its own picture is the still); a
    video scene only when its plan starts clips from a still (an anchor or
    a first frame), as _start_video_chain does. Takes share the first
    still, so with the scene's own picture they make none."""
    if cfg.source == "image_drift":
        return makes_stills(cfg)
    plan = plan_loop(cfg)
    return (plan.first_frame_still or plan.anchored) and makes_stills(cfg)


def _is_video_model(model: str) -> bool:
    if model in {c.id for c in VIDEO_MODEL_CHOICES} or model == _active_model("video"):
        return True
    try:
        from . import model_catalog

        return model_catalog.find("video", model) is not None
    except Exception:  # noqa: BLE001 — catalog unavailable: assume it's an image model
        return False


def _takes_reference(model: str) -> bool:
    """False for Sogni picture models: Sogni's workflow API doesn't apply a
    source image yet (see sogni.ImageModel.img2img)."""
    return not (_sogni.is_sogni(model) and not _sogni.takes_source_image(model))


def reference_still_chain(cfg: AmbientConfig) -> list[str]:
    """still_model_chain for a still made from a reference picture (Day to
    night's "same place, other light"): the models that apply the reference
    first, if a real provider serves them; the others, which would make it
    from the text alone, after, as the last resort."""
    from .model_registry import provider_for

    chain = still_model_chain(cfg)
    mock = get_settings().mock_providers
    first = [m for m in chain if _takes_reference(m) and (mock or provider_for("image", m) != "mock")]
    return first + [m for m in chain if m not in first]


def still_cost(cfg: AmbientConfig) -> float:
    """Rate-card cost of one still on this scene's primary still model."""
    chain = still_model_chain(cfg)
    return _still_model_cost(chain[0] if chain else "")


def reference_still_cost(cfg: AmbientConfig) -> float:
    """Rate-card cost of a "same place" still (see reference_still_chain)."""
    chain = reference_still_chain(cfg)
    return _still_model_cost(chain[0] if chain else "")


def _still_model_cost(model: str) -> float:
    s = get_settings()
    if model and model == s.ambient_still_model:
        return s.cost_ambient_still
    if _sogni.is_sogni(model):
        return _sogni.image_spend_usd(model)
    return _STILL_COST.get(model, s.cost_image_per_item)


def _video_rate(model: str) -> float:
    """Rate-card USD per second of loop video on `model`."""
    if _sogni.is_sogni(model):
        return _sogni.video_spend_rate_usd(model)
    return get_settings().cost_video_per_second


def generate_still(db: Session, cfg: AmbientConfig, *, prompt: str, aspect_ratio: str,
                   references: Optional[list[ImageRef]] = None, negative_prompt: str = "",
                   project_id: Optional[int] = None, endpoint: str = "image.generate(still)",
                   summary: Optional[dict] = None) -> tuple[bytes, str]:
    """One still for this scene, at the model's best settings. If the
    primary model fails (an error, a timeout, a refused prompt), retry once
    with the fallback model. Returns (image bytes, model used)."""
    from .pipeline import _log_provider_call, _resolve_model

    last: Optional[Exception] = None
    for model in (reference_still_chain(cfg) if references else still_model_chain(cfg)):
        model = _resolve_model(db, model)
        image = None
        # A model that can't apply the reference makes the still from text:
        # don't describe a reference it never sees.
        call_prompt = prompt if not references or _takes_reference(model) else prompt.replace(_SAME_PLACE, "")
        try:
            # Per model: a sogni/ primary can fall back to an OpenRouter model.
            image = get_image(model) if routes_per_model("image", model) else get_image()
            png = image.generate(model=model, prompt=call_prompt, references=references or [],
                                 negative_prompt=negative_prompt,
                                 settings=best_image_settings(model, aspect_ratio))
        except Exception as e:  # noqa: BLE001 — try the fallback model
            last = e
            log.warning("still with %s failed (%s); trying the next model", model, str(e)[:300])
            # Recorded (status=error) so the provider-error alerts see it.
            _log_provider_call(db, project_id=project_id,
                               provider=type(image).__name__ if image is not None else "image",
                               endpoint=endpoint,
                               request_summary={"model": model, **(summary or {})},
                               response_summary={"error": str(e)[:500]}, status="error")
            continue
        _log_provider_call(db, project_id=project_id, provider=type(image).__name__, endpoint=endpoint,
                           request_summary={"model": model, **(summary or {})},
                           response_summary={"bytes": len(png)})
        return png, model
    raise last or RuntimeError("no image model configured for scene stills")


def generate_preview(db: Session, preset_id: str) -> Path:
    """Generate (or regenerate) a preset's preview still with the scene-still
    model (see still_model_chain) and cache it."""
    preset = _PRESET_BY_ID.get(preset_id)
    if preset is None or not preset.prompt:
        raise ValueError(f"preset {preset_id!r} has no prompt")
    cfg = AmbientConfig(scene_preset=preset_id)
    png, _ = generate_still(db, cfg, prompt=still_prompt(cfg), aspect_ratio="16:9",
                            endpoint="image.generate(preview)", summary={"preset": preset_id})
    out = preview_path(preset_id)
    out.write_bytes(png)
    out.with_suffix(".prompt").write_text(_preview_fingerprint(preset_id))
    return out


def presets() -> AmbientPresetsOut:
    scenes = []
    for p in SCENE_PRESETS:
        path = preview_path(p.id)
        url = (f"/api/ambient/presets/{p.id}/preview?v={int(path.stat().st_mtime)}"
               if preview_is_current(p.id) else None)
        scenes.append(p.model_copy(update={"preview_url": url}))
    return AmbientPresetsOut(
        scenes=scenes,
        music_moods=MUSIC_MOODS,
        sounds=SOUND_PRESETS,
        looks=[OptionOut(id=k, label=v[0]) for k, v in LOOKS.items()],
        times_of_day=[OptionOut(id=k, label=v[0]) for k, v in TIMES_OF_DAY.items()],
        weathers=[OptionOut(id=k, label=v[0]) for k, v in WEATHERS.items()],
        loop_options=list(LOOP_OPTIONS),
        total_options=list(TOTAL_OPTIONS),
        aspect_options=["16:9", "9:16", "1:1"],
        still_models=STILL_MODEL_CHOICES,
        video_models=VIDEO_MODEL_CHOICES,
        default_still_model=still_model_chain(AmbientConfig())[0] if still_model_chain(AmbientConfig()) else "",
        default_video_model=_active_model("video"),
    )


# ---------- scene picture ----------

PICTURE_MAX_SIDE = 3840  # keeps 4K output sharp; larger uploads are scaled down
PICTURE_MIN_SIDE = 480


class PictureError(ValueError):
    """Not a usable picture (bad type, too small, unreadable)."""


def picture_path(project: models.VideoProject) -> Path:
    return storage.render_subdir(project.id) / "picture.png"


def has_picture(project: models.VideoProject, cfg: Optional[AmbientConfig] = None) -> bool:
    cfg = cfg or _config_of(project)
    return bool(cfg.picture_origin) and picture_path(project).exists()


def _crop_to_aspect(raw: bytes, aspect: str) -> bytes:
    """Center-crop to the scene's aspect and cap the size; PNG out."""
    import io

    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img).convert("RGB")
    except (UnidentifiedImageError, OSError) as e:
        raise PictureError("not a readable image") from e
    if min(img.size) < PICTURE_MIN_SIDE:
        raise PictureError(f"picture is too small ({img.size[0]}×{img.size[1]}); "
                           f"use at least {PICTURE_MIN_SIDE}px on the short side")
    tw, th = _dims_for_aspect(aspect)
    target = tw / th
    w, h = img.size
    if w / h > target:  # too wide: trim the sides
        nw = round(h * target)
        img = img.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
    elif w / h < target:  # too tall: trim top and bottom
        nh = round(w / target)
        img = img.crop((0, (h - nh) // 2, w, (h - nh) // 2 + nh))
    if max(img.size) > PICTURE_MAX_SIDE:
        img.thumbnail((PICTURE_MAX_SIDE, PICTURE_MAX_SIDE), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _reset_clips(project: models.VideoProject) -> None:
    """Mark the loop clip and every variation's clip as needing a regenerate."""
    for shot in project.shots:
        if shot.shot_type in (LOOP_SHOT_TYPE, VARIANT_SHOT_TYPE):
            shot.status, shot.clip_path, shot.error = "pending", "", ""


def _invalidate_loop(project: models.VideoProject) -> None:
    """The loop clip was made from a different image: it needs regenerating."""
    shot = _loop_shot(project)
    if shot is not None and shot.clip_path:
        _reset_clips(project)
    data = dict(project.ambient_config_json or {})
    data.pop(_GENERATED_KEY, None)
    project.ambient_config_json = data


def set_picture(db: Session, project: models.VideoProject, raw: bytes, origin: str) -> models.VideoProject:
    png = _crop_to_aspect(raw, project.aspect_ratio)
    picture_path(project).write_bytes(png)
    _invalidate_loop(project)
    _set_state(project, "picture_origin", origin)
    db.commit()
    db.refresh(project)
    return project


def clear_picture(db: Session, project: models.VideoProject) -> models.VideoProject:
    picture_path(project).unlink(missing_ok=True)
    if _config_of(project).picture_origin:
        _invalidate_loop(project)
    _set_state(project, "picture_origin", "")
    db.commit()
    db.refresh(project)
    return project


def still_prompt(cfg: AmbientConfig) -> str:
    """Prompt for the scene's still. Photoreal is written as a photograph
    (scene, then its light, then the camera, then what real photos look
    like); other looks are the scene, its light and the style."""
    rw = active_rewrite(cfg)
    base = _sentence(rw.scene if rw else _scene_text(cfg))
    light = _light_with_rewrite(cfg, rw)
    if cfg.look == "photoreal":
        if rw:
            camera = _sentence(rw.camera)
        else:
            camera = "" if cfg.scene_prompt.strip() else PRESET_CAMERA.get(cfg.scene_preset, "")
        parts = ["A real, unedited photograph.", base, light, camera, PHOTO_STILL,
                 "No people, no text, no watermark."]
    else:
        parts = [base, light, f"Wide establishing shot, {look_phrase(cfg)}.", "No people, no text."]
    return " ".join(x for x in parts if x)


def generate_picture(db: Session, project: models.VideoProject) -> models.VideoProject:
    """Generate the scene's picture now (one image call), so it can be
    previewed and regenerated before any video is made."""
    cfg = _with_current_rewrite(db, project, _config_of(project))
    # Same model choice as the loop path (still_model_chain), so the picture
    # previewed here is what the scene would have made anyway.
    png, _ = generate_still(db, cfg, prompt=still_prompt(cfg), aspect_ratio=project.aspect_ratio,
                            project_id=project.id, endpoint="image.generate(picture)")
    return set_picture(db, project, png, "generated")


def picture_from(db: Session, project: models.VideoProject, *, asset_id: Optional[int] = None,
                 studio_job_id: Optional[int] = None) -> models.VideoProject:
    if asset_id is not None:
        asset = db.get(models.Asset, asset_id)
        if asset is None or not asset.file_path or not Path(asset.file_path).exists():
            raise LookupError("asset not found")
        return set_picture(db, project, Path(asset.file_path).read_bytes(), "asset")
    if studio_job_id is not None:
        job = db.get(models.AssetGenerationJob, studio_job_id)
        if (job is None or job.output_kind != "image" or job.status not in ("completed", "saved")
                or not job.result_path or not Path(job.result_path).exists()):
            raise LookupError("studio image not found")
        return set_picture(db, project, Path(job.result_path).read_bytes(), "studio")
    raise PictureError("give asset_id or studio_job_id")


# ---------- prompt resolution ----------


def resolve_scene_prompt(cfg: AmbientConfig) -> str:
    """User text wins; otherwise the preset's prompt. Always append the loop
    directives — they're what keep a generative video model from panning,
    cutting, or putting a person in the frame."""
    if cfg.picture_origin and cfg.source == "video":
        # The clip starts from the scene's own picture: animate that image as
        # it is rather than describing the preset or restyling it. Light and
        # weather still guide the motion (falling snow, drifting fog).
        what = cfg.scene_prompt.strip() or "the scene in the given image"
        extras = light_phrase(cfg, preset_light=False)
        return (f"Bring the given image to life exactly as it is: {_sentence(what)}"
                + (f" {extras}" if extras else "")
                + " Keep its composition, colors and art style unchanged." + _LOOP_DIRECTIVES)
    base = _scene_text(cfg)
    if not base:
        raise ValueError("scene_prompt is empty and the preset has no default prompt")
    if cfg.source == "image_drift":
        return still_prompt(cfg)
    rw = active_rewrite(cfg)
    light = _light_with_rewrite(cfg, rw)
    if cfg.look == "photoreal":
        parts = ["Real, unedited camera footage.", _sentence(rw.scene if rw else base), light, PHOTO_VIDEO]
    else:
        parts = [_sentence(base), light, f"Rendered {look_phrase(cfg)}."]
    return " ".join(x for x in parts if x) + _LOOP_DIRECTIVES


# ---------- less repetition: variations ----------

# Day → night, by how many variations: always ends at night.
DAY_NIGHT_SEQUENCES: dict[int, list[str]] = {
    2: ["golden_hour", "night"],
    3: ["golden_hour", "dusk", "night"],
    4: ["midday", "golden_hour", "dusk", "night"],
    5: ["dawn", "midday", "golden_hour", "dusk", "night"],
}
# Small, different motion for each take, so the takes don't look identical.
_TAKE_HINTS = (
    "",
    " A soft breeze moves through the scene now and then.",
    " Soft clouds drift slowly and the light shifts gently.",
    " A little mist drifts slowly across the scene.",
    " Small details stir gently: leaves, ripples, a slow flicker of light.",
)
_SAME_PLACE = (" Exactly the same place, framing and composition as the reference image; "
               "only the time of day and the light change.")


def variant_count(cfg: AmbientConfig) -> int:
    """Loop clips the scene is made of (1 = no variety)."""
    if cfg.variety == "day_night":
        return cfg.variations
    if cfg.variety == "takes" and cfg.source == "video":
        return cfg.variations
    return 1


def variant_config(cfg: AmbientConfig, i: int) -> AmbientConfig:
    """The config variation i is made from (day → night sets its light)."""
    if cfg.variety == "day_night" and variant_count(cfg) > 1:
        return cfg.model_copy(update={"time_of_day": DAY_NIGHT_SEQUENCES[cfg.variations][i]})
    return cfg


def variant_labels(cfg: AmbientConfig) -> list[str]:
    k = variant_count(cfg)
    if k == 1:
        return []
    if cfg.variety == "day_night":
        return [TIMES_OF_DAY[t][0] for t in DAY_NIGHT_SEQUENCES[k]]
    return [f"Take {i + 1}" for i in range(k)]


def variant_prompt(cfg: AmbientConfig, i: int) -> str:
    """Scene prompt for variation i."""
    prompt = resolve_scene_prompt(variant_config(cfg, i))
    if cfg.variety == "takes" and variant_count(cfg) > 1:
        prompt += _TAKE_HINTS[i % len(_TAKE_HINTS)]
    return prompt


def overlay_summary(cfg: AmbientConfig) -> str:
    """e.g. "title+watermark+countdown" ("" when there are none)."""
    parts = [n for n, on in (("title", cfg.overlay_title), ("watermark", cfg.watermark)) if on]
    if cfg.timer != "none":
        parts.append(cfg.timer)
    return "+".join(parts)


def overlay_note(cfg: AmbientConfig) -> Optional[str]:
    if cfg.timer == "none":
        return None
    return ("Timer: the finished video is re-encoded once to draw the clock, so rendering takes "
            "several minutes longer per hour of video. No provider cost.")


def transition_seconds(cfg: AmbientConfig) -> float:
    """Dissolve between variations: short between takes of the same scene,
    long and gentle when the light changes."""
    return 6.0 if cfg.variety == "day_night" else max(1.0, min(cfg.crossfade_seconds or 2.0, 3.0))


def resolve_music_prompt(cfg: AmbientConfig) -> str:
    if cfg.music_mood == "none":
        return ""
    if cfg.music_prompt.strip():
        return cfg.music_prompt.strip()
    mood = _MOOD_BY_ID.get(cfg.music_mood)
    return mood.prompt if mood else _MOOD_BY_ID["spa"].prompt


def music_bed_seconds(cfg: AmbientConfig) -> float:
    """Length of the music bed. Auto: 45–90 s, longer than the clip so the
    audio and video seams don't line up. An explicit music_bed_seconds
    (e.g. 10 min) repeats far less over an hour."""
    if cfg.music_bed_seconds:
        return float(cfg.music_bed_seconds)
    return float(min(90, max(45, cfg.loop_seconds * 2)))


MUSIC_JOIN_CROSSFADE = 6.0


def music_cap_seconds(model: Optional[str] = None) -> float:
    """Longest track one music call returns, per active model."""
    m = model if model is not None else _active_model("music")
    if "lyria-3-clip" in m:
        return 30.0
    if m.startswith("google/lyria"):
        return 180.0  # "a couple of minutes", prompt-steered
    return 300.0  # ElevenLabs Music: 3 s – 5 min per call (and the mock)


def music_parts(cfg: AmbientConfig, model: Optional[str] = None) -> list[float]:
    """Per-call lengths that crossfade into a bed of music_bed_seconds."""
    total = music_bed_seconds(cfg)
    cap = music_cap_seconds(model)
    if total <= cap:
        return [total]
    n = math.ceil((total - MUSIC_JOIN_CROSSFADE) / (cap - MUSIC_JOIN_CROSSFADE))
    each = (total + MUSIC_JOIN_CROSSFADE * (n - 1)) / n
    return [round(min(cap, each), 1)] * n


def generate_music_bed(db: Session, project: models.VideoProject, cfg: AmbientConfig, prompt: str) -> Path:
    """One call when the bed fits the provider's cap; otherwise several
    tracks in the same mood, crossfaded into one long bed."""
    from ..providers import get_music
    from .pipeline import _log_provider_call, generate_music_for_project

    parts = music_parts(cfg)
    if len(parts) == 1:
        return generate_music_for_project(db, project.id, prompt=prompt, duration_seconds=parts[0])
    music = get_music()
    out_dir = storage.render_subdir(project.id)
    files: list[Path] = []
    for i, secs in enumerate(parts):
        variation = (
            f" Part {i + 1} of {len(parts)} of one continuous piece: keep the same key, tempo, "
            "instruments and mood; let it evolve gently, no ending or fade."
        )
        data = music.generate(prompt=prompt + variation, duration_seconds=secs)
        ext = (music.output_extension() or "mp3").lstrip(".")
        f = out_dir / f"music_part_{i + 1}.{ext}"
        f.write_bytes(data)
        files.append(f)
        _log_provider_call(
            db, project_id=project.id, provider=type(music).__name__, endpoint="music.generate(part)",
            request_summary={"part": i + 1, "of": len(parts), "duration": secs},
            response_summary={"bytes": len(data)},
        )
    for stale in out_dir.glob("music.*"):
        stale.unlink(missing_ok=True)
    bed, _ = join_audio(files, out_dir / "music.m4a", crossfade=MUSIC_JOIN_CROSSFADE)
    for f in files:
        f.unlink(missing_ok=True)
    project.music_upload_path = str(bed)
    db.commit()
    return bed


# ---------- loop planning ----------

JOIN_CROSSFADE = 0.5
# When the chain closes on its anchor frame the seam needs only a hedge
# against the model not hitting the end frame pixel-exactly.
CLOSED_SEAM_CROSSFADE = 0.5


def _video_model_for(cfg: AmbientConfig) -> str:
    return cfg.model_override or _active_model("video")


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


def caps_first_frame(cfg: AmbientConfig) -> bool:
    """Can the scene's video model start a clip from a given image?"""
    return video_capabilities(_video_model_for(cfg)).first_frame


def plan_loop(cfg: AmbientConfig, caps: Optional[VideoCapabilities] = None) -> LoopPlanOut:
    """Decide how this scene's loop gets made on the configured model."""
    notes: list[str] = []
    k = variant_count(cfg)
    labels = variant_labels(cfg)
    timer_note = overlay_note(cfg)
    if cfg.variety == "takes" and cfg.source == "image_drift":
        notes.append("Mix of takes needs a video scene; Still + drift uses one still. "
                     "Try Day to night instead.")
    if cfg.source == "image_drift":
        loop = float(cfg.loop_seconds)
        repeats = max(1, math.ceil(cfg.total_seconds / loop))
        notes.insert(0, "One still + periodic drift: seamless by construction, no video model needed."
                     if k == 1 else f"{k} stills ({', '.join(labels)}) + periodic drift, "
                     "each dissolving into the next.")
        return LoopPlanOut(
            model=(still_model_chain(cfg) or [""])[0],
            max_clip_seconds=cfg.loop_seconds, caps_known=True, supports_last_frame=False,
            strategy="drift", segments=[cfg.loop_seconds], anchored=False, closed=True,
            join_crossfade=0.0, seam_crossfade=0.0, loop_seconds=loop, unit_seconds=loop,
            repeats=repeats, provider_calls=k, notes=notes + ([timer_note] if timer_note else []),
            variants=k, variant_labels=labels, repeat_every_seconds=loop,
        )

    model = _video_model_for(cfg)
    caps = caps or video_capabilities(model)
    if not caps.known:
        notes.append(
            f"Couldn't read this model's limits; assuming {caps.max_seconds}s per clip "
            "(AMBIENT_DEFAULT_MAX_CLIP_SECONDS)."
        )
    if cfg.picture_origin and cfg.source == "video" and not caps.first_frame and not caps.needs_reference:
        notes.append("This model can't start a clip from an image, so your picture isn't used "
                     "for the video. Pick a model that takes a first frame, or use Still + drift.")
    # First + last frame only models can't make the open middle clips of a chain.
    chain_ok = cfg.chain_segments and caps.first_frame and not caps.both_frames_only
    if cfg.loop_seconds > caps.max_seconds and cfg.chain_segments and not caps.first_frame:
        notes.append("This model can't start from a given frame, so clips can't be chained; "
                     f"using one {caps.max_seconds}s clip.")
    elif cfg.loop_seconds > caps.max_seconds and cfg.chain_segments and caps.both_frames_only:
        notes.append("This model needs a first and a last frame for every clip, so clips can't be "
                     f"chained; using one {caps.max_seconds}s clip.")
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
    # With variations, a first-frame-only model still gets a shared start
    # still, so the takes / times of day show the same view. A model that
    # can't work from text alone always gets one: as its first frame, or
    # (reference models) as its reference picture.
    first_still = (anchored or (caps.first_frame and (bool(cfg.picture_origin) or k > 1 or caps.needs_first_frame))
                   or caps.needs_reference)
    anchor_calls = 1 if (anchored or (first_still and (k > 1 or not cfg.picture_origin))) else 0
    if caps.needs_reference:
        notes.append(("Your picture" if cfg.picture_origin else "A still of the scene") + " is this model's "
                     "reference picture: a loose guide, not an exact first frame.")
    elif caps.needs_first_frame and not anchored and not cfg.picture_origin:
        notes.append("This model needs a starting picture, so a still of the scene is made first.")
    repeat_every = unit
    if k > 1 and cfg.variety == "takes":
        repeat_every = unit * k
        notes.append(f"{k} takes of the scene, played in turn: the picture repeats every "
                     f"~{_fmt_duration(int(repeat_every))} instead of every {unit:.0f}s.")
    elif k > 1:
        anchor_calls = k if first_still else 0
        notes.append(f"Day to night: {' → '.join(labels)}, ~{_fmt_duration(int(cfg.total_seconds / k))} "
                     "each, with a slow dissolve between them.")
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
        provider_calls=n * k + anchor_calls,
        notes=notes + ([timer_note] if timer_note else []),
        variants=k,
        variant_labels=labels,
        repeat_every_seconds=round(repeat_every, 2),
        first_frame_still=first_still,
    )


# ---------- cost ----------


def estimate_ambient_cost(cfg: AmbientConfig, plan: Optional[LoopPlanOut] = None) -> dict:
    s = get_settings()
    plan = plan or plan_loop(cfg)
    lines: list[dict] = []
    own = bool(cfg.picture_origin)  # the scene's own picture: no image call at loop time
    k = plan.variants
    # Day to night needs its own still for each time of day after the first.
    more_stills = k - 1 if cfg.variety == "day_night" else 0
    if plan.strategy == "drift":
        lines.append({
            "item": "Your picture (no image call)" if own else "Scene still (image)",
            "qty": 1, "unit": "image",
            "unit_cost": 0.0 if own else still_cost(cfg),
            "cost": 0.0 if own else round(still_cost(cfg), 4),
        })
        if more_stills:
            lines.append({
                "item": f"Stills for {', '.join(plan.variant_labels[1:])}",
                "qty": more_stills, "unit": "image", "unit_cost": reference_still_cost(cfg),
                "cost": round(reference_still_cost(cfg) * more_stills, 4),
            })
    else:
        if own:
            lines.append({
                "item": "Your picture as the first frame" + (" = last frame" if plan.anchored else ""),
                "qty": 1, "unit": "image", "unit_cost": 0.0, "cost": 0.0,
            })
        elif plan.anchored or plan.first_frame_still:
            lines.append({
                "item": "Anchor still (first = last frame)" if plan.anchored
                        else "Shared first frame (image)" if k > 1 else "Starting still (image)",
                "qty": 1, "unit": "image",
                "unit_cost": still_cost(cfg),
                "cost": round(still_cost(cfg), 4),
            })
        if more_stills and plan.first_frame_still:
            lines.append({
                "item": f"Anchor stills for {', '.join(plan.variant_labels[1:])}",
                "qty": more_stills, "unit": "image", "unit_cost": reference_still_cost(cfg),
                "cost": round(reference_still_cost(cfg) * more_stills, 4),
            })
        secs = float(sum(plan.segments))
        n = len(plan.segments)
        label = (
            f"Loop clip ({plan.segments[0]}s video)" if n == 1
            else f"Loop clips ({n} × {'/'.join(str(x) for x in sorted(set(plan.segments), reverse=True))}s = {secs:.0f}s video)"
        )
        if k > 1:
            label += f" × {k} variations"
            secs *= k
        lines.append({
            "item": label,
            "qty": secs, "unit": "sec",
            "unit_cost": _video_rate(plan.model),
            "cost": round(secs * _video_rate(plan.model), 4),
        })
    if cfg.music_mood != "none":
        parts = music_parts(cfg)
        lines.append({
            "item": f"Music bed ({_fmt_duration(int(music_bed_seconds(cfg)))}"
                    + (f", {len(parts)} tracks crossfaded)" if len(parts) > 1 else ")"),
            "qty": len(parts), "unit": "track",
            "unit_cost": s.cost_music_per_generation,
            "cost": round(s.cost_music_per_generation * len(parts), 4),
        })
    if resolve_sound_prompt(cfg):
        lines.append({
            "item": "Nature sound (30 s seamless loop)",
            "qty": 1, "unit": "sound",
            "unit_cost": s.cost_sound_per_generation,
            "cost": round(s.cost_sound_per_generation, 4),
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
    _shape_clip_shot(shot, cfg, 0)
    db.flush()
    _ensure_variant_shots(db, project, cfg)
    return shot


_NEGATIVE = "people, faces, text, watermark, camera shake, fast motion, cuts, flicker"


def _shape_clip_shot(shot: models.VideoShot, cfg: AmbientConfig, i: int) -> None:
    shot.prompt = variant_prompt(cfg, i)
    shot.negative_prompt = _NEGATIVE
    shot.duration_seconds = float(cfg.loop_seconds)
    shot.model_override = cfg.model_override or ""
    shot.camera_direction = "locked-off, static"


def _variant_shots(project: models.VideoProject) -> list[models.VideoShot]:
    return sorted((s for s in project.shots if s.shot_type == VARIANT_SHOT_TYPE),
                  key=lambda s: s.shot_order)


def _ensure_variant_shots(db: Session, project: models.VideoProject,
                          cfg: AmbientConfig) -> list[models.VideoShot]:
    """Exactly variant_count - 1 variation shots (orders 101..), shaped for cfg."""
    want = variant_count(cfg) - 1
    have = _variant_shots(project)
    if len(have) > want:
        for extra in have[want:]:
            db.delete(extra)
        db.flush()
        db.expire(project, ["shots"])
        have = have[:want]
    for i in range(len(have), want):
        sh = models.VideoShot(project_id=project.id, shot_order=101 + i, shot_type=VARIANT_SHOT_TYPE,
                              status="pending", reference_strategy="input_references")
        db.add(sh)
        have.append(sh)
    for i, sh in enumerate(have, start=1):
        _shape_clip_shot(sh, cfg, i)
    db.flush()
    return have


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
        variant_shots=[ShotOut.model_validate(v) for v in _variant_shots(project)[:variant_count(cfg) - 1]],
        music_ready=bool(music and music.exists()),
        sound_ready=cfg.sound != "none" and sound_path(project).exists(),
        picture_ready=has_picture(project, cfg),
        picture_version=(str(picture_path(project).stat().st_mtime_ns) if has_picture(project, cfg) else ""),
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
    cfg = ensure_photo_rewrite(body.config)
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


class SceneBusy(Exception):
    """The scene started generating while a save was in progress (the API answers 409)."""


def update_scene(db: Session, project: models.VideoProject, body: AmbientUpdate, *,
                 allow_rewrite: Optional[Callable[[], bool]] = None) -> models.VideoProject:
    cfg: Optional[AmbientConfig] = None
    if body.config is not None:
        cfg = body.config
        resolve_scene_prompt(cfg)
        old = _config_of(project)
        # Keep a rewrite the page didn't send back (an older page, a partial edit).
        if cfg.photo is None and old.photo is not None:
            cfg = cfg.model_copy(update={"photo": old.photo})
        # Rewrite only when what it depends on changed, so an unrelated save
        # never swaps the prompt the existing clip was made from. This is a
        # network call of up to a minute, so it comes before anything is
        # changed here, and the row is re-read after it.
        if (cfg.scene_prompt.strip() != old.scene_prompt.strip() or cfg.rewrite_prompt != old.rewrite_prompt
                or cfg.look != old.look):
            probe = cfg.model_copy(update={"picture_origin": old.picture_origin})
            if needs_photo_rewrite(probe):
                cfg = cfg.model_copy(update={"photo": ensure_photo_rewrite(probe, allow=allow_rewrite).photo})
                db.expire_all()
    # A generation that started meanwhile (the API checked before the call)
    # owns the scene's state now.
    if project.status == "generating":
        raise SceneBusy("scene is generating; wait or cancel first")

    if body.title is not None:
        project.title = body.title.strip()
    aspect_changed = body.aspect_ratio is not None and body.aspect_ratio != project.aspect_ratio
    if body.aspect_ratio is not None:
        project.aspect_ratio = body.aspect_ratio
    if aspect_changed:
        # The clip was generated (and drift stills rendered) for the old
        # aspect; reusing it would letterbox it into the new frame.
        shot = _loop_shot(project)
        if shot is not None and shot.clip_path:
            _reset_clips(project)
            _set_state(project, _GENERATED_KEY, None)
    if cfg is not None:
        old = _config_of(project)
        # The picture is set through its own endpoints; a settings save
        # (maybe from a page loaded before the upload) must not clear it.
        cfg = cfg.model_copy(update={"picture_origin": old.picture_origin})
        # Internal state (_generated, _chain, ...) isn't part of the config
        # the page sends; keep it.
        data = {k: v for k, v in (project.ambient_config_json or {}).items() if k.startswith("_")}
        data.update(cfg.model_dump())
        project.ambient_config_json = data
        project.target_duration_seconds = int(cfg.total_seconds)
        project.music_volume = float(cfg.music_volume)
        shot = _ensure_loop_shot(db, project, cfg)
        # A different scene/source/length/model invalidates the existing clip —
        # flip it back to pending so the UI asks for a regenerate. Pure
        # compositing knobs (crossfade, fade, volume, total) keep it.
        clip_keys = ("scene_preset", "scene_prompt", "source", "loop_seconds",
                     "model_override", "chain_segments", "look", "look_strength",
                     "time_of_day", "weather", "variety", "variations")
        # The still model only matters when generating this scene calls it.
        still_changed = old.still_model != cfg.still_model and uses_still_model(cfg)
        # The rewrite changes the prompt only when one applies before or after.
        rewrite_changed = _rewrite_key(old) != _rewrite_key(cfg)
        if ((any(getattr(old, k) != getattr(cfg, k) for k in clip_keys) or still_changed or rewrite_changed)
                and shot.clip_path):
            _reset_clips(project)
            _set_state(project, _GENERATED_KEY, None)
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


_CHAIN_KEY = "_chain"  # in-flight chain state (cleared when the loop is done)


def _set_state(project: models.VideoProject, key: str, value: Optional[object]) -> None:
    data = dict(project.ambient_config_json or {})
    if value is None:
        data.pop(key, None)
    else:
        data[key] = value
    project.ambient_config_json = data


def _start_video_chain(
    db: Session, project: models.VideoProject, shot: models.VideoShot,
    cfg: AmbientConfig, plan: LoopPlanOut, *, regenerate_music: bool, render_after: bool,
    variant: int = 0, anchor0_url: Optional[str] = None,
) -> None:
    """Make the anchor still (if any), create the segment shots and submit
    segment 1. Each completion submits the next (on_segment_ready); the last
    one joins them and finishes this variation (_finish_chain), which starts
    the next variation's chain or finishes the scene.

    Variations share the first one's anchor ("takes": every loop closes on
    the same frame) or get their own still made from it ("day_night": the
    same place in different light)."""
    out_dir = storage.render_subdir(project.id)
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(exist_ok=True)
    anchor_url: Optional[str] = None
    picture = has_picture(project, cfg)
    if plan.first_frame_still or plan.anchored or (picture and caps_first_frame(cfg)):
        if variant > 0 and cfg.variety == "takes" and anchor0_url:
            anchor_url = anchor0_url
        else:
            # The scene's own picture when it has one; otherwise generate the still.
            anchor = frames_dir / ("anchor.png" if variant == 0 else f"anchor_{variant}.png")
            if picture and variant == 0:
                shutil.copyfile(picture_path(project), anchor)
            else:
                refs = [ImageRef(url=anchor0_url, role="scene")] if variant > 0 and anchor0_url else []
                prompt = still_prompt(variant_config(cfg, variant)) + (_SAME_PLACE if refs else "")
                png, _ = generate_still(db, cfg, prompt=prompt, aspect_ratio=project.aspect_ratio,
                                        references=refs, project_id=project.id,
                                        endpoint="image.generate(anchor)", summary={"variant": variant})
                anchor.write_bytes(png)
            _, anchor_url = _save_frame_public(anchor)
    if variant == 0:
        anchor0_url = anchor_url
    segs = _segment_shots(db, project, len(plan.segments))
    for i, (seg, dur) in enumerate(zip(segs, plan.segments, strict=True)):
        seg.prompt = shot.prompt + (
            "" if i == 0 else " Continue the exact same shot from the given first frame, same lighting."
        )
        seg.negative_prompt = shot.negative_prompt
        seg.duration_seconds = float(dur)
        seg.model_override = shot.model_override
    shot.status = "polling"
    _set_state(project, _CHAIN_KEY, {
        "segment_ids": [s.id for s in segs], "anchor_url": anchor_url, "plan": plan.model_dump(),
        "regenerate_music": regenerate_music, "render_after": render_after,
        "variant": variant, "shot_id": shot.id, "anchor0_url": anchor0_url,
    })
    db.commit()
    _submit_segment(db, project, 0, prev_url=anchor_url)


def _submit_segment(db: Session, project: models.VideoProject, index: int, *, prev_url: Optional[str]) -> None:
    from .pipeline import submit_shot_clip

    chain = (project.ambient_config_json or {}).get(_CHAIN_KEY) or {}
    plan = LoopPlanOut.model_validate(chain["plan"])
    seg = db.get(models.VideoShot, chain["segment_ids"][index])
    n = len(chain["segment_ids"])
    refs: list[ImageRef] = []
    if prev_url:
        refs.append(ImageRef(url=prev_url, role="scene", frame_type="first_frame"))
    if plan.closed and index == n - 1 and chain.get("anchor_url"):
        refs.append(ImageRef(url=chain["anchor_url"], role="scene", frame_type="last_frame"))
    seg.reference_strategy = "frame_images" if refs else "input_references"
    db.commit()
    submit_shot_clip(db, project, seg, purpose="ambient_segment", after="none",
                     references=refs, context={"index": index})


def on_segment_ready(db: Session, job: models.VideoJob) -> None:
    project = db.get(models.VideoProject, job.project_id)
    seg = db.get(models.VideoShot, job.ref_id)
    if project is None or seg is None:
        return
    chain = (project.ambient_config_json or {}).get(_CHAIN_KEY)
    if not chain:
        return  # chain was superseded (scene regenerated / edited)
    seg.clip_path = job.result_path
    seg.status = "completed"
    db.commit()
    index = int((job.context_json or {}).get("index", 0))
    n = len(chain["segment_ids"])
    try:
        if index < n - 1:
            frames_dir = storage.render_subdir(project.id) / "frames"
            frames_dir.mkdir(exist_ok=True)
            last = extract_frame(Path(seg.clip_path), frames_dir / f"seg{index + 1}_last.png", last=True)
            _, prev_url = _save_frame_public(last)
            _submit_segment(db, project, index + 1, prev_url=prev_url)
        else:
            _finish_chain(db, project)
    except Exception as e:  # noqa: BLE001
        _fail_generation(db, project.id, e)


def on_segment_failed(db: Session, job: models.VideoJob) -> None:
    seg = db.get(models.VideoShot, job.ref_id)
    if seg is not None:
        seg.status = "failed"
        seg.error = job.error[:500]
        db.commit()
    if job.project_id:
        _fail_generation(db, job.project_id, RuntimeError(f"segment failed: {job.error[:300]}"))


def _finish_chain(db: Session, project: models.VideoProject) -> None:
    chain = (project.ambient_config_json or {}).get(_CHAIN_KEY) or {}
    plan = LoopPlanOut.model_validate(chain["plan"])
    cfg = _config_of(project)
    loop = _loop_shot(project)
    shot = (db.get(models.VideoShot, chain["shot_id"]) if chain.get("shot_id") else None) or loop
    segs = [db.get(models.VideoShot, i) for i in chain["segment_ids"]]
    clip = storage.render_subdir(project.id) / f"shot_00_{shot.id}.mp4"
    _, length = join_segments([Path(s.clip_path) for s in segs], clip,
                              aspect_ratio=project.aspect_ratio, crossfade=plan.join_crossfade)
    shot.clip_path = str(clip)
    shot.provider = segs[0].provider
    shot.provider_model = segs[0].provider_model
    shot.duration_seconds = round(length, 2)
    variant, k = int(chain.get("variant") or 0), variant_count(cfg)
    _set_state(project, _CHAIN_KEY, None)
    if variant < k - 1:
        # More variations to make: the loop shot stays "polling" until the last.
        if variant > 0:
            shot.status = "completed"
        db.commit()
        nxt = _variant_shots(project)[variant]
        _start_video_chain(db, project, nxt, cfg, plan,
                           regenerate_music=bool(chain.get("regenerate_music", True)),
                           render_after=bool(chain.get("render_after")),
                           variant=variant + 1, anchor0_url=chain.get("anchor0_url"))
        return
    shot.status = "completed"
    loop.status = "completed"
    generated = {"strategy": plan.strategy, "segments": plan.segments, "closed": plan.closed,
                 "anchored": plan.anchored, "model": plan.model, "clip_seconds": round(loop.duration_seconds, 2),
                 "variants": k, "variety": cfg.variety if k > 1 else "none"}
    db.commit()
    _finalize_generation(db, project, cfg, plan, generated,
                         regenerate_music=bool(chain.get("regenerate_music", True)),
                         render_after=bool(chain.get("render_after")))


def _finalize_generation(
    db: Session, project: models.VideoProject, cfg: AmbientConfig, plan: LoopPlanOut,
    generated: dict, *, regenerate_music: bool, render_after: bool,
) -> None:
    """Record the loop, make the music bed, mark the scene ready (and
    render it when a batch asked for that)."""
    generated["estimated_cost"] = estimate_ambient_cost(cfg, plan)["total"]
    mocked = provider_label("image" if plan.strategy == "drift" else "video", plan.model) == "mock"
    generated["billed"] = 0.0 if mocked else generated["estimated_cost"]
    _set_state(project, _GENERATED_KEY, generated)
    db.commit()
    # Music: (re)generate when asked, and always when the scene wants a bed
    # but doesn't have one yet — a first generate must yield a renderable scene.
    has_music = bool(project.music_upload_path and Path(project.music_upload_path).exists())
    if regenerate_music or (cfg.music_mood != "none" and not has_music):
        prompt = resolve_music_prompt(cfg)
        if prompt:
            generate_music_bed(db, project, cfg, prompt)
        elif project.music_upload_path:
            Path(project.music_upload_path).unlink(missing_ok=True)
            project.music_upload_path = ""
    # Nature sound: same rule as music.
    if regenerate_music or (cfg.sound != "none" and not sound_path(project).exists()):
        generate_sound_bed(db, project, cfg)
    project.status = "planned"
    db.commit()
    if render_after:
        from ..workers.jobs import render_ambient_job
        from ..workers.queue import enqueue

        enqueue(render_ambient_job, project.id)


def _fail_generation(db: Session, project_id: int, e: Exception) -> None:
    log.error("ambient loop generation failed for %s: %s", project_id, e)
    db.rollback()
    project = db.get(models.VideoProject, project_id)
    shot = _loop_shot(project)
    if shot is not None and shot.status != "failed":
        shot.status = "failed"
        shot.error = str(e)[:500]
    _set_state(project, _CHAIN_KEY, None)
    project.status = "failed"
    db.commit()
    alerts.render_failed("ambient loop", project_id, project.title, str(e))


def generate_loop(db: Session, project_id: int, *, regenerate_music: bool = True,
                  render_after: bool = False) -> models.VideoShot:
    """Start producing the loop clip. Still + drift finishes here; a video
    loop is submitted segment by segment and finishes asynchronously
    (webhook / sweeper) — the scene goes "planned" when it's ready, and is
    rendered then if `render_after`."""
    from .pipeline import _log_provider_call

    project = db.get(models.VideoProject, project_id)
    if project is None or project.mode != AMBIENT_MODE:
        raise ValueError(f"ambient scene {project_id} not found")
    # Mark the scene generating before anything slow (the rewrite below is a
    # planner call of up to a minute), so the API's 409 turns away a second
    # Generate and a settings save in the meantime.
    _reset_clips(project)
    project.status = "generating"
    _set_state(project, _CHAIN_KEY, None)  # supersede any in-flight chain
    db.commit()
    try:
        cfg = _with_current_rewrite(db, project, _config_of(project))
        shot = _ensure_loop_shot(db, project, cfg)
        db.commit()
        plan = plan_loop(cfg)
        if cfg.source != "image_drift":
            _start_video_chain(db, project, shot, cfg, plan,
                               regenerate_music=regenerate_music, render_after=render_after)
            db.refresh(shot)
            return shot
        out_dir = storage.render_subdir(project.id)
        w, h = _dims_for_aspect(project.aspect_ratio)
        targets = [shot, *_variant_shots(project)[:variant_count(cfg) - 1]]
        first_url: Optional[str] = None  # reference for the other times of day
        model = ""
        for i, target in enumerate(targets):
            still = out_dir / ("ambient_still.png" if i == 0 else f"ambient_still_{i}.png")
            target.status = "submitted"
            if i == 0 and has_picture(project, cfg):
                # The scene's own picture is the still: no image call, nothing billed.
                target.provider, target.provider_model = "picture", "your picture"
                db.commit()
                png = picture_path(project).read_bytes()
            else:
                target.provider_model = still_model_chain(cfg)[0]
                target.provider = provider_label("image", target.provider_model)
                db.commit()
                refs = [ImageRef(url=first_url, role="scene")] if first_url else []
                png, used = generate_still(db, cfg, prompt=target.prompt + (_SAME_PLACE if refs else ""),
                                           aspect_ratio=project.aspect_ratio, references=refs,
                                           negative_prompt=target.negative_prompt or "",
                                           project_id=project.id, endpoint="image.generate(drift)")
                target.provider_model = used
            still.write_bytes(png)
            if i == 0:
                model = target.provider_model
                if len(targets) > 1:
                    _, first_url = _save_frame_public(still)
            clip = out_dir / f"shot_00_{target.id}.mp4"
            render_image_drift_clip(still, clip, seconds=float(cfg.loop_seconds), width=w, height=h)
            target.clip_path = str(clip)
            target.status = "completed"
            db.commit()
            _log_provider_call(
                db, project_id=project.id, provider=target.provider, endpoint="image.generate+drift",
                request_summary={"model": target.provider_model, "seconds": cfg.loop_seconds, "variant": i},
                response_summary={"bytes": len(png)},
            )
        generated = {"strategy": "drift", "segments": [cfg.loop_seconds], "closed": True,
                     "anchored": False, "model": model, "clip_seconds": float(cfg.loop_seconds),
                     "variants": len(targets), "variety": cfg.variety if len(targets) > 1 else "none"}
        _finalize_generation(db, project, cfg, plan, generated,
                             regenerate_music=regenerate_music, render_after=render_after)
        return shot
    except Exception as e:  # noqa: BLE001
        log.exception("ambient loop generation failed")
        _fail_generation(db, project_id, e)
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
    k = int(gen.get("variants") or 1)
    extra: list[Path] = []
    if k > 1:
        variants = _variant_shots(project)[:k - 1]
        if len(variants) < k - 1 or any(not v.clip_path or not Path(v.clip_path).exists() for v in variants):
            raise ValueError("generate the loop clips before rendering")
        extra = [Path(v.clip_path) for v in variants]

    # Tiling is local FFmpeg work: nothing is billed at render time. The
    # provider spend was incurred (and recorded) when the loop was generated.
    render = models.Render(project_id=project.id, status="rendering", estimated_cost=0.0)
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
                sound_path=(sound_path(project) if cfg.sound != "none" and sound_path(project).exists() else None),
                sound_volume=float(cfg.sound_volume),
                clip_is_periodic=(cfg.source == "image_drift"),
                resolution=cfg.output_resolution,
                extra_clips=extra,
                variety=str(gen.get("variety") or "none"),
                transition_seconds=transition_seconds(cfg),
                overlay_title=cfg.overlay_title,
                title_seconds=cfg.title_seconds,
                watermark=cfg.watermark,
                watermark_position=cfg.watermark_position,
                watermark_opacity=cfg.watermark_opacity,
                timer=cfg.timer,
                timer_position=cfg.timer_position,
            )
        )
        _check_cancel(db, render)
        # Best frame of the loop clip as the thumbnail (YouTube: 1280×720).
        try:
            tw, th = _dims_for_aspect(project.aspect_ratio)
            scale = 1280 / max(tw, th)
            best_frame(Path(shot.clip_path), outputs.thumbnail_path,
                       width=int(tw * scale) // 2 * 2, height=int(th * scale) // 2 * 2)
        except Exception as e:  # noqa: BLE001 — keep compose's default thumbnail
            log.warning("best-frame thumbnail failed: %s", e)
        render_files.finalize(db, render, outputs.final_video_path, outputs.thumbnail_path)
        segs = gen.get("segments") or []
        # Summary line first (it's what the UI shows), ffmpeg tail after.
        render.render_log = (
            f"ambient: clips={len(segs) or 1} variations={k} unit={outputs.unit_seconds:.2f}s "
            f"repeats={outputs.repeats} total={project.target_duration_seconds}s "
            f"res={cfg.output_resolution} overlays={overlay_summary(cfg) or 'none'} "
            f"seam={'closed' if closed else 'crossfade'}\n" + outputs.log[-7000:]
        )
        render.actual_cost = 0.0
        render.status = "completed"
        project.status = "completed"
        db.commit()
        render_files.prune(db, project.id)
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
        alerts.render_failed("ambient render", project.id, project.title, str(e))
        raise

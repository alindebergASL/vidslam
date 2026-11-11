"""Schemas for the Ambient section: hour-long calm/landscape/spa videos built by
seamlessly looping one short, barely-moving scene clip under a spa-style music
bed.

The economic trick that makes this mode attractive: a 1-hour output costs the
same as a *single* 25–60s clip plus one music generation — everything past
the loop boundary is FFmpeg stream-copy, which bills nothing and takes
seconds, not minutes.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .api import RenderOut, ShotOut

# Loop lengths the UI offers. Anything 4..60 is accepted by the API so tests
# can use short clips. Most video models cap a single call at 8–15s (a few
# reach 20–30s), so loops longer than the model's cap are built from
# chained segments — see services.ambient.plan_loop.
LOOP_OPTIONS = (10, 25, 30, 60)
# Total output lengths offered in the UI, in seconds (10m, 30m, 1h, 2h, 3h).
TOTAL_OPTIONS = (600, 1800, 3600, 7200, 10800)

SceneSource = Literal["video", "image_drift"]
# How the scene is rendered: photoreal (default) through fully stylized.
Look = Literal["photoreal", "cinematic", "painterly", "watercolor", "anime", "claymation", "dreamy"]
TimeOfDay = Literal["any", "dawn", "golden_hour", "midday", "dusk", "night"]
Weather = Literal["any", "clear", "light_rain", "heavy_rain", "snow", "fog"]
# Where the scene's picture came from; "" = none (generated at loop time).
PictureOrigin = Literal["", "upload", "generated", "studio", "asset"]
# Less repetition over a long video: "takes" = several clips of the same
# scene, cycled; "day_night" = the scene at several times of day, in order.
Variety = Literal["none", "takes", "day_night"]
Corner = Literal["top_left", "top_right", "bottom_left", "bottom_right"]
Timer = Literal["none", "countdown", "elapsed"]


class PhotoRewrite(BaseModel):
    """The scene description rewritten as a plain description of a real
    photograph (by the planner model), so hype words like "magical, 8k,
    turquoise" don't reach the image model. Made by the server."""

    source: str = ""  # fingerprint of the scene_prompt it was made from
    scene: str = Field(default="", max_length=3000)
    light: str = Field(default="", max_length=1000)
    camera: str = Field(default="", max_length=1000)


class AmbientConfig(BaseModel):
    """Everything that shapes an ambient scene other than title/aspect (which
    live on the VideoProject columns)."""

    scene_preset: str = "waterfall"
    scene_prompt: str = Field(default="", max_length=2000)
    # Photoreal scenes: rewrite a typed scene_prompt as a photograph for the
    # image/video model (kept in `photo`; the typed text stays as typed).
    rewrite_prompt: bool = True
    photo: Optional[PhotoRewrite] = None
    # "video": ask the video provider for the loop clip.
    # "image_drift": generate one still and give it a slow, periodic
    #   zoom/drift — perfectly seamless by construction and ~10× cheaper.
    source: SceneSource = "video"
    loop_seconds: int = Field(default=30, ge=4, le=60)
    total_seconds: int = Field(default=3600, ge=20, le=8 * 3600)
    # Seconds of tail→head crossfade that hides the loop seam. 0 disables.
    crossfade_seconds: float = Field(default=2.0, ge=0.0, le=6.0)
    # setpts multiplier applied to a provider clip: 2.0 = half speed, which is
    # what makes a "slow" generated clip read as "barely moves".
    slow_factor: float = Field(default=1.0, ge=1.0, le=3.0)
    # Fade from/to black (and silence) at the very start/end of the full render.
    fade_seconds: float = Field(default=4.0, ge=0.0, le=15.0)
    music_mood: str = "spa"  # id from MUSIC_MOODS, or "none"
    music_prompt: str = Field(default="", max_length=1000)
    music_volume: float = Field(default=0.7, ge=0.0, le=1.0)
    # Optional provider model id (or custom:<id>) for the loop clip (video
    # scenes). Older Still + drift scenes keep their image model here too.
    model_override: str = Field(default="", max_length=300)
    # Image model for this scene's stills (anchor frame, generated picture,
    # Still + drift). "" = AMBIENT_STILL_MODEL with its fallback.
    still_model: str = Field(default="", max_length=300)
    # When loop_seconds exceeds the model's per-call cap: True = chain
    # several clips (each starting on the previous one's last frame) to
    # reach the full loop length; False = one clip at the model's max and
    # loop that (cheaper, but the scene repeats more often).
    chain_segments: bool = True
    # Music bed length in seconds. 0 = auto (45–90 s, offset from the video
    # loop). Longer beds (3–20 min) repeat less over an hour; beds longer
    # than the music provider's per-call cap are generated in parts and
    # crossfaded together.
    music_bed_seconds: int = Field(default=0, ge=0, le=1200)
    # 4k = Lanczos upscale of the loop unit before tiling (no AI): free, and
    # YouTube streams 4K uploads at a higher bitrate even when viewed at 1080p.
    output_resolution: Literal["1080p", "4k"] = "1080p"
    # Look: art style of the scene, and how strongly it applies (0 = a hint,
    # 1 = fully stylized). Ignored for photoreal.
    look: Look = "photoreal"
    look_strength: float = Field(default=0.6, ge=0.0, le=1.0)
    # Light and weather layered on top of the preset/prompt; "any" = as written.
    time_of_day: TimeOfDay = "any"
    weather: Weather = "any"
    # Nature-sound bed, mixed separately from (or instead of) music:
    # "none", "auto" (matches the scene/weather) or an id from SOUND_PRESETS.
    sound: str = "none"
    sound_prompt: str = Field(default="", max_length=500)
    sound_volume: float = Field(default=0.6, ge=0.0, le=1.0)
    # Scene picture: set through the /picture endpoints, not edited directly.
    # When present it is the anchor (first + last frame) of a video loop, or
    # the still of a Still + drift scene, instead of a generated image.
    picture_origin: PictureOrigin = ""
    # Less repetition: how many variations ("takes" of the same scene, or
    # times of day for "day_night"), each its own loop clip. Costs one loop
    # per variation; the full-length render is still local.
    variety: Variety = "none"
    variations: int = Field(default=3, ge=2, le=5)
    # Overlays, applied at render time (changing them keeps the loop).
    # Title: shown over the opening seconds, fading in and out. Watermark:
    # small text in a corner for the whole video. Both are drawn on pieces
    # that are encoded anyway, so they cost nothing extra. Timer: a
    # countdown or elapsed clock; it changes every second, so the whole
    # video is re-encoded once (longer render, no provider cost).
    overlay_title: str = Field(default="", max_length=120)
    title_seconds: float = Field(default=8.0, ge=2.0, le=30.0)
    watermark: str = Field(default="", max_length=60)
    watermark_position: Corner = "bottom_right"
    watermark_opacity: float = Field(default=0.6, ge=0.1, le=1.0)
    timer: Timer = "none"
    timer_position: Corner = "top_right"

    @field_validator("scene_prompt", "music_prompt", "scene_preset", "music_mood", "model_override",
                     "sound", "sound_prompt", "overlay_title", "watermark")
    @classmethod
    def _strip(cls, v: str) -> str:
        return (v or "").strip()


class AmbientCreate(BaseModel):
    title: str = Field(default="", max_length=200)
    aspect_ratio: Literal["16:9", "9:16", "1:1"] = "16:9"
    config: AmbientConfig = Field(default_factory=AmbientConfig)
    # Kick off loop + music generation right after creating the row.
    generate: bool = True


class AmbientUpdate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=200)
    aspect_ratio: Optional[Literal["16:9", "9:16", "1:1"]] = None
    config: Optional[AmbientConfig] = None


class PictureFrom(BaseModel):
    """Use an existing image as the scene picture: a cast asset or a Studio result."""

    asset_id: Optional[int] = None
    studio_job_id: Optional[int] = None


class AmbientBatchRequest(BaseModel):
    presets: list[str] = Field(min_length=1, max_length=20)
    aspect_ratio: Literal["16:9", "9:16", "1:1"] = "16:9"
    # Shared settings; scene_preset/music_mood come from each preset.
    config: AmbientConfig = Field(default_factory=AmbientConfig)
    render: bool = True


class AmbientEstimateRequest(BaseModel):
    config: AmbientConfig = Field(default_factory=AmbientConfig)


class ScenePresetOut(BaseModel):
    id: str
    label: str
    emoji: str
    tagline: str
    prompt: str
    music_mood: str
    # Cached preview image (generated once with the image model), if any.
    preview_url: Optional[str] = None
    # Two CSS colors the UI uses for the preset's gradient art.
    palette: tuple[str, str]


class MusicMoodOut(BaseModel):
    id: str
    label: str
    prompt: str


class OptionOut(BaseModel):
    id: str
    label: str


class ModelChoiceOut(BaseModel):
    """A model offered up front in the scene builder, with what it's like."""

    id: str
    label: str
    note: str = ""
    est_cost: Optional[float] = None
    recommended: bool = False
    experimental: bool = False


class SoundPresetOut(BaseModel):
    id: str
    label: str
    prompt: str


class AmbientPresetsOut(BaseModel):
    scenes: list[ScenePresetOut]
    music_moods: list[MusicMoodOut]
    sounds: list[SoundPresetOut] = Field(default_factory=list)
    looks: list[OptionOut] = Field(default_factory=list)
    times_of_day: list[OptionOut] = Field(default_factory=list)
    weathers: list[OptionOut] = Field(default_factory=list)
    loop_options: list[int]
    total_options: list[int]
    aspect_options: list[str]
    # Curated model choices; any other catalog model can still be picked.
    still_models: list[ModelChoiceOut] = Field(default_factory=list)
    video_models: list[ModelChoiceOut] = Field(default_factory=list)
    # What "" (default) means on this deployment.
    default_still_model: str = ""
    default_video_model: str = ""
    # The default's provider is switched off in Settings → API keys.
    default_still_off: bool = False
    default_video_off: bool = False


class LoopPlanOut(BaseModel):
    """How a loop will be (or was) produced on the current model."""

    model: str
    max_clip_seconds: int
    caps_known: bool  # False = model limits not discoverable; used the default cap
    supports_last_frame: bool
    # "single" one clip · "chain" N clips joined · "drift" still + periodic motion
    strategy: Literal["single", "chain", "drift"]
    segments: list[int]  # per-call clip durations, seconds
    # Anchor still pinned as first frame of clip 1 and last frame of clip N,
    # so the loop closes natively instead of relying on a long crossfade.
    anchored: bool
    closed: bool
    join_crossfade: float  # between chained segments
    seam_crossfade: float  # loop-end → loop-start
    loop_seconds: float  # actual loop clip length after joins + slow-down
    unit_seconds: float  # seamless unit that gets tiled
    repeats: int
    provider_calls: int
    notes: list[str] = Field(default_factory=list)
    # Less repetition: how many loop clips the scene has, their labels, and
    # how often the picture repeats exactly (the takes cycle, or one unit).
    variants: int = 1
    variant_labels: list[str] = Field(default_factory=list)
    repeat_every_seconds: float = 0.0
    # A still is the first frame of the loop (the anchor, the scene picture,
    # or, with variations on a first-frame-only model, a shared start frame).
    first_frame_still: bool = False
    # Whether the loop is built from a picture of the scene when it has one
    # (False: the video model works from text alone, so checking a picture
    # first says nothing about the video).
    picture_usable: bool = True


class LoopAtStakeOut(BaseModel):
    """Loop clips already made for a scene: what a new picture or a setting
    that needs a new clip would discard."""

    # About what they cost to make, in USD: from the spend ledger when it has
    # entries for this loop, else the rate-card estimate.
    cost: float
    from_ledger: bool
    # Finished clips of a loop that failed part-way (no whole loop yet).
    partial: bool = False


class LoopResumeOut(BaseModel):
    """A loop that failed part-way and can be continued from the failed clip."""

    clip: int  # the clip to make next, 1-based; clips + 1 = all made, only joining is left
    clips: int  # clips per take
    take: int  # 1-based
    takes: int
    kept_clips: int  # finished clips of this take that are kept
    cost: float  # rate-card estimate of what continuing still buys
    error: str = ""
    # Resuming keeps the video model the kept clips were made with.
    model: str = ""
    model_changed: bool = False  # the scene would use a different model if started over now


Level = Literal["good", "warn", "bad"]


class SeamReadingOut(BaseModel):
    verdict: Literal["seamless", "slight_jump", "visible_jump"]
    level: Level
    text: str
    difference: float  # last frame vs first, mean abs luma difference 0-1
    typical: float     # typical difference between neighbouring frames
    ratio: float       # difference / typical (with a floor for near-still clips)


class FlickerReadingOut(BaseModel):
    verdict: Literal["none", "slight", "strong"]
    level: Level
    text: str
    percent: float


class FrozenReadingOut(BaseModel):
    # not_checked: an image-drift loop (a still picture by design)
    verdict: Literal["none", "some", "long", "not_checked"]
    level: Level
    text: str
    longest_seconds: float
    total_seconds: float
    at_seconds: Optional[float] = None  # where the longest frozen stretch starts


class DriftReadingOut(BaseModel):
    verdict: Literal["none", "slight", "strong"]
    level: Level
    text: str
    distance: float  # first vs last second's average colour, 0-1


class LoopHealthOut(BaseModel):
    """Local measurement of one loop clip (services/loop_health.py)."""

    shot_id: int
    measured_at: str
    clip_seconds: float
    sample_fps: float
    seam: SeamReadingOut
    flicker: FlickerReadingOut
    frozen: FrozenReadingOut
    drift: DriftReadingOut
    # Last few seconds then first few: the join plays at join_at_seconds.
    seam_preview_url: str
    join_at_seconds: float


class LoopHealthResultOut(BaseModel):
    """POST /api/ambient/{id}/loop-health."""

    loop_health: Optional[LoopHealthOut] = None
    variant_loop_health: list[Optional[LoopHealthOut]] = Field(default_factory=list)


class AmbientOut(BaseModel):
    """Ambient scene = VideoProject(mode=ambient_loop) + its single loop shot
    + latest render, flattened so the UI never has to understand storyboards."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    status: str
    aspect_ratio: str
    total_seconds: int
    config: AmbientConfig
    # Derived from the loop shot: pending|submitted|polling|completed|failed
    loop_status: str
    loop_error: str
    loop_shot: Optional[ShotOut] = None
    # Variations 2..N (variation 1 is loop_shot), when config.variety is on.
    variant_shots: list[ShotOut] = Field(default_factory=list)
    # Loop health of the loop clip as it is now (None = not measured yet, or
    # the clip changed since); one entry per variant_shots item, same order.
    loop_health: Optional[LoopHealthOut] = None
    variant_loop_health: list[Optional[LoopHealthOut]] = Field(default_factory=list)
    # True once a music bed exists on disk (generated or uploaded).
    music_ready: bool
    # True once a nature-sound bed exists (config.sound != "none").
    sound_ready: bool = False
    # True when the scene has its own picture (see config.picture_origin).
    picture_ready: bool = False
    # Changes whenever the picture file is replaced (cache-bust for <img>).
    picture_version: str = ""
    # How many times the loop unit tiles into the total output.
    repeats: int
    plan: LoopPlanOut
    # Rate-card estimate of what Generate loop would buy now (the loop, plus
    # the music and sound beds the scene doesn't have yet).
    generate_cost: float = 0.0
    # Set when loop clips exist: changing the picture or a clip setting
    # discards them, and needs discard_loop=true.
    loop_at_stake: Optional[LoopAtStakeOut] = None
    # Set when a failed loop can be continued (POST .../resume-loop).
    resume: Optional[LoopResumeOut] = None
    latest_render: Optional[RenderOut] = None
    render_count: int = 0
    created_at: datetime
    updated_at: datetime

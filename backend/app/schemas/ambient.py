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


class AmbientConfig(BaseModel):
    """Everything that shapes an ambient scene other than title/aspect (which
    live on the VideoProject columns)."""

    scene_preset: str = "waterfall"
    scene_prompt: str = Field(default="", max_length=2000)
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
    # Optional provider model id (or custom:<id>) for the loop clip / still.
    model_override: str = Field(default="", max_length=300)
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

    @field_validator("scene_prompt", "music_prompt", "scene_preset", "music_mood", "model_override")
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


class AmbientPresetsOut(BaseModel):
    scenes: list[ScenePresetOut]
    music_moods: list[MusicMoodOut]
    loop_options: list[int]
    total_options: list[int]
    aspect_options: list[str]


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
    # True once a music bed exists on disk (generated or uploaded).
    music_ready: bool
    # How many times the loop unit tiles into the total output.
    repeats: int
    plan: LoopPlanOut
    latest_render: Optional[RenderOut] = None
    render_count: int = 0
    created_at: datetime
    updated_at: datetime

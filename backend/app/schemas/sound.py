"""Request bodies of the Sound Studio API (api/sound.py)."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# Longest script text the API takes (a 90-minute story is about 90,000
# characters; markup and notes come on top).
SCRIPT_MAX = 400_000


class StorySettings(BaseModel):
    """settings_json of a story. Unknown keys are dropped."""

    model_config = ConfigDict(extra="ignore")

    style: str = Field(default="", max_length=40)
    sfx_density: Literal["none", "low", "medium", "high"] = "medium"
    beds: bool = True
    voice_provider: str = Field(default="", max_length=40)
    voice_model: str = Field(default="", max_length=200)
    music_prompt: str = Field(default="", max_length=400)


class StoryCreate(BaseModel):
    title: str = Field(default="", max_length=200)
    kind: Literal["story", "asmr"] = "story"
    premise: str = Field(default="", max_length=4000)
    target_minutes: int = Field(default=10, ge=1, le=600)
    settings: StorySettings = Field(default_factory=StorySettings)
    script_text: str = Field(default="", max_length=SCRIPT_MAX)


class VoiceSettings(BaseModel):
    """ElevenLabs voice settings (only these keys are sent)."""

    model_config = ConfigDict(extra="ignore")

    stability: Optional[float] = Field(default=None, ge=0, le=1)
    similarity_boost: Optional[float] = Field(default=None, ge=0, le=1)
    style: Optional[float] = Field(default=None, ge=0, le=1)
    speed: Optional[float] = Field(default=None, ge=0.5, le=2)
    use_speaker_boost: Optional[bool] = None


class CastVoice(BaseModel):
    """How one character is voiced (cast_json[NAME]): its own provider and
    model (else the story's), voice id, seed, voice settings and the
    loudness every clip of it is brought to."""

    model_config = ConfigDict(extra="ignore")

    provider: str = Field(default="", max_length=40)
    model: str = Field(default="", max_length=200)
    voice_id: str = Field(default="", max_length=120)
    seed: Optional[int] = Field(default=None, ge=0, le=4294967295)
    voice_settings: Optional[VoiceSettings] = None
    loudness: Optional[float] = Field(default=None, ge=-40, le=-10)


class StoryUpdate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=200)
    premise: Optional[str] = Field(default=None, max_length=4000)
    target_minutes: Optional[int] = Field(default=None, ge=1, le=600)
    settings: Optional[StorySettings] = None
    script_text: Optional[str] = Field(default=None, max_length=SCRIPT_MAX)
    # The version the editor started from: a save over a newer one is a 409.
    script_version: Optional[int] = None
    # Voices per character (names in any case), replacing the stored ones.
    cast: Optional[dict[str, CastVoice]] = None


class ParseIn(BaseModel):
    script_text: str = Field(default="", max_length=SCRIPT_MAX)
    style: str = Field(default="", max_length=40)


class EstimateIn(BaseModel):
    """Either a stored story (story_id) or a draft (premise, minutes,
    settings, script). enhance_scope: price an Enhance of that range too."""

    story_id: Optional[int] = None
    premise: str = Field(default="", max_length=4000)
    target_minutes: int = Field(default=10, ge=1, le=600)
    settings: StorySettings = Field(default_factory=StorySettings)
    script_text: str = Field(default="", max_length=SCRIPT_MAX)
    enhance_scope: Optional[Literal["selection", "chapter", "story"]] = None
    start: Optional[int] = Field(default=None, ge=0)
    end: Optional[int] = Field(default=None, ge=0)
    chapter_line: Optional[int] = Field(default=None, ge=1)
    # Price a new take of one clip, or a preview.
    cue_key: Optional[str] = Field(default=None, max_length=64)
    preview: Optional["PreviewIn"] = None


class WriteIn(BaseModel):
    # Replace a script the story already has (its text is kept until the
    # first new chapter is written over it).
    replace: bool = False


class EnhanceIn(BaseModel):
    action: Literal["tighten", "lengthen", "asmr", "sfx", "sleep"]
    scope: Literal["selection", "chapter", "story"] = "selection"
    start: Optional[int] = Field(default=None, ge=0)
    end: Optional[int] = Field(default=None, ge=0)
    # The selected text as the editor has it (must match the stored script).
    text: Optional[str] = Field(default=None, max_length=SCRIPT_MAX)
    chapter_line: Optional[int] = Field(default=None, ge=1)
    script_version: Optional[int] = None
    minutes: Optional[int] = Field(default=None, ge=1, le=600)


class PreviewIn(BaseModel):
    """One of: a chapter (its index), a range of script lines, or the first
    minutes (at most 3)."""

    chapter: Optional[int] = Field(default=None, ge=0)
    start_line: Optional[int] = Field(default=None, ge=1)
    end_line: Optional[int] = Field(default=None, ge=1)
    first_minutes: Optional[float] = Field(default=None, gt=0, le=3)


class RegenerateIn(BaseModel):
    cue_key: str = Field(min_length=1, max_length=64)


class EffectsIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=400)
    seconds: float = Field(default=3.0, ge=0.5, le=30)
    loop: bool = False
    variations: int = Field(default=1, ge=1, le=4)


EstimateIn.model_rebuild()

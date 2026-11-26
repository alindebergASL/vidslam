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


class StoryUpdate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=200)
    premise: Optional[str] = Field(default=None, max_length=4000)
    target_minutes: Optional[int] = Field(default=None, ge=1, le=600)
    settings: Optional[StorySettings] = None
    script_text: Optional[str] = Field(default=None, max_length=SCRIPT_MAX)
    # The version the editor started from: a save over a newer one is a 409.
    script_version: Optional[int] = None


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

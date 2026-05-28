from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ReferenceStrategy = Literal["frame_images", "input_references", "static_motion"]
ShotType = Literal["hero", "b_roll", "talking_head", "end_card", "transition"]


class CaptionChunk(BaseModel):
    start_hint: float = Field(default=0.0, description="Approx start time in seconds")
    text: str


class ShotPlan(BaseModel):
    order: int
    shot_type: ShotType = "b_roll"
    duration_seconds: float = 5.0
    visual_prompt: str
    negative_prompt: str = ""
    reference_strategy: ReferenceStrategy = "input_references"
    recommended_asset_types: list[str] = Field(default_factory=list)
    # asset types include avatar types (hero, expression_sheet, ...) and ingredient kinds (scene, object, style, prop)
    recommended_cast_roles: list[str] = Field(default_factory=list)
    caption_text: str = ""
    camera_direction: str = ""
    notes: str = ""


class StoryboardPlan(BaseModel):
    title: str
    cleaned_voice_script: str
    estimated_duration_seconds: float
    content_warning_notes: str = ""
    disclosure_text: str = "AI-generated virtual creator"
    end_card_text: str = ""
    caption_chunks: list[CaptionChunk] = Field(default_factory=list)
    shots: list[ShotPlan] = Field(default_factory=list)

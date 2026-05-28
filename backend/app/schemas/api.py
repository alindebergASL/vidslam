from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .storyboard import StoryboardPlan

# --- Avatars ---


class AvatarBase(BaseModel):
    name: str
    description: str = ""
    persona: str = ""
    visual_identity: str = ""
    default_disclosure_text: str = "AI-generated virtual creator"
    default_voice_provider: str = "mock"
    elevenlabs_voice_id: str = ""
    brand: str = ""


class AvatarCreate(AvatarBase):
    pass


class AvatarUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    persona: Optional[str] = None
    visual_identity: Optional[str] = None
    default_disclosure_text: Optional[str] = None
    default_voice_provider: Optional[str] = None
    elevenlabs_voice_id: Optional[str] = None
    brand: Optional[str] = None


class AssetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_kind: str
    avatar_id: Optional[int] = None
    ingredient_id: Optional[int] = None
    asset_type: str
    original_filename: str
    mime_type: str
    width: int
    height: int
    public_token: str
    source: str
    created_at: datetime


class AvatarOut(AvatarBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime
    updated_at: datetime
    assets: list[AssetOut] = Field(default_factory=list)


# --- Ingredients ---


class IngredientBase(BaseModel):
    name: str
    kind: Literal["object", "scene", "style", "prop"] = "object"
    description: str = ""
    visual_identity: str = ""


class IngredientCreate(IngredientBase):
    pass


class IngredientUpdate(BaseModel):
    name: Optional[str] = None
    kind: Optional[Literal["object", "scene", "style", "prop"]] = None
    description: Optional[str] = None
    visual_identity: Optional[str] = None


class IngredientOut(IngredientBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime
    updated_at: datetime
    assets: list[AssetOut] = Field(default_factory=list)


# --- Brand kits ---


class BrandKitBase(BaseModel):
    name: str
    primary_color: str = "#FF5C8A"
    end_card_bg_color: str = "#0E0E12"
    end_card_text_color: str = "#FFFFFF"
    default_cta_text: str = ""
    default_disclosure_text: str = ""


class BrandKitCreate(BrandKitBase):
    pass


class BrandKitUpdate(BaseModel):
    name: Optional[str] = None
    primary_color: Optional[str] = None
    end_card_bg_color: Optional[str] = None
    end_card_text_color: Optional[str] = None
    default_cta_text: Optional[str] = None
    default_disclosure_text: Optional[str] = None


class BrandKitOut(BrandKitBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    logo_public_token: str
    created_at: datetime
    updated_at: datetime


# --- Projects ---


class CastMemberIn(BaseModel):
    member_kind: Literal["avatar", "ingredient"] = "avatar"
    avatar_id: Optional[int] = None
    ingredient_id: Optional[int] = None
    role: str = "host"


class CastMemberOut(CastMemberIn):
    model_config = ConfigDict(from_attributes=True)
    id: int


class ProjectCreate(BaseModel):
    title: str = ""
    original_script: str = ""
    mode: Literal["reel_montage", "talking_head_beta", "static_motion"] = "reel_montage"
    aspect_ratio: str = "9:16"
    target_duration_seconds: int = 25
    cta_text: str = ""
    caption_style: str = "clean_white"
    include_disclosure: bool = True
    disclosure_text: str = ""
    creative_direction: str = ""
    voiceover_source: Literal["tts", "upload", "silent"] = "tts"
    music_volume: float = 0.25
    primary_avatar_id: Optional[int] = None
    brand_kit_id: Optional[int] = None
    cast: list[CastMemberIn] = Field(default_factory=list)


class ProjectUpdate(BaseModel):
    title: Optional[str] = None
    original_script: Optional[str] = None
    mode: Optional[str] = None
    aspect_ratio: Optional[str] = None
    target_duration_seconds: Optional[int] = None
    cta_text: Optional[str] = None
    caption_style: Optional[str] = None
    include_disclosure: Optional[bool] = None
    disclosure_text: Optional[str] = None
    creative_direction: Optional[str] = None
    voiceover_source: Optional[Literal["tts", "upload", "silent"]] = None
    music_volume: Optional[float] = None
    primary_avatar_id: Optional[int] = None
    brand_kit_id: Optional[int] = None
    generated_plan_json: Optional[dict] = None


class ShotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    shot_order: int
    shot_type: str
    prompt: str
    negative_prompt: str
    duration_seconds: float
    reference_asset_ids_json: list | None
    reference_strategy: str
    caption_text: str
    camera_direction: str
    provider: str
    provider_model: str
    status: str
    clip_path: str
    error: str


class ShotUpdate(BaseModel):
    prompt: Optional[str] = None
    negative_prompt: Optional[str] = None
    duration_seconds: Optional[float] = None
    reference_asset_ids_json: Optional[list] = None
    reference_strategy: Optional[str] = None
    caption_text: Optional[str] = None
    camera_direction: Optional[str] = None


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    title: str
    original_script: str
    mode: str
    aspect_ratio: str
    target_duration_seconds: int
    cta_text: str
    caption_style: str
    include_disclosure: bool
    disclosure_text: str
    creative_direction: str
    voiceover_source: str
    voiceover_upload_path: str
    music_upload_path: str
    music_volume: float
    status: str
    primary_avatar_id: Optional[int]
    brand_kit_id: Optional[int]
    generated_plan_json: Optional[dict]
    created_at: datetime
    updated_at: datetime
    cast_members: list[CastMemberOut] = Field(default_factory=list)
    shots: list[ShotOut] = Field(default_factory=list)


class RenderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    project_id: int
    status: str
    audio_path: str
    final_video_path: str
    thumbnail_path: str
    render_log: str
    error: str
    share_token: str
    estimated_cost: float
    actual_cost: float
    created_at: datetime
    updated_at: datetime


class ProjectStatusOut(BaseModel):
    project_id: int
    project_status: str
    latest_render: Optional[RenderOut] = None
    shots: list[ShotOut] = Field(default_factory=list)


class StoryboardOut(BaseModel):
    project_id: int
    plan: StoryboardPlan


# --- Asset Studio ---


class StudioRequest(BaseModel):
    owner_kind: Literal["avatar", "ingredient"]
    owner_id: int
    output_kind: Literal["image", "video_clip"] = "image"
    prompt: str
    negative_prompt: str = ""
    reference_asset_ids: list[int] = Field(default_factory=list)
    model: Optional[str] = None
    duration_seconds: float = 5.0


class StudioJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    owner_kind: str
    owner_id: int
    output_kind: str
    prompt: str
    provider: str
    provider_model: str
    status: str
    result_path: str
    result_asset_id: Optional[int]
    error: str
    created_at: datetime
    updated_at: datetime

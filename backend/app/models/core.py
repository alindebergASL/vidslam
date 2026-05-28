from __future__ import annotations

import secrets
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base


def _now() -> datetime:
    return datetime.utcnow()


def _token() -> str:
    return secrets.token_urlsafe(32)


# --- Cast: avatars + ingredients ---


class Avatar(Base):
    __tablename__ = "avatars"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    persona: Mapped[str] = mapped_column(Text, default="")
    visual_identity: Mapped[str] = mapped_column(Text, default="")
    default_disclosure_text: Mapped[str] = mapped_column(
        String(200), default="AI-generated virtual creator"
    )
    default_voice_provider: Mapped[str] = mapped_column(String(40), default="mock")
    elevenlabs_voice_id: Mapped[str] = mapped_column(String(120), default="")
    brand: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class Ingredient(Base):
    """A non-avatar reusable visual reference: object, scene, style, prop."""

    __tablename__ = "ingredients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), default="object")  # object|scene|style|prop
    description: Mapped[str] = mapped_column(Text, default="")
    visual_identity: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


# --- Assets (shared shape across avatar + ingredient) ---


class Asset(Base):
    """Common asset table; avatar/ingredient ownership via two FK columns (one nullable)."""

    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_kind: Mapped[str] = mapped_column(String(20), nullable=False)  # avatar|ingredient
    avatar_id: Mapped[Optional[int]] = mapped_column(ForeignKey("avatars.id"), nullable=True)
    ingredient_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("ingredients.id"), nullable=True
    )
    asset_type: Mapped[str] = mapped_column(String(40), default="other")
    # avatar asset_types: hero|reference_sheet|expression_sheet|outfit_sheet|lifestyle|logo|other
    # ingredient asset_types: hero|reference|other
    original_filename: Mapped[str] = mapped_column(String(255), default="")
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(80), default="application/octet-stream")
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    checksum: Mapped[str] = mapped_column(String(80), default="")
    public_token: Mapped[str] = mapped_column(String(80), default=_token, unique=True, index=True)
    source: Mapped[str] = mapped_column(String(20), default="upload")  # upload|generated
    generation_job_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("asset_generation_jobs.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


# Convenience views — these are SQLAlchemy ORM relationships, not separate tables.
# We keep `AvatarAsset` / `IngredientAsset` as aliases so the rest of the code
# (and the spec) can use the friendly names.
AvatarAsset = Asset
IngredientAsset = Asset

# Wire reverse relationships (declared after Asset exists):
Avatar.assets = relationship(  # type: ignore[assignment]
    Asset,
    primaryjoin="and_(Avatar.id==Asset.avatar_id, Asset.owner_kind=='avatar')",
    foreign_keys=[Asset.avatar_id],
    back_populates="avatar",
    cascade="all, delete-orphan",
)
Ingredient.assets = relationship(  # type: ignore[assignment]
    Asset,
    primaryjoin="and_(Ingredient.id==Asset.ingredient_id, Asset.owner_kind=='ingredient')",
    foreign_keys=[Asset.ingredient_id],
    back_populates="ingredient",
    cascade="all, delete-orphan",
)
Asset.avatar = relationship(  # type: ignore[attr-defined]
    Avatar, back_populates="assets", foreign_keys=[Asset.avatar_id]
)
Asset.ingredient = relationship(  # type: ignore[attr-defined]
    Ingredient, back_populates="assets", foreign_keys=[Asset.ingredient_id]
)


# --- Projects ---


class VideoProject(Base):
    __tablename__ = "video_projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    original_script: Mapped[str] = mapped_column(Text, default="")
    mode: Mapped[str] = mapped_column(String(40), default="reel_montage")
    # reel_montage | talking_head_beta | static_motion
    aspect_ratio: Mapped[str] = mapped_column(String(10), default="9:16")
    target_duration_seconds: Mapped[int] = mapped_column(Integer, default=25)
    cta_text: Mapped[str] = mapped_column(String(280), default="")
    caption_style: Mapped[str] = mapped_column(String(40), default="clean_white")
    include_disclosure: Mapped[bool] = mapped_column(Boolean, default=True)
    # Per-project overrides so the engine works for any vertical, not just social-creator content.
    disclosure_text: Mapped[str] = mapped_column(String(200), default="")
    creative_direction: Mapped[str] = mapped_column(Text, default="")
    voiceover_source: Mapped[str] = mapped_column(String(20), default="tts")
    # tts | upload | silent
    voiceover_upload_path: Mapped[str] = mapped_column(String(500), default="")
    music_upload_path: Mapped[str] = mapped_column(String(500), default="")
    music_volume: Mapped[float] = mapped_column(Float, default=0.25)
    status: Mapped[str] = mapped_column(String(40), default="draft")
    # draft|planning|planned|generating|completed|failed
    generated_plan_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    primary_avatar_id: Mapped[Optional[int]] = mapped_column(ForeignKey("avatars.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    cast_members: Mapped[list[ProjectCastMember]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    shots: Mapped[list[VideoShot]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="VideoShot.shot_order"
    )
    renders: Mapped[list[Render]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class ProjectCastMember(Base):
    """Many-to-many between projects and cast members (avatar or ingredient)."""

    __tablename__ = "project_cast_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("video_projects.id"))
    member_kind: Mapped[str] = mapped_column(String(20), default="avatar")  # avatar|ingredient
    avatar_id: Mapped[Optional[int]] = mapped_column(ForeignKey("avatars.id"), nullable=True)
    ingredient_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("ingredients.id"), nullable=True
    )
    role: Mapped[str] = mapped_column(String(60), default="host")  # host|co-host|location|prop
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    project: Mapped[VideoProject] = relationship(back_populates="cast_members")
    avatar: Mapped[Optional[Avatar]] = relationship(foreign_keys=[avatar_id])
    ingredient: Mapped[Optional[Ingredient]] = relationship(foreign_keys=[ingredient_id])


class VideoShot(Base):
    __tablename__ = "video_shots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("video_projects.id"))
    shot_order: Mapped[int] = mapped_column(Integer, default=0)
    shot_type: Mapped[str] = mapped_column(String(40), default="b_roll")
    prompt: Mapped[str] = mapped_column(Text, default="")
    negative_prompt: Mapped[str] = mapped_column(Text, default="")
    duration_seconds: Mapped[float] = mapped_column(Float, default=5.0)
    reference_asset_ids_json: Mapped[list | None] = mapped_column(JSON, default=list)
    reference_strategy: Mapped[str] = mapped_column(String(30), default="input_references")
    # frame_images|input_references|static_motion
    caption_text: Mapped[str] = mapped_column(Text, default="")
    camera_direction: Mapped[str] = mapped_column(String(120), default="")
    provider: Mapped[str] = mapped_column(String(40), default="")
    provider_model: Mapped[str] = mapped_column(String(120), default="")
    provider_job_id: Mapped[str] = mapped_column(String(200), default="")
    polling_url: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(40), default="pending")
    # pending|submitted|polling|downloaded|completed|failed
    clip_path: Mapped[str] = mapped_column(String(500), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    project: Mapped[VideoProject] = relationship(back_populates="shots")


class Render(Base):
    __tablename__ = "renders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("video_projects.id"))
    status: Mapped[str] = mapped_column(String(40), default="pending")
    # pending|planning|generating_audio|generating_shots|polling|rendering|completed|failed
    audio_path: Mapped[str] = mapped_column(String(500), default="")
    final_video_path: Mapped[str] = mapped_column(String(500), default="")
    thumbnail_path: Mapped[str] = mapped_column(String(500), default="")
    render_log: Mapped[str] = mapped_column(Text, default="")
    estimated_cost: Mapped[float] = mapped_column(Float, default=0.0)
    actual_cost: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str] = mapped_column(Text, default="")
    share_token: Mapped[str] = mapped_column(String(80), default=_token, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    project: Mapped[VideoProject] = relationship(back_populates="renders")


# --- Asset Studio (image/clip generation that produces reusable assets) ---


class AssetGenerationJob(Base):
    __tablename__ = "asset_generation_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_kind: Mapped[str] = mapped_column(String(20), default="avatar")  # avatar|ingredient
    owner_id: Mapped[int] = mapped_column(Integer)
    output_kind: Mapped[str] = mapped_column(String(20), default="image")  # image|video_clip
    prompt: Mapped[str] = mapped_column(Text, default="")
    negative_prompt: Mapped[str] = mapped_column(Text, default="")
    reference_asset_ids_json: Mapped[list | None] = mapped_column(JSON, default=list)
    provider: Mapped[str] = mapped_column(String(40), default="")
    provider_model: Mapped[str] = mapped_column(String(120), default="")
    provider_job_id: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(40), default="pending")
    # pending|submitted|polling|completed|failed|saved
    result_path: Mapped[str] = mapped_column(String(500), default="")
    result_asset_id: Mapped[Optional[int]] = mapped_column(ForeignKey("assets.id"), nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


# --- Provider call audit log ---


class ProviderLog(Base):
    __tablename__ = "provider_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("video_projects.id"), nullable=True
    )
    provider: Mapped[str] = mapped_column(String(60), default="")
    endpoint: Mapped[str] = mapped_column(String(200), default="")
    request_summary_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    response_summary_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(40), default="ok")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

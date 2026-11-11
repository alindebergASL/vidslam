from __future__ import annotations

import secrets
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
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


# --- Brand kit: reusable end-card branding ---


class BrandKit(Base):
    """Reusable branding (logo + colors + default CTA/disclosure) applied to a
    project's end card so a creator's videos stay visually consistent."""

    __tablename__ = "brand_kits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    primary_color: Mapped[str] = mapped_column(String(9), default="#FF5C8A")
    end_card_bg_color: Mapped[str] = mapped_column(String(9), default="#0E0E12")
    end_card_text_color: Mapped[str] = mapped_column(String(9), default="#FFFFFF")
    default_cta_text: Mapped[str] = mapped_column(String(280), default="")
    default_disclosure_text: Mapped[str] = mapped_column(String(200), default="")
    logo_path: Mapped[str] = mapped_column(String(500), default="")
    logo_public_token: Mapped[str] = mapped_column(String(80), default="", index=True)
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
    # reel_montage | talking_head_beta | static_motion | ambient_loop
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
    # The RQ job id of the plan or render job that claimed the project
    # (migration 0009): the sweeper leaves a claim alone while it is queued.
    queue_job_id: Mapped[str] = mapped_column(String(80), default="", server_default="")
    # Unique per claim (Generate / Retry / plan / ambient): the queued job
    # carries it and must consume it before doing anything, so a cancelled
    # entry never runs under a newer claim. "" = no claim waiting.
    claim_token: Mapped[str] = mapped_column(String(40), default="", server_default="")
    # draft|planning|planned|generating|completed|failed
    generated_plan_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Settings for mode="ambient_loop" (scene prompt, loop length, music mood,
    # crossfade…). Validated by schemas.ambient.AmbientConfig; None for every
    # other mode. Kept as JSON because these knobs evolve faster than the
    # storyboard pipeline's columns and never need to be queried individually.
    ambient_config_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # The project's own TTS voice; None = the primary character's voice
    # (and so the voiceover fingerprint of older projects is unchanged).
    voice_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    # The voiceover heard before paying for video ("Make voiceover"):
    # {path, fingerprint, duration_seconds, cost_estimate, created_at}.
    # A render whose voice fingerprint matches uses this file at no charge.
    voiceover_preview_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    primary_avatar_id: Mapped[Optional[int]] = mapped_column(ForeignKey("avatars.id"))
    brand_kit_id: Mapped[Optional[int]] = mapped_column(ForeignKey("brand_kits.id"), nullable=True)
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
    # User-chosen model for this shot's clip. Accepts a raw provider model id
    # or a `custom:<CustomModel.id>` selector for a trained LoRA; empty means
    # "use the provider default". Resolved by pipeline._resolve_model.
    model_override: Mapped[str] = mapped_column(String(300), default="")
    provider: Mapped[str] = mapped_column(String(40), default="")
    provider_model: Mapped[str] = mapped_column(String(120), default="")
    provider_job_id: Mapped[str] = mapped_column(String(200), default="")
    polling_url: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(40), default="pending")
    # pending|submitted|polling|downloaded|completed|failed
    clip_path: Mapped[str] = mapped_column(String(500), default="")
    # Hash of what the clip was made from (services/fingerprints.py).
    # "" = made before fingerprints existed: counts as current.
    clip_fingerprint: Mapped[str] = mapped_column(String(64), default="", server_default="")
    # The project's aspect ratio when the clip was made; "" = unknown (a
    # clip from before this was kept, filled in by migration 0006). A clip
    # whose aspect differs from the project's is out of date.
    clip_aspect_ratio: Mapped[str] = mapped_column(String(10), default="", server_default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    project: Mapped[VideoProject] = relationship(back_populates="shots")
    takes: Mapped[list[ShotTake]] = relationship(
        back_populates="shot", cascade="all, delete-orphan", passive_deletes=True,
        order_by="ShotTake.id",
    )

    @property
    def takes_count(self) -> int:
        """Clips kept for this shot, the current one included (a clip from
        before takes existed counts until it is recorded as a take)."""
        n = len(self.takes)
        if self.clip_path and not any(t.clip_path == self.clip_path for t in self.takes):
            n += 1
        return n

    @property
    def inspection(self) -> Optional[dict]:
        """The clip check's verdict on the current clip (its take), or None."""
        if not self.clip_path:
            return None
        for t in self.takes:
            if t.clip_path == self.clip_path:
                return t.inspection_json
        return None

    @property
    def clip_stale(self) -> bool:
        """The shot was edited since its clip was made: the next render
        makes (and bills) a new clip for it."""
        from ..services.fingerprints import shot_is_stale

        return shot_is_stale(self)


class ShotTake(Base):
    """One clip made for a storyboard shot. Re-rolling keeps the earlier
    takes so the creator can go back to one without paying again."""

    __tablename__ = "shot_takes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shot_id: Mapped[int] = mapped_column(ForeignKey("video_shots.id", ondelete="CASCADE"), index=True)
    clip_path: Mapped[str] = mapped_column(String(500), default="")
    clip_fingerprint: Mapped[str] = mapped_column(String(64), default="", server_default="")
    aspect_ratio: Mapped[str] = mapped_column(String(10), default="", server_default="")
    provider: Mapped[str] = mapped_column(String(40), default="")
    provider_model: Mapped[str] = mapped_column(String(300), default="")
    video_job_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # From the spend ledger when known; None = unknown (or free: mock).
    cost_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    # The clip check's verdict on this take (services/inspector.py); None =
    # not checked. {verdict, issues, summary, checked_at, model, ...}
    inspection_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    shot: Mapped[VideoShot] = relationship(back_populates="takes")


class Render(Base):
    __tablename__ = "renders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("video_projects.id"))
    status: Mapped[str] = mapped_column(String(40), default="pending")
    # pending|planning|generating_audio|generating_shots|polling|rendering|completed|failed
    audio_path: Mapped[str] = mapped_column(String(500), default="")
    # Hash of what the voiceover in audio_path was made from; "" = unknown.
    voice_fingerprint: Mapped[str] = mapped_column(String(64), default="", server_default="")
    final_video_path: Mapped[str] = mapped_column(String(500), default="")
    thumbnail_path: Mapped[str] = mapped_column(String(500), default="")
    render_log: Mapped[str] = mapped_column(Text, default="")
    estimated_cost: Mapped[float] = mapped_column(Float, default=0.0)
    actual_cost: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str] = mapped_column(Text, default="")
    share_token: Mapped[str] = mapped_column(String(80), default=_token, unique=True, index=True)
    # What this render was put together from (written at compose, migration
    # 0011): clips + take ids per shot, audio, caption timings, hook window,
    # variant id. None = composed before it was recorded.
    composition_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Set on a hook-lab variant: a copy of a base render with another opening.
    # use_alter: hook_variants also points back at renders.
    hook_variant_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("hook_variants.id", ondelete="SET NULL", use_alter=True,
                   name="fk_renders_hook_variant_id"),
        nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    project: Mapped[VideoProject] = relationship(back_populates="renders")


class HookVariant(Base):
    """One alternative opening (first spoken line, optionally the first
    shot) of a finished base render: the rest of the video is reused."""

    __tablename__ = "hook_variants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("video_projects.id", ondelete="CASCADE"), index=True)
    base_render_id: Mapped[int] = mapped_column(ForeignKey("renders.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(10), default="")  # "B", "C"...
    # question|bold_claim|stat|story|contrarian|custom|original
    style: Mapped[str] = mapped_column(String(30), default="custom")
    hook_text: Mapped[str] = mapped_column(Text, default="")
    # Where the original opening ends in the base render's voiceover.
    window_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    shot1_source: Mapped[str] = mapped_column(String(10), default="keep")  # keep|take|new|lipsync
    shot1_take_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    shot1_prompt: Mapped[str] = mapped_column(Text, default="", server_default="")
    shot1_clip_path: Mapped[str] = mapped_column(String(500), default="", server_default="")
    audio_path: Mapped[str] = mapped_column(String(500), default="", server_default="")
    audio_fingerprint: Mapped[str] = mapped_column(String(64), default="", server_default="")
    # The clips one make round needs and has: {"needed": [shot index...],
    # "clips": {"<index>": path}} (shot 1 = index 0; later lip-synced shots
    # that overlap the opening are redone too).
    clips_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # draft|making|ready|composing|completed|failed
    status: Mapped[str] = mapped_column(String(20), default="draft")
    error: Mapped[str] = mapped_column(Text, default="")
    # The variant's own Render (once composed).
    render_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("renders.id", ondelete="SET NULL"), nullable=True)
    cost_estimate: Mapped[float] = mapped_column(Float, default=0.0)
    # Set while "Listen" makes its voice (paid TTS): Make waits until then,
    # so the voice is never bought twice. Past it, the claim is stale.
    voicing_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


# --- Asset Studio (image/clip generation that produces reusable assets) ---


class AssetGenerationJob(Base):
    __tablename__ = "asset_generation_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_kind: Mapped[str] = mapped_column(String(20), default="avatar")  # avatar|ingredient
    owner_id: Mapped[int] = mapped_column(Integer)
    output_kind: Mapped[str] = mapped_column(String(20), default="image")  # image|video_clip
    # Requested clip length (video_clip only); the provider snaps it to what
    # the model supports.
    duration_seconds: Mapped[float] = mapped_column(Float, default=5.0)
    # "9:16" | "16:9" | "1:1"; "" = the owner's default (avatar 9:16,
    # ingredient 16:9). Migration 0003.
    aspect_ratio: Mapped[str] = mapped_column(String(10), default="", server_default="")
    prompt: Mapped[str] = mapped_column(Text, default="")
    negative_prompt: Mapped[str] = mapped_column(Text, default="")
    reference_asset_ids_json: Mapped[list | None] = mapped_column(JSON, default=list)
    provider: Mapped[str] = mapped_column(String(40), default="")
    provider_model: Mapped[str] = mapped_column(String(120), default="")
    provider_job_id: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(40), default="pending")
    # pending|submitted|polling|completed|failed|saved
    result_path: Mapped[str] = mapped_column(String(500), default="")
    # assets ↔ asset_generation_jobs reference each other. use_alter emits
    # this FK as a separate ALTER after both tables exist, so dependency
    # ordering (create_all on Postgres, the SQLite→Postgres copy) works.
    # The name matches the initial migration's constraint.
    result_asset_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("assets.id", use_alter=True, name="fk_asset_generation_jobs_result_asset_id"),
        nullable=True,
    )
    error: Mapped[str] = mapped_column(Text, default="")
    # Library: starred in Studio, and a short display tag ("Front view" for
    # character-sheet jobs, "" otherwise). Migration 0004.
    favorite: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    label: Mapped[str] = mapped_column(String(80), default="", server_default="")
    # "Animate this": the clip starts from a finished Studio image or a Cast
    # picture (at most one of the two). Migration 0009.
    first_frame_job_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    first_frame_asset_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # A clip's real size and length, probed once when it lands (None for
    # older jobs and images: show the requested values). Migration 0009.
    result_width: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    result_height: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    result_duration_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # The RQ job id of its latest queue entry ("" before migration 0009, or
    # "inline:..." when it ran inline): the sweeper only gives up on a
    # pending job whose queue entry is gone.
    queue_job_id: Mapped[str] = mapped_column(String(80), default="", server_default="")
    # An operation on an existing picture instead of a generation: "" =
    # generate, "remove_background" or "upscale". The source is a finished
    # Studio picture (source_job_id: no FK, jobs are tombstoned and ids are
    # never reused) or a Cast picture (source_asset_id). options_json holds
    # the operation's settings, e.g. {"factor": 2}. Migration 0010.
    operation: Mapped[str] = mapped_column(String(30), default="", server_default="")
    source_job_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source_asset_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("assets.id", use_alter=True, name="fk_asset_generation_jobs_source_asset_id",
                   ondelete="SET NULL"),
        nullable=True,
    )
    options_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class CustomModel(Base):
    """A custom model / adapter trained off a cast member's reference assets.

    Three flavors today:
      - `character_lora`: LoRA fine-tune on an image model. 10-50 reference
        images of one character, conditions every future image+video gen
        for tighter identity than reference-image conditioning gives.
      - `style_lora`: same mechanic, trained on a cast member's existing
        *renders* to capture brand/look. Applied alongside character LoRA.
      - `voice_clone`: ElevenLabs Voice Lab clone. Upload voice samples
        (audio assets on an Avatar), get back an elevenlabs voice_id we
        store in `provider_model_id`. The Avatar's `elevenlabs_voice_id`
        is set when the job completes so the existing TTS path uses it.

    `training_asset_ids_json` is the list of Asset rows feeding the
    training; `config_json` carries per-provider knobs (rank, steps,
    learning rate for LoRA; emotional range for voice). `provider_model_id`
    is what we hand the inference adapter to use the trained model at
    generation time."""

    __tablename__ = "custom_models"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    kind: Mapped[str] = mapped_column(String(40), default="character_lora")
    # character_lora|style_lora|voice_clone
    owner_kind: Mapped[str] = mapped_column(String(20), default="avatar")
    # avatar|ingredient
    owner_id: Mapped[int] = mapped_column(Integer)
    training_asset_ids_json: Mapped[list | None] = mapped_column(JSON, default=list)
    config_json: Mapped[dict | None] = mapped_column(JSON, default=dict)
    provider: Mapped[str] = mapped_column(String(40), default="")
    provider_model_id: Mapped[str] = mapped_column(String(300), default="")
    provider_job_id: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(40), default="pending")
    # pending|training|completed|failed|cancelled
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str] = mapped_column(Text, default="")
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


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


class AppSetting(Base):
    """Small key/value store for runtime settings the UI can change without a
    redeploy — chiefly the active model per capability ("model.video" →
    "alibaba/wan-3.0") and the last polled model catalog snapshot."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class PublishJob(Base):
    """One upload of a render to an external platform (YouTube for now)."""

    __tablename__ = "publish_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    render_id: Mapped[int] = mapped_column(ForeignKey("renders.id"), index=True)
    platform: Mapped[str] = mapped_column(String(20), default="youtube")
    status: Mapped[str] = mapped_column(String(20), default="queued")
    # queued | uploading | processing | scheduled | published | failed
    title: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    tags_json: Mapped[list | None] = mapped_column(JSON, default=list)
    category_id: Mapped[str] = mapped_column(String(10), default="22")
    privacy: Mapped[str] = mapped_column(String(20), default="private")  # private|unlisted|public
    publish_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # UTC
    made_for_kids: Mapped[bool] = mapped_column(Boolean, default=False)
    contains_synthetic_media: Mapped[bool] = mapped_column(Boolean, default=True)
    set_thumbnail: Mapped[bool] = mapped_column(Boolean, default=True)
    # Resumable session URI — kept so a crashed upload resumes, not restarts.
    upload_url: Mapped[str] = mapped_column(String(1000), default="")
    bytes_total: Mapped[int] = mapped_column(Integer, default=0)
    bytes_sent: Mapped[int] = mapped_column(Integer, default=0)
    external_id: Mapped[str] = mapped_column(String(100), default="")
    external_url: Mapped[str] = mapped_column(String(300), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    # When the last byte reached YouTube (None = not uploaded). Migration
    # 0008 fills it from updated_at for videos uploaded before it existed.
    uploaded_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # What YouTube said about the video the last time we asked
    # (videos.list part=status,snippet). "" / None = never checked.
    yt_privacy_status: Mapped[str] = mapped_column(String(20), default="", server_default="")
    yt_upload_status: Mapped[str] = mapped_column(String(20), default="", server_default="")
    yt_publish_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # UTC
    yt_failure_reason: Mapped[str] = mapped_column(String(100), default="", server_default="")
    yt_title: Mapped[str] = mapped_column(String(200), default="", server_default="")
    yt_removed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    yt_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # The YouTube channel the video went to ("" = not known yet). videos.list
    # leaves out private videos of other channels, so a video is only taken
    # for removed when this matches the channel connected now.
    channel_id: Mapped[str] = mapped_column(String(64), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    @property
    def state(self) -> str:
        """What the creator should read: uploading | published | scheduled |
        private | processing | removed | failed (services/publish_state.py)."""
        from ..services.publish_state import display_state

        return display_state(self)[0]

    @property
    def warning(self) -> str:
        """Where YouTube's state differs from what was asked; "" when not."""
        from ..services.publish_state import display_state

        return display_state(self)[1]


class VideoStat(Base):
    """One day's YouTube numbers for an uploaded video. At most one row per
    publish job per UTC day: a later fetch the same day updates it."""

    __tablename__ = "video_stats"
    __table_args__ = (Index("ix_video_stats_job_fetched", "publish_job_id", "fetched_at"),
                      UniqueConstraint("publish_job_id", "day", name="uq_video_stats_job_day"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    publish_job_id: Mapped[int] = mapped_column(ForeignKey("publish_jobs.id", ondelete="CASCADE"))
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    day: Mapped[date] = mapped_column(Date)  # UTC day of fetched_at
    views: Mapped[int] = mapped_column(BigInteger, default=0)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    comments: Mapped[int] = mapped_column(Integer, default=0)


class VideoJob(Base):
    """One asynchronous video generation at a provider — a storyboard shot,
    an ambient chain segment, a Studio clip, or an Avatar IV talking head.

    Workers submit and move on; completion arrives by webhook (OpenRouter
    `callback_url` → /api/webhooks/openrouter/videos/{callback_token}) or,
    as a safety net / when webhooks can't reach us, by the sweeper polling
    jobs whose `next_check_at` has passed."""

    __tablename__ = "video_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    purpose: Mapped[str] = mapped_column(String(30))  # shot|avatar|ambient_segment|studio
    ref_id: Mapped[int] = mapped_column(Integer)  # VideoShot.id or AssetGenerationJob.id
    project_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    render_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    provider: Mapped[str] = mapped_column(String(40), default="")
    model: Mapped[str] = mapped_column(String(300), default="")
    provider_job_id: Mapped[str] = mapped_column(String(200), default="")
    polling_url: Mapped[str] = mapped_column(String(500), default="")
    callback_token: Mapped[str] = mapped_column(String(80), default=_token, unique=True, index=True)
    uses_webhook: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="submitted", index=True)
    # submitted | running | completing | completed | failed
    context_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result_path: Mapped[str] = mapped_column(String(500), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    cost_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    checks: Mapped[int] = mapped_column(Integer, default=0)
    next_check_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    deadline_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class SpendEntry(Base):
    """One billed (or estimated) provider call — the spend ledger.

    `source` says where the number came from: "provider" (the API reported
    it: OpenRouter `usage.cost`, video job cost), "lookup" (fetched later
    from OpenRouter's generation endpoint), "pending" (waiting on that
    lookup; `cost_usd` holds a rate-card estimate meanwhile) or "rate_card"
    (provider reports nothing, e.g. ElevenLabs — `is_estimate` is True)."""

    __tablename__ = "spend_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    provider: Mapped[str] = mapped_column(String(40))  # openrouter|elevenlabs|gemini|replicate
    kind: Mapped[str] = mapped_column(String(20), index=True)  # chat|image|video|avatar|tts|music|training
    model: Mapped[str] = mapped_column(String(200), default="")
    project_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    render_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    ref: Mapped[str] = mapped_column(String(80), default="", index=True)  # e.g. "video_job:12", "studio_job:3"
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    is_estimate: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(20), default="provider", index=True)
    generation_id: Mapped[str] = mapped_column(String(120), default="")
    note: Mapped[str] = mapped_column(String(300), default="")


class Event(Base):
    """One thing that happened to a job, for live progress and the activity
    panel (services/events.py). Written in the same transaction as the
    status change it reports; streamed to browsers by /api/events/stream."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    # render|plan|ambient_loop|ambient_render|studio_job|training|publish|budget
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[int] = mapped_column(Integer)
    project_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20))  # started|progress|completed|failed|cancelled
    title: Mapped[str] = mapped_column(String(200), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(300), default="")  # deep link, e.g. /projects/12
    severity: Mapped[str] = mapped_column(String(10), default="info")  # info|warning|error
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)


class PromptCheck(Base):
    """Cached verdicts of the AI minors check, by a hash of the policy
    version and the normalised prompt (services/minor_check.py)."""

    __tablename__ = "prompt_checks"

    text_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    verdict: Mapped[str] = mapped_column(String(16))  # ok|minor_sexual
    model: Mapped[str] = mapped_column(String(120), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

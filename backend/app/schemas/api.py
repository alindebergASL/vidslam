from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

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
    # Provenance: for generated assets, the AssetGenerationJob that produced
    # this file. Join through it to see the prompt + resolved model/adapter
    # (including trained LoRAs) that made the image. None for uploads.
    generation_job_id: Optional[int] = None
    created_at: datetime


# asset_type values each owner kind accepts (the Asset model lists the same).
ASSET_TYPES: dict[str, tuple[str, ...]] = {
    "avatar": ("hero", "reference_sheet", "expression_sheet", "outfit_sheet",
               "lifestyle", "logo", "other"),
    "ingredient": ("hero", "reference", "other"),
}


class AssetUpdate(BaseModel):
    asset_type: str


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
    mode: Literal["reel_montage", "talking_head_beta", "static_motion", "ambient_loop"] = (
        "reel_montage"
    )
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
    # The project's own TTS voice; null or "" = back to the character's voice.
    voice_id: Optional[str] = Field(None, max_length=120)


class InspectionIssueOut(BaseModel):
    kind: Literal["likeness", "anatomy", "text", "prompt_mismatch", "artifact", "other"] = "other"
    detail: str


class InspectionOut(BaseModel):
    """The clip check's verdict on a clip (services/inspector.py).
    verdict: ok = looks right; problem = see issues; unsure = couldn't
    check it; skipped = the automatic check didn't run (summary says why)."""
    verdict: Literal["ok", "problem", "unsure", "skipped"]
    issues: list[InspectionIssueOut] = Field(default_factory=list)
    summary: str = ""
    checked_at: Optional[str] = None
    model: str = ""
    auto: bool = False
    cost_estimate: float = 0.0


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
    model_override: str
    provider: str
    provider_model: str
    status: str
    clip_path: str
    error: str
    # Edited since its clip was made: the next render makes a new clip.
    clip_stale: bool = False
    # Aspect ratio the clip was made at ("" = unknown / no clip).
    clip_aspect_ratio: str = ""
    # Clips kept for this shot (the current one included).
    takes_count: int = 0
    # The clip check's verdict on the current clip; None = not checked.
    inspection: Optional[InspectionOut] = None


AddShotType = Literal["hero", "b_roll", "talking_head", "transition"]


class ShotCreate(BaseModel):
    """Body for POST /api/projects/{id}/shots: a new shot with no clip."""
    after_shot_id: Optional[int] = None  # None = at the end (before the end card)
    shot_type: AddShotType = "b_roll"
    prompt: str = ""
    duration_seconds: float = Field(5.0, ge=1.0, le=60.0)


class ShotUpdate(BaseModel):
    prompt: Optional[str] = None
    negative_prompt: Optional[str] = None
    duration_seconds: Optional[float] = None
    reference_asset_ids_json: Optional[list] = None
    reference_strategy: Optional[str] = None
    caption_text: Optional[str] = None
    camera_direction: Optional[str] = None
    # Raw provider model id or `custom:<id>` trained-LoRA selector; empty
    # string clears the override back to the provider default.
    model_override: Optional[str] = None


class PlanEditIn(BaseModel):
    """Edits to the generated storyboard plan's spoken script / end card /
    disclosure. resync_captions re-derives caption_chunks from the (possibly
    edited) spoken script."""

    cleaned_voice_script: Optional[str] = None
    # The opening line (first sentence of the spoken script). A changed hook
    # replaces the script's first sentence (also when a script is sent);
    # otherwise the hook follows the script's first sentence.
    hook: Optional[str] = None
    end_card_text: Optional[str] = None
    disclosure_text: Optional[str] = None
    resync_captions: bool = False


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    # The project's own TTS voice; None = the primary character's voice.
    voice_id: Optional[str] = None
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
    ambient_config_json: Optional[dict] = None
    # Newest completed render (for list thumbnails); set by GET /projects only.
    latest_render_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    cast_members: list[CastMemberOut] = Field(default_factory=list)
    shots: list[ShotOut] = Field(default_factory=list)


class ShotEditOut(BaseModel):
    """Result of adding, duplicating or deleting a shot."""
    project: ProjectOut
    shot: Optional[ShotOut] = None  # the new shot (None after a delete)
    # Captions were spread over the new total shot time.
    captions_resynced: bool = False
    # Delete only: clip files thrown away (the shot's takes).
    clips_discarded: int = 0


class StaleShotOut(BaseModel):
    id: int
    shot_order: int
    cost: float


class ShotEditImpactOut(BaseModel):
    """What adding, duplicating or deleting a shot would put out of date."""
    # Lip-synced clips whose slice of the voiceover would move.
    stale_shots: list[StaleShotOut]
    stale_count: int
    stale_cost: float
    # The new shot's own clip (add / duplicate), made at the next Generate video.
    new_clip_cost: float
    currency: str = "USD"
    is_estimate: bool = True


class TakeOut(BaseModel):
    id: int
    shot_id: int
    current: bool
    provider: str
    provider_model: str
    cost_usd: Optional[float] = None
    note: str = ""
    aspect_ratio: str = ""
    file_exists: bool
    url: str
    inspection: Optional[InspectionOut] = None
    created_at: Optional[str] = None


class TakeFromStudioIn(BaseModel):
    studio_job_id: int


class TakeFromStudioOut(TakeOut):
    # A plain note when the clip doesn't fit the shot or project ("" = fits).
    warning: str = ""
    duration_seconds: Optional[float] = None


class TakesOut(BaseModel):
    takes: list[TakeOut]
    keep: int  # takes kept per shot; older ones are deleted (never the current one)


class VoiceoverPreviewOut(BaseModel):
    url: str
    source: Literal["preview", "render"]
    fingerprint: str
    # Still matches the spoken script, voice and voice model.
    current: bool
    duration_seconds: float
    video_seconds: float
    difference_seconds: float
    # "no_plan": no storyboard yet, so there are no shots to fit.
    fit: Literal["fits", "too_long", "too_short", "no_plan"]
    fit_text: str
    # Seconds of the voice's end the video would cut off (0.0: none).
    cut_seconds: float = 0.0
    # What a Re-compose with the current clips does, set only when it
    # differs from the fit above (which is what Generate video does).
    recompose_fit: Optional[Literal["fits", "too_long", "too_short"]] = None
    recompose_fit_text: str = ""
    recompose_video_seconds: Optional[float] = None
    recompose_cut_seconds: Optional[float] = None
    # Both in one plain sentence ("" when they agree).
    recompose_note: str = ""
    cost_estimate: float
    # POST only: true when an identical voiceover existed, so nothing was paid.
    reused: bool = False
    created_at: Optional[str] = None


class VoiceoverFitOut(BaseModel):
    """An uploaded voiceover's length against the shots, and what the render
    does with it (pipeline.voiceover_fit)."""
    duration_seconds: float
    video_seconds: float
    difference_seconds: float
    # "no_plan": no storyboard yet, so there are no shots to fit.
    fit: Literal["fits", "too_long", "too_short", "no_plan"]
    fit_text: str
    # Seconds of the voice's end the video would cut off (0.0: none).
    cut_seconds: float = 0.0
    # What a Re-compose with the current clips does, set only when it
    # differs from the fit above (which is what Generate video does).
    recompose_fit: Optional[Literal["fits", "too_long", "too_short"]] = None
    recompose_fit_text: str = ""
    recompose_video_seconds: Optional[float] = None
    recompose_cut_seconds: Optional[float] = None
    # Both in one plain sentence ("" when they agree).
    recompose_note: str = ""


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
    # A hook-lab variant of another render (its opening differs).
    hook_variant_id: Optional[int] = None
    # A variant's base render (known while it is still being made, before
    # its composition_json is written).
    hook_base_render_id: Optional[int] = None
    # What the render was put together from (None: composed before 0011).
    composition_json: Optional[dict] = None
    # The project's one version numbering (services/render_versions.py):
    # completed base renders 1, 2, 3…; a variant has its base's number.
    version: Optional[int] = None
    version_label: str = ""


class ProjectStatusOut(BaseModel):
    project_id: int
    project_status: str
    # The project is held by the hook lab making other openings (no render
    # of its own is waiting or running).
    hook_lab_making: bool = False
    # The base render (version) those openings are made for, when known.
    hook_lab_base_render_id: Optional[int] = None
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
    duration_seconds: float = Field(5.0, ge=1.0, le=60.0)
    # None = the owner's default (avatar 9:16 portrait, ingredient 16:9).
    aspect_ratio: Optional[Literal["9:16", "16:9", "1:1"]] = None
    # Images only: this many separate jobs from one request. A clip
    # request with more than 1 is refused (422) rather than billed N times.
    variations: int = Field(1, ge=1, le=4)
    # Clips only ("Animate this"): start from a finished Studio image or a
    # Cast picture (at most one). 422 when the video model can't.
    first_frame_job_id: Optional[int] = None
    first_frame_asset_id: Optional[int] = None


class StudioSaveRequest(BaseModel):
    """Body for POST /api/studio/jobs/{id}/save. Every field is optional:
    left out, the asset goes to the job's own owner with that kind's
    default type. Naming another owner files the image to that character."""
    owner_kind: Optional[Literal["avatar", "ingredient"]] = None
    owner_id: Optional[int] = None
    asset_type: Optional[str] = None


class StudioJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    owner_kind: str
    owner_id: int
    output_kind: str
    prompt: str
    # The user's own "avoid" text (defaults are added when the job runs).
    negative_prompt: str = ""
    reference_asset_ids: list[int] = Field(
        default_factory=list, validation_alias=AliasChoices("reference_asset_ids", "reference_asset_ids_json"))
    aspect_ratio: str = ""  # "" = the owner's default
    duration_seconds: float
    provider: str
    provider_model: str
    status: str
    result_path: str
    result_asset_id: Optional[int]
    error: str
    created_at: datetime
    updated_at: datetime
    # Every job a create request made (the first included); only on that response.
    batch_ids: list[int] = Field(default_factory=list)
    favorite: bool = False
    label: str = ""  # "" = no tag; character-sheet jobs carry their view
    # Money in the spend ledger for this job (an image's own calls, a clip's
    # video jobs), summed; None until something is recorded.
    cost_usd: Optional[float] = None
    cost_is_estimate: bool = False
    # "Animate this": the picture the clip starts from (null = none).
    first_frame_job_id: Optional[int] = None
    first_frame_asset_id: Optional[int] = None
    # A finished clip's real size and length (null: not probed, e.g. an
    # older job; show the requested values).
    result_width: Optional[int] = None
    result_height: Optional[int] = None
    result_duration_seconds: Optional[float] = None
    # Reference pictures in reference_asset_ids that were deleted since.
    missing_reference_ids: list[int] = Field(default_factory=list)
    # Picture operations: "" = generated from the prompt, else
    # "remove_background" | "upscale", made from a Studio picture
    # (source_job_id) or a Cast picture (source_asset_id), or
    # "extract_frame", a frame of a Studio clip (source_job_id) or of an
    # uploaded video (no source: the video isn't kept); options holds
    # e.g. {"factor": 2} or {"at": "last", "seconds": 4.95}. A clip can be
    # "join_clips": finished Studio clips played back to back (no single
    # source; options {"job_ids": [...], "transition", "crossfade",
    # "has_audio"}), or "upload_video": a video uploaded from the owner's
    # device (provider "local", no model; options {"upload": its name,
    # "has_audio", "reencoded"}). A clip made by "Continue this clip" is a
    # generation (operation "") whose options hold {"continues_job_id",
    # "auto_join"} and, once it finished, "join_job_id" (the join with the
    # original) or "join_note" (why there is none); that join's options
    # hold "auto_join_for" (the continuation's id).
    operation: str = ""
    source_job_id: Optional[int] = None
    source_asset_id: Optional[int] = None
    options: dict = Field(default_factory=dict, validation_alias=AliasChoices("options", "options_json"))
    # A Sogni picture made from reference pictures runs on one of Sogni's
    # editors, not the picked model: the editor that really ran (its name
    # and id, from options["ran_on"]); null when the picked model ran.
    ran_on_name: Optional[str] = None
    ran_on_editor: Optional[str] = None
    # What the reference pictures are for when Sogni picks an editor:
    # "identity" when one is a Cast character's picture (not its logo),
    # else "guide"; null for a picture without references.
    ref_intent: Optional[Literal["identity", "guide"]] = None

    @field_validator("options", mode="before")
    @classmethod
    def _options(cls, v):
        return dict(v or {})

    @model_validator(mode="after")
    def _ran_on(self):
        ran = self.options.get("ran_on")
        if isinstance(ran, dict):
            self.ran_on_name = self.ran_on_name or str(ran.get("name") or "") or None
            self.ran_on_editor = self.ran_on_editor or str(ran.get("model") or "") or None
        return self

    @field_validator("operation", mode="before")
    @classmethod
    def _op(cls, v):
        return v or ""

    @field_validator("reference_asset_ids", mode="before")
    @classmethod
    def _ref_ids(cls, v):
        return list(v or [])

    @field_validator("negative_prompt", "aspect_ratio", "label", mode="before")
    @classmethod
    def _none_is_empty(cls, v):
        return v or ""

    @field_validator("favorite", mode="before")
    @classmethod
    def _none_is_false(cls, v):
        return bool(v)


class StudioJobUpdate(BaseModel):
    """Body for PATCH /api/studio/jobs/{id}; a field left out stays as is."""
    favorite: Optional[bool] = None
    label: Optional[str] = Field(None, max_length=80)

    @field_validator("label", mode="before")
    @classmethod
    def _strip(cls, v):
        return v.strip() if isinstance(v, str) else v


class StudioBulkIds(BaseModel):
    ids: list[int] = Field(..., min_length=1, max_length=200)

    @field_validator("ids")
    @classmethod
    def _unique(cls, v: list[int]) -> list[int]:
        if len(set(v)) != len(v):
            raise ValueError("ids must be unique")
        return v


class StudioBulkFavorite(StudioBulkIds):
    favorite: bool


class StudioBulkSave(StudioBulkIds):
    owner_kind: Literal["avatar", "ingredient"]
    owner_id: int
    asset_type: Optional[str] = None


class StudioBulkDeleteOut(BaseModel):
    deleted: list[int]
    missing: list[int]


class StudioBulkFavoriteOut(BaseModel):
    updated: list[int]
    missing: list[int]


class StudioSkipped(BaseModel):
    id: int
    reason: str


class StudioBulkSaveOut(BaseModel):
    saved: list[AssetOut]
    skipped: list[StudioSkipped]


class StudioRemoveBackgroundRequest(BaseModel):
    """Body for POST .../remove-background; no model = the active one."""
    model: Optional[str] = Field(None, max_length=300)


class StudioUpscaleRequest(BaseModel):
    """Body for POST .../upscale; no model = the active one."""
    factor: Literal[2, 4] = 2
    model: Optional[str] = Field(None, max_length=300)


class StudioFrameRequest(BaseModel):
    """Body for POST .../frame-to-picture: which frame of the clip to save,
    "first", "last" or a time in seconds (kept within the clip)."""
    at: Literal["first", "last"] | float = "last"


class StudioJoinRequest(BaseModel):
    """Body for POST /api/studio/join-clips: finished Studio clips, in the
    order to play them (2 to 12; one may repeat), and how each one leads
    into the next: "cut" or a "dissolve" of `crossfade` seconds (0.5 when
    not given; kept to a third of the shortest clip)."""
    job_ids: list[int] = Field(..., max_length=100)
    transition: Literal["cut", "dissolve"] = "cut"
    crossfade: Optional[float] = Field(None, ge=0, le=1.5)


class StudioContinueRequest(BaseModel):
    """Body for POST /api/studio/jobs/{id}/continue: a new clip that starts
    from this finished clip's last frame. The prompt says what happens
    next; the aspect is the clip's own. `model`: one that can start from a
    picture (null = the clip's own model when it can, else the default
    video model). `auto_join`: once the new clip is finished, join the two
    into one more clip (a cut)."""
    prompt: str = Field(..., min_length=1)
    negative_prompt: str = ""
    model: Optional[str] = None
    duration_seconds: float = Field(5.0, ge=1.0, le=60.0)
    auto_join: bool = True


class StudioContinueInfo(BaseModel):
    """GET /api/studio/jobs/{id}/continue: what "Continue this clip" starts
    from: the prompt to prefill (a joined clip's last clip's; "" for an
    uploaded video), the aspect it keeps, and the model it would use when
    none is picked ("" = none can start from a picture)."""
    prompt: str = ""
    negative_prompt: str = ""
    aspect_ratio: str = ""
    model: str = ""
    duration_seconds: float = 5.0


class ImageOpEstimateOut(BaseModel):
    """GET /api/studio/operations/estimate: what a run would cost and make."""
    op: str
    model: str
    model_name: str
    provider: str  # "runware" | "local" | "mock" | "unavailable"
    cost_usd: float
    is_estimate: bool = True
    free: bool
    factor: Optional[int] = None
    output_width: Optional[int] = None  # null when no source picture was given
    output_height: Optional[int] = None
    too_large: bool = False
    message: str


class ImageOpModelOut(BaseModel):
    """One model offered for a picture operation."""
    id: str
    name: str
    description: str = ""
    provider: str = ""
    price_text: str = ""
    free: bool = False
    usable: bool = True
    factors: list[int] = Field(default_factory=list)
    active: bool = False


class StudioRetryRequest(BaseModel):
    """Body for POST /api/studio/jobs/{id}/retry: every field optional, a
    field left out (or null) keeps the source job's value."""
    model: Optional[str] = None  # "" = keep the source's model too
    prompt: Optional[str] = Field(None, min_length=1)
    negative_prompt: Optional[str] = None
    aspect_ratio: Optional[Literal["9:16", "16:9", "1:1"]] = None


# Character-sheet views: id → (label, aspect, what the picture shows).
CHARACTER_SHEET_VIEWS: dict[str, tuple[str, str, str]] = {
    "front": ("Front view", "9:16", "full body, facing the camera, neutral pose, arms relaxed"),
    "three_quarter": ("Three-quarter view", "9:16", "full body turned 45° to the left"),
    "profile": ("Side profile", "9:16", "full body in profile facing right"),
    "back": ("Back view", "9:16", "full body from behind"),
    "portrait": ("Close-up portrait", "1:1", "head and shoulders, neutral expression, facing the camera"),
    "smile": ("Smiling close-up", "1:1", "head and shoulders, natural smile"),
    "action": ("Walking", "9:16", "full body walking toward the camera, mid-stride"),
    "seated": ("Seated", "9:16", "sitting on a plain stool, full body"),
}
DEFAULT_SHEET_VIEWS = ["front", "three_quarter", "profile", "back"]


class CharacterSheetRequest(BaseModel):
    avatar_id: int
    views: list[str] = Field(default_factory=lambda: list(DEFAULT_SHEET_VIEWS), min_length=1, max_length=8)
    # None (or empty) = the avatar's own pictures.
    reference_asset_ids: Optional[list[int]] = None
    model: Optional[str] = None
    style: str = Field("", max_length=300)  # extra words, e.g. "studio photo, plain grey background"
    negative_prompt: str = ""

    @field_validator("views")
    @classmethod
    def _known_views(cls, v: list[str]) -> list[str]:
        unknown = [x for x in v if x not in CHARACTER_SHEET_VIEWS]
        if unknown:
            raise ValueError(f"unknown view {', '.join(unknown)}; one of {', '.join(CHARACTER_SHEET_VIEWS)}")
        if len(set(v)) != len(v):
            raise ValueError("views must be unique")
        return v


class CharacterSheetView(BaseModel):
    id: str
    label: str
    aspect_ratio: str


class CharacterSheetOut(BaseModel):
    batch_ids: list[int]
    jobs: list[StudioJobOut]


# --- Custom model training ---


class CustomModelTrainRequest(BaseModel):
    """Body for POST /api/cast/{kind}/{id}/train. The cast member is in the
    URL path; this body carries everything else."""
    name: str = ""
    kind: Literal["character_lora", "style_lora", "voice_clone"]
    training_asset_ids: list[int] = Field(default_factory=list)
    # Free-form provider knobs (LoRA rank/steps, voice tone tags). Validated
    # by the chosen provider, not here.
    config: dict = Field(default_factory=dict)


class CustomModelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    kind: str
    owner_kind: str
    owner_id: int
    training_asset_ids_json: list[int] = Field(default_factory=list)
    config_json: dict = Field(default_factory=dict)
    provider: str
    provider_model_id: str
    provider_job_id: str
    status: str
    progress: float
    error: str
    cost_usd: float
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None

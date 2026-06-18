from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional, Protocol

from ..schemas.storyboard import StoryboardPlan


@dataclass
class ImageRef:
    url: str  # tokenized public URL the provider can fetch
    role: Literal["character", "scene", "style", "object"] = "character"
    name: str = ""


@dataclass
class SubmittedJob:
    provider: str
    model: str
    job_id: str
    polling_url: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class JobStatus:
    state: Literal["queued", "running", "succeeded", "failed"]
    progress: float = 0.0
    result_url: Optional[str] = None
    error: Optional[str] = None


@dataclass
class ModelInfo:
    id: str
    name: str
    modality: Literal["chat", "image", "video"]
    description: str = ""


@dataclass
class CastContext:
    """Compact view of a project's cast handed to the planner."""

    avatars: list[dict] = field(default_factory=list)  # name, persona, visual_identity, role, asset_types, refs[]
    ingredients: list[dict] = field(default_factory=list)  # name, kind, visual_identity, refs[]


class ChatProvider(Protocol):
    def generate_storyboard(
        self,
        *,
        project: dict,
        cast: CastContext,
    ) -> StoryboardPlan: ...


class ImageProvider(Protocol):
    def list_models(self) -> list[ModelInfo]: ...

    def generate(
        self,
        *,
        model: str,
        prompt: str,
        references: list[ImageRef],
        negative_prompt: str = "",
        settings: dict | None = None,
    ) -> bytes:
        """Synchronous image generation: returns PNG/JPEG bytes."""
        ...


class VideoProvider(Protocol):
    def list_models(self) -> list[ModelInfo]: ...

    def submit(
        self,
        *,
        model: str,
        prompt: str,
        references: list[ImageRef],
        negative_prompt: str = "",
        reference_strategy: str = "input_references",
        duration_seconds: float = 5.0,
        settings: dict | None = None,
    ) -> SubmittedJob: ...

    def poll(self, job: SubmittedJob) -> JobStatus: ...

    def download(self, job: SubmittedJob) -> bytes: ...


class TTSProvider(Protocol):
    def synthesize(
        self,
        *,
        text: str,
        voice_id: str,
        settings: dict | None = None,
    ) -> bytes: ...

    def list_voices(self) -> list[dict]: ...


class MusicProvider(Protocol):
    def generate(
        self,
        *,
        prompt: str,
        duration_seconds: float,
        settings: dict | None = None,
    ) -> bytes:
        """Synchronous music generation: returns audio bytes (mp3 or m4a)."""
        ...

    def output_extension(self) -> str:
        """Return the file extension produced by `generate` (e.g. 'mp3')."""
        ...

    def list_models(self) -> list[ModelInfo]: ...


@dataclass
class LipSyncJob:
    """Result of a lip-sync render: a path to a new video clip where the
    speaker's mouth tracks the supplied audio. Producer is responsible for
    cleaning up; consumer should treat the file as ephemeral."""

    video_bytes: bytes
    duration_seconds: float


@dataclass
class TrainingAsset:
    """One file feeding a custom-model training job. Local path + mime so the
    adapter can multipart-upload to the training provider without going
    through the public-asset route."""

    local_path: str
    mime_type: str
    asset_id: int


@dataclass
class TrainingSubmit:
    provider: str
    provider_job_id: str  # e.g. Replicate prediction id, ElevenLabs voice id
    raw: dict = field(default_factory=dict)


@dataclass
class TrainingStatus:
    state: Literal["queued", "training", "succeeded", "failed"]
    progress: float = 0.0
    provider_model_id: str = ""
    """The id the inference adapter will reference to *use* the trained
    model — Replicate version hash, ElevenLabs voice_id, etc. Populated on
    succeeded; empty otherwise."""
    error: Optional[str] = None
    cost_usd: float = 0.0


class TrainingProvider(Protocol):
    """Adapter for training a custom model / adapter off a cast member's
    assets. Three concrete implementations (Mock + Replicate-style LoRA +
    ElevenLabs Voice Lab) share this surface. The pipeline submits, polls
    until terminal, and writes the resulting provider_model_id back onto
    the CustomModel row + (for voice clones) the owning Avatar."""

    def list_kinds(self) -> list[str]:
        """Which CustomModel.kind values this provider can train. Lets the
        registry route a request to the right adapter."""
        ...

    def submit(
        self,
        *,
        kind: str,
        name: str,
        assets: list[TrainingAsset],
        config: dict,
    ) -> TrainingSubmit: ...

    def poll(self, job: TrainingSubmit) -> TrainingStatus: ...

    def cancel(self, job: TrainingSubmit) -> bool:
        """Best-effort cancel. Returns True if the provider accepted the
        cancel request; False if the job already finished or the provider
        doesn't support mid-flight cancel."""
        ...


class LipSyncProvider(Protocol):
    """Adapter slot for a future lip-sync model (Wav2Lip, SadTalker, HeyGen,
    D-ID, etc.). Wired into the shot pipeline as an opt-in
    `ShotPlan.reference_strategy = "lip_sync"`; when set, the shot's video
    clip is post-processed by a provider implementing this protocol instead
    of being used directly. No real implementation yet — the slot exists so
    a future drop-in doesn't need an API/schema migration."""

    def list_models(self) -> list[ModelInfo]: ...

    def render(
        self,
        *,
        model: str,
        source_video: bytes,
        target_audio: bytes,
        settings: dict | None = None,
    ) -> LipSyncJob: ...

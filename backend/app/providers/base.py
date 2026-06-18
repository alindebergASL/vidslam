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

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

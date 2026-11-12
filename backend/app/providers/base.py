from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional, Protocol

from ..schemas.storyboard import StoryboardPlan


@dataclass
class ImageRef:
    url: str  # tokenized public URL the provider can fetch
    role: Literal["character", "scene", "style", "object"] = "character"
    name: str = ""
    # For reference_strategy="frame_images": pin this image as the clip's
    # first or last frame (image-to-video / keyframe interpolation).
    frame_type: Optional[Literal["first_frame", "last_frame"]] = None
    # A picture on the card of a Cast character marked "adult content
    # allowed" (fictional, or the person consented), however it was made:
    # in adult mode it skips the forced content filter
    # (services/content_mode.exempt). Minors are refused regardless.
    adult_ok: bool = False


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
    # Provider-reported cost in USD once the job is done (OpenRouter
    # `usage.cost`); None when the provider doesn't say.
    cost: Optional[float] = None


@dataclass
class ModelInfo:
    id: str
    name: str
    modality: Literal["chat", "image", "video"]
    description: str = ""


@dataclass
class VideoCapabilities:
    """What a video model accepts in a single generation call.

    `durations` is the explicit list of accepted clip lengths in seconds
    (providers 400 on anything else). `first_frame` / `last_frame` say
    whether the model can be pinned to start and/or end on a given image —
    the last one is what lets an ambient loop close on itself natively.
    """

    durations: list[int] = field(default_factory=lambda: [5, 10])
    first_frame: bool = False
    last_frame: bool = False
    known: bool = True  # False = fell back to defaults (model not listed)
    # Models that can't work from text alone: a first frame is required
    # (picture-only), first AND last frame are required (so open clips
    # can't be chained), or a loose reference picture is required.
    needs_first_frame: bool = False
    both_frames_only: bool = False
    needs_reference: bool = False

    @property
    def max_seconds(self) -> int:
        return max(self.durations) if self.durations else 5

    def snap(self, seconds: float) -> int:
        """Smallest supported duration >= seconds, else the maximum."""
        ok = sorted(d for d in self.durations if d >= seconds - 1e-6)
        return ok[0] if ok else self.max_seconds


def expand_durations(raw) -> list[int]:
    """Normalize a provider's supported_durations: either an explicit list
    ([4, 6, 8]) or a {min,max}-style range, into a sorted int list."""
    if raw is None:
        return []
    if isinstance(raw, dict):
        lo = int(raw.get("min") or raw.get("minimum") or 1)
        hi = int(raw.get("max") or raw.get("maximum") or lo)
        return list(range(lo, hi + 1))
    out = sorted({int(round(float(x))) for x in raw if x is not None})
    return out


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

    def rewrite_scene(self, text: str) -> dict:
        """A typed scene description as a real photograph:
        {"scene": str, "light": str, "camera": str}."""
        ...

    def enhance_prompt(self, text: str, kind: str, model: str = "") -> str:
        """An improved prompt for `kind` ("image" | "video" | "script" |
        "scene"), keeping the user's subject and intent. `model` is the
        target generation model, used for a model-family hint. Returns the
        improved text only."""
        ...

    def inspect_images(self, *, system: str, text: str, images: list[str],
                       note: str = "clip check", estimate: float = 0.0) -> str:
        """One call to a vision-capable model: `text` plus `images` (data
        URLs, in order) under `system`. Returns the reply text as is (the
        caller parses it). Spend is recorded with `note`; `estimate` is the
        rate-card cost when the provider reports none."""
        ...

    def classify_minor(self, text: str) -> dict:
        """The safety check (services/minor_check.py): does `text` put a
        minor, or someone made to look like one, in a sexual context?
        Returns {"minor_sexual": bool, "reason": str}; raises when no
        verdict came back (refusal, error, unreadable reply)."""
        ...


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


# Picture operations (Studio "Remove background" / "Upscale").
ImageOp = Literal["remove_background", "upscale"]


class ImageOpProvider(Protocol):
    def run(self, op: ImageOp, model: str, image_bytes: bytes, mime: str, options: dict) -> bytes:
        """One operation on one picture (`mime` is its type, e.g.
        "image/png"); `options` carries e.g. {"factor": 2} for an upscale.
        Returns the result's bytes (PNG when transparency must survive)."""
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

    def capabilities(self, model: str) -> VideoCapabilities:
        """Per-model duration list + first/last-frame support."""
        ...


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



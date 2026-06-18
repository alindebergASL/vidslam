"""Mock lip-sync provider. Returns the source video unchanged with a tag so
tests can see the lip-sync pass ran without needing a real model.

The slot exists so a future real adapter (Wav2Lip / SadTalker / D-ID / HeyGen)
drops in by implementing LipSyncProvider — no schema or API migration."""
from __future__ import annotations

from .base import LipSyncJob, ModelInfo


class MockLipSyncProvider:
    """No-op adapter that's deterministic in mock mode. Returns the input
    video bytes as-is; downstream just gets a clip whose lips do *not*
    track the audio, but the file exists and is playable."""

    def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(
                id="mock/lipsync-passthrough",
                name="Mock Lip-Sync (passthrough)",
                modality="video",
                description="Returns the source video unchanged. Stand-in until a real provider drops in.",
            )
        ]

    def render(
        self,
        *,
        model: str,  # noqa: ARG002
        source_video: bytes,
        target_audio: bytes,  # noqa: ARG002
        settings: dict | None = None,  # noqa: ARG002
    ) -> LipSyncJob:
        return LipSyncJob(video_bytes=source_video, duration_seconds=0.0)

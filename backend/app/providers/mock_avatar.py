"""Mock talking-head provider: a placeholder clip labelled with the script,
registered as a finished job on the mock video provider so it completes
through the same video-job path as real Avatar IV jobs."""
from __future__ import annotations

from .base import SubmittedJob
from .mock_video import _generate_clip, get_singleton


class MockAvatarProvider:
    def __init__(self, *, model: str = "mock/avatar") -> None:
        self.model = model

    def submit_avatar(self, *, portrait_url: str, audio_url: str | None, script: str,
                      aspect_ratio: str = "9:16", voice_id: str = "", motion_prompt: str = "",
                      duration_seconds: float = 5.0, callback_url: str | None = None,
                      **_: object) -> SubmittedJob:
        data = self.render(script=script, duration_seconds=duration_seconds)
        job = get_singleton().put(data)
        job.model = self.model
        return job

    def render(self, *, script: str, duration_seconds: float) -> bytes:
        return _generate_clip(f"TALKING HEAD · {script[:60]}", max(2.0, duration_seconds), 1)

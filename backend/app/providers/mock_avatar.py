"""Mock talking-head provider: a placeholder clip labelled with the script,
so the lip-sync path runs end-to-end in MOCK_PROVIDERS mode."""
from __future__ import annotations

from .mock_video import _generate_clip


class MockAvatarProvider:
    def __init__(self, *, model: str = "mock/avatar") -> None:
        self.model = model
        self.calls: list[dict] = []

    def animate(self, *, portrait_url: str, audio_url: str | None, script: str,
                aspect_ratio: str = "9:16", voice_id: str = "", motion_prompt: str = "",
                duration_seconds: float = 5.0, **_: object) -> bytes:
        self.calls.append({"portrait_url": portrait_url, "audio_url": audio_url, "script": script})
        return _generate_clip(f"TALKING HEAD · {script[:60]}", max(2.0, duration_seconds), 1)

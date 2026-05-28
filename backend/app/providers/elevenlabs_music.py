from __future__ import annotations

import httpx

from ..config import get_settings
from .base import ModelInfo, MusicProvider


class ElevenLabsMusicProvider(MusicProvider):
    """Adapter for ElevenLabs' text-to-music endpoint.

    The endpoint takes a natural-language `prompt` and a target `music_length_ms`
    and returns audio bytes (mp3). If the endpoint contract changes, only the
    request body shape and the parsing here need to be updated — nothing else in
    the app depends on provider specifics.
    """

    def __init__(self, *, api_key: str | None = None) -> None:
        s = get_settings()
        self.api_key = api_key or s.elevenlabs_api_key
        if not self.api_key:
            raise RuntimeError("ELEVENLABS_API_KEY missing for real music provider")

    def _headers(self) -> dict[str, str]:
        return {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }

    def output_extension(self) -> str:
        return "mp3"

    def list_models(self) -> list[ModelInfo]:
        s = get_settings()
        if not s.elevenlabs_music_model_id:
            return [ModelInfo(id="default", name="ElevenLabs Music", modality="image")]
        return [
            ModelInfo(
                id=s.elevenlabs_music_model_id,
                name=s.elevenlabs_music_model_id,
                modality="image",
            )
        ]

    def generate(
        self,
        *,
        prompt: str,
        duration_seconds: float,
        settings: dict | None = None,
    ) -> bytes:
        s = get_settings()
        url = f"{s.elevenlabs_base_url.rstrip('/')}/music"
        body: dict = {
            "prompt": prompt,
            "music_length_ms": int(max(5.0, duration_seconds) * 1000),
        }
        if s.elevenlabs_music_model_id:
            body["model_id"] = s.elevenlabs_music_model_id
        if settings:
            body.update(settings)
        with httpx.Client(timeout=180.0) as client:
            r = client.post(url, headers=self._headers(), json=body)
            r.raise_for_status()
            return r.content

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

    def __init__(self, *, api_key: str | None = None, model_id: str | None = None) -> None:
        s = get_settings()
        self.api_key = api_key or s.elevenlabs_api_key
        self.model_id = model_id or s.elevenlabs_music_model_id or "music_v2_5"
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
        # elevenlabs.io/docs/api-reference/music/compose: prompt +
        # music_length_ms (3 s–5 min per the FAQ; the schema allows 10 min),
        # force_instrumental (prompt mode only). model_id is sent
        # explicitly: the API default, music_v1, is deprecated and will be
        # removed on 2027-01-31.
        length_ms = int(min(300.0, max(3.0, duration_seconds)) * 1000)
        body: dict = {
            "prompt": prompt,
            "music_length_ms": length_ms,
            "model_id": self.model_id,
            "force_instrumental": True,  # beds sit under voice / ambient scenes
        }
        if settings:
            body.update(settings)
        with httpx.Client(timeout=180.0) as client:
            r = client.post(url, headers=self._headers(), json=body)
            r.raise_for_status()
            return r.content

from __future__ import annotations

import httpx

from ..config import get_settings
from .base import TTSProvider

settings = get_settings()


class ElevenLabsTTSProvider(TTSProvider):
    def __init__(self, *, api_key: str | None = None) -> None:
        self.api_key = api_key or settings.elevenlabs_api_key
        if not self.api_key:
            raise RuntimeError("ELEVENLABS_API_KEY missing for real tts provider")

    def _headers(self, *, accept_audio: bool = False) -> dict[str, str]:
        h = {"xi-api-key": self.api_key, "Content-Type": "application/json"}
        if accept_audio:
            h["Accept"] = "audio/mpeg"
        return h

    def list_voices(self) -> list[dict]:
        url = f"{settings.elevenlabs_base_url.rstrip('/')}/voices"
        with httpx.Client(timeout=30.0) as client:
            r = client.get(url, headers=self._headers())
            r.raise_for_status()
            data = r.json()
        return [
            {"voice_id": v.get("voice_id"), "name": v.get("name")}
            for v in data.get("voices", [])
        ]

    def synthesize(
        self,
        *,
        text: str,
        voice_id: str,
        settings: dict | None = None,
    ) -> bytes:
        vid = voice_id or get_settings().elevenlabs_default_voice_id
        if not vid:
            raise RuntimeError("No voice_id given and ELEVENLABS_DEFAULT_VOICE_ID not set")
        url = f"{get_settings().elevenlabs_base_url.rstrip('/')}/text-to-speech/{vid}"
        body = {
            "text": text,
            "model_id": get_settings().elevenlabs_model_id,
            "voice_settings": (settings or {}).get(
                "voice_settings", {"stability": 0.5, "similarity_boost": 0.7}
            ),
        }
        with httpx.Client(timeout=180.0) as client:
            r = client.post(url, headers=self._headers(accept_audio=True), json=body)
            r.raise_for_status()
            return r.content

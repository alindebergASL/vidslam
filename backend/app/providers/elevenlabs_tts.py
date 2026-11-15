from __future__ import annotations

import httpx

from ..config import get_settings
from ..services import provider_keys
from .base import TTSProvider

# Models that read audio tags ([whispers], [short pause]...) in place.
# Any other model would speak them, so they're taken out first.
TAG_MODELS = ("eleven_v3", "eleven_v4", "eleven_v4_turbo")


def _accept(output_format: str) -> str:
    fmt = (output_format or "").lower()
    if fmt.startswith("pcm"):
        return "audio/pcm"
    if fmt.startswith(("ulaw", "alaw")):
        return "audio/basic"
    if fmt.startswith("opus"):
        return "audio/opus"
    return "audio/mpeg"


class ElevenLabsTTSProvider(TTSProvider):
    def __init__(self, *, api_key: str | None = None, model_id: str | None = None) -> None:
        self.api_key = api_key or provider_keys.resolve("elevenlabs")
        if not self.api_key:
            raise RuntimeError("ELEVENLABS_API_KEY missing for real tts provider")
        # None: the configured default (ELEVENLABS_MODEL_ID), read per call.
        self.model_id = model_id

    def _headers(self, *, accept_audio: bool = False, accept: str = "audio/mpeg") -> dict[str, str]:
        h = {"xi-api-key": self.api_key, "Content-Type": "application/json"}
        if accept_audio:
            h["Accept"] = accept
        return h

    def list_voices(self) -> list[dict]:
        url = f"{get_settings().elevenlabs_base_url.rstrip('/')}/voices"
        with httpx.Client(timeout=30.0) as client:
            r = client.get(url, headers=self._headers())
            r.raise_for_status()
            data = r.json()
        return [
            # preview_url: ElevenLabs' own short sample (free to play).
            {"voice_id": v.get("voice_id"), "name": v.get("name"),
             "preview_url": v.get("preview_url") or ""}
            for v in data.get("voices", [])
        ]

    def resolve_voice(self, voice_id: str) -> str:
        """The voice a request for `voice_id` is really read with: the id
        itself, or the default voice (ELEVENLABS_DEFAULT_VOICE_ID) for none."""
        return voice_id or get_settings().elevenlabs_default_voice_id or ""

    def synthesize(
        self,
        *,
        text: str,
        voice_id: str,
        settings: dict | None = None,
    ) -> bytes:
        """Speech for `text`. Per-request `settings` (all optional; none of
        them changes what an existing caller gets): model_id, output_format
        (e.g. "mp3_44100_128", "pcm_48000"; sent as a query parameter), seed
        (an integer, for repeatable takes) and voice_settings."""
        s = get_settings()
        opts = settings or {}
        vid = voice_id or s.elevenlabs_default_voice_id
        if not vid:
            raise RuntimeError("No voice_id given and ELEVENLABS_DEFAULT_VOICE_ID not set")
        model_id = str(opts.get("model_id") or self.model_id or s.elevenlabs_model_id)
        from ..services.story_script import NONVERBAL_TEXT, has_tags, is_story, split_tags

        if is_story(opts) and model_id not in TAG_MODELS:
            # Story markup only (settings story_markup): any other caller's
            # text is sent exactly as given. Only text that carries voice
            # tags is touched ("Bonjour ! Ça va ?" keeps its spaces).
            if has_tags(text):
                text = split_tags(text)[1] or NONVERBAL_TEXT
        url = f"{s.elevenlabs_base_url.rstrip('/')}/text-to-speech/{vid}"
        body: dict = {
            "text": text,
            "model_id": model_id,
            "voice_settings": opts.get(
                "voice_settings", {"stability": 0.5, "similarity_boost": 0.7}
            ),
        }
        if opts.get("seed") is not None:
            body["seed"] = int(opts["seed"]) % 4294967296
        output_format = str(opts.get("output_format") or "")
        params = {"output_format": output_format} if output_format else None
        with httpx.Client(timeout=180.0) as client:
            r = client.post(url, headers=self._headers(accept_audio=True, accept=_accept(output_format)),
                            json=body, params=params)
            r.raise_for_status()
        from ..services import spend

        # ElevenLabs bills plan credits per character; no $ in the response.
        chars = int(r.headers.get("character-cost") or len(text))
        # A story voice: the model's own rate. Any other caller (video
        # voiceover, hook lab): COST_TTS_PER_1K_CHARS, as its estimate says.
        rate = s.tts_rate(model_id) if is_story(opts) else float(s.cost_tts_per_1k_chars)
        spend.record_rate_card("elevenlabs", "tts", model_id,
                               chars / 1000 * rate, note=f"{chars} chars")
        return r.content

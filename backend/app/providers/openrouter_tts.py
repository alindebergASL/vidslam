"""Voiceover through OpenRouter audio models.

Two documented OpenRouter paths (docs/guides/overview/multimodal/tts and
…/audio):
  * Speech models (output modality "speech": gpt-4o-mini-tts, Gemini TTS,
    HeyGen Voice, Voxtral, …) — POST /audio/speech
      {model, input, voice, response_format: "mp3"|"pcm", instructions?}
    → raw audio bytes (Content-Type audio/mpeg or audio/pcm).
  * Chat audio models (output modality "audio": openai/gpt-audio,
    gpt-audio-mini) — POST /chat/completions with
      modalities ["text","audio"], audio {voice, format}, stream: true
    → base64 chunks at choices[0].delta.audio.data.

Raw PCM is 16-bit mono 24 kHz (OpenRouter's cookbook for Gemini; OpenAI
pcm16 is the same) and gets wrapped in a WAV header so ffmpeg can read it.
"""
from __future__ import annotations

import base64
import io
import json
import wave

import httpx

from ..config import get_settings
from .base import TTSProvider

PCM_RATE = 24000


def pcm16_to_wav(pcm: bytes, rate: int = PCM_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


class OpenRouterTTSProvider(TTSProvider):
    def __init__(self, *, model: str, voice: str = "", api_key: str | None = None,
                 chat_audio: bool | None = None, voices: list[str] | None = None) -> None:
        s = get_settings()
        self.api_key = api_key or s.openrouter_api_key
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY missing for OpenRouter TTS")
        self.model = model
        self.voice = voice
        self.voices = voices or []
        # gpt-audio family speaks through chat completions; everything else
        # through the speech endpoint.
        self.chat_audio = ("gpt-audio" in model) if chat_audio is None else chat_audio

    @property
    def _base(self) -> str:
        return get_settings().openrouter_base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        s = get_settings()
        return {
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": s.openrouter_site_url,
            "X-Title": s.openrouter_app_title,
            "Content-Type": "application/json",
        }

    def list_voices(self) -> list[dict]:
        names = self.voices or ["alloy", "echo", "fable", "onyx", "nova", "shimmer"]
        return [{"voice_id": v, "name": v} for v in names]

    def _voice_for(self, voice_id: str) -> str:
        # Per-avatar ElevenLabs ids mean nothing to other providers; only use
        # a voice the model lists (or the configured one).
        if voice_id and (not self.voices or voice_id in self.voices):
            return voice_id
        return self.voice or (self.voices[0] if self.voices else "alloy")

    def synthesize(self, *, text: str, voice_id: str, settings: dict | None = None) -> bytes:
        voice = self._voice_for(voice_id)
        if self.chat_audio:
            return self._chat_audio(text, voice, settings or {})
        return self._speech(text, voice, settings or {})

    def _speech(self, text: str, voice: str, extra: dict) -> bytes:
        body = {"model": self.model, "input": text, "voice": voice, "response_format": "mp3"}
        if extra.get("instructions"):
            body["instructions"] = extra["instructions"]
        with httpx.Client(timeout=180.0) as client:
            r = client.post(f"{self._base}/audio/speech", headers=self._headers(), json=body)
            if r.status_code == 400 and "pcm" in r.text.lower():
                # Some providers (Gemini TTS, HeyGen Voice) only return PCM.
                body["response_format"] = "pcm"
                r = client.post(f"{self._base}/audio/speech", headers=self._headers(), json=body)
            if r.status_code >= 400:
                raise RuntimeError(f"tts {r.status_code}: {r.text[:300]}")
            ctype = r.headers.get("content-type", "")
            data = r.content
        from ..services import spend

        spend.record_openrouter("tts", self.model, cost=None,
                                generation_id=r.headers.get("x-generation-id", ""),
                                estimate=len(text) / 1000 * get_settings().cost_tts_per_1k_chars,
                                note=f"{len(text)} chars")
        if "pcm" in ctype or body["response_format"] == "pcm" and "mpeg" not in ctype:
            return pcm16_to_wav(data)
        return data

    def _chat_audio(self, text: str, voice: str, extra: dict) -> bytes:
        body = {
            "model": self.model,
            "modalities": ["text", "audio"],
            "audio": {"voice": voice, "format": "pcm16"},
            "stream": True,
            "messages": [
                {"role": "system", "content": extra.get("instructions")
                 or "Read the user's text aloud exactly as written, warmly and naturally. Do not add anything."},
                {"role": "user", "content": text},
            ],
        }
        chunks: list[bytes] = []
        usage: dict = {}
        with httpx.Client(timeout=300.0) as client:
            with client.stream("POST", f"{self._base}/chat/completions",
                               headers=self._headers(), json=body) as r:
                if r.status_code >= 400:
                    raise RuntimeError(f"chat audio {r.status_code}: {r.read()[:300]!r}")
                for line in r.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        evt = json.loads(payload)
                    except ValueError:
                        continue
                    if evt.get("usage"):
                        usage = {"usage": evt["usage"], "id": evt.get("id")}
                    usage.setdefault("id", evt.get("id"))
                    for ch in evt.get("choices") or []:
                        audio = (ch.get("delta") or {}).get("audio") or {}
                        if audio.get("data"):
                            chunks.append(base64.b64decode(audio["data"]))
        if not chunks:
            raise RuntimeError("chat audio stream returned no audio")
        from ..services import spend

        spend.record_openrouter("tts", self.model, cost=spend.usage_cost(usage),
                                generation_id=str(usage.get("id") or ""),
                                estimate=len(text) / 1000 * get_settings().cost_tts_per_1k_chars,
                                note=f"{len(text)} chars")
        return pcm16_to_wav(b"".join(chunks))

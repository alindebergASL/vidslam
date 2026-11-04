"""Google Lyria 3 music through OpenRouter (google/lyria-3-pro-preview:
full songs, $0.08/song; google/lyria-3-clip-preview: 30 s clips, $0.04).

EXPERIMENTAL request shape. OpenRouter's model pages document only a
Chat Completions call (messages in, choices out) and don't say where the
audio comes back. The models list them with output modalities
["text", "audio"], so we follow OpenRouter's documented chat-audio
convention (as for gpt-audio): modalities ["text","audio"], stream: true,
audio chunks base64 at choices[].delta.audio.data. We also accept the
non-streamed shapes (message.audio.data, audio content parts) so a
variation in the response doesn't break the bed.

Bytes are sniffed: MP3/WAV/OGG/FLAC pass through; anything else is
treated as raw 16-bit PCM at 48 kHz stereo (Lyria's stated output) and
wrapped in a WAV header.
"""
from __future__ import annotations

import base64
import io
import json
import wave

import httpx

from ..config import get_settings
from .base import ModelInfo, MusicProvider
from .lyria_music import _fmt_len


def sniff_audio(data: bytes) -> tuple[bytes, str]:
    """(bytes, extension). Wrap headerless PCM so ffmpeg can read it."""
    if data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return data, "mp3"
    if data[:4] == b"RIFF":
        return data, "wav"
    if data[:4] == b"OggS":
        return data, "ogg"
    if data[:4] == b"fLaC":
        return data, "flac"
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(data[: len(data) - (len(data) % 4)])
    return buf.getvalue(), "wav"


def _collect(obj: dict, out: list[bytes]) -> None:
    for ch in obj.get("choices") or []:
        for holder in (ch.get("delta") or {}, ch.get("message") or {}):
            audio = holder.get("audio") or {}
            if isinstance(audio, dict) and audio.get("data"):
                out.append(base64.b64decode(audio["data"]))
            content = holder.get("content")
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    a = part.get("audio") or part.get("output_audio") or part.get("input_audio")
                    if isinstance(a, dict) and a.get("data"):
                        out.append(base64.b64decode(a["data"]))
                    elif part.get("type") in ("audio", "output_audio") and part.get("data"):
                        out.append(base64.b64decode(part["data"]))


class OpenRouterMusicProvider(MusicProvider):
    def __init__(self, *, model: str = "google/lyria-3-pro-preview", api_key: str | None = None) -> None:
        s = get_settings()
        self.api_key = api_key or s.openrouter_api_key
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY missing for OpenRouter music")
        self.model = model
        self._ext = "mp3"

    def output_extension(self) -> str:
        return self._ext

    def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(id=self.model, name=self.model, modality="image")]

    def build_prompt(self, prompt: str, duration_seconds: float) -> str:
        text = prompt.strip()
        if "clip" not in self.model:
            text = f"Create a {_fmt_len(duration_seconds)} instrumental track. {text}"
        if "vocal" not in text.lower():
            text += " Instrumental only, no vocals."
        return text

    def generate(self, *, prompt: str, duration_seconds: float, settings: dict | None = None) -> bytes:
        s = get_settings()
        body = {
            "model": self.model,
            "modalities": ["text", "audio"],
            "audio": {"format": "mp3"},
            "stream": True,
            "messages": [{"role": "user", "content": self.build_prompt(prompt, duration_seconds)}],
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": s.openrouter_site_url,
            "X-Title": s.openrouter_app_title,
            "Content-Type": "application/json",
        }
        chunks: list[bytes] = []
        url = f"{s.openrouter_base_url.rstrip('/')}/chat/completions"
        with httpx.Client(timeout=600.0) as client:
            with client.stream("POST", url, headers=headers, json=body) as r:
                if r.status_code >= 400:
                    raise RuntimeError(f"lyria via openrouter {r.status_code}: {r.read()[:300]!r}")
                ctype = r.headers.get("content-type", "")
                if "event-stream" in ctype:
                    for line in r.iter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            _collect(json.loads(payload), chunks)
                        except ValueError:
                            continue
                else:  # provider answered non-streamed
                    _collect(json.loads(r.read() or b"{}"), chunks)
        if not chunks:
            raise RuntimeError(
                "Lyria via OpenRouter returned no audio (response shape undocumented) — "
                "try the Google route (GEMINI_API_KEY) or ElevenLabs"
            )
        data, self._ext = sniff_audio(b"".join(chunks))
        return data

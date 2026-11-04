"""Google Lyria music via the Gemini API (aistudio.google.com/docs/music-generation).

  POST {GEMINI_BASE}/interactions
  headers: x-goog-api-key, Api-Revision: 2026-05-20
  body:    {"model": "lyria-3.5", "input": "<prompt>"}
  → Interaction; audio is base64 at
    steps[type=model_output].content[type=audio].data  (MP3, 44.1 kHz stereo)

Synchronous. Lyria 3.5 / 3 Pro make tracks of "a couple of minutes", with
length steered by the prompt; lyria-3-clip-preview always makes 30 s.
There's no negative-prompt or seed field, so "no vocals" goes in the text.
All output carries a SynthID watermark.
"""
from __future__ import annotations

import base64

import httpx

from ..config import get_settings
from .base import ModelInfo, MusicProvider


def _fmt_len(seconds: float) -> str:
    s = int(round(seconds))
    m, r = divmod(s, 60)
    if m and r:
        return f"{m} minute{'s' if m > 1 else ''} {r} seconds"
    if m:
        return f"{m} minute{'s' if m > 1 else ''}"
    return f"{r} seconds"


class LyriaMusicProvider(MusicProvider):
    def __init__(self, *, model: str = "google/lyria-3.5", api_key: str | None = None) -> None:
        s = get_settings()
        self.api_key = api_key or s.gemini_api_key
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY missing for Lyria music")
        self.model = model.removeprefix("google/")

    def output_extension(self) -> str:
        return "mp3"

    def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(id=f"google/{self.model}", name=f"Lyria ({self.model})", modality="image")]

    def build_prompt(self, prompt: str, duration_seconds: float) -> str:
        text = prompt.strip()
        if "clip" not in self.model:
            text = f"Create a {_fmt_len(duration_seconds)} instrumental track. {text}"
        if "vocal" not in text.lower():
            text += " Instrumental only, no vocals."
        return text

    def generate(self, *, prompt: str, duration_seconds: float, settings: dict | None = None) -> bytes:
        s = get_settings()
        body = {"model": self.model, "input": self.build_prompt(prompt, duration_seconds)}
        headers = {
            "Content-Type": "application/json",
            "Api-Revision": s.gemini_api_revision,
            "x-goog-api-key": self.api_key,
        }
        with httpx.Client(timeout=300.0) as client:
            r = client.post(f"{s.gemini_base_url.rstrip('/')}/interactions", headers=headers, json=body)
            if r.status_code >= 400:
                raise RuntimeError(f"lyria {r.status_code}: {r.text[:300]}")
            data = r.json()
        for step in data.get("steps") or []:
            if step.get("type") != "model_output":
                continue
            for item in step.get("content") or []:
                if item.get("type") == "audio" and item.get("data"):
                    return base64.b64decode(item["data"])
        raise RuntimeError("lyria response contained no audio")

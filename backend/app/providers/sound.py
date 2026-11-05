"""Nature-sound beds (rain, waves, fire…) for ambient scenes.

ElevenLabs text-to-sound-effects: POST /v1/sound-generation with `text`,
`duration_seconds` (0.5–30), `loop: true` for a seamless loop, and an
explicit `model_id` (the API default, eleven_text_to_sound_v2, is
deprecated). The renderer still crossfades the seam, so an imperfect loop
doesn't click.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx

from ..config import get_settings

MAX_SECONDS = 30.0


class ElevenLabsSoundProvider:
    name = "elevenlabs"

    def __init__(self, *, api_key: str | None = None, model_id: str | None = None) -> None:
        s = get_settings()
        self.api_key = api_key or s.elevenlabs_api_key
        self.model_id = model_id or s.elevenlabs_sound_model_id
        if not self.api_key:
            raise RuntimeError("ELEVENLABS_API_KEY missing for real sound provider")

    def generate(self, *, prompt: str, duration_seconds: float = MAX_SECONDS) -> bytes:
        s = get_settings()
        secs = max(0.5, min(MAX_SECONDS, float(duration_seconds)))
        body = {"text": prompt, "duration_seconds": secs, "loop": True,
                "prompt_influence": 0.5, "model_id": self.model_id}
        with httpx.Client(timeout=180.0) as client:
            r = client.post(f"{s.elevenlabs_base_url.rstrip('/')}/sound-generation",
                            params={"output_format": "mp3_44100_128"},
                            headers={"xi-api-key": self.api_key, "Content-Type": "application/json",
                                     "Accept": "audio/mpeg"}, json=body)
            if r.status_code >= 400:
                raise RuntimeError(f"sound generation {r.status_code}: {r.text[:300]}")
        from ..services import spend

        spend.record_rate_card("elevenlabs", "sound", self.model_id, s.cost_sound_per_generation,
                               note=f"{secs:.0f}s loop")
        return r.content


class MockSoundProvider:
    """Filtered noise as a stand-in (brown noise reads as rain/surf), so demo
    mode renders the full audio path without an API call."""

    name = "mock"
    model_id = "mock/sound"

    def generate(self, *, prompt: str, duration_seconds: float = MAX_SECONDS) -> bytes:
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg required for the mock sound provider")
        secs = max(0.5, min(MAX_SECONDS, float(duration_seconds)))
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "sound.mp3"
            proc = subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi", "-i", f"anoisesrc=color=brown:amplitude=0.25:d={secs}",
                 "-af", "lowpass=f=1800,afade=t=in:d=0.5", "-ac", "2", "-ar", "44100",
                 "-c:a", "libmp3lame", "-b:a", "128k", str(out)],
                capture_output=True, text=True,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"mock sound ffmpeg failed: {proc.stderr[-500:]}")
            return out.read_bytes()

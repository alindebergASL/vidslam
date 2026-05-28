from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from .base import TTSProvider


def _estimate_seconds(text: str) -> float:
    words = len([w for w in text.split() if w])
    # ~150 wpm = 2.5 wps
    return max(2.0, words / 2.5)


class MockTTSProvider(TTSProvider):
    def list_voices(self) -> list[dict]:
        return [{"voice_id": "mock-voice", "name": "Mock Voice"}]

    def synthesize(
        self,
        *,
        text: str,
        voice_id: str,
        settings: dict | None = None,
    ) -> bytes:
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg required for mock tts provider")
        seconds = (settings or {}).get("duration") or _estimate_seconds(text)
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "voice.m4a"
            cmd = [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-t", str(seconds),
                "-c:a", "aac", "-b:a", "128k",
                str(out),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                raise RuntimeError(f"mock tts ffmpeg failed: {proc.stderr[-1000:]}")
            return out.read_bytes()

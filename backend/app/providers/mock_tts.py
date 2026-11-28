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
        return [{"voice_id": "mock-voice", "name": "Mock Voice", "preview_url": ""}]

    def resolve_voice(self, voice_id: str) -> str:
        """The mock reads every request with the voice asked for."""
        return voice_id or ""

    def synthesize(
        self,
        *,
        text: str,
        voice_id: str,
        settings: dict | None = None,
    ) -> bytes:
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg required for mock tts provider")
        from ..services.story_script import is_story, split_tags

        # In story markup, voice tags ([whispers], [short pause]...) aren't words.
        story = is_story(settings)
        words = split_tags(text)[1] if story else text
        seconds = (settings or {}).get("duration") or _estimate_seconds(words)
        # A story line is a quiet tone, not silence: the clip pipeline
        # (services/sound_clips.py) trims silence and rejects silent clips.
        source = "sine=frequency=220:sample_rate=44100" if story else "anullsrc=r=44100:cl=stereo"
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "voice.m4a"
            cmd = [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", source,
                "-t", str(seconds),
                "-c:a", "aac", "-b:a", "128k",
                str(out),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                raise RuntimeError(f"mock tts ffmpeg failed: {proc.stderr[-1000:]}")
            return out.read_bytes()

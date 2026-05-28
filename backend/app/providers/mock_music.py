from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from .base import ModelInfo, MusicProvider

# Twelve-tone equal temperament from A2 (110 Hz) so prompts hash to a key.
_BASE_FREQUENCIES = [110.0 * (2 ** (i / 12)) for i in range(12)]


def _triad_for_prompt(prompt: str) -> tuple[float, float, float]:
    """Map the prompt to a deterministic major triad root + 3rd + 5th."""
    root_idx = abs(hash(prompt)) % len(_BASE_FREQUENCIES)
    third_idx = (root_idx + 4) % len(_BASE_FREQUENCIES)
    fifth_idx = (root_idx + 7) % len(_BASE_FREQUENCIES)
    # Push up an octave so it sits in a music-bed range, not bassy.
    return (
        _BASE_FREQUENCIES[root_idx] * 2,
        _BASE_FREQUENCIES[third_idx] * 2,
        _BASE_FREQUENCIES[fifth_idx] * 2,
    )


class MockMusicProvider(MusicProvider):
    """Deterministic, audible music bed built from a major triad via FFmpeg lavfi.

    Used in MOCK_PROVIDERS mode and tests; renders a soft three-tone pad with a
    gentle tremolo so the user can hear that music actually got mixed in.
    """

    def output_extension(self) -> str:
        return "mp3"

    def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(
                id="mock/music-pad",
                name="Mock Music (lavfi triad)",
                modality="image",  # base class doesn't have a 'music' modality enum
                description="Three-tone major-triad pad with tremolo, deterministic by prompt.",
            ),
        ]

    def generate(
        self,
        *,
        prompt: str,
        duration_seconds: float,
        settings: dict | None = None,
    ) -> bytes:
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg required for mock music provider")
        dur = max(2.0, float(duration_seconds))
        f1, f2, f3 = _triad_for_prompt(prompt or "untitled")
        # Three sines summed at descending amplitude; tremolo across the mix.
        filt = (
            f"sine=frequency={f1:.2f}:duration={dur}[a];"
            f"sine=frequency={f2:.2f}:duration={dur}[b];"
            f"sine=frequency={f3:.2f}:duration={dur}[c];"
            f"[a][b][c]amix=inputs=3:duration=longest:weights='1 0.7 0.5',"
            f"tremolo=f=4:d=0.25,"
            f"volume=0.6[out]"
        )
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "music.mp3"
            cmd = [
                "ffmpeg", "-y",
                "-filter_complex", filt,
                "-map", "[out]",
                "-t", str(dur),
                "-c:a", "libmp3lame", "-q:a", "4",
                str(out),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                # tremolo may be unavailable on minimal builds; retry without it.
                filt_simple = (
                    f"sine=frequency={f1:.2f}:duration={dur}[a];"
                    f"sine=frequency={f2:.2f}:duration={dur}[b];"
                    f"sine=frequency={f3:.2f}:duration={dur}[c];"
                    f"[a][b][c]amix=inputs=3:duration=longest:weights='1 0.7 0.5',"
                    f"volume=0.6[out]"
                )
                cmd_simple = [
                    "ffmpeg", "-y",
                    "-filter_complex", filt_simple,
                    "-map", "[out]",
                    "-t", str(dur),
                    "-c:a", "libmp3lame", "-q:a", "4",
                    str(out),
                ]
                proc2 = subprocess.run(cmd_simple, capture_output=True, text=True)
                if proc2.returncode != 0:
                    raise RuntimeError(
                        f"mock music ffmpeg failed: {(proc.stderr + proc2.stderr)[-1000:]}"
                    )
            return out.read_bytes()

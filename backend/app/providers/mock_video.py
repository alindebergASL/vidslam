from __future__ import annotations

import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from .base import ImageRef, JobStatus, ModelInfo, SubmittedJob, VideoProvider


def _generate_clip(prompt: str, duration: float, refs_count: int) -> bytes:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg required for mock video provider")
    h = abs(hash(prompt)) & 0xFFFFFF
    r, g, b = (h >> 16) & 0xFF, (h >> 8) & 0xFF, h & 0xFF
    color = f"0x{r:02X}{g:02X}{b:02X}"
    safe = (
        prompt[:120]
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "’")
    )
    overlay = f"MOCK CLIP\\nrefs: {refs_count}\\n{safe}"
    vf = (
        f"color=c={color}:s=720x1280:d={duration},"
        f"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
        f"text='{overlay}':fontcolor=white:fontsize=42:"
        f"x=(w-text_w)/2:y=h-th-160:line_spacing=10:box=1:boxcolor=black@0.45:boxborderw=20"
    )
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / f"{uuid.uuid4().hex}.mp4"
        cmd = [
            "ffmpeg", "-y", "-f", "lavfi", "-i", vf,
            "-t", str(duration),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
            "-r", "30",
            str(out),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"mock video ffmpeg failed: {proc.stderr[-1000:]}")
        return out.read_bytes()


class MockVideoProvider(VideoProvider):
    """In-process synchronous mock: 'submit' renders immediately, poll returns succeeded."""

    def __init__(self) -> None:
        # job_id -> bytes
        self._cache: dict[str, bytes] = {}

    def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(
                id="mock/video-default",
                name="Mock Video (FFmpeg lavfi)",
                modality="video",
                description="Deterministic placeholder used in MOCK_PROVIDERS mode.",
            ),
        ]

    def submit(
        self,
        *,
        model: str,
        prompt: str,
        references: list[ImageRef],
        negative_prompt: str = "",
        reference_strategy: str = "input_references",
        duration_seconds: float = 5.0,
        settings: dict | None = None,
    ) -> SubmittedJob:
        job_id = uuid.uuid4().hex
        data = _generate_clip(prompt, max(2.0, duration_seconds), len(references))
        self._cache[job_id] = data
        return SubmittedJob(provider="mock", model=model or "mock/video-default", job_id=job_id)

    def poll(self, job: SubmittedJob) -> JobStatus:
        if job.job_id in self._cache:
            return JobStatus(state="succeeded", progress=1.0)
        return JobStatus(state="failed", error="unknown job id")

    def download(self, job: SubmittedJob) -> bytes:
        return self._cache.pop(job.job_id, b"") or b""


# Process-wide singleton so submit→poll→download work across calls.
_singleton: MockVideoProvider | None = None


def get_singleton() -> MockVideoProvider:
    global _singleton
    if _singleton is None:
        _singleton = MockVideoProvider()
    return _singleton

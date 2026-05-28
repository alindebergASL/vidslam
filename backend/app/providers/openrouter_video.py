from __future__ import annotations

import httpx

from ..config import get_settings
from .base import ImageRef, JobStatus, ModelInfo, SubmittedJob, VideoProvider

settings = get_settings()


class OpenRouterVideoProvider(VideoProvider):
    """Adapter for OpenRouter's asynchronous video generation endpoint.

    Job lifecycle:
      POST /videos        -> {id, status_url} (status_url may be derived)
      GET  /videos/{id}   -> {status, progress, video?: {url}}
      Final video is downloaded from the result URL.
    """

    def __init__(self, *, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or settings.openrouter_api_key
        self.model = model or settings.openrouter_video_model
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY missing for real video provider")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": settings.openrouter_site_url,
            "X-Title": settings.openrouter_app_title,
            "Content-Type": "application/json",
        }

    def list_models(self) -> list[ModelInfo]:
        url = f"{settings.openrouter_base_url.rstrip('/')}/models"
        with httpx.Client(timeout=30.0) as client:
            r = client.get(url, headers=self._headers())
            r.raise_for_status()
            data = r.json()
        out: list[ModelInfo] = []
        for m in data.get("data", []):
            modality = (m.get("architecture", {}) or {}).get("modality", "")
            if "video" in (modality or "").lower():
                out.append(
                    ModelInfo(
                        id=m.get("id", ""),
                        name=m.get("name", m.get("id", "")),
                        modality="video",
                        description=m.get("description", "") or "",
                    )
                )
        return out

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
        url = f"{get_settings().openrouter_base_url.rstrip('/')}/videos"
        body: dict = {
            "model": model or self.model,
            "prompt": prompt,
            "duration_seconds": duration_seconds,
        }
        if negative_prompt:
            body["negative_prompt"] = negative_prompt
        if references:
            ref_payload = [{"url": r.url, "role": r.role} for r in references]
            if reference_strategy == "frame_images":
                body["frame_images"] = ref_payload
            else:
                body["input_references"] = ref_payload
        if settings:
            body.update(settings)
        with httpx.Client(timeout=120.0) as client:
            r = client.post(url, headers=self._headers(), json=body)
            r.raise_for_status()
            data = r.json()
        job_id = data.get("id") or data.get("job_id") or ""
        polling_url = data.get("status_url") or (
            f"{get_settings().openrouter_base_url.rstrip('/')}/videos/{job_id}"
        )
        return SubmittedJob(
            provider="openrouter",
            model=body["model"],
            job_id=str(job_id),
            polling_url=polling_url,
            raw=data,
        )

    def poll(self, job: SubmittedJob) -> JobStatus:
        url = job.polling_url or (
            f"{get_settings().openrouter_base_url.rstrip('/')}/videos/{job.job_id}"
        )
        with httpx.Client(timeout=30.0) as client:
            r = client.get(url, headers=self._headers())
            r.raise_for_status()
            data = r.json()
        state = (data.get("status") or "").lower()
        progress = float(data.get("progress") or 0.0)
        if state in ("succeeded", "completed", "success"):
            result = data.get("video", {}).get("url") or data.get("result_url")
            return JobStatus(state="succeeded", progress=1.0, result_url=result)
        if state in ("failed", "error"):
            return JobStatus(state="failed", error=data.get("error") or "provider error")
        if state in ("running", "processing", "in_progress"):
            return JobStatus(state="running", progress=progress)
        return JobStatus(state="queued", progress=progress)

    def download(self, job: SubmittedJob) -> bytes:
        st = self.poll(job)
        if st.state != "succeeded" or not st.result_url:
            raise RuntimeError(f"job not ready or missing result url: {st}")
        with httpx.Client(timeout=300.0) as client:
            r = client.get(st.result_url)
            r.raise_for_status()
            return r.content

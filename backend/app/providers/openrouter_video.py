from __future__ import annotations

import logging
import time

import httpx

from ..config import get_settings
from .base import (
    ImageRef,
    JobStatus,
    ModelInfo,
    SubmittedJob,
    VideoCapabilities,
    VideoProvider,
    expand_durations,
)

log = logging.getLogger("avs.openrouter.video")

# /videos/models is small and changes rarely; cache it per process so the
# cost preview (called on every keystroke in the ambient builder) doesn't
# hit OpenRouter each time.
_MODELS_TTL_SECONDS = 3600
_models_cache: tuple[float, list[dict]] | None = None


def _reset_models_cache() -> None:
    global _models_cache
    _models_cache = None


class OpenRouterVideoProvider(VideoProvider):
    """Adapter for OpenRouter's asynchronous video generation API.

    Contract (https://openrouter.ai/docs/guides/overview/multimodal/video-generation):
      POST /videos           {model, prompt, duration, aspect_ratio, resolution?,
                              frame_images[]?, input_references[]?, provider?}
                             -> 202 {id, polling_url, status}
      GET  /videos/{id}      -> {status: pending|in_progress|completed|failed|
                                 cancelled|expired, unsigned_urls[], error?}
      GET  /videos/{id}/content?index=0   (needs the API key)
      GET  /videos/models    -> {data: [{id, supported_durations,
                                 supported_frame_images, ...}]}
    """

    def __init__(self, *, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or get_settings().openrouter_api_key
        self.model = model or get_settings().openrouter_video_model
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY missing for real video provider")

    @property
    def _base(self) -> str:
        return get_settings().openrouter_base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": get_settings().openrouter_site_url,
            "X-Title": get_settings().openrouter_app_title,
            "Content-Type": "application/json",
        }

    # ---- model discovery ----

    def _video_models(self) -> list[dict]:
        global _models_cache
        now = time.monotonic()
        if _models_cache and now - _models_cache[0] < _MODELS_TTL_SECONDS:
            return _models_cache[1]
        with httpx.Client(timeout=30.0) as client:
            r = client.get(f"{self._base}/videos/models", headers=self._headers())
            r.raise_for_status()
            rows = r.json().get("data", []) or []
        _models_cache = (now, rows)
        return rows

    def list_models(self) -> list[ModelInfo]:
        out: list[ModelInfo] = []
        for m in self._video_models():
            durations = expand_durations(m.get("supported_durations"))
            if not durations:
                continue  # edit/upscale endpoints, not text/image-to-video
            frames = m.get("supported_frame_images") or []
            tag = f"up to {max(durations)}s"
            if "last_frame" in frames:
                tag += " · first+last frame"
            elif "first_frame" in frames:
                tag += " · first frame"
            out.append(
                ModelInfo(
                    id=m.get("id", ""),
                    name=f"{m.get('name') or m.get('id', '')} ({tag})",
                    modality="video",
                    description=m.get("description", "") or "",
                )
            )
        return out

    def capabilities(self, model: str) -> VideoCapabilities:
        fallback = VideoCapabilities(
            durations=sorted({5, max(5, get_settings().ambient_default_max_clip_seconds)}),
            first_frame=True,
            last_frame=False,
            known=False,
        )
        try:
            rows = self._video_models()
        except Exception as e:  # noqa: BLE001 — never block a render on discovery
            log.warning("video model discovery failed: %s", e)
            return fallback
        model = model or self.model
        for m in rows:
            if m.get("id") == model or m.get("canonical_slug") == model:
                durations = expand_durations(m.get("supported_durations")) or fallback.durations
                frames = m.get("supported_frame_images") or []
                return VideoCapabilities(
                    durations=durations,
                    first_frame="first_frame" in frames,
                    last_frame="last_frame" in frames,
                )
        return fallback

    # ---- generation ----

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
        model = model or self.model
        caps = self.capabilities(model)
        # Providers 400 on any duration outside supported_durations; snap
        # up to the nearest accepted length (or the model's max).
        body: dict = {
            "model": model,
            "prompt": prompt,
            "duration": caps.snap(duration_seconds),
        }
        if negative_prompt:
            # No portable top-level negative-prompt field; fold it into the
            # prompt so every routed provider sees it.
            body["prompt"] = f"{prompt}\nAvoid: {negative_prompt}"
        if references:
            if reference_strategy == "frame_images":
                frames = []
                for i, r in enumerate(references):
                    ft = r.frame_type or ("first_frame" if i == 0 else "last_frame")
                    if ft == "last_frame" and not caps.last_frame:
                        continue
                    frames.append(
                        {"type": "image_url", "image_url": {"url": r.url}, "frame_type": ft}
                    )
                if frames:
                    body["frame_images"] = frames
            else:
                body["input_references"] = [
                    {"type": "image_url", "image_url": {"url": r.url}} for r in references
                ]
        if settings:
            body.update({k: v for k, v in settings.items() if v not in (None, "")})
        with httpx.Client(timeout=120.0) as client:
            r = client.post(f"{self._base}/videos", headers=self._headers(), json=body)
            if r.status_code >= 400:
                raise RuntimeError(f"video submit {r.status_code}: {r.text[:500]}")
            data = r.json()
        job_id = str(data.get("id") or "")
        polling_url = data.get("polling_url") or f"{self._base}/videos/{job_id}"
        return SubmittedJob(
            provider="openrouter",
            model=model,
            job_id=job_id,
            polling_url=polling_url,
            raw={"duration": body["duration"]},
        )

    def poll(self, job: SubmittedJob) -> JobStatus:
        url = job.polling_url or f"{self._base}/videos/{job.job_id}"
        with httpx.Client(timeout=30.0) as client:
            r = client.get(url, headers=self._headers())
            r.raise_for_status()
            data = r.json()
        state = (data.get("status") or "").lower()
        if state == "completed":
            urls = data.get("unsigned_urls") or []
            result = urls[0] if urls else f"{self._base}/videos/{job.job_id}/content?index=0"
            cost = (data.get("usage") or {}).get("cost")
            return JobStatus(state="succeeded", progress=1.0, result_url=result,
                             cost=float(cost) if cost is not None else None)
        if state in ("failed", "cancelled", "expired"):
            err = data.get("error")
            if isinstance(err, dict):
                err = err.get("message") or str(err)
            return JobStatus(state="failed", error=str(err or f"video job {state}"))
        if state == "in_progress":
            return JobStatus(state="running", progress=float(data.get("progress") or 0.0))
        return JobStatus(state="queued")

    def download(self, job: SubmittedJob, result_url: str | None = None) -> bytes:
        """Fetch the finished video. `result_url` (from a webhook's
        unsigned_urls) skips the extra status poll."""
        if not result_url:
            st = self.poll(job)
            if st.state != "succeeded" or not st.result_url:
                raise RuntimeError(f"job not ready or missing result url: {st}")
            result_url = st.result_url
        # unsigned_urls are not presigned: they need the API key.
        with httpx.Client(timeout=300.0, follow_redirects=True) as client:
            r = client.get(result_url, headers={"Authorization": f"Bearer {self.api_key}"})
            r.raise_for_status()
            return r.content

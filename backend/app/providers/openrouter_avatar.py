"""Talking-head clips from a portrait via an OpenRouter avatar model
(heygen/avatar-iv): "animates a single photo into an expressive,
lip-synced talking-head video".

EXPERIMENTAL. OpenRouter's /videos listing for avatar-iv advertises no
frame-image or duration support and doesn't document how speech is
supplied. We send what the documented request schema allows:
  input_references: [{image_url: portrait}, {audio_url: voiceover}]
  prompt:           the spoken script (used if the model voices it itself)
  provider.options.heygen: {voice_id?, motion_prompt?} (listed passthroughs)
and fall back to the regular clip path if the job fails.
"""
from __future__ import annotations

import httpx

from ..config import get_settings
from ..services import provider_keys
from .base import ModelInfo, SubmittedJob


class OpenRouterAvatarProvider:
    def __init__(self, *, model: str = "heygen/avatar-iv", api_key: str | None = None) -> None:
        self.api_key = api_key or provider_keys.resolve("openrouter")
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY missing for avatar provider")
        self.model = model

    @property
    def _base(self) -> str:
        return get_settings().openrouter_base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        s = get_settings()
        return {"Authorization": f"Bearer {self.api_key}", "HTTP-Referer": s.openrouter_site_url,
                "X-Title": s.openrouter_app_title, "Content-Type": "application/json"}

    def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(id=self.model, name=self.model, modality="video")]

    def submit_avatar(
        self, *, portrait_url: str, audio_url: str | None, script: str,
        aspect_ratio: str = "9:16", voice_id: str = "", motion_prompt: str = "",
        duration_seconds: float | None = None, callback_url: str | None = None, **_: object,
    ) -> SubmittedJob:
        """Submit and return immediately; completion arrives like any other
        OpenRouter video job (webhook or poll via the video provider)."""
        refs = [{"type": "image_url", "image_url": {"url": portrait_url}}]
        if audio_url:
            refs.append({"type": "audio_url", "audio_url": {"url": audio_url}})
        body: dict = {
            "model": self.model,
            "prompt": script[:4000],
            "aspect_ratio": aspect_ratio,
            "input_references": refs,
        }
        if callback_url:
            body["callback_url"] = callback_url
        heygen = {k: v for k, v in {"voice_id": voice_id, "motion_prompt": motion_prompt}.items() if v}
        if heygen:
            body["provider"] = {"options": {"heygen": heygen}}
        with httpx.Client(timeout=120.0) as client:
            r = client.post(f"{self._base}/videos", headers=self._headers(), json=body)
            if r.status_code >= 400:
                raise RuntimeError(f"avatar submit {r.status_code}: {r.text[:300]}")
            job = r.json()
        jid = str(job.get("id") or "")
        return SubmittedJob(provider="openrouter", model=self.model, job_id=jid,
                            polling_url=job.get("polling_url") or f"{self._base}/videos/{jid}")

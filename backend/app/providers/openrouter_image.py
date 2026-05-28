from __future__ import annotations

import base64

import httpx

from ..config import get_settings
from .base import ImageProvider, ImageRef, ModelInfo

settings = get_settings()


class OpenRouterImageProvider(ImageProvider):
    """Thin adapter for OpenRouter's image generation endpoint.

    OpenRouter exposes image models behind an OpenAI-compatible `/images/generations`-style
    surface; we keep this generic so it works with any image model exposed via OpenRouter
    (or a custom OPENROUTER_IMAGE_MODEL slug). If the response shape differs, only the
    parsing here needs to change — the rest of the app is unaffected.
    """

    def __init__(self, *, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or settings.openrouter_api_key
        self.model = model or settings.openrouter_image_model
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY missing for real image provider")

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
            if "image" in (modality or "").lower():
                out.append(
                    ModelInfo(
                        id=m.get("id", ""),
                        name=m.get("name", m.get("id", "")),
                        modality="image",
                        description=m.get("description", "") or "",
                    )
                )
        return out

    def generate(
        self,
        *,
        model: str,
        prompt: str,
        references: list[ImageRef],
        negative_prompt: str = "",
        settings: dict | None = None,
    ) -> bytes:
        url = f"{get_settings().openrouter_base_url.rstrip('/')}/images/generations"
        body: dict = {
            "model": model or self.model,
            "prompt": prompt,
            "n": 1,
        }
        if negative_prompt:
            body["negative_prompt"] = negative_prompt
        if references:
            # Pass tokenized public URLs the provider can fetch.
            body["input_references"] = [{"url": r.url, "role": r.role} for r in references]
        if settings:
            body.update(settings)
        with httpx.Client(timeout=180.0) as client:
            r = client.post(url, headers=self._headers(), json=body)
            r.raise_for_status()
            data = r.json()
        item = (data.get("data") or [{}])[0]
        if "b64_json" in item:
            return base64.b64decode(item["b64_json"])
        if "url" in item:
            with httpx.Client(timeout=180.0) as client:
                rr = client.get(item["url"])
                rr.raise_for_status()
                return rr.content
        raise RuntimeError(f"unexpected image response shape: keys={list(item.keys())}")

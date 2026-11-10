from __future__ import annotations

import base64

import httpx

from ..config import get_settings
from ..services import provider_keys
from .base import ImageProvider, ImageRef, ModelInfo


class OpenRouterImageProvider(ImageProvider):
    """Thin adapter for OpenRouter's image generation endpoint.

    Uses OpenRouter's Image API (docs/guides/overview/multimodal/image-generation):
    POST /images → base64 in data[].b64_json; models listed at GET /images/models.
    """

    def __init__(self, *, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or provider_keys.resolve("openrouter")
        self.model = model or get_settings().openrouter_image_model
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY missing for real image provider")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": get_settings().openrouter_site_url,
            "X-Title": get_settings().openrouter_app_title,
            "Content-Type": "application/json",
        }

    def list_models(self) -> list[ModelInfo]:
        # Dedicated listing for the Image API (GET /images/models).
        url = f"{get_settings().openrouter_base_url.rstrip('/')}/images/models"
        with httpx.Client(timeout=30.0) as client:
            r = client.get(url, headers=self._headers())
            r.raise_for_status()
            data = r.json()
        return [
            ModelInfo(id=m.get("id", ""), name=m.get("name", m.get("id", "")), modality="image",
                      description=m.get("description", "") or "")
            for m in data.get("data", []) if m.get("id")
        ]

    def parameters(self, model: str) -> dict:
        """What `model` accepts on POST /images, from GET
        /images/models/{id}/endpoints: {name: {"type": "enum", "values": [...]}
        or a range}. Enum values are merged across the model's endpoints."""
        url = f"{get_settings().openrouter_base_url.rstrip('/')}/images/models/{model}/endpoints"
        with httpx.Client(timeout=15.0) as client:
            r = client.get(url, headers=self._headers())
            r.raise_for_status()
            data = r.json()
        merged: dict = {}
        for ep in data.get("endpoints") or []:
            for name, spec in (ep.get("supported_parameters") or {}).items():
                if isinstance(spec, dict) and isinstance(spec.get("values"), list):
                    cur = merged.setdefault(name, {"type": "enum", "values": []})
                    cur["values"] += [v for v in spec["values"] if v not in cur["values"]]
                else:
                    merged.setdefault(name, spec)
        return merged

    def generate(
        self,
        *,
        model: str,
        prompt: str,
        references: list[ImageRef],
        negative_prompt: str = "",
        settings: dict | None = None,
    ) -> bytes:
        # Image API: POST /images {model, prompt, aspect_ratio?, n,
        # input_references?} → {data: [{b64_json, media_type}]}.
        from ..services.safety import check_prompt

        check_prompt(prompt)  # minors: refused here too, not only by the vendor
        url = f"{get_settings().openrouter_base_url.rstrip('/')}/images"
        body: dict = {
            "model": model or self.model,
            "prompt": prompt if not negative_prompt else f"{prompt}\nAvoid: {negative_prompt}",
            "n": 1,
        }
        if references:
            # Tokenized public URLs the provider can fetch.
            body["input_references"] = [
                {"type": "image_url", "image_url": {"url": r.url}} for r in references
            ]
        if settings:
            body.update(settings)
        # Quality "max" on GPT Image 2.6 takes ~2 minutes.
        with httpx.Client(timeout=300.0) as client:
            r = client.post(url, headers=self._headers(), json=body)
            r.raise_for_status()
            data = r.json()
        from ..services import spend

        spend.record_openrouter("image", body["model"], cost=spend.usage_cost(data),
                                generation_id=str(data.get("id") or ""),
                                estimate=get_settings().cost_image_per_item)
        item = (data.get("data") or [{}])[0]
        if "b64_json" in item:
            return base64.b64decode(item["b64_json"])
        if "url" in item:
            with httpx.Client(timeout=180.0) as client:
                rr = client.get(item["url"])
                rr.raise_for_status()
                return rr.content
        raise RuntimeError(f"unexpected image response shape: keys={list(item.keys())}")

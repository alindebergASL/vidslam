from __future__ import annotations

import base64

import httpx

from ..config import get_settings
from ..services import provider_keys
from .base import ImageProvider, ImageRef, ModelInfo


def _error_reason(r: httpx.Response) -> str:
    """OpenRouter's error message from a failed response (never headers)."""
    try:
        err = (r.json() or {}).get("error")
    except ValueError:
        err = None
    if isinstance(err, dict):
        msg = str(err.get("message") or "").strip()
        meta = err.get("metadata") if isinstance(err.get("metadata"), dict) else {}
        extra = "; ".join(str(meta[k]) for k in ("reason", "provider_name") if meta.get(k))
        if msg:
            return f"{msg}{f' ({extra})' if extra else ''}"[:300]
    if isinstance(err, str) and err.strip():
        return err.strip()[:300]
    return (r.reason_phrase or "error")[:300]


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
        if negative_prompt:
            check_prompt(negative_prompt)  # sent too, as "Avoid: …"
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
            if r.status_code >= 400:
                # Keep OpenRouter's reason ("Key limit exceeded", a moderation
                # flag…): a bare "403 Forbidden" can't be acted on.
                raise httpx.HTTPStatusError(f"OpenRouter image {r.status_code}: {_error_reason(r)}",
                                            request=r.request, response=r)
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

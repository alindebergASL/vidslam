"""Best output settings for an image model.

OpenRouter's image models take different knobs: GPT Image has `quality`
(default "auto" is a cheap tier; "max" was the most realistic in a blind
test, Nov 2026), Gemini/FLUX take `resolution` (default 1K; "2K" is sharper
for ~$0.02 more, while 4K came out crunchier, not better). Each endpoint
lists what it accepts (GET /images/models/{id}/endpoints), so the settings
are picked from that list, cached, with built-in rules when the list can't
be read (mock mode, offline, a model OpenRouter doesn't describe)."""
from __future__ import annotations

import logging
import re
import threading
import time

from ..config import get_settings

log = logging.getLogger("avs.image_settings")

# Preference order, best first. 4K is deliberately absent (see above).
QUALITY_ORDER = ("max", "xhigh", "high")
RESOLUTION_ORDER = ("2K", "1.5K", "1K")

_TTL_SECONDS = 6 * 3600
_cache: dict[str, tuple[float, dict | None]] = {}
_lock = threading.Lock()


def _fetch_parameters(model: str) -> dict | None:
    """The model's accepted parameters, or None when they can't be read."""
    if not model or get_settings().image_is_mocked():
        return None
    now = time.monotonic()
    with _lock:
        hit = _cache.get(model)
        if hit and now - hit[0] < _TTL_SECONDS:
            return hit[1]
    try:
        from ..providers.openrouter_image import OpenRouterImageProvider

        params: dict | None = OpenRouterImageProvider(model=model).parameters(model)
    except Exception as e:  # noqa: BLE001 — fall back to built-in rules
        log.info("image parameters for %s unavailable (%s); using built-in rules", model, e)
        params = None
    with _lock:
        _cache[model] = (now, params)
    return params


def _values(params: dict, name: str) -> list:
    spec = params.get(name)
    return list(spec.get("values") or []) if isinstance(spec, dict) else []


def _first(preferred: tuple[str, ...], offered: list) -> str | None:
    return next((p for p in preferred if p in offered), None)


def _builtin(model: str, aspect_ratio: str) -> dict:
    out: dict = {"aspect_ratio": aspect_ratio}
    if model.startswith("openai/gpt-image"):
        out["quality"] = "max" if re.search(r"gpt-image-2\.[5-9]", model) else "high"
    elif model.startswith(("google/gemini", "google/nano", "black-forest-labs/")):
        out["resolution"] = "2K"
    return out


def best_image_settings(model: str, aspect_ratio: str = "16:9") -> dict:
    """Settings for the most realistic output `model` offers, for POST /images."""
    if model.startswith("sogni/"):
        # Sogni's generate_image takes the aspect ratio and sizes the image
        # itself; there is no OpenRouter parameters listing to read.
        return {"aspect_ratio": aspect_ratio}
    params = _fetch_parameters(model)
    if params is None:
        return _builtin(model, aspect_ratio)
    out: dict = {}
    ratios = _values(params, "aspect_ratio")
    if not ratios or aspect_ratio in ratios:
        out["aspect_ratio"] = aspect_ratio
    quality = _first(QUALITY_ORDER, _values(params, "quality"))
    if quality:
        out["quality"] = quality
    resolution = _first(RESOLUTION_ORDER, _values(params, "resolution"))
    if resolution:
        out["resolution"] = resolution
    return out


def clear_cache() -> None:
    with _lock:
        _cache.clear()

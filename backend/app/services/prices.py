"""Rough prices in plain words for pickers and buttons ("about $0.04 per
image"), from what the model catalog knows. Empty text = price unknown."""
from __future__ import annotations

from typing import Optional


def money(usd: Optional[float]) -> str:
    """$0.30, $0.04, $0.018, $0.0006: enough digits to show the price.
    A positive price too small to show in 4 digits is "" (unknown), never
    "$0.0000"."""
    if usd is None:
        return ""
    x = float(usd)
    if x <= 0:
        return "$0"
    if round(x, 4) <= 0:
        return ""
    if x >= 0.1:
        return f"${x:.2f}"
    for digits in (2, 3, 4):
        if round(x, digits) > 0 and abs(round(x, digits) - x) < x * 0.1:
            return f"${x:.{digits}f}"
    return f"${x:.4f}"


def _per_second(entry: dict) -> Optional[float]:
    rate = entry.get("price_per_second")
    if isinstance(rate, (int, float)) and rate > 0:
        return float(rate)
    rates = []
    for k, v in (entry.get("pricing_skus") or {}).items():
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0 and ("second" in k.lower() or k.lower() in ("duration", "per_second")):
            rates.append(f)
    return min(rates) if rates else None


def image_price(entry: dict) -> Optional[float]:
    """Price of one OUTPUT picture, or None when the catalog doesn't say.

    OpenRouter's `pricing.image` is the price per INPUT image, and its output
    pictures are billed through completion / image tokens (per-token keys such
    as `image_output`), so neither is a per-picture price: OpenRouter entries
    get no price here rather than a figure ~30x too low. Only entries built
    locally (Runware, Sogni) carry a real per-picture `image` price."""
    if (entry.get("provider") or "") == "openrouter":
        return None
    pricing = entry.get("pricing") or {}
    for key in ("image", "per_image"):
        v = pricing.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None


def price_text(entry: Optional[dict], kind: str) -> str:
    """"about $0.04 per image" / "about $0.30 per 5 s" for a catalog entry."""
    if not entry:
        return ""
    if entry.get("price_text"):
        return str(entry["price_text"])
    if kind == "image":
        usd = image_price(entry)
        shown = money(usd) if usd else ""
        return f"about {shown} per image" if shown else ""
    if kind == "video":
        rate = _per_second(entry)
        shown = money(rate * 5) if rate else ""
        return f"about {shown} per 5 s" if shown else ""
    return ""

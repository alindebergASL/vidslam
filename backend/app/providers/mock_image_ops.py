"""Placeholder picture operations for mock mode: deterministic (the same
picture gives the same result), instant and never billed."""
from __future__ import annotations

import hashlib
import io

from PIL import Image, ImageDraw

from .base import ImageOp, ImageOpProvider
from .mock_image import _font


def _tint(raw: bytes) -> tuple[int, int, int]:
    h = hashlib.sha256(raw).digest()
    return 155 + h[0] % 100, 155 + h[1] % 100, 155 + h[2] % 100


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def factor_of(options: dict | None) -> int:
    try:
        return max(1, int((options or {}).get("factor") or 2))
    except (TypeError, ValueError):
        return 2


class MockImageOpProvider(ImageOpProvider):
    def run(self, op: ImageOp, model: str, image_bytes: bytes, mime: str, options: dict) -> bytes:
        with Image.open(io.BytesIO(image_bytes)) as src:
            src.load()
            had_alpha = src.mode in ("RGBA", "LA", "P")
            img = src.convert("RGBA")
        w, h = img.size
        color = _tint(image_bytes)
        if op == "remove_background":
            # A centred ellipse stays; everything outside it is transparent.
            mask = Image.new("L", (w, h), 0)
            ImageDraw.Draw(mask).ellipse((w * 0.1, h * 0.05, w * 0.9, h * 0.95), fill=255)
            img.putalpha(mask)
            ImageDraw.Draw(img).text((w * 0.3, h * 0.45), "MOCK CUTOUT", font=_font(max(12, w // 16)),
                                     fill=(*color, 255))
            return _png(img)
        if op == "upscale":
            n = factor_of(options)
            out = img.resize((w * n, h * n), Image.LANCZOS)
            ImageDraw.Draw(out).text((16, 16), f"MOCK ×{n}", font=_font(max(14, (w * n) // 20)),
                                     fill=(*color, 255))
            if not had_alpha:
                out = out.convert("RGB")
            return _png(out)
        raise ValueError(f"unknown picture operation {op!r}")

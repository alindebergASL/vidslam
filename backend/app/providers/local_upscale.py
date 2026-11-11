"""Basic upscale in this app: Pillow's Lanczos filter. Free and offline,
no AI (edges get smoother, no new detail), so nothing is recorded as spend."""
from __future__ import annotations

import io

from PIL import Image

from .base import ImageOp, ImageOpProvider
from .mock_image_ops import factor_of


class LocalUpscaleProvider(ImageOpProvider):
    def run(self, op: ImageOp, model: str, image_bytes: bytes, mime: str, options: dict) -> bytes:
        if op != "upscale":
            raise ValueError("The basic upscale can only upscale; pick another model to remove a background")
        n = factor_of(options)
        with Image.open(io.BytesIO(image_bytes)) as src:
            src.load()
            alpha = src.mode in ("RGBA", "LA") or (src.mode == "P" and "transparency" in src.info)
            img = src.convert("RGBA" if alpha else "RGB")
        out = img.resize((img.width * n, img.height * n), Image.LANCZOS)
        buf = io.BytesIO()
        if alpha:
            out.save(buf, format="PNG")
        else:
            out.save(buf, format="JPEG", quality=92, optimize=True)
        return buf.getvalue()

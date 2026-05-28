from __future__ import annotations

import io
import textwrap

from PIL import Image, ImageDraw, ImageFont

from .base import ImageProvider, ImageRef, ModelInfo

_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _font(size: int) -> ImageFont.ImageFont:
    for p in _FONT_PATHS:
        try:
            return ImageFont.truetype(p, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


class MockImageProvider(ImageProvider):
    def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(id="mock/image-default", name="Mock Image", modality="image"),
        ]

    def generate(
        self,
        *,
        model: str,
        prompt: str,
        references: list[ImageRef],
        negative_prompt: str = "",
        settings: dict | None = None,
    ) -> bytes:
        size = (1024, 1024)
        # Deterministic background from the prompt hash so different prompts vary.
        h = abs(hash(prompt)) & 0xFFFFFF
        r, g, b = (h >> 16) & 0xFF, (h >> 8) & 0xFF, h & 0xFF
        img = Image.new("RGB", size, (max(20, r // 3), max(20, g // 3), max(20, b // 3)))
        draw = ImageDraw.Draw(img)

        title_font = _font(48)
        body_font = _font(28)
        small_font = _font(22)

        draw.text((40, 40), "MOCK IMAGE", font=title_font, fill="white")
        draw.text((40, 100), f"model: {model}", font=small_font, fill=(220, 220, 220))

        wrapped = textwrap.fill(prompt, width=42)
        draw.multiline_text((40, 160), wrapped, font=body_font, fill="white", spacing=8)

        if references:
            y = 600
            draw.text((40, y), "References:", font=body_font, fill=(255, 220, 120))
            for ref in references[:8]:
                y += 36
                line = f"• [{ref.role}] {ref.name or ref.url[-32:]}"
                draw.text((60, y), line, font=small_font, fill=(255, 220, 120))

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

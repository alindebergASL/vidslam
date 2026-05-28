"""Seed the database with a demo Cast (Naina + Arjun + cafe scene + film style)
and a draft Kissmet project.

Usage (inside the backend container):

    python -m app.seed
"""
from __future__ import annotations

import io
import logging
import textwrap

from PIL import Image, ImageDraw, ImageFont

from . import models
from .config import get_settings
from .db import SessionLocal, init_db
from .services import storage

log = logging.getLogger("avs.seed")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
settings = get_settings()


def _placeholder_image(text: str, color: tuple[int, int, int]) -> bytes:
    img = Image.new("RGB", (1024, 1024), color)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 48)
    except OSError:
        font = ImageFont.load_default()
    draw.multiline_text((40, 40), textwrap.fill(text, 22), font=font, fill="white", spacing=10)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _save_placeholder(owner_kind: str, owner_id: int, asset_type: str, text: str,
                      color: tuple[int, int, int]) -> dict:
    raw = _placeholder_image(text, color)
    subdir = settings.data_path / "uploads" / owner_kind / str(owner_id)
    subdir.mkdir(parents=True, exist_ok=True)
    out = subdir / f"seed_{asset_type}.png"
    out.write_bytes(raw)
    return {
        "owner_kind": owner_kind,
        "asset_type": asset_type,
        "original_filename": out.name,
        "file_path": str(out),
        "mime_type": "image/png",
        "width": 1024,
        "height": 1024,
        "checksum": "",
        "source": "upload",
    }


def run() -> None:
    init_db()
    db = SessionLocal()
    try:
        if db.query(models.Avatar).count() > 0:
            log.info("seed: avatars already exist, skipping cast creation")
        else:
            naina = models.Avatar(
                name="Naina",
                persona="Witty Indian-American dating-app creator who riffs on modern romance.",
                visual_identity=(
                    "Mid-20s, warm brown skin, long wavy black hair, hoop earrings, "
                    "soft cinematic lighting, casual streetwear."
                ),
                description="AI-generated virtual creator for Kissmet promo content.",
                default_disclosure_text="AI-generated virtual creator",
                default_voice_provider="elevenlabs",
                brand="Kissmet",
            )
            arjun = models.Avatar(
                name="Arjun",
                persona="Naina's deadpan best-friend co-host who deflates her hot takes.",
                visual_identity=(
                    "Mid-20s, light brown skin, short curly hair, round glasses, "
                    "oversized linen shirt, dry expression."
                ),
                description="AI-generated virtual creator (co-host).",
                default_disclosure_text="AI-generated virtual creator",
                default_voice_provider="elevenlabs",
                brand="Kissmet",
            )
            db.add_all([naina, arjun])
            db.flush()

            for av, color in [(naina, (200, 90, 140)), (arjun, (90, 130, 200))]:
                db.add(models.Asset(
                    avatar_id=av.id,
                    **_save_placeholder("avatar", av.id, "hero", f"{av.name}\nhero portrait", color),
                ))
                db.add(models.Asset(
                    avatar_id=av.id,
                    **_save_placeholder("avatar", av.id, "lifestyle", f"{av.name}\nlifestyle still", color),
                ))

            log.info("seed: created avatars Naina (id=%s) + Arjun (id=%s)", naina.id, arjun.id)

        if db.query(models.Ingredient).count() == 0:
            scene = models.Ingredient(
                name="Brooklyn rooftop at golden hour",
                kind="scene",
                visual_identity="Warm orange-pink sunset, brick parapet, fairy lights, NYC skyline backdrop.",
                description="Default location ingredient for Kissmet creator content.",
            )
            style = models.Ingredient(
                name="Warm grainy 35mm film",
                kind="style",
                visual_identity="Subtle film grain, warm color cast, soft halation around highlights.",
            )
            db.add_all([scene, style])
            db.flush()
            db.add(models.Asset(
                ingredient_id=scene.id,
                **_save_placeholder("ingredient", scene.id, "hero",
                                    "Brooklyn rooftop\ngolden hour", (220, 140, 80)),
            ))
            db.add(models.Asset(
                ingredient_id=style.id,
                **_save_placeholder("ingredient", style.id, "hero",
                                    "Warm 35mm\nfilm style", (170, 120, 80)),
            ))
            log.info("seed: created ingredients (scene id=%s, style id=%s)", scene.id, style.id)

        brand = None
        if db.query(models.BrandKit).count() == 0:
            brand = models.BrandKit(
                name="Kissmet",
                primary_color="#FF5C8A",
                end_card_bg_color="#10243A",
                end_card_text_color="#FFD0DE",
                default_cta_text="Kissmet dating app coming soon",
                default_disclosure_text="AI-generated virtual creator",
            )
            db.add(brand)
            db.flush()
            log.info("seed: created brand kit Kissmet (id=%s)", brand.id)
        else:
            brand = db.query(models.BrandKit).filter_by(name="Kissmet").first()

        if db.query(models.VideoProject).count() == 0:
            naina = db.query(models.Avatar).filter_by(name="Naina").one()
            arjun = db.query(models.Avatar).filter_by(name="Arjun").one()
            scene = db.query(models.Ingredient).filter_by(name="Brooklyn rooftop at golden hour").one()
            style = db.query(models.Ingredient).filter_by(name="Warm grainy 35mm film").one()

            project = models.VideoProject(
                title="Kissmet — Specific Detail",
                original_script=(
                    "If your dating bio says food, travel, and music, congratulations — "
                    "you have described every human being with Wi-Fi. "
                    "Try one specific detail instead. "
                    "Kissmet dating app coming soon."
                ),
                mode="reel_montage",
                aspect_ratio="9:16",
                target_duration_seconds=25,
                cta_text="Kissmet dating app coming soon",
                caption_style="influencer_bold",
                include_disclosure=True,
                primary_avatar_id=naina.id,
                brand_kit_id=brand.id if brand else None,
                status="draft",
            )
            db.add(project)
            db.flush()
            db.add_all([
                models.ProjectCastMember(project_id=project.id, member_kind="avatar",
                                         avatar_id=naina.id, role="host"),
                models.ProjectCastMember(project_id=project.id, member_kind="avatar",
                                         avatar_id=arjun.id, role="co-host"),
                models.ProjectCastMember(project_id=project.id, member_kind="ingredient",
                                         ingredient_id=scene.id, role="location"),
                models.ProjectCastMember(project_id=project.id, member_kind="ingredient",
                                         ingredient_id=style.id, role="style"),
            ])
            log.info("seed: created demo project id=%s", project.id)

        db.commit()
        log.info("seed: done")
    finally:
        db.close()


if __name__ == "__main__":
    run()

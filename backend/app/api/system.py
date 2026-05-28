from __future__ import annotations

import shutil

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from ..db import get_db
from .auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])

APP_VERSION = "0.1.0"


@router.get("/info")
def system_info(db: Session = Depends(get_db)) -> dict:
    """Non-secret deployment overview for the Settings page. Reports *presence* of
    keys (booleans), never the values."""
    s = get_settings()

    def _count(model) -> int:
        return db.query(func.count(model.id)).scalar() or 0

    return {
        "version": APP_VERSION,
        "mock_providers": s.mock_providers,
        "ffmpeg_available": shutil.which("ffmpeg") is not None,
        "ffprobe_available": shutil.which("ffprobe") is not None,
        "data_dir": s.data_dir,
        "public_base_url": s.public_base_url,
        "redis_configured": bool(s.redis_url),
        "keys": {
            "openrouter": bool(s.openrouter_api_key),
            "elevenlabs": bool(s.elevenlabs_api_key),
        },
        "models": {
            "chat": s.openrouter_chat_model or None,
            "image": s.openrouter_image_model or None,
            "video": s.openrouter_video_model or None,
            "tts": s.elevenlabs_model_id or None,
            "music": s.elevenlabs_music_model_id or None,
        },
        "counts": {
            "avatars": _count(models.Avatar),
            "ingredients": _count(models.Ingredient),
            "brand_kits": _count(models.BrandKit),
            "projects": _count(models.VideoProject),
            "renders": _count(models.Render),
        },
    }

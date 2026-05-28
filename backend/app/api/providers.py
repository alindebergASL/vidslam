from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..config import get_settings
from ..providers import get_image, get_video, provider_status
from ..providers.base import ModelInfo
from .auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])
settings = get_settings()


def _to_dict(models: list[ModelInfo]) -> list[dict]:
    return [{"id": m.id, "name": m.name, "description": m.description} for m in models]


@router.get("/status")
def status() -> dict:
    return provider_status()


@router.get("/openrouter/video-models")
def video_models() -> list[dict]:
    try:
        return _to_dict(get_video().list_models())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"failed to list video models: {e}") from e


@router.get("/openrouter/image-models")
def image_models() -> list[dict]:
    try:
        return _to_dict(get_image().list_models())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"failed to list image models: {e}") from e


@router.get("/caption-styles")
def caption_styles() -> list[str]:
    from ..services.captions import available_styles

    return available_styles()

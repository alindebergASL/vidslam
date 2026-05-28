from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..schemas import AssetOut
from ..services import storage
from .auth import require_auth

router = APIRouter()
auth_dep = Depends(require_auth)


@router.post(
    "/avatars/{avatar_id}/assets",
    response_model=AssetOut,
    status_code=201,
    dependencies=[auth_dep],
)
async def upload_avatar_asset(
    avatar_id: int,
    asset_type: str = Form("other"),
    rights_confirmed: bool = Form(False),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> models.Asset:
    if not rights_confirmed:
        raise HTTPException(400, "You must confirm rights to use this asset.")
    avatar = db.get(models.Avatar, avatar_id)
    if avatar is None:
        raise HTTPException(404, "avatar not found")
    try:
        meta = storage.save_uploaded_image(
            file.file,
            original_filename=file.filename or "",
            mime_type=file.content_type or "",
            owner_kind="avatar",
            owner_id=avatar_id,
        )
    except storage.UploadError as e:
        raise HTTPException(400, str(e)) from e
    a = models.Asset(
        owner_kind="avatar",
        avatar_id=avatar_id,
        asset_type=asset_type,
        source="upload",
        **meta,
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


@router.get(
    "/avatars/{avatar_id}/assets",
    response_model=list[AssetOut],
    dependencies=[auth_dep],
)
def list_avatar_assets(avatar_id: int, db: Session = Depends(get_db)) -> list[models.Asset]:
    if db.get(models.Avatar, avatar_id) is None:
        raise HTTPException(404, "avatar not found")
    return (
        db.query(models.Asset)
        .filter(models.Asset.owner_kind == "avatar", models.Asset.avatar_id == avatar_id)
        .order_by(models.Asset.created_at.desc())
        .all()
    )


@router.post(
    "/ingredients/{ingredient_id}/assets",
    response_model=AssetOut,
    status_code=201,
    dependencies=[auth_dep],
)
async def upload_ingredient_asset(
    ingredient_id: int,
    asset_type: str = Form("hero"),
    rights_confirmed: bool = Form(False),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> models.Asset:
    if not rights_confirmed:
        raise HTTPException(400, "You must confirm rights to use this asset.")
    ing = db.get(models.Ingredient, ingredient_id)
    if ing is None:
        raise HTTPException(404, "ingredient not found")
    try:
        meta = storage.save_uploaded_image(
            file.file,
            original_filename=file.filename or "",
            mime_type=file.content_type or "",
            owner_kind="ingredient",
            owner_id=ingredient_id,
        )
    except storage.UploadError as e:
        raise HTTPException(400, str(e)) from e
    a = models.Asset(
        owner_kind="ingredient",
        ingredient_id=ingredient_id,
        asset_type=asset_type,
        source="upload",
        **meta,
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


@router.get(
    "/ingredients/{ingredient_id}/assets",
    response_model=list[AssetOut],
    dependencies=[auth_dep],
)
def list_ingredient_assets(
    ingredient_id: int, db: Session = Depends(get_db)
) -> list[models.Asset]:
    if db.get(models.Ingredient, ingredient_id) is None:
        raise HTTPException(404, "ingredient not found")
    return (
        db.query(models.Asset)
        .filter(
            models.Asset.owner_kind == "ingredient",
            models.Asset.ingredient_id == ingredient_id,
        )
        .order_by(models.Asset.created_at.desc())
        .all()
    )


@router.delete("/assets/{asset_id}", status_code=204, dependencies=[auth_dep])
def delete_asset(asset_id: int, db: Session = Depends(get_db)) -> None:
    a = db.get(models.Asset, asset_id)
    if a is None:
        raise HTTPException(404, "asset not found")
    try:
        Path(a.file_path).unlink(missing_ok=True)
    except OSError:
        pass
    db.delete(a)
    db.commit()


@router.get("/assets/{asset_id}/preview", dependencies=[auth_dep])
def asset_preview(asset_id: int, db: Session = Depends(get_db)) -> FileResponse:
    a = db.get(models.Asset, asset_id)
    if a is None:
        raise HTTPException(404, "asset not found")
    return FileResponse(a.file_path, media_type=a.mime_type)


@router.get("/public-assets/{token}")
def public_asset(token: str, db: Session = Depends(get_db)) -> FileResponse:
    """Tokenized, unauthenticated public asset URL exposed to providers as reference images."""
    a = db.query(models.Asset).filter(models.Asset.public_token == token).first()
    if a is None:
        raise HTTPException(404, "asset not found")
    if not Path(a.file_path).exists():
        raise HTTPException(404, "asset file missing")
    return FileResponse(a.file_path, media_type=a.mime_type)

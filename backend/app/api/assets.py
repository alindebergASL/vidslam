from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..schemas import AssetOut
from ..schemas.api import ASSET_TYPES, AssetUpdate
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
        # 413 too large; 400 not a supported image (message says which).
        raise HTTPException(e.status_code, str(e)) from e
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
    from ..services import cast_pictures, content_mode

    if content_mode.is_tick_picture(a):
        # A ticked character's new picture is checked off this request.
        await run_in_threadpool(cast_pictures.picture_added, avatar_id)
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
        # 413 too large; 400 not a supported image (message says which).
        raise HTTPException(e.status_code, str(e)) from e
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


@router.patch("/assets/{asset_id}", response_model=AssetOut, dependencies=[auth_dep])
def update_asset(asset_id: int, body: AssetUpdate, db: Session = Depends(get_db)) -> models.Asset:
    a = db.get(models.Asset, asset_id)
    if a is None:
        raise HTTPException(404, "asset not found")
    allowed = ASSET_TYPES.get(a.owner_kind, ())
    if body.asset_type not in allowed:
        raise HTTPException(422, f"asset_type must be one of {', '.join(allowed)}")
    from ..services import cast_pictures, content_mode

    was_picture = content_mode.is_tick_picture(a)
    a.asset_type = body.asset_type
    db.commit()
    db.refresh(a)
    if not was_picture and content_mode.is_tick_picture(a):
        # A logo moved to a picture slot is a new picture for a ticked character.
        cast_pictures.picture_added(a.avatar_id)
    return a


@router.delete("/assets/{asset_id}", status_code=204, dependencies=[auth_dep])
def delete_asset(asset_id: int, db: Session = Depends(get_db)) -> None:
    a = db.get(models.Asset, asset_id)
    if a is None:
        raise HTTPException(404, "asset not found")
    path = a.file_path
    # Assets saved from Studio before saves made copies share the job's
    # result file, so the file may still be someone else's.
    shared = (
        db.query(models.Asset.id)
        .filter(models.Asset.file_path == path, models.Asset.id != asset_id)
        .first()
        is not None
        or db.query(models.AssetGenerationJob.id)
        .filter(models.AssetGenerationJob.result_path == path)
        .first()
        is not None
    )
    # A studio job that was saved as this asset goes back to plain
    # "completed" (savable again); the FK would otherwise block the delete
    # on Postgres.
    for job in (
        db.query(models.AssetGenerationJob)
        .filter(models.AssetGenerationJob.result_asset_id == asset_id)
        .all()
    ):
        job.result_asset_id = None
        if job.status == "saved":
            job.status = "completed"
    # Strip this id from any shot's reference_asset_ids_json so the editor's
    # reference strip + the renderer's lookup don't carry a dangling id.
    shots_with_ref = (
        db.query(models.VideoShot)
        .filter(models.VideoShot.reference_asset_ids_json.is_not(None))
        .all()
    )
    for shot in shots_with_ref:
        refs = list(shot.reference_asset_ids_json or [])
        if asset_id in refs:
            shot.reference_asset_ids_json = [x for x in refs if x != asset_id]
    # Flush the FK clears before the row goes so the UPDATE runs first.
    db.flush()
    db.delete(a)
    db.commit()
    if path and not shared:
        from ..services.storage import drop_reference_copy

        drop_reference_copy(Path(path))
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass


@router.get("/assets/{asset_id}/preview", dependencies=[auth_dep])
def asset_preview(asset_id: int, db: Session = Depends(get_db)) -> FileResponse:
    a = db.get(models.Asset, asset_id)
    if a is None:
        raise HTTPException(404, "asset not found")
    return FileResponse(a.file_path, media_type=a.mime_type)


@router.get("/public-assets/{token}")
def public_asset(token: str, ref: bool = False, db: Session = Depends(get_db)):
    """Tokenized, unauthenticated public asset URL exposed to providers as
    reference images.

    When S3 is configured (`s3_bucket` set + boto3 installed), redirect to
    a presigned S3 URL so the provider fetches off S3/CDN instead of
    through this app — saves bandwidth and decouples render throughput
    from app worker count. When S3 isn't configured this falls back to
    streaming the local file directly, which is the MVP behavior."""
    a = db.query(models.Asset).filter(models.Asset.public_token == token).first()
    if a is None:
        raise HTTPException(404, "asset not found")

    from fastapi.responses import RedirectResponse

    from ..services.s3 import presigned_url_for
    from ..services.storage import reference_copy

    # `?ref=1` is the URL handed to providers (storage.public_url_for_token):
    # a big picture (e.g. a 4× upscale saved to the Cast) goes to them as a
    # scaled-down copy. The app's own views and downloads (no flag) get the
    # stored picture at full size.
    if ref and (a.mime_type or "").startswith("image/") and Path(a.file_path).exists():
        small = reference_copy(Path(a.file_path))
        if small != Path(a.file_path):
            return FileResponse(small, media_type=a.mime_type)

    presigned = presigned_url_for(Path(a.file_path), content_type=a.mime_type)
    if presigned is not None:
        # 302 keeps the redirect uncached; the next request will re-presign
        # with a fresh TTL. Provider HTTP clients follow 302 by default.
        return RedirectResponse(url=presigned, status_code=302)

    if not Path(a.file_path).exists():
        raise HTTPException(404, "asset file missing")
    return FileResponse(a.file_path, media_type=a.mime_type)

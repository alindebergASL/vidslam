from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..schemas import BrandKitCreate, BrandKitOut, BrandKitUpdate
from ..services import storage
from .auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/brand-kits", response_model=list[BrandKitOut])
def list_brand_kits(db: Session = Depends(get_db)) -> list[models.BrandKit]:
    return db.query(models.BrandKit).order_by(models.BrandKit.created_at.desc()).all()


@router.post("/brand-kits", response_model=BrandKitOut, status_code=201)
def create_brand_kit(body: BrandKitCreate, db: Session = Depends(get_db)) -> models.BrandKit:
    bk = models.BrandKit(**body.model_dump())
    db.add(bk)
    db.commit()
    db.refresh(bk)
    return bk


@router.get("/brand-kits/{brand_kit_id}", response_model=BrandKitOut)
def get_brand_kit(brand_kit_id: int, db: Session = Depends(get_db)) -> models.BrandKit:
    bk = db.get(models.BrandKit, brand_kit_id)
    if bk is None:
        raise HTTPException(404, "brand kit not found")
    return bk


@router.patch("/brand-kits/{brand_kit_id}", response_model=BrandKitOut)
def update_brand_kit(
    brand_kit_id: int, body: BrandKitUpdate, db: Session = Depends(get_db)
) -> models.BrandKit:
    bk = db.get(models.BrandKit, brand_kit_id)
    if bk is None:
        raise HTTPException(404, "brand kit not found")
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(bk, k, v)
    db.commit()
    db.refresh(bk)
    return bk


@router.delete("/brand-kits/{brand_kit_id}", status_code=204)
def delete_brand_kit(brand_kit_id: int, db: Session = Depends(get_db)) -> None:
    bk = db.get(models.BrandKit, brand_kit_id)
    if bk is None:
        raise HTTPException(404, "brand kit not found")
    if bk.logo_path:
        try:
            Path(bk.logo_path).unlink(missing_ok=True)
        except OSError:
            pass
    # Detach from any projects still referencing it.
    for p in db.query(models.VideoProject).filter(models.VideoProject.brand_kit_id == brand_kit_id):
        p.brand_kit_id = None
    db.delete(bk)
    db.commit()


@router.post("/brand-kits/{brand_kit_id}/logo", response_model=BrandKitOut)
async def upload_logo(
    brand_kit_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> models.BrandKit:
    bk = db.get(models.BrandKit, brand_kit_id)
    if bk is None:
        raise HTTPException(404, "brand kit not found")
    try:
        meta = storage.save_uploaded_image(
            file.file,
            original_filename=file.filename or "",
            mime_type=file.content_type or "",
            owner_kind="brand",
            owner_id=brand_kit_id,
        )
    except storage.UploadError as e:
        raise HTTPException(400, str(e)) from e
    import secrets

    bk.logo_path = meta["file_path"]
    bk.logo_public_token = secrets.token_urlsafe(32)
    db.commit()
    db.refresh(bk)
    return bk


@router.get("/brand-kits/{brand_kit_id}/logo")
def stream_logo(brand_kit_id: int, db: Session = Depends(get_db)) -> FileResponse:
    bk = db.get(models.BrandKit, brand_kit_id)
    if bk is None or not bk.logo_path or not Path(bk.logo_path).exists():
        raise HTTPException(404, "no logo uploaded")
    return FileResponse(bk.logo_path)

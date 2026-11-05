from __future__ import annotations

import hashlib
import io
import uuid
from pathlib import Path
from typing import BinaryIO

from PIL import Image, UnidentifiedImageError

from ..config import get_settings

settings = get_settings()


class UploadError(ValueError):
    pass


# Pillow format → (extension, mime type): what a file really is.
_FORMAT_TYPES = {"PNG": ("png", "image/png"), "JPEG": ("jpg", "image/jpeg"),
                 "WEBP": ("webp", "image/webp"), "GIF": ("gif", "image/gif")}


def _ext_for_mime(mime: str) -> str:
    return {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/webp": "webp",
    }.get(mime, "bin")


def save_uploaded_image(
    file: BinaryIO,
    *,
    original_filename: str,
    mime_type: str,
    owner_kind: str,
    owner_id: int,
) -> dict:
    """Validate + strip EXIF + persist; return metadata dict."""
    if mime_type not in settings.allowed_image_mimes:
        raise UploadError(f"Unsupported mime type: {mime_type}")

    raw = file.read()
    if len(raw) > settings.max_upload_bytes:
        raise UploadError(
            f"File too large: {len(raw)} > {settings.max_upload_bytes} bytes"
        )

    try:
        with Image.open(io.BytesIO(raw)) as img:
            img.load()
            fmt = img.format
            width, height = img.size
            # Strip EXIF: re-encode without the exif chunk.
            buf = io.BytesIO()
            save_kwargs: dict = {}
            if fmt == "JPEG":
                save_kwargs["quality"] = 90
                save_kwargs["optimize"] = True
                img_to_save = img.convert("RGB")
            elif fmt == "PNG":
                img_to_save = img
            elif fmt == "WEBP":
                save_kwargs["quality"] = 92
                img_to_save = img
            else:
                raise UploadError(f"Unsupported image format: {fmt}")
            img_to_save.save(buf, format=fmt, **save_kwargs)
            cleaned = buf.getvalue()
    except UnidentifiedImageError as e:
        raise UploadError("File is not a recognizable image") from e

    checksum = hashlib.sha256(cleaned).hexdigest()
    subdir = settings.data_path / "uploads" / owner_kind / str(owner_id)
    subdir.mkdir(parents=True, exist_ok=True)
    # Name and type the file by what it is, not what the browser claimed
    # (a JPEG sent as image/png would be served as PNG, and providers such
    # as HeyGen reject the mismatch).
    ext, mime_type = _FORMAT_TYPES[fmt]
    filename = f"{uuid.uuid4().hex}.{ext}"
    out_path = subdir / filename
    out_path.write_bytes(cleaned)

    # Best-effort S3 mirror when configured. Never raises into the upload
    # path; an S3 outage just means the local-fs delivery URL is used.
    from .s3 import mirror_to_s3

    mirror_to_s3(out_path, content_type=mime_type)

    return {
        "file_path": str(out_path),
        "original_filename": original_filename,
        "mime_type": mime_type,
        "width": width,
        "height": height,
        "checksum": checksum,
    }


def image_info(raw: bytes) -> tuple[str, str, int, int]:
    """(extension, mime type, width, height) read from the bytes themselves.
    Providers return PNG, JPEG or WebP regardless of what was asked for."""
    try:
        with Image.open(io.BytesIO(raw)) as img:
            ext, mime = _FORMAT_TYPES.get(img.format or "", ("png", "image/png"))
            return ext, mime, img.size[0], img.size[1]
    except Exception:  # noqa: BLE001 — unreadable: keep the old default
        return "png", "image/png", 0, 0


def save_generated_bytes(
    raw: bytes,
    *,
    extension: str,
    subdir_name: str,
) -> Path:
    """Persist generated image/clip bytes to data/studio/<subdir>/<uuid>.ext and return the path."""
    subdir = settings.data_path / "studio" / subdir_name
    subdir.mkdir(parents=True, exist_ok=True)
    path = subdir / f"{uuid.uuid4().hex}.{extension.lstrip('.')}"
    path.write_bytes(raw)
    # Best-effort S3 mirror — see save_uploaded_image for the contract.
    from .s3 import mirror_to_s3

    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "webp": "image/webp", "mp4": "video/mp4"}.get(
        extension.lstrip(".").lower(), "application/octet-stream"
    )
    mirror_to_s3(path, content_type=mime)
    return path


def render_subdir(project_id: int) -> Path:
    p = settings.data_path / "renders" / str(project_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def public_url_for_token(token: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/api/public-assets/{token}"


def repair_image_types(db) -> int:
    """Correct assets whose recorded type doesn't match the file's contents.

    Images saved before types were read from the bytes can be JPEG data
    recorded (and served) as image/png; HeyGen rejects those with "Content
    type not match". Only the header of each file is read. The file name is
    left alone: it's referenced elsewhere, and the served type comes from
    the record. Returns how many assets were fixed."""
    from sqlalchemy import or_

    from .. import models

    fixed = 0
    q = db.query(models.Asset).filter(or_(models.Asset.mime_type.like("image/%"),
                                          models.Asset.mime_type.in_(("", "application/octet-stream"))))
    for asset in q:
        path = Path(asset.file_path or "")
        if not path.is_file():
            continue
        try:
            with Image.open(path) as img:
                fmt, size = img.format, img.size
        except Exception:  # noqa: BLE001 — not an image: leave it alone
            continue
        types = _FORMAT_TYPES.get(fmt or "")
        if types is None:
            continue
        changed = False
        if asset.mime_type != types[1]:
            asset.mime_type = types[1]
            changed = True
        if not asset.width or not asset.height:
            asset.width, asset.height = size
            changed = True
        fixed += changed
    if fixed:
        db.commit()
    return fixed

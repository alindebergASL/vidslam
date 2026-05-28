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
    ext = _ext_for_mime(mime_type)
    filename = f"{uuid.uuid4().hex}.{ext}"
    out_path = subdir / filename
    out_path.write_bytes(cleaned)

    return {
        "file_path": str(out_path),
        "original_filename": original_filename,
        "mime_type": mime_type,
        "width": width,
        "height": height,
        "checksum": checksum,
    }


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
    return path


def render_subdir(project_id: int) -> Path:
    p = settings.data_path / "renders" / str(project_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def public_url_for_token(token: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/api/public-assets/{token}"

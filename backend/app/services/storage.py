from __future__ import annotations

import hashlib
import io
import os
import re
import uuid
from pathlib import Path
from typing import BinaryIO, Optional

from PIL import Image, ImageOps, UnidentifiedImageError

from ..config import get_settings

settings = get_settings()


# iPhone photos are HEIC by default. Registering the opener once here lets
# every Image.open on uploaded bytes (Cast, brand logos, ambient pictures)
# read them; they're converted to JPEG before anything else sees them.
try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIF_SUPPORTED = True
except ImportError:  # pragma: no cover — the dependency is required, but never crash on boot
    HEIF_SUPPORTED = False

# Long side cap for uploads; bigger photos are scaled down (Lanczos) first.
MAX_IMAGE_SIDE = 4096
# Decompression-bomb guard, checked from the header before any pixel is
# decoded: a tiny file can claim gigapixels. 100 MP covers every phone camera.
MAX_IMAGE_PIXELS = 100_000_000

HEIF_MIMES = ("image/heic", "image/heif", "image/heic-sequence", "image/heif-sequence")
_HEIF_EXTS = (".heic", ".heif", ".hif")
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", *_HEIF_EXTS)
# What a browser sends when it doesn't know the type (Chrome/Firefox on
# Windows or Android for a .heic file); the name then decides.
_UNKNOWN_MIMES = ("", "application/octet-stream", "binary/octet-stream")


class UploadError(ValueError):
    """A rejected upload. `status_code` is what the API answers with."""

    status_code = 400


class UploadTooLarge(UploadError):
    status_code = 413


class UnsupportedUpload(UploadError):
    # 400 (not 415) keeps the Cast/brand contract existing clients rely on.
    status_code = 400


# Pillow format → (extension, mime type): what a file really is.
_FORMAT_TYPES = {"PNG": ("png", "image/png"), "JPEG": ("jpg", "image/jpeg"),
                 "WEBP": ("webp", "image/webp"), "GIF": ("gif", "image/gif")}

# Formats an upload may be in, and what each is saved as.
# MPO is a JPEG with an MPF block (iPhone HDR/Portrait, many Samsung camera
# photos): its first frame is kept, as JPEG.
_UPLOAD_OUTPUT = {"PNG": "PNG", "JPEG": "JPEG", "WEBP": "WEBP", "HEIF": "JPEG", "AVIF": "JPEG",
                  "MPO": "JPEG"}


def _ext_for_mime(mime: str) -> str:
    return {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/webp": "webp",
    }.get(mime, "bin")


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):.0f} MB"


def too_large_message(size: int, limit: Optional[int] = None) -> str:
    """'That file is 62 MB; the limit is 50 MB.'"""
    limit = settings.max_upload_bytes if limit is None else limit
    shown = _mb(size)
    if shown == _mb(limit):  # just over: whole MB would read "50 MB; the limit is 50 MB"
        shown = f"{size / (1024 * 1024):.1f} MB"
    return f"That file is {shown}; the limit is {_mb(limit)}."


def _describe(content_type: str, fmt: Optional[str] = None) -> str:
    """A short, safe name for what a rejected file is (never its contents)."""
    if fmt:
        return f"a {fmt} image"
    ct = re.sub(r"[^a-zA-Z0-9.+/_-]", "", (content_type or "").split(";")[0].strip())[:60]
    return ct or "an unknown file type"


def unsupported_message(content_type: str, fmt: Optional[str] = None) -> str:
    return f"HEIC/PNG/JPEG/WebP only — this looks like {_describe(content_type, fmt)}."


def is_heif_name(filename: str) -> bool:
    return (filename or "").lower().endswith(_HEIF_EXTS)


def accepted_upload_type(content_type: str, filename: str) -> bool:
    """Whether the browser-declared type (or, for an unknown type, the file
    name) is one we take. The bytes are still checked when decoding."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in settings.allowed_image_mimes and ct not in _UNKNOWN_MIMES:
        return True
    if ct in HEIF_MIMES:
        return True
    return ct in _UNKNOWN_MIMES and (filename or "").lower().endswith(_IMAGE_EXTS)


def check_upload_type(content_type: str, filename: str) -> None:
    if not accepted_upload_type(content_type, filename):
        raise UnsupportedUpload(unsupported_message(content_type))


def check_upload_size(size: int) -> None:
    if size > settings.max_upload_bytes:
        raise UploadTooLarge(too_large_message(size))


def _upload_size(file: BinaryIO) -> Optional[int]:
    try:
        pos = file.tell()
        file.seek(0, io.SEEK_END)
        size = file.tell()
        file.seek(pos)
        return size - pos
    except (AttributeError, OSError, ValueError):
        return None


def read_upload(file: BinaryIO) -> bytes:
    """Read an uploaded file, refusing (413) before reading one over the
    limit when its size is known (Starlette spools uploads to a seekable
    temp file)."""
    size = _upload_size(file)
    if size is not None:
        check_upload_size(size)
    raw = file.read(settings.max_upload_bytes + 1)
    if len(raw) > settings.max_upload_bytes:
        check_upload_size(size if size and size > len(raw) else len(raw))
    return raw


def open_image(raw: bytes) -> "Image.Image":
    """Open uploaded bytes with the decompression-bomb guard applied from the
    header (no pixels decoded yet). Raises UploadTooLarge / UnsupportedUpload."""
    try:
        img = Image.open(io.BytesIO(raw))
    except Image.DecompressionBombError as e:
        raise UploadTooLarge("That image has too many pixels to process safely.") from e
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as e:
        raise UnsupportedUpload("File is not a recognizable image. " + unsupported_message("")) from e
    w, h = img.size
    if w * h > MAX_IMAGE_PIXELS:
        img.close()
        raise UploadTooLarge(f"That image is {w}×{h} px; the limit is "
                             f"{MAX_IMAGE_PIXELS // 1_000_000} megapixels.")
    return img


def _downscaled(img: "Image.Image") -> "Image.Image":
    if max(img.size) > MAX_IMAGE_SIDE:
        img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE), Image.LANCZOS)
    return img


def normalize_uploaded_image(raw: bytes) -> tuple[bytes, str, str, int, int]:
    """Decode uploaded bytes and re-encode them clean: EXIF stripped (after
    applying its rotation), HEIC/HEIF converted to JPEG (quality 90), long
    side capped at MAX_IMAGE_SIDE. Returns (bytes, ext, mime, width, height)."""
    img = open_image(raw)
    try:
        fmt = img.format or ""
        out_fmt = _UPLOAD_OUTPUT.get(fmt)
        if out_fmt is None:
            raise UnsupportedUpload(unsupported_message("", fmt))
        if fmt in ("JPEG", "MPO"):
            # Decode a huge JPEG at reduced scale instead of in full.
            img.draft("RGB", (MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        try:
            img.load()
            img = ImageOps.exif_transpose(img)
        except Image.DecompressionBombError as e:
            raise UploadTooLarge("That image has too many pixels to process safely.") from e
        except (OSError, ValueError, SyntaxError) as e:
            raise UnsupportedUpload("File is not a recognizable image. "
                                    + unsupported_message("", fmt)) from e
        img = _downscaled(img)
        save_kwargs: dict = {}
        if out_fmt == "JPEG":
            save_kwargs = {"quality": 90, "optimize": True}
            if img.mode != "RGB":
                img = img.convert("RGB")
        elif out_fmt == "WEBP":
            save_kwargs["quality"] = 92
        buf = io.BytesIO()
        # A fresh save carries no EXIF/XMP: Pillow only writes what it's given.
        img.save(buf, format=out_fmt, **save_kwargs)
        ext, mime = _FORMAT_TYPES[out_fmt]
        return buf.getvalue(), ext, mime, img.size[0], img.size[1]
    finally:
        img.close()


def save_uploaded_image(
    file: BinaryIO,
    *,
    original_filename: str,
    mime_type: str,
    owner_kind: str,
    owner_id: int,
) -> dict:
    """Validate + strip EXIF + persist; return metadata dict.

    Takes PNG, JPEG, WebP and HEIC/HEIF (also when the browser sent no type
    or application/octet-stream for a .heic name). HEIC is saved as JPEG."""
    check_upload_type(mime_type, original_filename)
    raw = read_upload(file)
    cleaned, ext, mime_type, width, height = normalize_uploaded_image(raw)

    checksum = hashlib.sha256(cleaned).hexdigest()
    subdir = settings.data_path / "uploads" / owner_kind / str(owner_id)
    subdir.mkdir(parents=True, exist_ok=True)
    # Name and type the file by what it is, not what the browser claimed
    # (a JPEG sent as image/png would be served as PNG, and providers such
    # as HeyGen reject the mismatch).
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


# Pictures sent to providers as references are at most this long (a 4×
# upscale saved to the Cast would otherwise go out at full size).
REFERENCE_LONG_SIDE = 2048
_REF_FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}


# Reference copies not used for this long are swept (see prune_reference_cache).
REFERENCE_CACHE_MAX_AGE_SECONDS = 3 * 24 * 3600


def _reference_cache_dir() -> Path:
    return settings.data_path / "cache" / "references"


def _reference_cache_path(path: Path, long_side: int = REFERENCE_LONG_SIDE) -> Path:
    """Where the scaled-down copy of `path` (as it is now) is cached."""
    st = path.stat()
    key = hashlib.sha256(f"{path.resolve()}:{st.st_mtime_ns}:{st.st_size}:{long_side}".encode()).hexdigest()[:32]
    return _reference_cache_dir() / f"{key}{path.suffix.lower()}"


def drop_reference_copy(path: Path) -> None:
    """Delete the cached reference copy of `path` (call before the picture
    itself is deleted: the cache key needs the file's size and time)."""
    try:
        Path(_reference_cache_path(Path(path))).unlink(missing_ok=True)
    except OSError:
        pass


def prune_reference_cache(max_age_seconds: float = REFERENCE_CACHE_MAX_AGE_SECONDS,
                          now: Optional[float] = None) -> int:
    """Delete reference copies not used for `max_age_seconds` (a copy is
    made again when it's needed). Returns how many were deleted."""
    import time

    cutoff = (now if now is not None else time.time()) - max_age_seconds
    n = 0
    d = _reference_cache_dir()
    if not d.is_dir():
        return 0
    for f in d.iterdir():
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                n += 1
        except OSError:
            continue
    return n


def reference_copy(path: Path, long_side: int = REFERENCE_LONG_SIDE) -> Path:
    """`path` itself when its long side is at most `long_side`, else a
    scaled-down copy (cached by the file's path, size and time; the stored
    picture is never changed). Unreadable files are answered as they are."""
    path = Path(path)
    fmt = _REF_FORMATS.get(path.suffix.lower())
    if fmt is None:
        return path
    try:
        with Image.open(path) as img:
            w, h = img.size
    except Exception:  # noqa: BLE001 — not a picture we can read: send it as is
        return path
    if max(w, h) <= long_side:
        return path
    out = _reference_cache_path(path, long_side)
    if out.exists():
        try:
            os.utime(out)  # still in use: the age sweep keeps it
        except OSError:
            pass
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(path) as img:
        img.load()
        small = img.copy()
    small.thumbnail((long_side, long_side), Image.LANCZOS)
    kwargs: dict = {}
    if fmt == "JPEG":
        small = small.convert("RGB")
        kwargs = {"quality": 92}
    elif fmt == "WEBP":
        kwargs = {"quality": 92}
    tmp = out.with_name(f"{out.stem}.{uuid.uuid4().hex}.tmp")
    small.save(tmp, format=fmt, **kwargs)
    tmp.replace(out)
    return out


def image_size(path: Path) -> tuple[int, int]:
    """(width, height) from the file's header; (0, 0) when unreadable."""
    try:
        with Image.open(path) as img:
            return img.size
    except Exception:  # noqa: BLE001
        return 0, 0


def render_subdir(project_id: int) -> Path:
    p = settings.data_path / "renders" / str(project_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def public_url_for_token(token: str) -> str:
    """The URL a provider fetches a Cast picture from: `?ref=1` makes a big
    picture go out as its scaled-down reference copy (the app's own views use
    the plain URL and get the full-size picture)."""
    return f"{settings.public_base_url.rstrip('/')}/api/public-assets/{token}?ref=1"


def repair_image_types(db, became_pictures: "set[int] | None" = None) -> tuple[int, int]:
    """Correct assets whose recorded type doesn't match the file's contents.

    Images saved before types were read from the bytes can be JPEG data
    recorded (and served) as image/png; HeyGen rejects those with "Content
    type not match". Only the header of each file is read. The file name is
    left alone: it's referenced elsewhere, and the served type comes from
    the record. Returns (types corrected, sizes filled in); the ids of the
    Cast characters with a file that only now counts as a picture go into
    `became_pictures` (their tick's picture check: cast_pictures)."""
    from sqlalchemy import or_

    from .. import models

    types_fixed = sizes_filled = 0
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
        if asset.mime_type != types[1]:
            if (became_pictures is not None and asset.owner_kind == "avatar" and asset.avatar_id is not None
                    and not (asset.mime_type or "").startswith("image/")):
                became_pictures.add(int(asset.avatar_id))
            asset.mime_type = types[1]
            types_fixed += 1
        if not asset.width or not asset.height:
            asset.width, asset.height = size
            sizes_filled += 1
    if types_fixed or sizes_filled:
        db.commit()
    return types_fixed, sizes_filled

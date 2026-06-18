"""S3 storage adapter for asset delivery.

When `s3_bucket` is configured, uploaded asset bytes are pushed to S3 in
addition to the local filesystem, and `public_url_for_token()` returns a
time-limited presigned URL pointing at the S3 object instead of the
app-served route. Reads from disk still work locally; S3 is the
public-delivery channel that off-loads bandwidth and lets a CDN front it.

Boto3 is an optional dependency — if the import fails (e.g. on a
local-fs-only deployment), the S3 mirror silently no-ops and the local
URL path is used as a fallback. This module never raises into the upload
hot path; an S3 outage degrades to local-fs behavior, it does not break
uploads.

This adapter is **untested against a real bucket in this checkout** —
verify in a staging environment before flipping on. The expected
verification commands are documented in scripts/test_real_providers.md.
"""
from __future__ import annotations

import logging
from pathlib import Path

from ..config import get_settings

log = logging.getLogger("avs.s3")


def _client_or_none():
    """Lazy-import boto3 so the local-fs path doesn't need it installed."""
    s = get_settings()
    if not s.s3_bucket:
        return None
    try:
        import boto3  # noqa: WPS433
    except ImportError:
        log.warning("s3_bucket is set but boto3 is not installed — falling back to local-fs delivery")
        return None
    try:
        return boto3.client("s3", region_name=s.s3_region)
    except Exception as e:  # noqa: BLE001
        log.warning("S3 client init failed: %s — falling back to local-fs delivery", e)
        return None


def _s3_key_for(local_path: Path) -> str:
    """Map a local file path under DATA_DIR onto an S3 key under s3_prefix.
    Preserves the data/uploads/<kind>/<id>/<uuid>.<ext> shape so a single
    bucket can mirror multiple environments."""
    s = get_settings()
    try:
        rel = local_path.relative_to(s.data_path)
    except ValueError:
        # The file isn't under DATA_DIR — use just the filename so we still
        # produce a valid key rather than refuse to mirror.
        rel = Path(local_path.name)
    prefix = s.s3_prefix.strip("/")
    return f"{prefix}/{rel.as_posix()}" if prefix else rel.as_posix()


def mirror_to_s3(local_path: Path, content_type: str) -> bool:
    """Best-effort push of a freshly-written local file up to S3. Returns
    True on success, False on any failure (logged, never raised)."""
    client = _client_or_none()
    if client is None:
        return False
    s = get_settings()
    key = _s3_key_for(local_path)
    try:
        with local_path.open("rb") as fh:
            client.put_object(
                Bucket=s.s3_bucket,
                Key=key,
                Body=fh,
                ContentType=content_type,
                # Public delivery URLs are presigned + time-limited; the
                # object itself is private (default).
            )
    except Exception as e:  # noqa: BLE001
        log.warning("S3 mirror failed for %s: %s", key, e)
        return False
    return True


def presigned_url_for(local_path: Path, content_type: str = "application/octet-stream") -> str | None:
    """Return a time-limited GET URL pointing at the S3 mirror of this
    local file, or None when S3 isn't configured or the URL can't be
    minted. Callers fall back to the app-served URL when this returns
    None."""
    client = _client_or_none()
    if client is None:
        return None
    s = get_settings()
    key = _s3_key_for(local_path)
    try:
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": s.s3_bucket, "Key": key, "ResponseContentType": content_type},
            ExpiresIn=s.s3_presign_ttl_seconds,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("S3 presign failed for %s: %s", key, e)
        return None

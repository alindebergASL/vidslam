"""Random-token public URLs for generated media that providers must fetch
(voiceover audio for talking-head models, chained-frame images, …).

Same model as cast assets: unguessable `secrets.token_urlsafe(32)` names,
served unauthenticated from /api/public-media/{token}.{ext}. PUBLIC_BASE_URL
must be reachable by the provider."""
from __future__ import annotations

import re
import secrets
import shutil
from pathlib import Path
from typing import Optional

from ..config import get_settings

_NAME_RE = re.compile(r"([A-Za-z0-9_-]{32,64})\.(png|jpg|mp3|m4a|wav|mp4)")
MIME = {"png": "image/png", "jpg": "image/jpeg", "mp3": "audio/mpeg",
        "m4a": "audio/mp4", "wav": "audio/wav", "mp4": "video/mp4"}


def _dir() -> Path:
    d = get_settings().data_path / "public_media"
    d.mkdir(parents=True, exist_ok=True)
    return d


def publish(src: Path, ext: Optional[str] = None) -> str:
    ext = (ext or src.suffix.lstrip(".")).lower()
    if ext not in MIME:
        raise ValueError(f"unsupported media type {ext!r}")
    name = f"{secrets.token_urlsafe(32)}.{ext}"
    shutil.copyfile(src, _dir() / name)
    return f"{get_settings().public_base_url.rstrip('/')}/api/public-media/{name}"


def resolve(name: str) -> Optional[tuple[Path, str]]:
    m = _NAME_RE.fullmatch(name or "")
    if not m:
        return None
    p = _dir() / name
    return (p, MIME[m.group(2)]) if p.exists() else None

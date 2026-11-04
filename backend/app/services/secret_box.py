"""Encrypt small secrets at rest (e.g. the YouTube refresh token) with a
key derived from SESSION_SECRET. Rotating SESSION_SECRET invalidates them,
which forces a reconnect, the same as it logs everyone out."""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from ..config import get_settings


def _fernet() -> Fernet:
    digest = hashlib.sha256(("avs-secret-box:" + get_settings().session_secret).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def seal(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def open_(token: str) -> str | None:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None

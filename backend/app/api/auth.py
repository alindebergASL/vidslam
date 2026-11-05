from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel

from ..config import get_settings

router = APIRouter()
settings = get_settings()
_serializer = URLSafeTimedSerializer(settings.session_secret, salt="avs-session")
COOKIE_NAME = "avs_session"
SESSION_SECONDS = 60 * 60 * 24 * 7


def _secure_cookie() -> bool:
    """Send the session cookie over HTTPS only when the app is served over
    HTTPS (production). Plain-http dev on localhost still works."""
    return get_settings().public_base_url.lower().startswith("https://")


def _is_authed(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return False
    try:
        # The signature carries its issue time: tokens expire server-side
        # too, not only when the browser drops the cookie.
        payload = _serializer.loads(token, max_age=SESSION_SECONDS)
        return bool(payload.get("ok"))
    except BadSignature:
        return False


def require_auth(request: Request) -> None:
    if not _is_authed(request):
        raise HTTPException(status_code=401, detail="login required")


class LoginIn(BaseModel):
    password: str


@router.post("/auth/login")
def login(body: LoginIn, response: Response) -> dict:
    if body.password != settings.mvp_password:
        raise HTTPException(status_code=401, detail="invalid password")
    token = _serializer.dumps({"ok": True})
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_SECONDS,
        secure=_secure_cookie(),
    )
    return {"ok": True}


@router.post("/auth/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(COOKIE_NAME, httponly=True, samesite="lax", secure=_secure_cookie())
    return {"ok": True}


@router.get("/auth/status")
def auth_status(request: Request) -> dict:
    return {"authenticated": _is_authed(request)}

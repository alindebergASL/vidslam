from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeSerializer
from pydantic import BaseModel

from ..config import get_settings

router = APIRouter()
settings = get_settings()
_serializer = URLSafeSerializer(settings.session_secret, salt="avs-session")
COOKIE_NAME = "avs_session"


def _is_authed(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return False
    try:
        payload = _serializer.loads(token)
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
        max_age=60 * 60 * 24 * 7,
        secure=False,
    )
    return {"ok": True}


@router.post("/auth/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@router.get("/auth/status")
def auth_status(request: Request) -> dict:
    return {"authenticated": _is_authed(request)}

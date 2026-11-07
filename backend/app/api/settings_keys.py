"""Settings → API keys: write-only provider keys.

GET reports presence/source/last4 per key, never a value. PUT stores a key
(encrypted), DELETE removes the app-stored copy (the .env value applies
again), and POST …/test makes one cheap authenticated call with the key the
app would use. Request bodies are parsed by hand so a validation error can
never echo the submitted key (FastAPI's default 422 includes `input`).
"""
from __future__ import annotations

import json
import logging
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from ..config import get_settings
from ..services import provider_keys
from ..services.ratelimit import rate_limit
from .auth import require_auth

log = logging.getLogger("avs.settings_keys")

router = APIRouter(dependencies=[Depends(require_auth)])

USER_AGENT = "vidslam/1.0"
_PROBE_TIMEOUT = 15.0


def _known(name: str) -> str:
    if name not in provider_keys.KEYS:
        raise HTTPException(404, "unknown provider key")
    return name


@router.get("")
def list_keys() -> dict:
    return {"mock_providers": get_settings().mock_providers, "keys": provider_keys.status()}


@router.put("/{name}")
async def put_key(name: str, request: Request) -> dict:
    _known(name)
    raw = await request.body()
    try:
        body = json.loads(raw or b"null")
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(422, "body must be JSON: {\"value\": \"<key>\"}") from None
    if not isinstance(body, dict) or "value" not in body:
        raise HTTPException(422, "body must be JSON: {\"value\": \"<key>\"}")
    try:
        value = provider_keys.validate_value(body.get("value"))
    except ValueError as e:
        # The message is ours and never contains the value.
        raise HTTPException(422, str(e)) from None
    return provider_keys.set_key(name, value)


@router.delete("/{name}")
def delete_key(name: str) -> dict:
    _known(name)
    return provider_keys.clear_key(name)


def _probe_request(name: str, key: str) -> tuple[str, dict, dict]:
    s = get_settings()
    if name == "openrouter":
        return (f"{s.openrouter_base_url.rstrip('/')}/key",
                {"Authorization": f"Bearer {key}"}, {})
    if name == "elevenlabs":
        return (f"{s.elevenlabs_base_url.rstrip('/')}/user", {"xi-api-key": key}, {})
    if name == "gemini":
        return (f"{s.gemini_base_url.rstrip('/')}/models", {"x-goog-api-key": key}, {"pageSize": 1})
    if name == "replicate":
        return (f"{s.replicate_base_url.rstrip('/')}/account", {"Authorization": f"Bearer {key}"}, {})
    if name == "sogni":
        return (f"{s.sogni_base_url.rstrip('/')}/v2/account/me",
                {"Authorization": f"Bearer {key}", "X-App-Source": "vidslam"}, {})
    raise HTTPException(404, "unknown provider key")


def _account_name(name: str, r: httpx.Response) -> str:
    """The account a working key belongs to, where the probe returns it
    (Sogni: data.username). Never the key."""
    if name != "sogni":
        return ""
    try:
        data = (r.json() or {}).get("data") or {}
    except (ValueError, AttributeError):
        return ""
    who = str(data.get("username") or "").strip() if isinstance(data, dict) else ""
    return who[:60]


def _provider_message(r: httpx.Response) -> str:
    """A short human message from the provider's error body, if any."""
    try:
        body = r.json()
    except ValueError:
        return ""
    msg = ""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("status") or ""
        elif isinstance(err, str):
            msg = err
        det = body.get("detail")
        if not msg and isinstance(det, dict):
            msg = det.get("message") or det.get("status") or ""
        elif not msg and isinstance(det, str):
            msg = det
        msg = msg or body.get("message") or ""
        if body.get("errorCode") is not None and msg:
            msg = f"{body['errorCode']}: {msg}"
    return str(msg)


def _scrub(text: str, key: str) -> str:
    text = " ".join(str(text).split())
    if key and key in text:
        text = text.replace(key, "…")
    return text[:200]


@router.post("/{name}/test", dependencies=[Depends(rate_limit("generation"))])
def test_key(name: str) -> dict:
    _known(name)
    key = provider_keys.resolve(name)
    if not key:
        return {"ok": False, "detail": "No key set."}
    url, headers, params = _probe_request(name, key)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json", **headers}
    t0 = time.monotonic()
    try:
        # Never follow redirects: httpx strips only Authorization on a
        # cross-origin hop, so xi-api-key / x-goog-api-key would leak. A 3xx
        # falls through to "Unexpected response" below.
        with httpx.Client(timeout=_PROBE_TIMEOUT, follow_redirects=False) as client:
            r = client.get(url, headers=headers, params=params)
    except httpx.HTTPError as e:
        # Exception text carries the URL at most, never headers.
        return {"ok": False, "detail": _scrub(f"Couldn't reach the provider: {type(e).__name__}", key)}
    ms = int((time.monotonic() - t0) * 1000)
    if r.status_code == 200:
        # Sogni worker keys pass account routes but get 403 on paid work.
        warning = provider_keys.key_warning(name, key)
        if warning:
            return {"ok": False, "detail": warning}
        who = _account_name(name, r)
        return {"ok": True, "detail": f"Key works{f' — account {who}' if who else ''} (HTTP 200, {ms} ms)."}
    msg = _provider_message(r)
    if r.status_code in (401, 403):
        detail = f"Key rejected (HTTP {r.status_code})"
    else:
        detail = f"Unexpected response (HTTP {r.status_code})"
    if msg:
        detail += f": {msg}"
    return {"ok": False, "detail": _scrub(detail, key)}

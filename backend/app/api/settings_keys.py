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
import uuid
from typing import Optional

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


def _bool_or_null(v: object) -> bool:
    """True for a real JSON true/false or null. Numbers such as 0 and 1 are
    refused: Python treats 1 as equal to True, which would let them through."""
    return v is None or type(v) is bool


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


@router.put("/{name}/enabled")
async def put_enabled(name: str, request: Request) -> dict:
    """Switch a provider on/off: {"enabled": true | false | null} (null =
    back to its default). Off hides its models from every picker and refuses
    new work that needs it (409); work already running finishes."""
    _known(name)
    try:
        body = json.loads((await request.body()) or b"null")
    except (ValueError, UnicodeDecodeError):
        body = None
    if not isinstance(body, dict) or "enabled" not in body or not _bool_or_null(body["enabled"]):
        raise HTTPException(422, 'body must be JSON: {"enabled": true | false | null}')
    row = provider_keys.set_enabled(name, body["enabled"])
    row["in_use"] = _in_use(name) if row["enabled"] is False else []
    return row


@router.put("/{name}/options")
async def put_options(name: str, request: Request) -> dict:
    """Set a provider's options: Runware {"check_content": …} or Sogni
    {"safe_filter": …}, each true | false | null (null = back to its
    default). They apply while adult mode is on; off, both are always on."""
    _known(name)
    allowed = dict(provider_keys.KEYS[name].options)
    try:
        body = json.loads((await request.body()) or b"null")
    except (ValueError, UnicodeDecodeError):
        body = None
    if not isinstance(body, dict) or not body or any(
            k not in allowed or not _bool_or_null(v) for k, v in body.items()):
        raise HTTPException(422, f"body must be JSON with options {sorted(allowed) or '(none)'}: true | false | null")
    if body.get("check_content") is False or body.get("safe_filter") is False:
        from ..services import content_mode

        if not content_mode.features():
            raise HTTPException(403, "Adult content is turned off for this deployment.")
    row: dict = {}
    for k, v in body.items():
        row = provider_keys.set_option(name, k, v)
    return row


def _in_use(name: str) -> list[str]:
    """The active model kinds (Settings → Models) this provider serves now
    that it's off, so the UI can say what stops working."""
    from ..services import model_registry as reg

    out = []
    for kind in reg.KINDS:
        provider, blocked = reg.route_info(kind)
        if provider == "unavailable" and blocked == name:
            out.append(kind)
    return out


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
    if name == "runware":
        return (s.runware_base_url.rstrip("/"), {"Authorization": f"Bearer {key}"}, {})
    raise HTTPException(404, "unknown provider key")


def _probe_body(name: str) -> Optional[list]:
    """A POST body for providers whose only endpoint is POST (Runware: the
    free account-details task)."""
    if name == "runware":
        return [{"taskType": "accountManagement", "operation": "getDetails", "taskUUID": str(uuid.uuid4())}]
    return None


def _account_name(name: str, r: httpx.Response) -> str:
    """The account a working key belongs to, where the probe returns it
    (Sogni: data.username; Runware: the team and its balance). Never the key."""
    if name == "runware":
        return _runware_account(r)
    if name != "sogni":
        return ""
    try:
        data = (r.json() or {}).get("data") or {}
    except (ValueError, AttributeError):
        return ""
    who = str(data.get("username") or "").strip() if isinstance(data, dict) else ""
    return who[:60]


def _runware_account(r: httpx.Response) -> str:
    try:
        rows = (r.json() or {}).get("data") or []
    except (ValueError, AttributeError):
        return ""
    row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
    team = row.get("teamName") or row.get("organizationName") or row.get("name") or ""
    if not team and isinstance(row.get("team"), dict):
        team = row["team"].get("name") or ""
    bal = row.get("balance")
    if bal is None and isinstance(row.get("team"), dict):
        bal = row["team"].get("balance")
    bits = [str(team).strip()[:60]] if team else []
    if isinstance(bal, (int, float)):
        bits.append(f"balance ${bal:.2f}")
    return ", ".join(bits)


def _provider_message(r: httpx.Response) -> str:
    """A short human message from the provider's error body, if any."""
    try:
        body = r.json()
    except ValueError:
        return ""
    msg = ""
    if isinstance(body, dict) and isinstance(body.get("errors"), list) and body["errors"]:
        first = body["errors"][0]  # Runware: {"errors": [{"code", "message"}]}
        if isinstance(first, dict):
            msg = str(first.get("message") or first.get("code") or "")
    if isinstance(body, dict) and not msg:
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
            body = _probe_body(name)
            if body is not None:
                r = client.post(url, headers=headers, json=body)
            else:
                r = client.get(url, headers=headers, params=params)
    except httpx.HTTPError as e:
        # Exception text carries the URL at most, never headers.
        return {"ok": False, "detail": _scrub(f"Couldn't reach the provider: {type(e).__name__}", key)}
    ms = int((time.monotonic() - t0) * 1000)
    if r.status_code == 200 and name == "runware" and _provider_message(r):
        # Runware can answer 200 with an errors[] body.
        return {"ok": False, "detail": _scrub(f"Key rejected: {_provider_message(r)}", key)}
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

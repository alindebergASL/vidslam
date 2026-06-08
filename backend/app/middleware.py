"""Request-id + structured-logging + JSON-error middleware.

Every request gets a unique X-Request-ID (or the one a caller / load balancer
already attached, if any). The id is echoed on the response and stitched into
the access-log line, so operators can trace a single user action through the
logs of a misbehaving deploy. Uncaught exceptions become a single structured
log line + a JSON `{detail, request_id}` response — never an opaque
'Internal Server Error' without context.
"""
from __future__ import annotations

import logging
import time
import uuid
from contextvars import ContextVar

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

log = logging.getLogger("avs.access")

# ContextVar so handlers can fetch the active id (for log enrichment) without
# threading a Request object through every call.
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")

_HEADER = "X-Request-ID"


def current_request_id() -> str:
    return request_id_ctx.get()


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Attach a request id, emit an access-log line, and translate uncaught
    exceptions into a structured JSON 500 with the same id."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        # Honour an inbound X-Request-ID (load balancer / client supplied) when
        # it looks reasonable; otherwise mint a fresh one.
        incoming = request.headers.get(_HEADER, "")
        rid = incoming if 8 <= len(incoming) <= 64 and incoming.isascii() else uuid.uuid4().hex
        token = request_id_ctx.set(rid)
        start = time.monotonic()
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001
            duration_ms = int((time.monotonic() - start) * 1000)
            log.exception(
                "request_failed method=%s path=%s status=500 duration_ms=%d request_id=%s",
                request.method, request.url.path, duration_ms, rid,
            )
            response = JSONResponse(
                {
                    "detail": "Internal Server Error",
                    "request_id": rid,
                    "error_type": type(exc).__name__,
                },
                status_code=500,
            )
            response.headers[_HEADER] = rid
            request_id_ctx.reset(token)
            return response

        duration_ms = int((time.monotonic() - start) * 1000)
        response.headers[_HEADER] = rid
        # One structured line per request. Skip the noisy probes by default so
        # the access log stays signal-dense in production.
        if request.url.path not in ("/health", "/healthz", "/readyz"):
            log.info(
                "method=%s path=%s status=%d duration_ms=%d request_id=%s",
                request.method, request.url.path, response.status_code, duration_ms, rid,
            )
        request_id_ctx.reset(token)
        return response


def install(app: FastAPI) -> None:
    app.add_middleware(RequestIdMiddleware)

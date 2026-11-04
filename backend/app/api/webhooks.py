"""Inbound provider webhooks (unauthenticated — each URL carries a random
per-job token, and deliveries are HMAC-verified when a signing secret is
configured).

POST /api/webhooks/openrouter/videos/{token}
    OpenRouter calls this when a video job reaches a terminal state
    (video.generation.completed | failed | cancelled | expired). We verify,
    dedupe (terminal jobs are no-ops, so retries are safe) and enqueue the
    download / next step — the response goes back immediately.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..services import video_jobs

router = APIRouter()


@router.post("/webhooks/openrouter/videos/{token}")
async def openrouter_video_webhook(token: str, request: Request, db: Session = Depends(get_db)) -> Response:
    raw = await request.body()
    try:
        video_jobs.handle_webhook(db, token, raw, request.headers.get("X-OpenRouter-Signature"))
    except video_jobs.WebhookRejected as e:
        return JSONResponse({"detail": str(e)}, status_code=e.status)
    return Response(status_code=204)

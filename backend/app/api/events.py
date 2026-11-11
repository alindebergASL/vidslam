"""Live progress: a Server-Sent Events stream of the events table, the
activity panel's recent list, read state, and what is running now."""
from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func
from starlette.concurrency import run_in_threadpool

from .. import models
from ..db import SessionLocal, get_db
from ..services import events as svc
from .auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])

# How often the stream looks for new rows, and how often it sends a comment
# so proxies (nginx proxy_read_timeout 600 s) keep the connection open.
POLL_SECONDS = 1.5
HEARTBEAT_SECONDS = 20.0
RETRY_MS = 5000
BATCH = 100
# An id below the newest one sent that hasn't shown up yet may belong to a
# transaction that is still open (Postgres hands out ids at insert, not at
# commit): the stream waits this long for it before moving past.
GAP_WAIT_SECONDS = 10.0
LATE_LIMIT = 1000
# Tests bound the stream so TestClient returns; honoured only under pytest.
ALLOW_MAX_SECONDS = "pytest" in sys.modules
# Unread events that count toward the badge: finished and failed work.
_BADGE_STATUSES = ("completed", "failed")


def _sse(data: dict | str, *, event: Optional[str] = None, eid: Optional[int] = None) -> str:
    lines = []
    if eid is not None:
        lines.append(f"id: {eid}")
    if event:
        lines.append(f"event: {event}")
    payload = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))
    lines += [f"data: {line}" for line in payload.splitlines() or [""]]
    return "\n".join(lines) + "\n\n"


def _fetch_after(last: int, high: Optional[int] = None) -> list[dict]:
    """Rows after `high` (at most BATCH), plus rows between `last` and
    `high` that may have been committed late, in id order."""
    high = last if high is None else max(high, last)
    with SessionLocal() as db:
        E = models.Event
        late = []
        if high > last:
            late = (db.query(E).filter(E.id > last, E.id <= high)
                    .order_by(E.id).limit(LATE_LIMIT).all())
        rows = db.query(E).filter(E.id > high).order_by(E.id).limit(BATCH).all()
        return [svc.out(r) for r in late] + [svc.out(r) for r in rows]


class Cursor:
    """Where a stream is: every id up to `last` was sent (or given up on
    after GAP_WAIT_SECONDS); ids above it that were sent are in `sent`.
    `last` is what a reconnect resumes from (the SSE `id:`), so an event
    committed out of id order is still delivered, at worst twice (clients
    de-duplicate on the event id)."""

    def __init__(self, last: int):
        self.last = last
        self.high = last
        self.sent: set[int] = set()
        self.gaps: dict[int, float] = {}

    def take(self, row: dict, now: float) -> bool:
        """Whether to send `row` (not sent before); moves the cursor."""
        i = int(row["id"])
        if i <= self.last or i in self.sent:
            return False
        self.sent.add(i)
        self.high = max(self.high, i)
        self.advance(now)
        return True

    def advance(self, now: float) -> None:
        for i in range(self.last + 1, self.high):
            if i not in self.sent:
                self.gaps.setdefault(i, now)
        while self.last < self.high:
            nxt = self.last + 1
            if nxt in self.sent:
                self.sent.discard(nxt)
            elif now - self.gaps.get(nxt, now) < GAP_WAIT_SECONDS:
                break
            self.gaps.pop(nxt, None)
            self.last = nxt


def _bounds() -> tuple[int, int]:
    with SessionLocal() as db:
        lo, hi = db.query(func.min(models.Event.id), func.max(models.Event.id)).one()
        return int(lo or 0), int(hi or 0)


def _snapshot() -> dict:
    with SessionLocal() as db:
        return {"latest_id": svc.latest_id(db), "active": svc.active(db)}


def _parse_id(raw: Optional[str]) -> Optional[int]:
    text = (raw or "").strip()
    return int(text) if text.isdigit() else None


@router.get("/events/stream")
async def stream(
    request: Request,
    after: Optional[int] = Query(None, ge=0),
    max_seconds: Optional[float] = Query(None, gt=0),
    last_event_id: Optional[str] = Header(None),
) -> StreamingResponse:
    """`id:` is the event id; reconnects resume from `Last-Event-ID` (or
    `?after=`). A fresh connect gets `event: snapshot` (what is running and
    the latest id) first; a resume point older than the oldest kept event
    (or newer than the newest: a reset database) gets `event: reset` and a
    snapshot, so the client refetches. Ordinary events are unnamed
    (`onmessage`), their data the same object as /events/recent rows."""
    resume = _parse_id(last_event_id)
    if resume is None:
        resume = after
    limit = max_seconds if (max_seconds and ALLOW_MAX_SECONDS) else None

    async def gen():
        started = time.monotonic()
        yield f"retry: {RETRY_MS}\n\n"
        last: int
        if resume is None:
            snap = await run_in_threadpool(_snapshot)
            last = snap["latest_id"]
            yield _sse(snap, event="snapshot")
        else:
            lo, hi = await run_in_threadpool(_bounds)
            if (resume > 0 and lo and resume < lo - 1) or resume > hi:
                snap = await run_in_threadpool(_snapshot)
                last = snap["latest_id"]
                yield _sse({"latest_id": last}, event="reset")
                yield _sse(snap, event="snapshot")
            else:
                last = resume
        beat = time.monotonic()
        cursor = Cursor(last)
        while True:
            if await request.is_disconnected():
                return
            rows = await run_in_threadpool(_fetch_after, cursor.last, cursor.high)
            fresh = 0
            for row in rows:
                if cursor.take(row, time.monotonic()):
                    fresh += 1
                    yield _sse(row, eid=cursor.last)
            cursor.advance(time.monotonic())
            now = time.monotonic()
            if now - beat >= HEARTBEAT_SECONDS:
                beat = now
                yield ": keep-alive\n\n"
            if limit is not None and now - started >= limit:
                return
            if fresh < BATCH:
                await asyncio.sleep(POLL_SECONDS if limit is None else min(POLL_SECONDS, limit))

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


def _unread(db) -> int:  # noqa: ANN001
    return (db.query(models.Event.id)
            .filter(models.Event.read_at.is_(None), models.Event.status.in_(_BADGE_STATUSES)).count())


@router.get("/events/recent")
def recent(
    limit: int = Query(50, ge=1, le=200),
    before_id: Optional[int] = None,
    status: Optional[str] = Query(None, description="comma-separated statuses to keep"),
    kind: Optional[str] = Query(None, description="comma-separated kinds to keep"),
    db=Depends(get_db),  # noqa: ANN001
) -> dict:
    """Newest first. `unread` counts unread finished/failed events (the badge)."""
    q = db.query(models.Event)
    if before_id is not None:
        q = q.filter(models.Event.id < before_id)
    if status:
        q = q.filter(models.Event.status.in_([s.strip() for s in status.split(",") if s.strip()]))
    if kind:
        q = q.filter(models.Event.kind.in_([k.strip() for k in kind.split(",") if k.strip()]))
    rows = q.order_by(models.Event.id.desc()).limit(limit + 1).all()
    return {
        "events": [svc.out(r) for r in rows[:limit]],
        "has_more": len(rows) > limit,
        "unread": _unread(db),
        "latest_id": svc.latest_id(db),
    }


class ReadIn(BaseModel):
    ids: list[int] = Field(default_factory=list, max_length=1000)
    all: bool = False


@router.post("/events/read")
def mark_read(body: ReadIn, db=Depends(get_db)) -> dict:  # noqa: ANN001
    q = db.query(models.Event).filter(models.Event.read_at.is_(None))
    if not body.all:
        if not body.ids:
            return {"updated": 0, "unread": _unread(db)}
        q = q.filter(models.Event.id.in_(body.ids))
    n = q.update({models.Event.read_at: datetime.utcnow()}, synchronize_session=False)
    db.commit()
    return {"updated": int(n or 0), "unread": _unread(db)}


@router.get("/activity/active")
def activity(db=Depends(get_db)) -> dict:  # noqa: ANN001
    return svc.active(db)

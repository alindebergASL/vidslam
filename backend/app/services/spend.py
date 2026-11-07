"""Spend ledger: what each provider call actually cost, plus a monthly budget.

Every paid call records one `SpendEntry`. The number comes from the
provider whenever it reports one:

  * OpenRouter chat / image / streamed audio → `usage.cost` on the response
  * OpenRouter video jobs (shots, avatars, ambient, studio) → `usage.cost`
    from the webhook or the completed poll
  * OpenRouter `/audio/speech` → no cost in the response, only an
    `X-Generation-Id` header; the entry is recorded as "pending" with a
    rate-card estimate and `reconcile_pending()` later swaps in the real
    `total_cost` from `GET /generation?id=…`
  * ElevenLabs, Gemini (Lyria) → no per-call cost in the API; recorded from
    the COST_* rate card and flagged `is_estimate`
  * Sogni → no per-job cost in the workflow API; recorded from the Spark
    price table in providers/sogni.py and flagged `is_estimate`

Mock providers never record anything (nothing is billed).

Callers don't pass project/render ids around: workers open a `scope()` and
every entry recorded inside it is attributed to that project / render.
"""
from __future__ import annotations

import contextvars
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Optional

import httpx
from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from ..db import SessionLocal

log = logging.getLogger("avs.spend")

BUDGET_KEY = "spend.budget"
ALERTS_KEY = "spend.alerts"

_scope: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar("spend_scope", default=None)


# ---------- attribution ----------


@contextmanager
def scope(*, project_id: Optional[int] = None, render_id: Optional[int] = None,
          ref: str = "") -> Iterator[None]:
    """Attribute every entry recorded inside the block to this project/render."""
    token = _scope.set({**(_scope.get() or {}), **{k: v for k, v in
                        {"project_id": project_id, "render_id": render_id, "ref": ref}.items() if v}})
    try:
        yield
    finally:
        _scope.reset(token)


def bind(**fields: Any) -> None:
    """Add fields (e.g. the render id once it exists) to the current scope.
    A no-op outside scope(): it must never leak into later work on this
    thread."""
    current = _scope.get()
    if current is not None:
        current.update({k: v for k, v in fields.items() if v})  # scope() made this dict


# ---------- recording ----------


def _month(dt: Optional[datetime] = None) -> str:
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m")


def _month_bounds(month: str) -> tuple[datetime, datetime]:
    y, m = (int(x) for x in month.split("-"))
    start = datetime(y, m, 1)
    end = datetime(y + (m == 12), m % 12 + 1, 1)
    return start, end


def record(*, provider: str, kind: str, model: str = "", cost: float, is_estimate: bool = False,
           source: str = "provider", generation_id: str = "", note: str = "",
           project_id: Optional[int] = None, render_id: Optional[int] = None, ref: str = "",
           db: Optional[Session] = None) -> Optional[int]:
    """Write one ledger entry. Never raises: losing a spend row must not
    fail a render that already succeeded (and was already billed)."""
    ctx = _scope.get() or {}
    own = db is None
    db = db or SessionLocal()
    try:
        entry = models.SpendEntry(
            provider=provider, kind=kind, model=(model or "")[:200],
            project_id=project_id or ctx.get("project_id"),
            render_id=render_id or ctx.get("render_id"),
            ref=(ref or ctx.get("ref") or "")[:80],
            cost_usd=round(max(float(cost or 0.0), 0.0), 6),
            is_estimate=is_estimate, source=source,
            generation_id=(generation_id or "")[:120], note=(note or "")[:300],
        )
        db.add(entry)
        db.commit()
        _check_alerts(db)
        return entry.id
    except Exception:  # noqa: BLE001
        log.exception("could not record spend (%s %s %s)", provider, kind, cost)
        db.rollback()
        return None
    finally:
        if own:
            db.close()


def usage_cost(data: Any) -> Optional[float]:
    """OpenRouter `usage.cost` (USD) from a response or stream chunk."""
    if not isinstance(data, dict):
        return None
    cost = (data.get("usage") or {}).get("cost")
    try:
        return float(cost) if cost is not None else None
    except (TypeError, ValueError):
        return None


def record_openrouter(kind: str, model: str, *, cost: Optional[float], generation_id: str = "",
                      estimate: float = 0.0, note: str = "") -> None:
    """Record an OpenRouter call: the reported cost when there is one, else
    a pending entry (reconciled via the generation id) or a rate-card one."""
    if cost is not None:
        record(provider="openrouter", kind=kind, model=model, cost=cost,
               generation_id=generation_id, note=note)
    elif generation_id:
        record(provider="openrouter", kind=kind, model=model, cost=estimate, is_estimate=True,
               source="pending", generation_id=generation_id, note=note)
    else:
        record(provider="openrouter", kind=kind, model=model, cost=estimate, is_estimate=True,
               source="rate_card", note=note)


def record_rate_card(provider: str, kind: str, model: str, cost: float, note: str = "") -> None:
    record(provider=provider, kind=kind, model=model, cost=cost, is_estimate=True,
           source="rate_card", note=note)


def record_video_job(db: Session, job: models.VideoJob) -> None:
    """Called once when a video job completes (webhook or poll)."""
    if job.provider == "mock":
        return
    kind = "avatar" if job.purpose == "avatar" else "video"
    ref = f"video_job:{job.id}"
    if db.query(models.SpendEntry.id).filter_by(ref=ref).first():
        return  # already recorded (a retried completion)
    if job.provider == "sogni":
        _record_sogni_video(db, job, kind, ref)
        return
    if job.cost_usd is not None:
        record(provider=job.provider, kind=kind, model=job.model, cost=job.cost_usd,
               project_id=job.project_id, render_id=job.render_id, ref=ref,
               generation_id=job.provider_job_id, db=db)
        return
    ctx = job.context_json or {}
    s = get_settings()
    if kind == "avatar":
        # Lip-sync bills the speech it was given, at its own rate.
        secs = float(ctx.get("speech_seconds") or ctx.get("duration") or 5.0)
        rate = s.cost_avatar_per_second
    else:
        secs, rate = float(ctx.get("duration") or 5.0), s.cost_video_per_second
    record(provider=job.provider, kind=kind, model=job.model,
           cost=secs * rate, is_estimate=True, source="rate_card",
           project_id=job.project_id, render_id=job.render_id, ref=ref,
           note="provider reported no cost", db=db)


def _record_sogni_video(db: Session, job: models.VideoJob, kind: str, ref: str) -> None:
    """Sogni reports no per-job cost: the job's cost is our Spark price-table
    estimate (set at poll time), so it's recorded as one, never as billed."""
    from ..providers import sogni

    cost = job.cost_usd
    if cost is None:
        cost = sogni.video_spend_usd(job.model, float((job.context_json or {}).get("duration") or 5.0))
    record(provider="sogni", kind=kind, model=job.model, cost=cost, is_estimate=True,
           source="rate_card", project_id=job.project_id, render_id=job.render_id, ref=ref,
           generation_id=job.provider_job_id, note=sogni.spend_note(job.model, "video"), db=db)


def render_actual_cost(db: Session, render: models.Render) -> float:
    total = db.query(func.coalesce(func.sum(models.SpendEntry.cost_usd), 0.0)) \
        .filter(models.SpendEntry.render_id == render.id).scalar()
    return round(float(total or 0.0), 4)


# How long a pending entry may 404 on GET /generation before we give up.
PENDING_GIVE_UP = timedelta(hours=1)


def reconcile_pending(limit: int = 20) -> int:
    """Swap rate-card placeholders for OpenRouter's reported cost once the
    generation is queryable. Returns how many entries were settled."""
    s = get_settings()
    from . import provider_keys

    key = provider_keys.resolve("openrouter")
    if not key:
        return 0
    db = SessionLocal()
    settled = 0
    try:
        rows = db.query(models.SpendEntry).filter_by(source="pending") \
            .order_by(models.SpendEntry.id).limit(limit).all()
        if not rows:
            return 0
        with httpx.Client(timeout=15.0) as client:
            for e in rows:
                try:
                    r = client.get(f"{s.openrouter_base_url.rstrip('/')}/generation",
                                   params={"id": e.generation_id},
                                   headers={"Authorization": f"Bearer {key}"})
                except httpx.HTTPError:
                    break
                if r.status_code == 404:
                    # Usually "not queryable yet". A generation that errored is
                    # never queryable, though, so stop polling after a while and
                    # keep the rate-card estimate.
                    if e.created_at and datetime.utcnow() - e.created_at > PENDING_GIVE_UP:
                        e.source = "rate_card"
                        e.note = (e.note + " · " if e.note else "") + "generation lookup 404; kept estimate"
                        settled += 1
                    continue
                if r.status_code >= 400:
                    continue
                cost = (r.json().get("data") or {}).get("total_cost")
                if cost is None:
                    continue
                e.cost_usd = round(float(cost), 6)
                e.is_estimate = False
                e.source = "lookup"
                settled += 1
        db.commit()
        if settled:
            _check_alerts(db)
        return settled
    finally:
        db.close()


# ---------- budget ----------


def get_budget(db: Session) -> dict:
    s = get_settings()
    row = db.get(models.AppSetting, BUDGET_KEY)
    v = (row.value if row else None) or {}
    return {
        "monthly_usd": float(v.get("monthly_usd", s.spend_monthly_budget_usd) or 0.0),
        "hard_stop": bool(v.get("hard_stop", s.spend_hard_stop)),
        "alert_pcts": sorted({int(p) for p in v.get("alert_pcts", [80, 100]) if 0 < int(p) <= 1000}),
    }


def set_budget(db: Session, *, monthly_usd: float, hard_stop: bool, alert_pcts: list[int]) -> dict:
    value = {"monthly_usd": max(float(monthly_usd), 0.0), "hard_stop": bool(hard_stop),
             "alert_pcts": sorted({int(p) for p in alert_pcts if 0 < int(p) <= 1000})}
    row = db.get(models.AppSetting, BUDGET_KEY)
    if row:
        row.value = value
    else:
        db.add(models.AppSetting(key=BUDGET_KEY, value=value))
    # A new cap re-arms this month's alerts.
    alerts = db.get(models.AppSetting, ALERTS_KEY)
    if alerts:
        alerts.value = {"month": _month(), "sent": []}
    db.commit()
    return get_budget(db)


def month_total(db: Session, month: Optional[str] = None) -> float:
    start, end = _month_bounds(month or _month())
    total = db.query(func.coalesce(func.sum(models.SpendEntry.cost_usd), 0.0)) \
        .filter(models.SpendEntry.created_at >= start, models.SpendEntry.created_at < end).scalar()
    return round(float(total or 0.0), 4)


def _all_mocked() -> bool:
    s = get_settings()
    return s.mock_providers


def over_budget(db: Session) -> Optional[str]:
    """A message when the hard cap is reached, else None."""
    b = get_budget(db)
    if not b["hard_stop"] or b["monthly_usd"] <= 0 or _all_mocked():
        return None
    spent = month_total(db)
    if spent >= b["monthly_usd"]:
        return (f"Monthly budget of ${b['monthly_usd']:.2f} reached (${spent:.2f} spent this month). "
                "Raise it in Settings → Spend to keep generating.")
    return None


def budget_guard() -> None:
    """FastAPI dependency for endpoints that start paid generation."""
    db = SessionLocal()
    try:
        msg = over_budget(db)
    finally:
        db.close()
    if msg:
        raise HTTPException(status_code=402, detail=msg)


def _check_alerts(db: Session) -> None:
    from . import alerts

    b = get_budget(db)
    if b["monthly_usd"] <= 0 or not b["alert_pcts"]:
        return
    month = _month()
    row = db.get(models.AppSetting, ALERTS_KEY)
    state = (row.value if row else None) or {}
    sent = set(state.get("sent", [])) if state.get("month") == month else set()
    spent = month_total(db, month)
    pct = spent / b["monthly_usd"] * 100
    new = [p for p in b["alert_pcts"] if pct >= p and p not in sent]
    if not new:
        return
    top = max(new)
    alerts.send(
        f"spend:{month}:{top}",
        f"Spend at {pct:.0f}% of the monthly budget",
        f"${spent:.2f} of ${b['monthly_usd']:.2f} spent in {month}."
        + (" New paid generations are blocked until the budget is raised."
           if top >= 100 and b["hard_stop"] else ""),
        severity="error" if top >= 100 else "warning",
    )
    value = {"month": month, "sent": sorted(sent | set(new))}
    if row:
        row.value = value
    else:
        db.add(models.AppSetting(key=ALERTS_KEY, value=value))
    db.commit()


# ---------- reporting ----------


def summary(db: Session, month: Optional[str] = None) -> dict:
    month = month or _month()
    start, end = _month_bounds(month)
    E = models.SpendEntry
    in_month = (E.created_at >= start, E.created_at < end)

    def grouped(col):
        return [{"key": k or "", "cost_usd": round(float(c), 4), "count": n}
                for k, c, n in db.query(col, func.sum(E.cost_usd), func.count(E.id))
                .filter(*in_month).group_by(col).order_by(func.sum(E.cost_usd).desc()).all()]

    total = month_total(db, month)
    estimated = float(db.query(func.coalesce(func.sum(E.cost_usd), 0.0))
                      .filter(*in_month, E.is_estimate.is_(True)).scalar() or 0.0)
    day = func.date(E.created_at)
    daily = [{"date": str(d), "cost_usd": round(float(c), 4)}
             for d, c in db.query(day, func.sum(E.cost_usd)).filter(*in_month).group_by(day).order_by(day).all()]
    projects = []
    for pid, c in (db.query(E.project_id, func.sum(E.cost_usd)).filter(*in_month, E.project_id.isnot(None))
                   .group_by(E.project_id).order_by(func.sum(E.cost_usd).desc()).limit(10).all()):
        p = db.get(models.VideoProject, pid)
        projects.append({"project_id": pid, "title": p.title if p else f"#{pid} (deleted)",
                         "cost_usd": round(float(c), 4)})
    b = get_budget(db)
    return {
        "month": month,
        "currency": "USD",
        "total_usd": total,
        "estimated_usd": round(estimated, 4),
        "pending": db.query(func.count(E.id)).filter(*in_month, E.source == "pending").scalar() or 0,
        "budget": b,
        "budget_pct": round(total / b["monthly_usd"] * 100, 1) if b["monthly_usd"] > 0 else None,
        "blocked": bool(over_budget(db)) if month == _month() else False,
        "mock_mode": _all_mocked(),
        "by_kind": grouped(E.kind),
        "by_provider": grouped(E.provider),
        "by_model": grouped(E.model)[:15],
        "top_projects": projects,
        "daily": daily,
    }


def entries(db: Session, *, month: Optional[str] = None, project_id: Optional[int] = None,
            kind: Optional[str] = None, limit: int = 100, offset: int = 0) -> list[models.SpendEntry]:
    q = db.query(models.SpendEntry)
    if month:
        start, end = _month_bounds(month)
        q = q.filter(models.SpendEntry.created_at >= start, models.SpendEntry.created_at < end)
    if project_id:
        q = q.filter(models.SpendEntry.project_id == project_id)
    if kind:
        q = q.filter(models.SpendEntry.kind == kind)
    return q.order_by(models.SpendEntry.id.desc()).offset(offset).limit(min(limit, 500)).all()


_account_cache: dict = {}


def openrouter_account(*, refresh: bool = False) -> dict:
    """OpenRouter's own view of this key's usage (GET /key), for comparing
    against the ledger. Cached for a minute; never includes the key."""
    import time

    s = get_settings()
    from . import provider_keys

    key = provider_keys.resolve("openrouter")
    if not key:
        return {"available": False, "reason": "OPENROUTER_API_KEY not set"}
    if not refresh and _account_cache.get("at", 0) > time.time() - 60:
        return _account_cache["data"]
    try:
        r = httpx.get(f"{s.openrouter_base_url.rstrip('/')}/key", timeout=10.0,
                      headers={"Authorization": f"Bearer {key}"})
        r.raise_for_status()
        d = r.json().get("data") or {}
        data = {"available": True, **{k: d.get(k) for k in (
            "usage", "usage_daily", "usage_weekly", "usage_monthly", "limit", "limit_remaining", "limit_reset")}}
    except Exception as e:  # noqa: BLE001
        data = {"available": False, "reason": str(e)[:200]}
    _account_cache.update(at=time.time(), data=data)
    return data

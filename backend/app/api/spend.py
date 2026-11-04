"""Spend ledger + monthly budget.

GET /api/spend/summary[?month=YYYY-MM]     totals by kind / provider / model / project, daily series
GET /api/spend/entries[?month&project_id&kind&limit&offset]
GET /api/spend/budget                      {monthly_usd, hard_stop, alert_pcts}
PUT /api/spend/budget                      same shape
GET /api/spend/openrouter[?refresh=true]   OpenRouter's own usage for this key (to cross-check)
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..services import spend
from .auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])


class BudgetIn(BaseModel):
    monthly_usd: float = Field(ge=0, le=1_000_000)
    hard_stop: bool = True
    alert_pcts: list[int] = Field(default_factory=lambda: [80, 100], max_length=10)


class SpendEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    provider: str
    kind: str
    model: str
    project_id: Optional[int]
    render_id: Optional[int]
    ref: str
    cost_usd: float
    is_estimate: bool
    source: str
    note: str


def _month(month: Optional[str]) -> Optional[str]:
    if month and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, "month must be YYYY-MM")
    return month


@router.get("/summary")
def summary(month: Optional[str] = None, db: Session = Depends(get_db)) -> dict:
    return spend.summary(db, _month(month))


@router.get("/entries", response_model=list[SpendEntryOut])
def entries(
    month: Optional[str] = None,
    project_id: Optional[int] = None,
    kind: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    return spend.entries(db, month=_month(month), project_id=project_id, kind=kind, limit=limit, offset=offset)


@router.get("/budget")
def get_budget(db: Session = Depends(get_db)) -> dict:
    return spend.get_budget(db)


@router.put("/budget")
def put_budget(body: BudgetIn, db: Session = Depends(get_db)) -> dict:
    return spend.set_budget(db, monthly_usd=body.monthly_usd, hard_stop=body.hard_stop,
                            alert_pcts=body.alert_pcts)


@router.get("/openrouter")
def openrouter(refresh: bool = False) -> dict:
    return spend.openrouter_account(refresh=refresh)

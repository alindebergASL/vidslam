"""Model catalog + active-model switching.

GET  /api/models/catalog[?kind=video&refresh=true]   polled catalog (cached)
POST /api/models/refresh                              poll providers now
GET  /api/models/active                               active model per kind
PUT  /api/models/active   {kind, model|null}          switch (null = .env default)
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..services import model_catalog, model_registry
from ..services.ratelimit import rate_limit
from .auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])


class SetActive(BaseModel):
    kind: str
    model: Optional[str] = Field(default=None, max_length=300)
    # Allow ids the last poll didn't list (new/private models, BYOK routes).
    allow_unlisted: bool = False
    # Per-kind extras saved with the selection, e.g. {"voice": "nova"} for tts.
    options: Optional[dict[str, str]] = None


def _catalog_view(snap: Optional[dict], kind: Optional[str]) -> dict:
    snap = snap or {"fetched_at": None, "entries": [], "errors": {}, "counts": {}, "new_count": 0}
    entries = snap.get("entries") or []
    if kind:
        entries = [e for e in entries if e["kind"] == kind]
    active = model_registry.snapshot()
    for e in entries:
        e["active"] = active.get(e["kind"], {}).get("model") == e["id"]
    return {**snap, "entries": entries, "active": active}


@router.get("/catalog")
def get_catalog(kind: Optional[str] = None, refresh: bool = False) -> dict:
    if kind and kind not in model_registry.KINDS:
        raise HTTPException(422, f"kind must be one of {', '.join(model_registry.KINDS)}")
    snap = model_catalog.get_snapshot()
    if refresh or snap is None:
        model_catalog.refresh()
        snap = model_catalog.get_snapshot()  # the live view: on/off switches and keys applied
    return _catalog_view(snap, kind)


@router.post("/refresh", dependencies=[Depends(rate_limit("generation"))])
def refresh_catalog() -> dict:
    snap = model_catalog.refresh()
    # The live view (on/off switches and keys applied), keeping the poll's report.
    live = model_catalog.get_snapshot() or snap
    return _catalog_view({**live, "errors": snap.get("errors", live.get("errors"))}, None)


@router.get("/active")
def get_active() -> dict:
    return model_registry.snapshot()


@router.put("/active")
def set_active(body: SetActive) -> dict:
    if body.kind not in model_registry.KINDS:
        raise HTTPException(422, f"kind must be one of {', '.join(model_registry.KINDS)}")
    model = body.model.strip() if body.model is not None else None
    if model:
        known = model_catalog.find(body.kind, model)
        is_mock = model.startswith("mock/")
        if known is None and not is_mock and not body.allow_unlisted:
            raise HTTPException(
                422,
                f"{model!r} isn't in the {body.kind} catalog — refresh the catalog, "
                "or pass allow_unlisted=true to use it anyway",
            )
    if model:
        model_registry.require(body.kind, model)  # its provider switched off: 409
    model_registry.set_active(body.kind, model, body.options)
    return model_registry.snapshot()

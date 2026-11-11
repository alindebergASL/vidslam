"""Clip check (services/inspector.py).

GET  /api/settings/clip-check            auto check on/off, model, rough cost per check
PUT  /api/settings/clip-check            {auto_check?, model?, reset_model?, allow_unlisted?}
GET  /api/settings/clip-check/models     vision-capable chat models from the catalog
POST /api/projects/{id}/shots/{shot_id}/check   {take_id?}  check a clip now (paid, guarded)
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..schemas import ShotOut
from ..schemas.api import InspectionOut
from ..services import inspector, model_registry, spend
from ..services.ratelimit import rate_limit
from ..services.spend import budget_guard
from .auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])


class ClipCheckSettingsIn(BaseModel):
    auto_check: Optional[bool] = None
    model: Optional[str] = Field(default=None, max_length=300)
    reset_model: bool = False  # back to the default model
    # Allow ids the catalog doesn't list as taking pictures (new/private models).
    allow_unlisted: bool = False


class CheckIn(BaseModel):
    take_id: Optional[int] = None  # None = the shot's current clip


class CheckOut(BaseModel):
    shot_id: int
    take_id: int
    inspection: InspectionOut
    shot: ShotOut


@router.get("/settings/clip-check")
def get_settings_() -> dict:
    return inspector.settings_out()


@router.put("/settings/clip-check")
def put_settings(body: ClipCheckSettingsIn) -> dict:
    model = (body.model or "").strip() or None
    if model and not body.reset_model:
        takes = inspector.is_vision(model)
        if not model.startswith("mock/") and not body.allow_unlisted:
            if takes is None:
                raise HTTPException(422, f"{model!r} isn't in the catalog — refresh the catalog, "
                                         "or pass allow_unlisted=true to use it anyway")
            if takes is False:
                raise HTTPException(422, f"{model!r} can't look at pictures, so it can't check clips")
        model_registry.require("chat", model)  # its provider switched off: 409
    return inspector.save_settings(auto=body.auto_check, model=model, reset_model=body.reset_model)


@router.get("/settings/clip-check/models")
def models_() -> dict:
    return {"models": inspector.model_choices(), "active": inspector.active_model()}


@router.post("/projects/{project_id}/shots/{shot_id}/check", response_model=CheckOut,
             dependencies=[Depends(rate_limit("generation")), Depends(budget_guard)])
def check_clip(project_id: int, shot_id: int, body: Optional[CheckIn] = None,
               db: Session = Depends(get_db)) -> CheckOut:
    """Check a clip now ("Check this clip" / "Check again"). Runs whether or
    not the automatic check is on; costs about cost_per_check."""
    from ..services import takes as takes_svc

    shot = db.get(models.VideoShot, shot_id)
    if shot is None or shot.project_id != project_id:
        raise HTTPException(404, "shot not found in project")
    if not takes_svc._is_storyboard_shot(shot):
        raise HTTPException(409, "Only storyboard clips can be checked.")
    take_id = body.take_id if body else None
    if take_id is None:
        take = takes_svc.ensure_current_take(db, shot)
        if take is None:
            raise HTTPException(409, "This shot has no clip to check yet.")
    else:
        take = next((t for t in shot.takes if t.id == take_id), None)
        if take is None:
            raise HTTPException(404, "take not found for this shot")
    if not take.clip_path or not Path(take.clip_path).is_file():
        raise HTTPException(409, "This clip's file is gone, so it can't be checked.")
    if inspector.adult_mode():
        # The vision models refuse adult frames (and still bill) or moralise.
        raise HTTPException(409, inspector.ADULT_MODE_WHY)
    model_registry.require("chat", inspector.active_model())  # switched off: 409
    if inspector.placeholder():
        raise HTTPException(409, "Add an OpenRouter key in Settings → API keys to check clips.")
    with spend.scope(project_id=project_id, ref=f"take:{take.id}"):
        result = inspector.check_take(db, take, auto=False)
    db.refresh(shot)
    return CheckOut(shot_id=shot.id, take_id=take.id, inspection=InspectionOut(**result),
                    shot=ShotOut.model_validate(shot))

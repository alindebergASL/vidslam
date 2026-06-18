"""API for custom-model training (character/style LoRA + voice clones).

Routes:
  POST   /api/cast/{kind}/{owner_id}/train       — kick off a new training job
  GET    /api/training-jobs                      — list (optionally filtered)
  GET    /api/training-jobs/{id}                 — single job detail
  POST   /api/training-jobs/{id}/cancel          — best-effort cancel
  DELETE /api/training-jobs/{id}                 — remove a finished/failed row

The actual training runs as an enqueued worker job
(`workers.jobs.train_custom_model_job`) so the request returns 202 quickly
and the UI polls for status — same pattern as the storyboard plan + render
endpoints."""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..schemas import CustomModelOut, CustomModelTrainRequest
from ..services.ratelimit import rate_limit
from ..workers.jobs import train_custom_model_job
from ..workers.queue import enqueue
from .auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])


def _resolve_owner(
    db: Session, kind: Literal["avatar", "ingredient"], owner_id: int
):
    if kind == "avatar":
        row = db.get(models.Avatar, owner_id)
    else:
        row = db.get(models.Ingredient, owner_id)
    if row is None:
        raise HTTPException(404, f"{kind} not found")
    return row


@router.post(
    "/cast/{kind}/{owner_id}/train",
    response_model=CustomModelOut,
    status_code=202,
    dependencies=[Depends(rate_limit("generation"))],
)
def start_training(
    kind: Literal["avatar", "ingredient"],
    owner_id: int,
    body: CustomModelTrainRequest,
    db: Session = Depends(get_db),
) -> models.CustomModel:
    _resolve_owner(db, kind, owner_id)

    # Validate every training asset id actually belongs to this cast member —
    # prevents a caller from training Naina's LoRA on Arjun's photos by
    # accident or design.
    if not body.training_asset_ids:
        raise HTTPException(422, "training_asset_ids: at least one asset is required")
    rows = (
        db.query(models.Asset)
        .filter(models.Asset.id.in_(body.training_asset_ids))
        .all()
    )
    if len(rows) != len(body.training_asset_ids):
        raise HTTPException(422, "one or more training_asset_ids do not exist")
    for r in rows:
        owns = (
            r.owner_kind == kind
            and (r.avatar_id if kind == "avatar" else r.ingredient_id) == owner_id
        )
        if not owns:
            raise HTTPException(
                422,
                f"asset {r.id} doesn't belong to {kind} {owner_id} — only train on this cast member's own assets",
            )

    # Voice clones must target an avatar (the voice plays in their TTS slot).
    if body.kind == "voice_clone" and kind != "avatar":
        raise HTTPException(422, "voice_clone training must target an avatar")

    cm = models.CustomModel(
        name=body.name or f"{kind}-{owner_id}-{body.kind}",
        kind=body.kind,
        owner_kind=kind,
        owner_id=owner_id,
        training_asset_ids_json=body.training_asset_ids,
        config_json=body.config,
        status="pending",
    )
    db.add(cm)
    db.commit()
    db.refresh(cm)

    enqueue(train_custom_model_job, cm.id)
    # In Redis-less mode `enqueue` runs the worker inline, so the DB row
    # has already transitioned. Refresh so the response carries the final
    # state rather than the just-submitted "pending" snapshot.
    db.refresh(cm)
    return cm


@router.get("/training-jobs", response_model=list[CustomModelOut])
def list_training_jobs(
    owner_kind: Optional[Literal["avatar", "ingredient"]] = None,
    owner_id: Optional[int] = None,
    kind: Optional[Literal["character_lora", "style_lora", "voice_clone"]] = None,
    db: Session = Depends(get_db),
) -> list[models.CustomModel]:
    q = db.query(models.CustomModel)
    if owner_kind:
        q = q.filter(models.CustomModel.owner_kind == owner_kind)
    if owner_id is not None:
        q = q.filter(models.CustomModel.owner_id == owner_id)
    if kind:
        q = q.filter(models.CustomModel.kind == kind)
    return q.order_by(models.CustomModel.created_at.desc()).all()


@router.get("/training-jobs/{job_id}", response_model=CustomModelOut)
def get_training_job(job_id: int, db: Session = Depends(get_db)) -> models.CustomModel:
    cm = db.get(models.CustomModel, job_id)
    if cm is None:
        raise HTTPException(404, "training job not found")
    return cm


@router.post("/training-jobs/{job_id}/cancel", response_model=CustomModelOut)
def cancel_training(job_id: int, db: Session = Depends(get_db)) -> models.CustomModel:
    cm = db.get(models.CustomModel, job_id)
    if cm is None:
        raise HTTPException(404, "training job not found")
    if cm.status in {"completed", "failed", "cancelled"}:
        # Already terminal — no-op, return current state. Cleaner UX than
        # 409: the user clicked cancel, the job's already done, just show
        # the final state.
        return cm
    # Tell the provider to stop. Mark our row as cancelled regardless so
    # the worker loop sees a terminal state on its next poll.
    try:
        from ..providers import get_training
        from ..providers.base import TrainingSubmit

        provider = get_training(cm.kind)
        if cm.provider_job_id:
            provider.cancel(
                TrainingSubmit(
                    provider=cm.provider,
                    provider_job_id=cm.provider_job_id,
                )
            )
    except Exception:  # noqa: BLE001
        # Provider-side cancel is best-effort; the local row still flips.
        pass
    cm.status = "cancelled"
    db.commit()
    db.refresh(cm)
    return cm


@router.delete("/training-jobs/{job_id}", status_code=204)
def delete_training(job_id: int, db: Session = Depends(get_db)) -> None:
    cm = db.get(models.CustomModel, job_id)
    if cm is None:
        return  # idempotent delete
    if cm.status in {"pending", "training"}:
        raise HTTPException(
            409,
            "cancel the job before deleting (a running training job would be orphaned)",
        )
    db.delete(cm)
    db.commit()

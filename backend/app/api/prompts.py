"""Prompting: default avoid lists, "enhance my prompt", saved presets.

GET  /api/prompts/defaults            {"image": str, "video": str}
PUT  /api/prompts/defaults            same body → same shape
DELETE /api/prompts/defaults          back to the built-in lists → same shape
POST /api/prompts/enhance             {"text", "kind", "model"?} → {"text", "model"}
GET  /api/prompts/presets             newest first
POST /api/prompts/presets             → preset (201)
PUT  /api/prompts/presets/{id}        → preset
DELETE /api/prompts/presets/{id}      → 204
"""
from __future__ import annotations

import logging
from typing import Literal, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, field_validator

from ..services import prompting
from ..services.ratelimit import rate_limit
from ..services.spend import budget_guard
from .auth import require_auth

log = logging.getLogger("avs.api.prompts")

router = APIRouter(dependencies=[Depends(require_auth)])


class NegativeDefaults(BaseModel):
    image: str = Field(default="", max_length=prompting.NEGATIVE_MAX)
    video: str = Field(default="", max_length=prompting.NEGATIVE_MAX)


class EnhanceIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    kind: Literal["image", "video", "script", "scene"]
    model: Optional[str] = Field(default=None, max_length=200)

    @field_validator("text")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("text must not be empty")
        return v.strip()


class EnhanceOut(BaseModel):
    text: str
    model: str


class PresetIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["image", "video"]
    prompt: str = Field(min_length=1, max_length=4000)
    negative_prompt: str = Field(default="", max_length=prompting.NEGATIVE_MAX)
    model: str = Field(default="", max_length=200)
    aspect_ratio: Literal["", "9:16", "16:9", "1:1"] = ""

    @field_validator("name", "prompt")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be empty")
        return v.strip()

    @field_validator("negative_prompt", "model")
    @classmethod
    def _strip(cls, v: str) -> str:
        return (v or "").strip()


class PresetOut(PresetIn):
    id: str
    created_at: str


@router.get("/defaults", response_model=NegativeDefaults)
def get_defaults() -> dict:
    return prompting.negative_defaults()


@router.put("/defaults", response_model=NegativeDefaults)
def put_defaults(body: NegativeDefaults) -> dict:
    return prompting.set_negative_defaults(body.image, body.video)


@router.delete("/defaults", response_model=NegativeDefaults)
def reset_defaults() -> dict:
    """Back to the built-in lists; returns them."""
    return prompting.reset_negative_defaults()


@router.post("/enhance", response_model=EnhanceOut,
             dependencies=[Depends(rate_limit("generation")), Depends(budget_guard)])
def enhance(body: EnhanceIn) -> dict:
    try:
        out = prompting.enhance(body.text, body.kind, body.model or "")
    except httpx.HTTPStatusError as e:
        log.warning("prompt enhance failed: HTTP %s", e.response.status_code)
        raise HTTPException(502, f"The planner model returned HTTP {e.response.status_code}; try again.") from None
    except (httpx.HTTPError, RuntimeError, ValueError) as e:
        log.warning("prompt enhance failed: %s", str(e)[:300])
        raise HTTPException(502, "Couldn't enhance the prompt right now; try again.") from None
    if not out["text"].strip():
        raise HTTPException(502, "The planner model returned an empty prompt; try again.")
    return out


@router.get("/presets", response_model=list[PresetOut])
def list_presets() -> list[dict]:
    return prompting.list_presets()


@router.post("/presets", response_model=PresetOut, status_code=201)
def create_preset(body: PresetIn) -> dict:
    try:
        return prompting.create_preset(body.model_dump())
    except prompting.PresetLimitError as e:
        raise HTTPException(409, str(e)) from None


@router.put("/presets/{preset_id}", response_model=PresetOut)
def update_preset(preset_id: str, body: PresetIn) -> dict:
    out = prompting.update_preset(preset_id, body.model_dump())
    if out is None:
        raise HTTPException(404, "Preset not found")
    return out


@router.delete("/presets/{preset_id}", status_code=204)
def delete_preset(preset_id: str) -> Response:
    if not prompting.delete_preset(preset_id):
        raise HTTPException(404, "Preset not found")
    return Response(status_code=204)

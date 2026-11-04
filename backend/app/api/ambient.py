"""Ambient section API — calm / landscape / spa long-form loops.

Mounted at /api/ambient. Scenes are VideoProjects under the hood, so the
generic project endpoints (delete, renders, cancel, music upload/preview,
shot clip preview, share links) keep working on an ambient id; this router
only adds the ambient-specific lifecycle.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..schemas.ambient import (
    AmbientBatchRequest,
    AmbientCreate,
    AmbientEstimateRequest,
    AmbientOut,
    AmbientPresetsOut,
    AmbientUpdate,
)
from ..services import ambient as svc
from ..services.ratelimit import rate_limit
from ..workers.jobs import (
    ambient_batch_job,
    generate_ambient_loop_job,
    generate_previews_job,
    render_ambient_job,
)
from ..workers.queue import enqueue
from .auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])

# Unauthenticated, random-token route: video providers fetch first/last-frame
# images for chained ambient clips from here, the same way they fetch cast
# assets from /api/public-assets/{token}.
public_router = APIRouter()


@public_router.get("/public-media/{name}")
def public_media_file(name: str) -> FileResponse:
    from ..services import public_media

    hit = public_media.resolve(name)
    if hit is None:
        raise HTTPException(404, "not found")
    return FileResponse(hit[0], media_type=hit[1])


@public_router.get("/public-ambient-frames/{token}")
def public_frame(token: str) -> FileResponse:
    path = svc.frame_path_for_token(token)
    if path is None:
        raise HTTPException(404, "not found")
    return FileResponse(path, media_type="image/png")


def _get_scene(db: Session, scene_id: int) -> models.VideoProject:
    project = db.get(models.VideoProject, scene_id)
    if project is None or project.mode != svc.AMBIENT_MODE:
        raise HTTPException(404, "ambient scene not found")
    return project


@router.get("/presets", response_model=AmbientPresetsOut)
def get_presets() -> AmbientPresetsOut:
    return svc.presets()


@router.get("/presets/{preset_id}/preview")
def preset_preview(preset_id: str) -> FileResponse:
    path = svc.preview_path(preset_id)
    if not path.exists():
        raise HTTPException(404, "no preview yet")
    return FileResponse(path, media_type="image/png")


@router.post("/presets/previews", status_code=202, dependencies=[Depends(rate_limit("generation"))])
def generate_previews(missing_only: bool = True) -> dict:
    """Generate preview stills for presets (one image call each)."""
    ids = [p.id for p in svc.SCENE_PRESETS if p.prompt
           and not (missing_only and svc.preview_path(p.id).exists())]
    job_id = enqueue(generate_previews_job, ids) if ids else None
    return {"presets": ids, "job_id": job_id, "status": "enqueued" if ids else "nothing_to_do"}


@router.post("/batch", status_code=201, dependencies=[Depends(rate_limit("generation"))])
def batch_create(body: AmbientBatchRequest, db: Session = Depends(get_db)) -> dict:
    """Create one scene per preset with shared settings, then generate (and
    optionally render) them one after another in a single background job."""
    unknown = [p for p in body.presets if p not in svc._PRESET_BY_ID or p == "custom"]
    if unknown:
        raise HTTPException(422, f"unknown or custom presets: {', '.join(unknown)}")
    created = []
    total = 0.0
    for pid in body.presets:
        preset = svc._PRESET_BY_ID[pid]
        cfg = body.config.model_copy(update={"scene_preset": pid, "scene_prompt": "",
                                             "music_mood": body.config.music_mood if body.config.music_mood == "none" else preset.music_mood})
        project = svc.create_scene(db, AmbientCreate(aspect_ratio=body.aspect_ratio, config=cfg, generate=False))
        created.append(project.id)
        total += svc.estimate_ambient_cost(cfg)["total"]
    job_id = enqueue(ambient_batch_job, created, body.render)
    db.expire_all()
    return {
        "scene_ids": created,
        "job_id": job_id,
        "estimated_total": round(total, 2),
        "scenes": [svc.to_out(db, _get_scene(db, i)) for i in created],
    }


@router.post("/estimate")
def estimate(body: AmbientEstimateRequest) -> dict:
    return svc.estimate_ambient_cost(body.config)


@router.get("", response_model=list[AmbientOut])
def list_scenes(db: Session = Depends(get_db)) -> list[AmbientOut]:
    return [svc.to_out(db, p) for p in svc.list_scenes(db)]


@router.post("", response_model=AmbientOut, status_code=201,
             dependencies=[Depends(rate_limit("generation"))])
def create_scene(body: AmbientCreate, db: Session = Depends(get_db)) -> AmbientOut:
    try:
        project = svc.create_scene(db, body)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    if body.generate:
        enqueue(generate_ambient_loop_job, project.id)
        db.expire_all()
        project = _get_scene(db, project.id)
    return svc.to_out(db, project)


@router.get("/{scene_id}", response_model=AmbientOut)
def get_scene(scene_id: int, db: Session = Depends(get_db)) -> AmbientOut:
    return svc.to_out(db, _get_scene(db, scene_id))


@router.patch("/{scene_id}", response_model=AmbientOut)
def update_scene(scene_id: int, body: AmbientUpdate, db: Session = Depends(get_db)) -> AmbientOut:
    project = _get_scene(db, scene_id)
    if project.status == "generating":
        raise HTTPException(409, "scene is generating; wait or cancel first")
    try:
        project = svc.update_scene(db, project, body)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return svc.to_out(db, project)


@router.post("/{scene_id}/generate-loop", status_code=202,
             dependencies=[Depends(rate_limit("generation"))])
def generate_loop(
    scene_id: int,
    regenerate_music: bool = True,
    db: Session = Depends(get_db),
) -> dict:
    project = _get_scene(db, scene_id)
    if project.status == "generating":
        raise HTTPException(409, "scene is already generating")
    job_id = enqueue(generate_ambient_loop_job, scene_id, regenerate_music)
    return {"scene_id": scene_id, "job_id": job_id, "status": "enqueued"}


@router.post("/{scene_id}/music", dependencies=[Depends(rate_limit("generation"))])
def regenerate_music(scene_id: int, db: Session = Depends(get_db)) -> AmbientOut:
    """(Re)generate the scene's music bed at its configured length —
    multi-part + crossfaded when longer than the provider's per-call cap."""
    project = _get_scene(db, scene_id)
    cfg = svc._config_of(project)
    prompt = svc.resolve_music_prompt(cfg)
    if not prompt:
        raise HTTPException(409, "music mood is 'none'")
    svc.generate_music_bed(db, project, cfg, prompt)
    db.refresh(project)
    return svc.to_out(db, project)


@router.post("/{scene_id}/render", status_code=202,
             dependencies=[Depends(rate_limit("generation"))])
def render_scene(scene_id: int, db: Session = Depends(get_db)) -> dict:
    project = _get_scene(db, scene_id)
    if project.status == "generating":
        raise HTTPException(409, "scene is already generating")
    shot = svc._loop_shot(project)
    if shot is None or not shot.clip_path or not Path(shot.clip_path).exists():
        raise HTTPException(409, "generate the loop clip first")
    job_id = enqueue(render_ambient_job, scene_id)
    return {"scene_id": scene_id, "job_id": job_id, "status": "enqueued"}

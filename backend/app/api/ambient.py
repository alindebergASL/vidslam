"""Ambient section API — calm / landscape / spa long-form loops.

Mounted at /api/ambient. Scenes are VideoProjects under the hood, so the
generic project endpoints (delete, renders, cancel, music upload/preview,
shot clip preview, share links) keep working on an ambient id; this router
only adds the ambient-specific lifecycle.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from ..db import get_db
from ..schemas.ambient import (
    AmbientBatchRequest,
    AmbientCreate,
    AmbientEstimateRequest,
    AmbientOut,
    AmbientPresetsOut,
    AmbientUpdate,
    PictureFrom,
)
from ..services import ambient as svc
from ..services import storage
from ..services.ratelimit import rate_limit
from ..services.spend import budget_guard
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


def _enqueue_generation(db: Session, project: models.VideoProject, *args) -> str:
    """Mark the scene generating before queueing its job: on RQ the job may
    wait behind a render for a long time, and until a worker picks it up a
    second Generate would otherwise pass the 409 and queue a second paid run
    (which resets the clips and drops the first run's chain). The claim is a
    conditional UPDATE, so two simultaneous requests can't both get through."""
    previous = project.status
    claimed = (
        db.query(models.VideoProject)
        .filter(models.VideoProject.id == project.id, models.VideoProject.status != "generating")
        .update({"status": "generating"}, synchronize_session=False)
    )
    db.commit()
    if not claimed:
        raise HTTPException(409, "scene is already generating")
    try:
        return enqueue(generate_ambient_loop_job, project.id, *args)
    except Exception:
        # QueueUnavailable, or Redis dropping between the health check and
        # the enqueue: no job was accepted, so nothing would ever clear the
        # claim. Put the scene back so it can be edited and generated again.
        # (If a job did get through, its worker sets 'generating' itself.)
        db.query(models.VideoProject).filter(
            models.VideoProject.id == project.id, models.VideoProject.status == "generating",
        ).update({"status": previous}, synchronize_session=False)
        db.commit()
        raise


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
    # Previews come back as PNG or JPEG depending on the model.
    _, mime, _, _ = storage.image_info(path.read_bytes()[:65536])
    return FileResponse(path, media_type=mime)


@router.post("/presets/previews", status_code=202, dependencies=[Depends(rate_limit("generation")), Depends(budget_guard)])
def generate_previews(missing_only: bool = True) -> dict:
    """Generate preview stills for presets (one image call each)."""
    # "Missing" includes a preview made from an older version of the prompt.
    ids = [p.id for p in svc.SCENE_PRESETS if p.prompt
           and not (missing_only and svc.preview_is_current(p.id))]
    job_id = enqueue(generate_previews_job, ids) if ids else None
    return {"presets": ids, "job_id": job_id, "status": "enqueued" if ids else "nothing_to_do"}


@router.post("/batch", status_code=201, dependencies=[Depends(rate_limit("generation")), Depends(budget_guard)])
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
    if body.generate:
        budget_guard()  # creating without generating is free (a description rewrite skips itself over budget)
    try:
        project = svc.create_scene(db, body)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    if body.generate:
        _enqueue_generation(db, project)
        db.expire_all()
        project = _get_scene(db, project.id)
    return svc.to_out(db, project)


@router.get("/{scene_id}", response_model=AmbientOut)
def get_scene(scene_id: int, db: Session = Depends(get_db)) -> AmbientOut:
    return svc.to_out(db, _get_scene(db, scene_id))


def _generation_token(request: Request) -> Callable[[], bool]:
    """A save's description rewrite is a paid planner call: it takes a token
    from the generation rate limit, but only when one is actually made, and
    an empty bucket skips the rewrite instead of refusing the save."""
    def take() -> bool:
        try:
            rate_limit("generation")(request)
        except HTTPException:
            return False
        return True
    return take


@router.patch("/{scene_id}", response_model=AmbientOut)
def update_scene(scene_id: int, body: AmbientUpdate, request: Request,
                 db: Session = Depends(get_db)) -> AmbientOut:
    project = _get_scene(db, scene_id)
    if project.status == "generating":
        raise HTTPException(409, "scene is generating; wait or cancel first")
    try:
        project = svc.update_scene(db, project, body, allow_rewrite=_generation_token(request))
    except svc.SceneBusy as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return svc.to_out(db, project)


@router.post("/{scene_id}/generate-loop", status_code=202,
             dependencies=[Depends(rate_limit("generation")), Depends(budget_guard)])
def generate_loop(
    scene_id: int,
    regenerate_music: bool = True,
    db: Session = Depends(get_db),
) -> dict:
    project = _get_scene(db, scene_id)
    if project.status == "generating":
        raise HTTPException(409, "scene is already generating")
    job_id = _enqueue_generation(db, project, regenerate_music)
    return {"scene_id": scene_id, "job_id": job_id, "status": "enqueued"}


@router.post("/{scene_id}/music", dependencies=[Depends(rate_limit("generation")), Depends(budget_guard)])
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


@router.post("/{scene_id}/sound", dependencies=[Depends(rate_limit("generation")), Depends(budget_guard)])
def regenerate_sound(scene_id: int, db: Session = Depends(get_db)) -> AmbientOut:
    """(Re)generate the scene's nature-sound bed (one 30 s seamless loop)."""
    project = _get_scene(db, scene_id)
    cfg = svc._config_of(project)
    if not svc.resolve_sound_prompt(cfg):
        raise HTTPException(409, "nature sound is off for this scene")
    svc.generate_sound_bed(db, project, cfg)
    db.refresh(project)
    return svc.to_out(db, project)


@router.get("/{scene_id}/sound")
def sound_file(scene_id: int, db: Session = Depends(get_db)) -> FileResponse:
    """Preview the nature-sound bed."""
    project = _get_scene(db, scene_id)
    path = svc.sound_path(project)
    if not path.exists():
        raise HTTPException(404, "no nature sound yet")
    return FileResponse(path, media_type="audio/mpeg")


# ---------- scene picture ----------


def _picture_busy(project: models.VideoProject) -> None:
    if project.status == "generating":
        raise HTTPException(409, "the loop is generating; change the picture when it's done")


@router.post("/{scene_id}/picture")
async def upload_picture(scene_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)) -> AmbientOut:
    """Use your own image: it becomes the loop's first (and last) frame, or
    the still of a Still + drift scene. Cropped to the scene's aspect."""
    project = _get_scene(db, scene_id)
    _picture_busy(project)
    s = get_settings()
    if (file.content_type or "") not in s.allowed_image_mimes:
        raise HTTPException(415, f"unsupported image type {file.content_type!r}")
    raw = await file.read(s.max_upload_bytes + 1)
    if len(raw) > s.max_upload_bytes:
        raise HTTPException(413, f"image is larger than {s.max_upload_bytes // (1024 * 1024)} MB")
    try:
        svc.set_picture(db, project, raw, "upload")
    except svc.PictureError as e:
        raise HTTPException(422, str(e)) from e
    return svc.to_out(db, project)


@router.post("/{scene_id}/picture/generate",
             dependencies=[Depends(rate_limit("generation")), Depends(budget_guard)])
def generate_picture(scene_id: int, db: Session = Depends(get_db)) -> AmbientOut:
    """Generate the picture now (one image call) to preview it before
    making the video; regenerate until it looks right."""
    project = _get_scene(db, scene_id)
    _picture_busy(project)
    try:
        svc.generate_picture(db, project)
    except svc.PictureError as e:
        raise HTTPException(422, str(e)) from e
    return svc.to_out(db, project)


@router.post("/{scene_id}/picture/from")
def picture_from(scene_id: int, body: PictureFrom, db: Session = Depends(get_db)) -> AmbientOut:
    """Use a Cast image (asset_id) or a Studio result (studio_job_id)."""
    project = _get_scene(db, scene_id)
    _picture_busy(project)
    try:
        svc.picture_from(db, project, asset_id=body.asset_id, studio_job_id=body.studio_job_id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except svc.PictureError as e:
        raise HTTPException(422, str(e)) from e
    return svc.to_out(db, project)


@router.delete("/{scene_id}/picture")
def clear_picture(scene_id: int, db: Session = Depends(get_db)) -> AmbientOut:
    """Back to generating the image from the prompt at loop time."""
    project = _get_scene(db, scene_id)
    _picture_busy(project)
    svc.clear_picture(db, project)
    return svc.to_out(db, project)


@router.get("/{scene_id}/picture")
def picture_file(scene_id: int, db: Session = Depends(get_db)) -> FileResponse:
    project = _get_scene(db, scene_id)
    if not svc.has_picture(project):
        raise HTTPException(404, "this scene has no picture")
    return FileResponse(svc.picture_path(project), media_type="image/png")


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

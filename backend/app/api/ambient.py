"""Ambient section API — calm / landscape / spa long-form loops.

Mounted at /api/ambient. Scenes are VideoProjects under the hood, so the
generic project endpoints (delete, renders, cancel, music upload/preview,
shot clip preview, share links) keep working on an ambient id; this router
only adds the ambient-specific lifecycle.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Callable, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..schemas.ambient import (
    AmbientBatchRequest,
    AmbientConfig,
    AmbientCreate,
    AmbientEstimateRequest,
    AmbientOut,
    AmbientPresetsOut,
    AmbientUpdate,
    LoopHealthResultOut,
    PictureFrom,
)
from ..services import ambient as svc
from ..services import provider_gate, storage
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


def _enqueue_generation(db: Session, project: models.VideoProject, *args,
                        job: Callable = generate_ambient_loop_job, makes_music: Optional[bool] = None) -> str:
    """Mark the scene generating before queueing its job: on RQ the job may
    wait behind a render for a long time, and until a worker picks it up a
    second Generate would otherwise pass the 409 and queue a second paid run
    (which resets the clips and drops the first run's chain). The claim is a
    conditional UPDATE, so two simultaneous requests can't both get through."""
    # A provider switched off: 409, nothing queued (args[0]: regenerate_music).
    if makes_music is None:
        makes_music = bool(args[0]) if args else True
    provider_gate.require_ambient(svc._config_of(project), regenerate_music=makes_music, project=project)
    previous = project.status
    token = uuid.uuid4().hex
    claimed = (
        db.query(models.VideoProject)
        .filter(models.VideoProject.id == project.id, models.VideoProject.status != "generating")
        .update({"status": "generating", "claim_token": token}, synchronize_session=False)
    )
    db.commit()
    if not claimed:
        raise HTTPException(409, "scene is already generating")
    try:
        # The job consumes the token before it starts: a cancel while it is
        # queued (or a newer claim) makes it a no-op.
        return enqueue(job, project.id, *args, claim_token=token)
    except Exception:
        # QueueUnavailable, or Redis dropping between the health check and
        # the enqueue: no job was accepted, so nothing would ever clear the
        # claim. Put the scene back so it can be edited and generated again.
        # (If a job did get through, its worker sets 'generating' itself.)
        db.query(models.VideoProject).filter(
            models.VideoProject.id == project.id, models.VideoProject.status == "generating",
            models.VideoProject.claim_token == token,
        ).update({"status": previous, "claim_token": ""}, synchronize_session=False)
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
    if ids:
        provider_gate.require_still(AmbientConfig())
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

    def cfg_for(pid: str):
        preset = svc._PRESET_BY_ID[pid]
        return body.config.model_copy(update={"scene_preset": pid, "scene_prompt": "",
                                              "music_mood": body.config.music_mood if body.config.music_mood == "none" else preset.music_mood})

    for pid in body.presets:  # every scene's providers first: no half-created batch
        provider_gate.require_ambient(cfg_for(pid))
    for pid in body.presets:
        cfg = cfg_for(pid)
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


def _check_scene(config) -> None:
    """The typed scene text gets the safety check on save (a 422), not only
    when its pictures and clips are made."""
    text = (getattr(config, "scene_prompt", "") or "").strip() if config is not None else ""
    if text:
        from ..services.minor_check import check_or_422

        check_or_422(text, kind="image")


@router.post("", response_model=AmbientOut, status_code=201,
             dependencies=[Depends(rate_limit("generation"))])
def create_scene(body: AmbientCreate, db: Session = Depends(get_db)) -> AmbientOut:
    _check_scene(body.config)
    if body.generate:
        budget_guard()  # creating without generating is free (a description rewrite skips itself over budget)
        provider_gate.require_ambient(body.config)  # before the scene (and its rewrite) is made
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
def update_scene(scene_id: int, body: AmbientUpdate, request: Request, discard_loop: bool = False,
                 db: Session = Depends(get_db)) -> AmbientOut:
    """Save settings. A change that needs a new loop clip is refused (409)
    when clips are already made, unless discard_loop=true."""
    project = _get_scene(db, scene_id)
    if project.status == "generating":
        raise HTTPException(409, "scene is generating; wait or cancel first")
    _check_scene(body.config)
    try:
        project = svc.update_scene(db, project, body, allow_rewrite=_generation_token(request),
                                   discard_loop=discard_loop)
    except (svc.SceneBusy, svc.LoopDiscard) as e:
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


@router.post("/{scene_id}/resume-loop", status_code=202,
             dependencies=[Depends(rate_limit("generation")), Depends(budget_guard)])
def resume_loop(scene_id: int, db: Session = Depends(get_db)) -> dict:
    """Continue a loop that failed part-way from the clip that failed; the
    finished clips and takes are kept. (generate-loop starts over.)"""
    project = _get_scene(db, scene_id)
    if project.status == "generating":
        raise HTTPException(409, "scene is already generating")
    cfg = svc._config_of(project)
    info = svc.resume_info(project, cfg)
    if info is None:
        raise HTTPException(409, "this scene has no failed loop to resume; generate the loop instead")
    chain = (project.ambient_config_json or {}).get(svc._CHAIN_KEY) or {}
    job_id = _enqueue_generation(db, project, job=svc.resume_loop_job,
                                 makes_music=bool(chain.get("regenerate_music", True)))
    return {"scene_id": scene_id, "job_id": job_id, "status": "enqueued", "clip": info.clip, "take": info.take}


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


# Every picture change needs a new loop: when clips are already made it is
# refused (409, nothing changed) unless the request says discard_loop=true.


@router.post("/{scene_id}/picture")
async def upload_picture(scene_id: int, file: UploadFile = File(...), discard_loop: bool = False,
                         db: Session = Depends(get_db)) -> AmbientOut:
    """Use your own image: it becomes the loop's first (and last) frame, or
    the still of a Still + drift scene. Cropped to the scene's aspect."""
    project = _get_scene(db, scene_id)
    _picture_busy(project)
    try:
        storage.check_upload_type(file.content_type or "", file.filename or "")
        raw = storage.read_upload(file.file)
    except storage.UploadError as e:
        raise HTTPException(413 if e.status_code == 413 else 415, str(e)) from e
    try:
        svc.set_picture(db, project, raw, "upload", discard_loop=discard_loop)
    except svc.LoopDiscard as e:
        raise HTTPException(409, str(e)) from e
    except svc.PictureTooLarge as e:
        raise HTTPException(413, str(e)) from e
    except svc.PictureError as e:
        raise HTTPException(422, str(e)) from e
    return svc.to_out(db, project)


@router.post("/{scene_id}/picture/generate",
             dependencies=[Depends(rate_limit("generation")), Depends(budget_guard)])
def generate_picture(scene_id: int, discard_loop: bool = False, db: Session = Depends(get_db)) -> AmbientOut:
    """Generate the picture now (one image call) to preview it before
    making the video; regenerate until it looks right."""
    project = _get_scene(db, scene_id)
    _picture_busy(project)
    provider_gate.require_still(svc._config_of(project))  # the scene's chosen model, not a silent swap
    try:
        svc.generate_picture(db, project, discard_loop=discard_loop)
    except svc.LoopDiscard as e:
        raise HTTPException(409, str(e)) from e
    except svc.PictureError as e:
        raise HTTPException(422, str(e)) from e
    return svc.to_out(db, project)


@router.post("/{scene_id}/picture/from")
def picture_from(scene_id: int, body: PictureFrom, discard_loop: bool = False,
                 db: Session = Depends(get_db)) -> AmbientOut:
    """Use a Cast image (asset_id) or a Studio result (studio_job_id)."""
    project = _get_scene(db, scene_id)
    _picture_busy(project)
    try:
        svc.picture_from(db, project, asset_id=body.asset_id, studio_job_id=body.studio_job_id,
                         discard_loop=discard_loop)
    except svc.LoopDiscard as e:
        raise HTTPException(409, str(e)) from e
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except svc.PictureError as e:
        raise HTTPException(422, str(e)) from e
    return svc.to_out(db, project)


@router.delete("/{scene_id}/picture")
def clear_picture(scene_id: int, discard_loop: bool = False, db: Session = Depends(get_db)) -> AmbientOut:
    """Back to generating the image from the prompt at loop time."""
    project = _get_scene(db, scene_id)
    _picture_busy(project)
    try:
        svc.clear_picture(db, project, discard_loop=discard_loop)
    except svc.LoopDiscard as e:
        raise HTTPException(409, str(e)) from e
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
    # Claim before queueing (a conditional UPDATE): a double-click or a
    # second tab can't queue a second render, and the page keeps polling
    # while the job waits for a worker.
    previous = project.status
    token = uuid.uuid4().hex
    claimed = (
        db.query(models.VideoProject)
        .filter(models.VideoProject.id == project.id, models.VideoProject.status != "generating")
        .update({"status": "generating", "claim_token": token}, synchronize_session=False)
    )
    db.commit()
    if not claimed:
        raise HTTPException(409, "scene is already generating")
    try:
        # The job consumes the token before rendering: cancelled while
        # queued (or replaced by a newer claim) → it does nothing.
        job_id = enqueue(render_ambient_job, scene_id, claim_token=token)
    except Exception:
        db.query(models.VideoProject).filter(
            models.VideoProject.id == project.id, models.VideoProject.status == "generating",
            models.VideoProject.claim_token == token,
        ).update({"status": previous, "claim_token": ""}, synchronize_session=False)
        db.commit()
        raise
    return {"scene_id": scene_id, "job_id": job_id, "status": "enqueued"}


# ---------- loop health (local measurement, free) ----------


def _health_result(project: models.VideoProject) -> LoopHealthResultOut:
    from ..services import loop_health

    shots = loop_health.clip_shots(project)
    return LoopHealthResultOut(
        loop_health=loop_health.current(project, shots[0]) if shots else None,
        variant_loop_health=[loop_health.current(project, s) for s in shots[1:]],
    )


@router.post("/{scene_id}/loop-health", response_model=LoopHealthResultOut,
             dependencies=[Depends(rate_limit("generation"))])
def measure_loop_health(scene_id: int, db: Session = Depends(get_db)) -> LoopHealthResultOut:
    """Measure the loop clip (and each variation's) again now. Free: no
    provider is called. 409 while generating or when there is no clip."""
    from ..services import loop_health

    project = _get_scene(db, scene_id)
    if project.status == "generating":
        raise HTTPException(409, "The loop is still being made. Measure it once it's ready.")
    shots = [s for s in loop_health.clip_shots(project)
             if s.clip_path and s.status == "completed" and Path(s.clip_path).is_file()]
    if not shots:
        raise HTTPException(409, "There's no loop clip to measure yet. Generate the loop first.")
    got = loop_health.measure_scene(db, project, force=True)
    if not got:
        raise HTTPException(502, "Couldn't measure the loop clip (the file may be damaged). Try again, "
                                 "or regenerate the loop.")
    db.refresh(project)
    return _health_result(project)


@router.get("/{scene_id}/seam-preview")
def seam_preview(scene_id: int, shot_id: Optional[int] = None, db: Session = Depends(get_db)) -> FileResponse:
    """The last 3 s of the loop followed by its first 3 s (MP4), made once
    per version of the clip. The join is at X-Join-At seconds."""
    from ..services import loop_health

    project = _get_scene(db, scene_id)
    shots = loop_health.clip_shots(project)
    shot = next((s for s in shots if s.id == shot_id), None) if shot_id is not None else (shots[0] if shots else None)
    if shot is None:
        raise HTTPException(404, "no such loop clip in this scene")
    if not shot.clip_path or shot.status != "completed" or not Path(shot.clip_path).is_file():
        raise HTTPException(404, "this loop has no clip yet")
    try:
        path, join_at = loop_health.seam_preview(Path(shot.clip_path))
    except RuntimeError as e:
        raise HTTPException(500, str(e)) from None
    return FileResponse(path, media_type="video/mp4", headers={"X-Join-At": f"{join_at:.2f}",
                                                              "Cache-Control": "no-cache"})

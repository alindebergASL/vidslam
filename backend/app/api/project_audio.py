from __future__ import annotations

import threading
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from ..db import get_db
from ..schemas import ProjectOut
from ..schemas.api import VoiceoverFitOut, VoiceoverPreviewOut
from ..services import pipeline, storage
from ..services.ratelimit import rate_limit
from ..services.spend import budget_guard
from .auth import require_auth
from .projects import refuse_while_busy

router = APIRouter(dependencies=[Depends(require_auth)])
settings = get_settings()

_ALLOWED_AUDIO_MIMES = {
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/aac": "aac",
    "audio/x-m4a": "m4a",
    "audio/m4a": "m4a",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
}
_MAX_AUDIO_BYTES = 50 * 1024 * 1024  # 50 MB


def _save_audio(file: UploadFile, *, project_id: int, kind: str) -> Path:
    if file.content_type not in _ALLOWED_AUDIO_MIMES:
        raise HTTPException(400, f"Unsupported audio mime: {file.content_type}")
    raw = file.file.read()
    if len(raw) > _MAX_AUDIO_BYTES:
        raise HTTPException(413, "Audio file too large (>50 MB)")
    ext = _ALLOWED_AUDIO_MIMES[file.content_type]
    out_dir = storage.render_subdir(project_id)
    # Stable filename so re-uploading replaces the previous file.
    path = out_dir / f"{kind}.{ext}"
    # Clean up any other extensions for this kind so the project never has two
    # voiceover.* files lying around.
    for stale in out_dir.glob(f"{kind}.*"):
        if stale != path:
            try:
                stale.unlink()
            except OSError:
                pass
    # A new file, never the old one rewritten: a render's own copy of the
    # music (a hard link to it, pipeline.keep_music) must keep its sound.
    path.unlink(missing_ok=True)
    path.write_bytes(raw)
    return path


@router.post("/projects/{project_id}/voiceover", response_model=ProjectOut)
async def upload_voiceover(
    project_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> models.VideoProject:
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    refuse_while_busy(db, project, "The voiceover")
    path = _save_audio(file, project_id=project_id, kind="voiceover")
    project.voiceover_upload_path = str(path)
    project.voiceover_source = "upload"
    db.commit()
    db.refresh(project)
    return project


@router.delete("/projects/{project_id}/voiceover", response_model=ProjectOut)
def delete_voiceover(project_id: int, db: Session = Depends(get_db)) -> models.VideoProject:
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    if project.voiceover_upload_path or project.voiceover_source == "upload":
        refuse_while_busy(db, project, "The voiceover")
    if project.voiceover_upload_path:
        try:
            Path(project.voiceover_upload_path).unlink(missing_ok=True)
        except OSError:
            pass
    project.voiceover_upload_path = ""
    if project.voiceover_source == "upload":
        project.voiceover_source = "tts"
    db.commit()
    db.refresh(project)
    return project


_FIT_KEYS = ("video_seconds", "difference_seconds", "fit", "fit_text", "cut_seconds", "recompose_fit",
             "recompose_fit_text", "recompose_video_seconds", "recompose_cut_seconds", "recompose_note")


def _fit_out(fit: dict) -> dict:
    """pipeline.voiceover_fit's result as the API's fit fields."""
    return {"duration_seconds": fit["audio_seconds"], **{k: fit[k] for k in _FIT_KEYS}}


@router.get("/projects/{project_id}/voiceover/fit", response_model=VoiceoverFitOut)
def uploaded_voiceover_fit(project_id: int, db: Session = Depends(get_db)) -> dict:
    """The uploaded voiceover's length against the shots, in plain words:
    a long one is cut at the end of the video, and this says so before
    anything is rendered."""
    from ..services.renderer import _probe_duration

    project = db.get(models.VideoProject, project_id)
    if project is None or not project.voiceover_upload_path:
        raise HTTPException(404, "no voiceover uploaded")
    path = Path(project.voiceover_upload_path)
    if not path.is_file():
        raise HTTPException(404, "voiceover file missing")
    fit = pipeline.voiceover_fit(project, float(_probe_duration(path) or 0.0))
    return _fit_out(fit)


@router.get("/projects/{project_id}/voiceover")
def stream_voiceover(project_id: int, db: Session = Depends(get_db)) -> FileResponse:
    project = db.get(models.VideoProject, project_id)
    if project is None or not project.voiceover_upload_path:
        raise HTTPException(404, "no voiceover uploaded")
    path = Path(project.voiceover_upload_path)
    if not path.exists():
        raise HTTPException(404, "voiceover file missing")
    return FileResponse(path)


@router.post("/projects/{project_id}/music", response_model=ProjectOut)
async def upload_music(
    project_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> models.VideoProject:
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    path = _save_audio(file, project_id=project_id, kind="music")
    project.music_upload_path = str(path)
    db.commit()
    db.refresh(project)
    return project


@router.delete("/projects/{project_id}/music", response_model=ProjectOut)
def delete_music(project_id: int, db: Session = Depends(get_db)) -> models.VideoProject:
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    if project.music_upload_path:
        try:
            Path(project.music_upload_path).unlink(missing_ok=True)
        except OSError:
            pass
    project.music_upload_path = ""
    db.commit()
    db.refresh(project)
    return project


@router.get("/projects/{project_id}/music")
def stream_music(project_id: int, db: Session = Depends(get_db)) -> FileResponse:
    project = db.get(models.VideoProject, project_id)
    if project is None or not project.music_upload_path:
        raise HTTPException(404, "no music uploaded")
    path = Path(project.music_upload_path)
    if not path.exists():
        raise HTTPException(404, "music file missing")
    return FileResponse(path)


class MusicGenerateIn(BaseModel):
    prompt: str
    duration_seconds: float | None = None


@router.post("/projects/{project_id}/music/generate", response_model=ProjectOut, dependencies=[Depends(rate_limit("generation")), Depends(budget_guard)])
def generate_music(
    project_id: int,
    body: MusicGenerateIn,
    db: Session = Depends(get_db),
) -> models.VideoProject:
    """Synchronously generate a music bed from a prompt via the MusicProvider
    and attach it to the project. The mock provider returns a deterministic
    triad pad; the real ElevenLabs adapter calls /v1/music."""
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    try:
        pipeline.generate_music_for_project(
            db, project_id, prompt=body.prompt, duration_seconds=body.duration_seconds
        )
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(502, f"music provider failed: {e}") from e
    db.refresh(project)
    return project


# ---------- hear the voiceover before paying for video ----------


def _preview_out(db: Session, project: models.VideoProject, info: dict, *, reused: bool = False) -> dict:
    fit = pipeline.voiceover_fit(project, float(info.get("duration_seconds") or 0.0))
    fp = str(info.get("fingerprint") or "")
    return {
        "url": f"/api/projects/{project.id}/voiceover/preview/audio?v={fp[:12] or 'x'}",
        "source": info.get("source", "preview"),
        "fingerprint": fp,
        "current": bool(info.get("current")),
        **_fit_out(fit),
        "cost_estimate": float(info.get("cost_estimate") or 0.0),
        "reused": reused,
        "created_at": info.get("created_at"),
    }


_preview_locks: dict[int, threading.Lock] = {}
_preview_locks_guard = threading.Lock()


def _preview_lock(project_id: int) -> threading.Lock:
    """One voiceover at a time per project (a double click must not pay twice)."""
    with _preview_locks_guard:
        return _preview_locks.setdefault(project_id, threading.Lock())


@router.post("/projects/{project_id}/voiceover/preview", response_model=VoiceoverPreviewOut,
             dependencies=[Depends(rate_limit("generation")), Depends(budget_guard)])
def make_voiceover_preview(project_id: int, db: Session = Depends(get_db)) -> dict:
    """Make the voiceover from the current spoken script and voice (paid
    TTS), so it can be heard before any clip is paid for. The next Generate
    video uses it at no charge. When an identical voiceover already exists,
    nothing is paid for and that one comes back (`reused: true`)."""
    from ..services import fingerprints, provider_gate, spend

    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    if not project.generated_plan_json:
        raise HTTPException(409, "generate a storyboard plan first")
    if fingerprints.voice_source(project) != "tts":
        raise HTTPException(409, str(pipeline.VoiceoverNotSynthesized(project)))
    # A render makes its own voiceover; making one here at the same time
    # would pay for the same voiceover twice.
    refuse_while_busy(db, project, "The voiceover")
    if not pipeline._reusable_voiceover(db, project, fingerprints.voice_fingerprint(db, project)):
        provider_gate.require_model("tts")
    if not _preview_lock(project.id).acquire(blocking=False):
        raise HTTPException(409, "A voiceover for this project is already being made. Wait for it to finish.")
    try:
        with spend.scope(project_id=project.id):
            made = pipeline.make_voiceover_preview(db, project)
    except pipeline.VoiceoverFailed as e:
        raise HTTPException(502, f"Voiceover failed: {e}") from e
    finally:
        _preview_lock(project.id).release()
    db.refresh(project)
    info = pipeline.current_voiceover(db, project) or {}
    if info.get("path") != made["path"]:  # (cannot happen: the one just made is current)
        info = {"source": "preview", "path": made["path"], "fingerprint": made["fingerprint"], "current": True}
    info = {**info, "cost_estimate": made["cost_estimate"]}
    return _preview_out(db, project, info, reused=made["reused"])


@router.get("/projects/{project_id}/voiceover/preview", response_model=VoiceoverPreviewOut)
def get_voiceover_preview(project_id: int, db: Session = Depends(get_db)) -> dict:
    """The voiceover to play in the editor (the one made with "Make
    voiceover", else the last render's), or 404 when there is none."""
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    info = pipeline.current_voiceover(db, project)
    if info is None:
        raise HTTPException(404, "no voiceover yet")
    return _preview_out(db, project, info)


@router.get("/projects/{project_id}/voiceover/preview/audio")
def stream_voiceover_preview(project_id: int, db: Session = Depends(get_db)) -> FileResponse:
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    info = pipeline.current_voiceover(db, project)
    if info is None or not Path(info["path"]).is_file():
        raise HTTPException(404, "no voiceover yet")
    return FileResponse(info["path"], media_type="audio/mp4")

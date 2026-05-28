from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from pydantic import BaseModel

from .. import models
from ..config import get_settings
from ..db import get_db
from ..schemas import ProjectOut
from ..services import pipeline, storage
from .auth import require_auth

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


@router.post("/projects/{project_id}/music/generate", response_model=ProjectOut)
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

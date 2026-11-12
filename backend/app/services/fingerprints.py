"""Fingerprints of what a clip or a voiceover was made from.

A storyboard render reuses a shot's clip when it still matches the shot, and
the voiceover when the spoken script, voice and voice model are unchanged.
A fingerprint is a hash of exactly the inputs that were sent to the
provider, stored when the clip / voiceover is made and compared with the
shot's current settings later.

An empty stored fingerprint means "made before fingerprints existed": such a
clip counts as current (it is never regenerated just for lacking one).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session, object_session

from .. import models
from ..config import get_settings


def _digest(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _spoken_script(project: models.VideoProject) -> str:
    return str((project.generated_plan_json or {}).get("cleaned_voice_script") or "")


def voice_source(project: models.VideoProject) -> str:
    """Where the voiceover really comes from: "upload", "silent" or "tts"
    (an upload whose file is gone falls back to TTS, as the render does).
    TTS with an empty spoken script is "silent": there is nothing to say."""
    source = (project.voiceover_source or "tts").lower()
    if source == "upload" and Path(project.voiceover_upload_path or "").is_file():
        return "upload"
    if source == "silent" or not _spoken_script(project).strip():
        return "silent"
    return "tts"


def tts_voice_id(db: Session, project: models.VideoProject) -> str:
    """The voice the TTS speaks with: the project's own choice, else the
    primary character's voice ("" = the provider default)."""
    if (project.voice_id or "").strip():
        return project.voice_id.strip()
    avatar = db.get(models.Avatar, project.primary_avatar_id) if project.primary_avatar_id else None
    return (avatar.elevenlabs_voice_id if avatar else "") or ""


def tts_identity(db: Session, project: models.VideoProject) -> dict:
    """The voice the TTS speaks with right now: voice, voice model and its
    options (not the script). JSON-plain, so it compares equal to a copy
    stored in a render's composition_json."""
    from . import model_registry as reg

    voice_id = tts_voice_id(db, project)
    return json.loads(json.dumps({
        "voice_id": voice_id,
        "default_voice": "" if voice_id else get_settings().elevenlabs_default_voice_id,
        "model": reg.active_model("tts") or "",
        "options": reg.active_options("tts") or {},
    }, sort_keys=True, ensure_ascii=False, default=str))


def voice_fingerprint(db: Session, project: models.VideoProject) -> str:
    """What the project's voiceover would be made from right now.
    "" for a silent project (there is no voiceover to compare)."""
    source = voice_source(project)
    if source == "silent":
        return ""
    if source == "upload":
        path = Path(project.voiceover_upload_path)
        stat = path.stat()
        return _digest({"source": "upload", "path": str(path), "size": stat.st_size,
                        "mtime": stat.st_mtime_ns})
    from . import model_registry as reg

    voice_id = tts_voice_id(db, project)
    return _digest({
        "source": "tts",
        "script": _spoken_script(project),
        "voice_id": voice_id,
        "default_voice": "" if voice_id else get_settings().elevenlabs_default_voice_id,
        "model": reg.active_model("tts") or "",
        "options": reg.active_options("tts") or {},
    })


def reference_ids(project: models.VideoProject, shot: models.VideoShot) -> list[int]:
    """The asset ids a shot's clip is made with: its own picks, else the
    first two pictures of each cast member (same rule as the render)."""
    from .pipeline import _asset_ids

    ids = _asset_ids(shot.reference_asset_ids_json)
    if not ids:
        for cm in project.cast_members:
            owner = cm.avatar if cm.member_kind == "avatar" else cm.ingredient
            if owner is None:
                continue
            ids.extend(a.id for a in owner.assets[:2])
    return sorted(set(ids))


def video_fingerprint(project: models.VideoProject, shot: models.VideoShot) -> str:
    """A clip from the video model: prompt, avoid text, references, the
    shot's own model choice and duration."""
    return _digest({
        "kind": "video",
        "prompt": shot.prompt or "",
        "avoid": shot.negative_prompt or "",
        "references": reference_ids(project, shot),
        "strategy": shot.reference_strategy or "input_references",
        "model": shot.model_override or "",
        "duration": round(float(shot.duration_seconds or 5.0), 3),
    })


def lipsync_fingerprint(project: models.VideoProject, shot: models.VideoShot, *,
                        voice: str, start: float, portrait_id: Optional[int]) -> str:
    """A lip-synced clip: the voiceover it was cut from, where its slice
    starts, how long it is, and the face it animates. `voice` is that
    voiceover's fingerprint; "" (unknown) gives "" so the clip counts as
    current rather than being flagged on a guess."""
    if not voice:
        return ""
    return _digest({
        "kind": "lipsync",
        "voice": voice,
        "start": round(float(start), 3),
        "duration": round(float(shot.duration_seconds or 5.0), 3),
        "portrait": portrait_id,
        "motion": shot.camera_direction or "",
    })


def current_fingerprint(db: Session, project: models.VideoProject, shot: models.VideoShot) -> str:
    """The fingerprint the shot's existing clip would need to still match."""
    if shot.provider == "lipsync":
        from .pipeline import _hero_portrait, _shot_start

        portrait = _hero_portrait(db, project)
        return lipsync_fingerprint(project, shot, voice=voice_fingerprint(db, project),
                                   start=_shot_start(project, shot),
                                   portrait_id=portrait.id if portrait else None)
    return video_fingerprint(project, shot)


def aspect_changed(shot: models.VideoShot, project: Optional[models.VideoProject] = None) -> bool:
    """The clip was made at another aspect ratio than the project's now.
    "" (not known) never counts as changed."""
    project = project or shot.project
    made = (shot.clip_aspect_ratio or "").strip()
    return bool(made and project is not None and made != (project.aspect_ratio or "").strip())


def shot_is_stale(shot: models.VideoShot) -> bool:
    """True when the shot has a clip that no longer matches its settings."""
    if shot.status != "completed" or not shot.clip_path:
        return False
    if shot.shot_type in ("end_card", "ambient_segment"):
        return False
    db = object_session(shot)
    project = shot.project
    if db is None or project is None:
        return False
    if project.mode != "ambient_loop" and aspect_changed(shot, project):
        return True  # the video's shape changed: every clip is made again
    if not shot.clip_fingerprint:
        return False
    if shot.provider == "lipsync" and voice_source(project) == "silent":
        return False  # no voiceover to be out of step with
    return current_fingerprint(db, project, shot) != shot.clip_fingerprint

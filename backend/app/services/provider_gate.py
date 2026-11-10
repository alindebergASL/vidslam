"""Refuse paid work up front when a provider it needs is switched off in
Settings → API keys (ProviderUnavailable → HTTP 409), instead of queueing a
job that fails in the worker or, worse, half-runs. Work already under way
finishes (provider_keys.continuing)."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from . import fingerprints, provider_keys
from .model_registry import active_model, provider_for, require


def require_model(kind: str, model: Optional[str] = None, db: Optional[Session] = None) -> None:
    """`model` (or the active one) for `kind` must be servable. A trained
    LoRA (custom:<id>) needs the provider that trained it and the one that
    runs it (routed as the job will: see registry._route)."""
    m = model or active_model(kind)
    if m.startswith("custom:") and db is not None:
        try:
            cm = db.get(models.CustomModel, int(m.split(":", 1)[1]))
        except (TypeError, ValueError):
            cm = None
        p = getattr(cm, "provider", "") or ""
        if p in provider_keys.KEYS and not get_settings().mock_providers and provider_keys.has(p) \
                and not provider_keys.enabled(p):
            raise provider_keys.ProviderUnavailable(p, kind=kind, model=m)
        from ..providers.registry import routes_per_model
        from .pipeline import _resolve_model

        resolved = _resolve_model(db, m)
        require(kind, resolved if routes_per_model(kind, resolved) else None)
        return
    require(kind, model)


def require_sound() -> None:
    if not get_settings().music_is_mocked() and not provider_keys.serves("elevenlabs"):
        raise provider_keys.ProviderUnavailable("elevenlabs", kind="sound")


def require_still(cfg) -> None:  # noqa: ANN001 — AmbientConfig
    """The scene's still: its own choice must be servable; a default chain
    needs one model on a real provider (a switched-off one isn't replaced
    by placeholder output)."""
    from . import ambient

    chain = ambient.still_model_chain(cfg)
    if cfg.still_model:
        require("image", cfg.still_model)
        return
    routes = [provider_for("image", m) for m in chain]
    if "unavailable" in routes and not any(r not in ("unavailable", "mock") for r in routes) \
            and not get_settings().mock_providers:
        require("image", chain[routes.index("unavailable")])


def require_ambient(cfg, *, regenerate_music: bool = True,  # noqa: ANN001 — AmbientConfig
                    project: Optional[models.VideoProject] = None) -> None:
    """Everything generating this scene will call (the same decisions as
    generate_loop / _finalize_generation: music and sound only when made)."""
    from . import ambient

    if cfg.source == "video":
        require("video", ambient._video_model_for(cfg))
    if ambient.uses_still_model(cfg):
        require_still(cfg)
    has_music = bool(project is not None and project.music_upload_path and Path(project.music_upload_path).exists())
    if ambient.resolve_music_prompt(cfg) and (regenerate_music or not has_music):
        require("music")
    has_sound = project is not None and ambient.sound_path(project).exists()
    if ambient.resolve_sound_prompt(cfg) and (regenerate_music or not has_sound):
        require_sound()


def _shots_to_submit(project: models.VideoProject, only: Optional[set[int]] = None) -> list[models.VideoShot]:
    """The shots a render (or a regen of `only`) will submit: render_project
    reuses a completed shot whose clip is on disk, and draws end cards."""
    out = []
    for sh in project.shots:
        if sh.shot_type == "end_card":
            continue
        if only is not None:
            if sh.id in only:
                out.append(sh)
            continue
        if sh.status == "completed" and sh.clip_path and Path(sh.clip_path).exists() \
                and not fingerprints.shot_is_stale(sh):
            continue  # reused; an edited shot is made again, so it is checked
        out.append(sh)
    return out


def _require_voice(project: models.VideoProject) -> None:
    """TTS whenever the voiceover will be synthesised (generate_audio)."""
    source = (project.voiceover_source or "tts").lower()
    if source == "silent":
        return
    if source == "upload" and Path(project.voiceover_upload_path or "").exists():
        return
    require("tts")


def require_project(db: Session, project: models.VideoProject, *,
                    shot_ids: Optional[set[int]] = None) -> None:
    """A storyboard video: the video model of every shot that will be
    submitted, and the voice when it's synthesised (not for a regen of
    chosen shots, which reuses the voiceover)."""
    for m in sorted({sh.model_override or "" for sh in _shots_to_submit(project, shot_ids)}):
        require_model("video", m or None, db)
    if shot_ids is None:
        _require_voice(project)

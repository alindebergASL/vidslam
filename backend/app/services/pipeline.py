from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from ..db import SessionLocal
from ..providers import get_chat, get_tts, get_video
from ..providers.base import CastContext, ImageRef
from ..schemas.storyboard import StoryboardPlan
from . import alerts, render_files, storage
from .captions import chunks_to_timed
from .model_registry import active_model as _active_model
from .renderer import RenderInputs, compose

log = logging.getLogger("avs.pipeline")
settings = get_settings()


# ---------- helpers ----------


def _build_cast_context(db: Session, project: models.VideoProject) -> CastContext:
    avatars_payload = []
    ingredients_payload = []
    for cm in project.cast_members:
        if cm.member_kind == "avatar" and cm.avatar is not None:
            a = cm.avatar
            refs = [
                {"url": storage.public_url_for_token(asset.public_token), "type": asset.asset_type}
                for asset in a.assets[:6]
            ]
            avatars_payload.append(
                {
                    "name": a.name,
                    "role": cm.role,
                    "persona": a.persona,
                    "visual_identity": a.visual_identity,
                    "asset_types": sorted({asset.asset_type for asset in a.assets}),
                    "refs": refs,
                }
            )
        elif cm.member_kind == "ingredient" and cm.ingredient is not None:
            i = cm.ingredient
            refs = [
                {"url": storage.public_url_for_token(asset.public_token), "type": asset.asset_type}
                for asset in i.assets[:6]
            ]
            ingredients_payload.append(
                {
                    "name": i.name,
                    "kind": i.kind,
                    "visual_identity": i.visual_identity,
                    "refs": refs,
                }
            )
    return CastContext(avatars=avatars_payload, ingredients=ingredients_payload)


def _project_dict(project: models.VideoProject) -> dict:
    return {
        "title": project.title,
        "mode": project.mode,
        "aspect_ratio": project.aspect_ratio,
        "target_duration_seconds": project.target_duration_seconds,
        "cta_text": project.cta_text,
        "caption_style": project.caption_style,
        "include_disclosure": project.include_disclosure,
        "disclosure_text": project.disclosure_text,
        "creative_direction": project.creative_direction,
        "original_script": project.original_script,
    }


def _brand_kit_for(db: Session, project: models.VideoProject) -> Optional[models.BrandKit]:
    if not project.brand_kit_id:
        return None
    return db.get(models.BrandKit, project.brand_kit_id)


def _disclosure_for(
    project: models.VideoProject,
    plan: StoryboardPlan,
    brand: Optional[models.BrandKit] = None,
) -> Optional[str]:
    """Burn-in disclosure precedence: project override → brand kit → planner → default.
    Returns None when the project disables the overlay."""
    if not project.include_disclosure:
        return None
    return (
        (project.disclosure_text or "").strip()
        or (brand.default_disclosure_text.strip() if brand and brand.default_disclosure_text else "")
        or (plan.disclosure_text or "").strip()
        or "AI-generated virtual creator"
    )


def _log_provider_call(
    db: Session,
    *,
    project_id: Optional[int],
    provider: str,
    endpoint: str,
    request_summary: dict,
    response_summary: dict,
    status: str = "ok",
) -> None:
    db.add(
        models.ProviderLog(
            project_id=project_id,
            provider=provider,
            endpoint=endpoint,
            request_summary_json=request_summary,
            response_summary_json=response_summary,
            status=status,
        )
    )
    db.commit()
    if status == "error":
        alerts.provider_error(provider, endpoint, str((response_summary or {}).get("error", "")))


def _resolve_model(db: Session, model: str) -> str:
    """Expand a `custom:<id>` selector to the trained model's
    `provider_model_id`. Studio + (eventually) shot pipelines surface user-
    trained LoRAs as `custom:<CustomModel.id>` in their dropdowns; the
    underlying provider needs the real adapter id (a Replicate version
    hash or, in mock mode, a deterministic `mock/<digest>`). Untrained
    model strings pass through unchanged."""
    if not model.startswith("custom:"):
        return model
    try:
        cm_id = int(model.split(":", 1)[1])
    except (IndexError, ValueError):
        return model
    cm = db.get(models.CustomModel, cm_id)
    if cm is None or cm.status != "completed" or not cm.provider_model_id:
        # Caller asked for a trained model that no longer applies; fall
        # back to the unresolved string so the provider raises a clean
        # error instead of silently using the wrong adapter.
        return model
    return cm.provider_model_id


def _resolve_references_for_shot(
    db: Session, project: models.VideoProject, shot: models.VideoShot
) -> list[ImageRef]:
    """Convert per-shot reference_asset_ids (or fall back to all project cast assets) → ImageRefs."""
    asset_ids: list[int] = list(shot.reference_asset_ids_json or [])
    if not asset_ids:
        for cm in project.cast_members:
            owner = cm.avatar if cm.member_kind == "avatar" else cm.ingredient
            if owner is None:
                continue
            for a in owner.assets[:2]:
                asset_ids.append(a.id)
    refs: list[ImageRef] = []
    if not asset_ids:
        return refs
    rows = db.query(models.Asset).filter(models.Asset.id.in_(asset_ids)).all()
    for row in rows:
        role = "character" if row.owner_kind == "avatar" else "scene"
        if row.owner_kind == "ingredient" and row.ingredient is not None:
            kind = row.ingredient.kind
            role = {
                "scene": "scene",
                "style": "style",
                "object": "object",
                "prop": "object",
            }.get(kind, "scene")
        name = ""
        if row.owner_kind == "avatar" and row.avatar is not None:
            name = row.avatar.name
        elif row.owner_kind == "ingredient" and row.ingredient is not None:
            name = row.ingredient.name
        refs.append(
            ImageRef(
                url=storage.public_url_for_token(row.public_token),
                role=role,  # type: ignore[arg-type]
                name=name,
            )
        )
    return refs


# ---------- public entry points ----------


def generate_plan(
    db: Session, project_id: int, *, preserve_edits: bool = False
) -> StoryboardPlan:
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise ValueError(f"project {project_id} not found")
    project.status = "planning"
    db.commit()

    chat = get_chat()
    cast = _build_cast_context(db, project)
    try:
        plan = chat.generate_storyboard(project=_project_dict(project), cast=cast)
    except Exception as e:  # noqa: BLE001
        project.status = "failed"
        db.commit()
        _log_provider_call(
            db,
            project_id=project.id,
            provider=type(chat).__name__,
            endpoint="generate_storyboard",
            request_summary={"title": project.title, "mode": project.mode},
            response_summary={"error": str(e)[:500]},
            status="error",
        )
        raise

    project.generated_plan_json = plan.model_dump()
    project.status = "planned"

    # Reconcile shots against the new plan.
    #
    # When `preserve_edits` is set we keep user-editable fields (prompt,
    # duration, references, caption) on shots that match an existing order,
    # only refreshing fields the planner is authoritative for. New plan
    # entries get added; existing shots whose order isn't in the new plan
    # get dropped. This lets a creator iterate on the script and re-plan
    # without losing the per-shot tweaks they've already made.
    #
    # When unset (default), wipe and recreate — the legacy behavior, so
    # callers that *want* a clean slate (e.g. the very first plan) still
    # work without an extra flag.
    if preserve_edits and project.shots:
        existing = {s.shot_order: s for s in project.shots}
        new_orders = {s.order for s in plan.shots}
        for s in plan.shots:
            if s.order in existing:
                shot = existing[s.order]
                # Planner-authoritative fields refresh; user-editable fields
                # (prompt, duration_seconds, reference_asset_ids_json,
                # caption_text) stay untouched.
                shot.shot_type = s.shot_type
                shot.negative_prompt = s.negative_prompt
                shot.reference_strategy = s.reference_strategy
                shot.camera_direction = s.camera_direction
            else:
                db.add(
                    models.VideoShot(
                        project_id=project.id,
                        shot_order=s.order,
                        shot_type=s.shot_type,
                        prompt=s.visual_prompt,
                        negative_prompt=s.negative_prompt,
                        duration_seconds=s.duration_seconds,
                        reference_strategy=s.reference_strategy,
                        caption_text=s.caption_text,
                        camera_direction=s.camera_direction,
                        reference_asset_ids_json=[],
                    )
                )
        # Drop shots the new plan no longer includes.
        for order, shot in existing.items():
            if order not in new_orders:
                db.delete(shot)
    else:
        for s in list(project.shots):
            db.delete(s)
        db.flush()
        for s in plan.shots:
            db.add(
                models.VideoShot(
                    project_id=project.id,
                    shot_order=s.order,
                    shot_type=s.shot_type,
                    prompt=s.visual_prompt,
                    negative_prompt=s.negative_prompt,
                    duration_seconds=s.duration_seconds,
                    reference_strategy=s.reference_strategy,
                    caption_text=s.caption_text,
                    camera_direction=s.camera_direction,
                    reference_asset_ids_json=[],
                )
            )
    db.commit()

    _log_provider_call(
        db,
        project_id=project.id,
        provider=type(chat).__name__,
        endpoint="generate_storyboard",
        request_summary={"title": project.title, "mode": project.mode},
        response_summary={"shots": len(plan.shots), "duration": plan.estimated_duration_seconds},
    )
    return plan


def submit_shot_clip(
    db: Session,
    project: models.VideoProject,
    shot: models.VideoShot,
    *,
    render_id: Optional[int] = None,
    after: str = "compose",
    references: Optional[list[ImageRef]] = None,
    purpose: str = "shot",
    context: Optional[dict] = None,
    note: str = "",
) -> models.VideoJob:
    """Submit a shot's clip as a VideoJob and return without waiting.

    Completion (webhook or sweeper) lands in on_shot_clip_ready, which marks
    the shot done and — per `after` — composes the waiting render
    ("compose"), recomposes the project once every regenerated shot is back
    ("recompose"), or does nothing ("none"). `references` overrides the
    cast-asset lookup (the ambient chain pins first/last frames with it).
    `note` is kept on the shot's error once the clip lands (e.g. why a
    lip-sync shot fell back to the video model)."""
    from . import video_jobs

    video = get_video()
    refs = references if references is not None else _resolve_references_for_shot(db, project, shot)
    # Per-shot override (raw id or custom:<id> trained-LoRA selector) wins
    # over the deployment-wide default.
    model = _resolve_model(db, shot.model_override or _active_model("video"))
    shot.status = "submitted"
    shot.error = note
    shot.clip_path = ""
    shot.provider = "mock" if get_settings().video_is_mocked() else "openrouter"
    shot.provider_model = model
    db.commit()

    def start(callback: Optional[str]):
        settings_extra = {
            "aspect_ratio": project.aspect_ratio or None,
            # generate_audio defaults to true on audio-capable models (Veo…);
            # the renderer strips clip audio and lays in voice/music itself.
            "generate_audio": False,
        }
        if callback:
            settings_extra["callback_url"] = callback
        return video.submit(
            model=model, prompt=shot.prompt, references=refs,
            negative_prompt=shot.negative_prompt or "",
            reference_strategy=shot.reference_strategy or "input_references",
            duration_seconds=shot.duration_seconds or 5.0, settings=settings_extra,
        )

    try:
        job = video_jobs.submit(
            db, purpose=purpose, ref_id=shot.id, project_id=project.id, render_id=render_id,
            context={"after": after, **({"note": note} if note else {}), **(context or {})}, start=start,
        )
    except Exception as e:  # noqa: BLE001 — submit itself failed
        shot.status = "failed"
        shot.error = str(e)[:500]
        db.commit()
        _log_provider_call(db, project_id=project.id, provider="video", endpoint="video.submit",
                           request_summary={"shot": shot.id, "model": model},
                           response_summary={"error": str(e)[:500]}, status="error")
        raise
    _log_provider_call(db, project_id=project.id, provider=job.provider, endpoint="video.submit",
                       request_summary={"model": model, "refs": len(refs), "shot": shot.id,
                                        "webhook": job.uses_webhook},
                       response_summary={"job": job.provider_job_id})
    return job


def on_shot_clip_ready(db: Session, job: models.VideoJob) -> None:
    shot = db.get(models.VideoShot, job.ref_id)
    if shot is None:
        return
    path = Path(job.result_path)
    if job.purpose == "avatar":
        # Avatar models size the clip to their audio/script; trim to the shot.
        from .renderer import trim_clip

        trimmed = path.with_name(f"shot_{shot.shot_order:02d}_{shot.id}.mp4")
        trim_clip(path, trimmed, duration=float((job.context_json or {}).get("duration") or shot.duration_seconds or 5.0))
        path.unlink(missing_ok=True)
        path = trimmed
    shot.clip_path = str(path)
    shot.status = "completed"
    shot.error = (job.context_json or {}).get("note", "")
    db.commit()
    _after_shot(db, job)


def on_shot_clip_failed(db: Session, job: models.VideoJob) -> None:
    shot = db.get(models.VideoShot, job.ref_id)
    if shot is None:
        return
    project = db.get(models.VideoProject, job.project_id)
    if job.purpose == "avatar" and project is not None:
        # Lip-sync failed → fall back to the regular video model for this shot.
        shot.error = f"lip-sync failed, used video model instead: {job.error[:300]}"
        db.commit()
        _log_provider_call(db, project_id=project.id, provider="avatar", endpoint="avatar.submit",
                           request_summary={"shot": shot.id}, response_summary={"error": job.error[:500]},
                           status="error")
        try:
            submit_shot_clip(db, project, shot, render_id=job.render_id,
                             after=(job.context_json or {}).get("after", "compose"), note=shot.error)
        except Exception:  # noqa: BLE001
            log.exception("fallback video submit failed for shot %s", shot.id)
            _after_shot(db, job)
        return
    shot.status = "failed"
    shot.error = job.error[:500]
    db.commit()
    _log_provider_call(db, project_id=job.project_id, provider=job.provider or "video",
                       endpoint="video.job", request_summary={"shot": shot.id, "job": job.id},
                       response_summary={"error": job.error[:500]}, status="error")
    _after_shot(db, job)


def _after_shot(db: Session, job: models.VideoJob) -> None:
    after = (job.context_json or {}).get("after", "compose")
    if after == "compose" and job.render_id:
        maybe_compose(db, job.render_id)
    elif after == "recompose" and job.project_id:
        maybe_recompose(db, job.project_id)


def _body_shots(project: models.VideoProject) -> list[models.VideoShot]:
    return [s for s in project.shots if s.shot_type not in ("end_card", "ambient_segment")]


def maybe_compose(db: Session, render_id: int) -> None:
    """Compose once every body shot of the render's project has its clip.
    Exactly-once: the render moves generating_shots → rendering atomically."""
    from sqlalchemy import update as sa_update

    from ..workers.jobs import compose_render_job
    from ..workers.queue import enqueue

    render = db.get(models.Render, render_id)
    if render is None or render.status != "generating_shots":
        return
    db.refresh(render)
    project = db.get(models.VideoProject, render.project_id)
    db.refresh(project)
    shots = _body_shots(project)
    for s in shots:
        db.refresh(s)
    failed = [s for s in shots if s.status == "failed"]
    if failed:
        _fail_render(db, render, project, f"shot {failed[0].shot_order} failed: {failed[0].error[:300]}")
        return
    if any(s.status != "completed" or not s.clip_path for s in shots):
        return
    res = db.execute(sa_update(models.Render)
                     .where(models.Render.id == render_id, models.Render.status == "generating_shots")
                     .values(status="rendering"))
    db.commit()
    if res.rowcount == 1:
        enqueue(compose_render_job, render_id)


def maybe_recompose(db: Session, project_id: int) -> None:
    """After shot regenerations: recompose once none are outstanding."""
    from sqlalchemy import update as sa_update

    from ..workers.jobs import recompose_after_regen_job
    from ..workers.queue import enqueue
    from . import video_jobs

    if video_jobs.pending_for(db, project_id=project_id) > 0:
        return
    res = db.execute(sa_update(models.VideoProject)
                     .where(models.VideoProject.id == project_id, models.VideoProject.status == "regenerating")
                     .values(status="generating"))
    db.commit()
    if res.rowcount == 1:
        enqueue(recompose_after_regen_job, project_id)


def _fail_render(db: Session, render: models.Render, project: models.VideoProject, error: str) -> None:
    render.status = "failed"
    render.error = error[:800]
    project.status = "failed"
    db.commit()
    alerts.render_failed("render", project.id, project.title, error)


def _hero_portrait(db: Session, project: models.VideoProject) -> Optional[models.Asset]:
    """The primary avatar's hero image (or first image) — the face a
    talking-head model animates."""
    avatar = db.get(models.Avatar, project.primary_avatar_id) if project.primary_avatar_id else None
    if avatar is None:
        for cm in project.cast_members:
            if cm.member_kind == "avatar" and cm.avatar is not None:
                avatar = cm.avatar
                break
    if avatar is None or not avatar.assets:
        return None
    heroes = [a for a in avatar.assets if a.asset_type == "hero"]
    return (heroes or avatar.assets)[0]


def _shot_start(project: models.VideoProject, shot: models.VideoShot) -> float:
    """Where `shot` starts in the voiceover: the summed length of the body
    shots before it (end cards are appended after the voice)."""
    t = 0.0
    for s in project.shots:
        if s.id == shot.id:
            break
        if s.shot_type != "end_card":
            t += float(s.duration_seconds or 0)
    return t


def submit_talking_head(
    db: Session, project: models.VideoProject, shot: models.VideoShot,
    *, plan: StoryboardPlan, audio_path: str, start: float, render_id: Optional[int],
    after: str = "compose",
) -> bool | str:
    """Submit a lip-synced talking-head job (portrait + this shot's slice of
    the voiceover) when a lip-sync model is active. Returns True when
    submitted; False when lip-sync is off or there's no portrait; or, when
    the submit itself fails, a note to keep on the shot. Anything but True
    means the caller uses the regular video model.
    If the avatar job later fails, on_shot_clip_failed falls back to the
    regular video model for this shot."""
    from ..providers import get_avatar
    from . import public_media, video_jobs
    from .renderer import slice_audio

    avatar = get_avatar()
    if avatar is None:
        return False
    portrait = _hero_portrait(db, project)
    if portrait is None:
        log.info("talking-head lip-sync skipped for shot %s: no avatar image", shot.id)
        return False
    out_dir = storage.render_subdir(project.id)
    dur = float(shot.duration_seconds or 5.0)
    body = [s for s in project.shots if s.shot_type != "end_card"]
    audio_url = None
    if audio_path and Path(audio_path).exists():
        # Each talking-head shot speaks only its own part of the voiceover,
        # so lips, captions and the voice track stay aligned.
        seg = slice_audio(Path(audio_path), out_dir / f"voice_shot_{shot.id}.m4a", start=start, duration=dur)
        audio_url = public_media.publish(seg, "m4a")
    script = (plan.cleaned_voice_script if len(body) == 1 else "") or shot.caption_text \
        or plan.cleaned_voice_script or shot.prompt
    shot.status = "submitted"
    shot.provider = "lipsync"
    shot.provider_model = getattr(avatar, "model", "")
    shot.error = ""
    shot.clip_path = ""
    db.commit()

    def start_fn(callback: Optional[str]):
        return avatar.submit_avatar(
            portrait_url=storage.public_url_for_token(portrait.public_token), audio_url=audio_url,
            script=script, aspect_ratio=project.aspect_ratio, motion_prompt=shot.camera_direction or "",
            duration_seconds=dur, callback_url=callback,
        )

    try:
        video_jobs.submit(db, purpose="avatar", ref_id=shot.id, project_id=project.id, render_id=render_id,
                          context={"after": after, "duration": dur}, start=start_fn)
    except Exception as e:  # noqa: BLE001 — submit failed: use the video model
        log.warning("talking-head submit failed for shot %s, falling back: %s", shot.id, e)
        db.rollback()
        return f"lip-sync failed, used video model instead: {str(e)[:300]}"
    return True


def generate_audio(
    db: Session, project: models.VideoProject, *, plan: StoryboardPlan
) -> Optional[Path]:
    """Produce the project's voiceover track. Source is controlled by
    `project.voiceover_source`:

      - "upload": return the user-uploaded file from `voiceover_upload_path`.
      - "silent": return None (renderer will skip the voice track).
      - "tts" (default): call the TTS provider.
    """
    source = (project.voiceover_source or "tts").lower()
    if source == "upload":
        path = Path(project.voiceover_upload_path or "")
        if path.exists():
            return path
        log.warning(
            "voiceover_source=upload but file missing for project %s; falling back to TTS",
            project.id,
        )
        source = "tts"
    if source == "silent":
        return None

    tts = get_tts()
    voice_id = ""
    primary = (
        db.get(models.Avatar, project.primary_avatar_id) if project.primary_avatar_id else None
    )
    if primary and primary.elevenlabs_voice_id:
        voice_id = primary.elevenlabs_voice_id
    try:
        audio_bytes = tts.synthesize(text=plan.cleaned_voice_script, voice_id=voice_id)
        out_dir = storage.render_subdir(project.id)
        audio_path = out_dir / "voice.m4a"
        audio_path.write_bytes(audio_bytes)
        _log_provider_call(
            db,
            project_id=project.id,
            provider=type(tts).__name__,
            endpoint="tts.synthesize",
            request_summary={"chars": len(plan.cleaned_voice_script), "voice_id": bool(voice_id)},
            response_summary={"bytes": len(audio_bytes)},
        )
        return audio_path
    except Exception as e:  # noqa: BLE001
        _log_provider_call(
            db,
            project_id=project.id,
            provider=type(tts).__name__,
            endpoint="tts.synthesize",
            request_summary={"chars": len(plan.cleaned_voice_script)},
            response_summary={"error": str(e)[:500]},
            status="error",
        )
        return None


def _music_path_if_any(project: models.VideoProject) -> Optional[Path]:
    p = Path(project.music_upload_path or "") if project.music_upload_path else None
    return p if p and p.exists() else None


class RenderCancelled(Exception):
    """Raised by the pipeline when a cancel request flips the render to
    status='cancelled' between cooperative checkpoints."""


def _check_cancel(db: Session, render: models.Render) -> None:
    """Cooperative cancel check. Reads the render's status from a fresh session so
    a parallel HTTP request that wrote status='cancelled' is visible."""
    probe = SessionLocal()
    try:
        fresh = probe.get(models.Render, render.id)
        if fresh and fresh.status == "cancelled":
            raise RenderCancelled()
    finally:
        probe.close()


def generate_music_for_project(
    db: Session,
    project_id: int,
    *,
    prompt: str,
    duration_seconds: Optional[float] = None,
) -> Path:
    """Generate a music bed via the MusicProvider and save it as the project's
    music_upload_path. Returns the saved file path. Overwrites any prior music."""
    from ..providers import get_music

    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise ValueError(f"project {project_id} not found")
    if not prompt or not prompt.strip():
        raise ValueError("music prompt is empty")

    music = get_music()
    target = float(duration_seconds or project.target_duration_seconds or 25)
    try:
        data = music.generate(prompt=prompt.strip(), duration_seconds=target)
        ext = music.output_extension() or "mp3"
    except Exception as e:  # noqa: BLE001
        _log_provider_call(
            db,
            project_id=project.id,
            provider=type(music).__name__,
            endpoint="music.generate",
            request_summary={"prompt_chars": len(prompt), "duration": target},
            response_summary={"error": str(e)[:500]},
            status="error",
        )
        raise

    # Remove any previous music file (any extension) so we never leave orphans.
    out_dir = storage.render_subdir(project.id)
    for stale in out_dir.glob("music.*"):
        try:
            stale.unlink()
        except OSError:
            pass
    if project.music_upload_path:
        try:
            Path(project.music_upload_path).unlink(missing_ok=True)
        except OSError:
            pass

    out_path = out_dir / f"music.{ext.lstrip('.')}"
    out_path.write_bytes(data)
    project.music_upload_path = str(out_path)
    db.commit()

    _log_provider_call(
        db,
        project_id=project.id,
        provider=type(music).__name__,
        endpoint="music.generate",
        request_summary={"prompt_chars": len(prompt), "duration": target},
        response_summary={"bytes": len(data), "ext": ext},
    )
    return out_path


def render_project(db: Session, project_id: int) -> models.Render:
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise ValueError(f"project {project_id} not found")
    if not project.generated_plan_json:
        raise ValueError("project has no storyboard plan yet")

    plan = StoryboardPlan.model_validate(project.generated_plan_json)
    from .cost import estimate_plan_cost

    estimate = estimate_plan_cost(
        {"voiceover_source": project.voiceover_source, "music_will_generate": False},
        plan,
    )
    render = models.Render(
        project_id=project.id,
        status="generating_audio",
        estimated_cost=estimate["total"],
    )
    db.add(render)
    # Mark the project itself as generating so clients polling project_status keep
    # polling while a worker-backed render runs in the background.
    project.status = "generating"
    db.commit()
    db.refresh(render)
    from . import spend

    spend.bind(project_id=project.id, render_id=render.id)  # voiceover etc. count toward this render

    try:
        _check_cancel(db, render)
        # 1. audio
        audio_path = generate_audio(db, project, plan=plan)
        if audio_path:
            render.audio_path = str(audio_path)
        render.status = "generating_shots"
        db.commit()
        _check_cancel(db, render)

        # 2. Submit every body shot at once (they generate in parallel at the
        #    provider); end cards are drawn by compose. Completion arrives by
        #    webhook / sweeper and maybe_compose() runs compose exactly once.
        project = db.get(models.VideoProject, project.id)  # refresh
        body = 0
        for shot in project.shots:
            _check_cancel(db, render)
            if shot.shot_type == "end_card":
                shot.status = "completed"
                db.commit()
                continue
            body += 1
            if shot.status == "completed" and shot.clip_path and Path(shot.clip_path).exists():
                continue  # reuse a clip that's already there
            note = ""
            if shot.shot_type == "talking_head":
                ok = submit_talking_head(
                    db, project, shot, plan=plan, audio_path=render.audio_path,
                    start=_shot_start(project, shot), render_id=render.id,
                )
                if ok is True:
                    continue
                note = ok or ""
                shot = db.get(models.VideoShot, shot.id)
            submit_shot_clip(db, project, shot, render_id=render.id, after="compose", note=note)
        if body == 0:
            raise RuntimeError("no clips were produced for any shot")
        maybe_compose(db, render.id)
        db.refresh(render)
        return render
    except RenderCancelled:
        log.info("render %s cancelled by user", render.id)
        # The render row was already flipped to 'cancelled' by the cancel endpoint;
        # mirror it onto the project so the editor leaves polling and offers Retry.
        render.status = "cancelled"
        render.error = ""
        project.status = "cancelled"
        db.commit()
        return render
    except Exception as e:  # noqa: BLE001
        log.exception("render failed")
        render.status = "failed"
        render.error = str(e)[:800]
        project.status = "failed"
        db.commit()
        alerts.render_failed("render", project.id, project.title, str(e))
        raise


def compose_render(db: Session, render_id: int) -> models.Render:
    """FFmpeg composition for a storyboard render whose clips are all in
    (called once by maybe_compose)."""
    render = db.get(models.Render, render_id)
    project = db.get(models.VideoProject, render.project_id)
    plan = StoryboardPlan.model_validate(project.generated_plan_json)
    try:
        _check_cancel(db, render)
        clip_paths = [Path(s.clip_path) for s in _body_shots(project)]
        body_duration = sum(s.duration_seconds for s in _body_shots(project))
        timed = chunks_to_timed(plan.caption_chunks, max(body_duration, 1.0))
        brand = _brand_kit_for(db, project)
        outputs = compose(
            RenderInputs(
                project_id=project.id,
                out_dir=storage.render_subdir(project.id),
                clip_paths=clip_paths,
                audio_path=Path(render.audio_path) if render.audio_path else None,
                caption_timings=timed,
                caption_style=project.caption_style or "clean_white",
                disclosure_text=_disclosure_for(project, plan, brand),
                cta_text=project.cta_text or None,
                aspect_ratio=project.aspect_ratio,
                music_path=_music_path_if_any(project),
                music_volume=float(project.music_volume or 0.25),
                end_card_bg_color=(brand.end_card_bg_color if brand else "#0E0E12"),
                end_card_text_color=(brand.end_card_text_color if brand else "#FFFFFF"),
                logo_path=(
                    Path(brand.logo_path) if brand and brand.logo_path and Path(brand.logo_path).exists() else None
                ),
                caption_color=(brand.primary_color if brand else None),
            )
        )
        render.render_log = outputs.log
        render_files.finalize(db, render, outputs.final_video_path, outputs.thumbnail_path)
        render.status = "completed"
        project.status = "completed"
        db.commit()
        _settle_render_cost(db, render)
        render_files.prune(db, project.id)
        return render
    except RenderCancelled:
        render.status = "cancelled"
        render.error = ""
        project.status = "cancelled"
        db.commit()
        return render
    except Exception as e:  # noqa: BLE001
        log.exception("compose failed")
        _fail_render(db, render, project, str(e))
        raise


def _settle_render_cost(db: Session, render: models.Render) -> None:
    """Actual cost = what providers reported for this render's jobs."""
    from . import spend

    render.actual_cost = spend.render_actual_cost(db, render)
    db.commit()


def _latest_voiceover(db: Session, project: models.VideoProject) -> Optional[str]:
    """The newest render's voiceover file, if it's still on disk."""
    for r in (db.query(models.Render).filter(models.Render.project_id == project.id,
                                            models.Render.audio_path != "")
              .order_by(models.Render.id.desc()).limit(5)):
        if Path(r.audio_path).exists():
            return r.audio_path
    return None


def _submit_regen(db: Session, project: models.VideoProject, shot: models.VideoShot, *, after: str) -> None:
    """Re-roll one shot the same way a full render makes it: talking-head
    shots go to the lip-sync model with their slice of the latest voiceover
    (falling back to the video model, with a note, if that isn't possible);
    everything else goes to the video model."""
    note = ""
    if shot.shot_type == "talking_head" and project.generated_plan_json:
        audio = _latest_voiceover(db, project)
        if audio:
            ok = submit_talking_head(
                db, project, shot, plan=StoryboardPlan.model_validate(project.generated_plan_json),
                audio_path=audio, start=_shot_start(project, shot), render_id=None, after=after,
            )
            if ok is True:
                return
            note = ok or ""
            shot = db.get(models.VideoShot, shot.id)
    submit_shot_clip(db, project, shot, after=after, note=note)


def regenerate_shot(
    db: Session, project_id: int, shot_id: int, *, recompose: bool = True
) -> None:
    """Regenerate one shot's clip (async). With `recompose`, the project is
    recomposed once the new clip is back."""
    project = db.get(models.VideoProject, project_id)
    shot = db.get(models.VideoShot, shot_id)
    if not project or not shot or shot.project_id != project.id:
        raise ValueError("shot not found in project")
    if recompose:
        project.status = "regenerating"
        db.commit()
    _submit_regen(db, project, shot, after="recompose" if recompose else "none")


def regenerate_shots_bulk(
    db: Session, project_id: int, shot_ids: list[int], *, recompose: bool = True
) -> list[int]:
    """Regenerate several shots at once (submitted in parallel); recompose
    once, after the last one is back. Returns the ids that were submitted."""
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise ValueError(f"project {project_id} not found")
    by_id = {s.id: s for s in project.shots}
    targets = [by_id[i] for i in shot_ids if i in by_id and by_id[i].shot_type != "end_card"]
    if recompose and targets:
        # Barrier: maybe_recompose only acts on "regenerating", so a clip that
        # completes while the rest are still being submitted (mock/inline, or
        # a very fast provider) can't recompose with the others' old clips.
        project.status = "submitting"
        db.commit()
    submitted: list[int] = []
    for shot in targets:
        try:
            _submit_regen(db, project, shot, after="recompose" if recompose else "none")
            submitted.append(shot.id)
        except Exception:  # noqa: BLE001
            log.exception("bulk regen failed for shot %s", shot.id)
    if recompose and targets:
        db.refresh(project)
        project.status = "regenerating"
        db.commit()
        maybe_recompose(db, project_id)  # all already back (or every submit failed)
    return submitted


def _latest_completed_audio(db: Session, project_id: int) -> Optional[Path]:
    """Find the audio file from the latest render that has one, if it still exists on disk."""
    rows = (
        db.query(models.Render)
        .filter(models.Render.project_id == project_id)
        .order_by(models.Render.created_at.desc())
        .all()
    )
    for r in rows:
        if r.audio_path and Path(r.audio_path).exists():
            return Path(r.audio_path)
    return None


def recompose_project(db: Session, project_id: int) -> models.Render:
    """Re-run only the FFmpeg composition step, reusing existing shot clips + audio.

    Used when a single shot has been regenerated and the user wants the final video
    rebuilt without paying for audio synthesis or other shots again. Creates a new
    Render row so the history of versions is preserved.
    """
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise ValueError(f"project {project_id} not found")
    if not project.generated_plan_json:
        raise ValueError("project has no storyboard plan yet")

    plan = StoryboardPlan.model_validate(project.generated_plan_json)

    missing: list[int] = []
    clip_paths: list[Path] = []
    for shot in project.shots:
        if shot.shot_type == "end_card":
            continue
        if not shot.clip_path or not Path(shot.clip_path).exists():
            missing.append(shot.shot_order)
            continue
        clip_paths.append(Path(shot.clip_path))
    if missing:
        raise ValueError(f"shots not yet generated: {missing}")
    if not clip_paths:
        raise ValueError("no shot clips available to recompose")

    audio = _latest_completed_audio(db, project_id)

    render = models.Render(
        project_id=project.id,
        status="rendering",
        audio_path=str(audio) if audio else "",
    )
    db.add(render)
    db.commit()
    db.refresh(render)

    try:
        body_duration = sum(
            s.duration_seconds for s in project.shots if s.shot_type != "end_card"
        )
        timed = chunks_to_timed(plan.caption_chunks, max(body_duration, 1.0))
        _check_cancel(db, render)
        brand = _brand_kit_for(db, project)
        out_dir = storage.render_subdir(project.id)
        outputs = compose(
            RenderInputs(
                project_id=project.id,
                out_dir=out_dir,
                clip_paths=clip_paths,
                audio_path=audio,
                caption_timings=timed,
                caption_style=project.caption_style or "clean_white",
                disclosure_text=_disclosure_for(project, plan, brand),
                cta_text=project.cta_text or None,
                aspect_ratio=project.aspect_ratio,
                music_path=_music_path_if_any(project),
                music_volume=float(project.music_volume or 0.25),
                end_card_bg_color=(brand.end_card_bg_color if brand else "#0E0E12"),
                end_card_text_color=(brand.end_card_text_color if brand else "#FFFFFF"),
                logo_path=(
                    Path(brand.logo_path) if brand and brand.logo_path and Path(brand.logo_path).exists() else None
                ),
                caption_color=(brand.primary_color if brand else None),
            )
        )
        render.render_log = outputs.log
        render_files.finalize(db, render, outputs.final_video_path, outputs.thumbnail_path)
        render.status = "completed"
        project.status = "completed"
        db.commit()
        render_files.prune(db, project.id)
        return render
    except RenderCancelled:
        log.info("recompose %s cancelled by user", render.id)
        render.status = "cancelled"
        render.error = ""
        project.status = "cancelled"
        db.commit()
        return render
    except Exception as e:  # noqa: BLE001
        log.exception("recompose failed")
        render.status = "failed"
        render.error = str(e)[:800]
        db.commit()
        raise


def run_studio_job(db: Session, job_id: int) -> models.AssetGenerationJob:
    job = db.get(models.AssetGenerationJob, job_id)
    if job is None:
        raise ValueError(f"studio job {job_id} not found")
    job.status = "submitted"
    db.commit()
    try:
        refs: list[ImageRef] = []
        if job.reference_asset_ids_json:
            rows = (
                db.query(models.Asset)
                .filter(models.Asset.id.in_(job.reference_asset_ids_json))
                .all()
            )
            for row in rows:
                role = "character" if row.owner_kind == "avatar" else "scene"
                refs.append(
                    ImageRef(url=storage.public_url_for_token(row.public_token), role=role)
                )

        if job.output_kind == "image":
            from ..providers import get_image

            image = get_image()
            model = _resolve_model(
                db,
                job.provider_model or _active_model("image"),
            )
            job.provider = "mock" if settings.image_is_mocked() else "openrouter"
            job.provider_model = model
            db.commit()
            data = image.generate(model=model, prompt=job.prompt, references=refs,
                                  negative_prompt=job.negative_prompt)
            ext, _mime, _w, _h = storage.image_info(data)
            path = storage.save_generated_bytes(data, extension=ext, subdir_name="images")
            job.result_path = str(path)
            job.status = "completed"
        else:  # video_clip — async: completes via webhook / sweeper
            from . import video_jobs

            video = get_video()
            model = _resolve_model(db, job.provider_model or _active_model("video"))
            job.provider = "mock" if settings.video_is_mocked() else "openrouter"
            job.provider_model = model
            job.status = "polling"
            db.commit()

            def start(callback):
                return video.submit(
                    model=model, prompt=job.prompt, references=refs,
                    negative_prompt=job.negative_prompt or "",
                    duration_seconds=float(job.duration_seconds or 5.0),
                    settings={"callback_url": callback} if callback else None,
                )

            vj = video_jobs.submit(db, purpose="studio", ref_id=job.id, project_id=None, start=start)
            job.provider_job_id = vj.provider_job_id
            db.refresh(job)
            return job
        db.commit()
        return job
    except Exception as e:  # noqa: BLE001
        job.status = "failed"
        job.error = str(e)[:500]
        db.commit()
        raise


def save_studio_result_as_asset(
    db: Session, job: models.AssetGenerationJob, *, asset_type: str = "lifestyle"
) -> models.Asset:
    """Promote a completed studio job into the owner's asset library."""
    if job.status != "completed" or not job.result_path:
        raise ValueError("studio job is not completed")
    is_image = job.output_kind == "image"
    if not is_image:
        # We only file image results into the visual asset library; clips stay in /studio.
        raise ValueError("only image studio results can be saved as cast assets")
    import hashlib

    raw = Path(job.result_path).read_bytes()
    _ext, mime, width, height = storage.image_info(raw)
    asset = models.Asset(
        owner_kind=job.owner_kind,
        avatar_id=job.owner_id if job.owner_kind == "avatar" else None,
        ingredient_id=job.owner_id if job.owner_kind == "ingredient" else None,
        asset_type=asset_type,
        original_filename=Path(job.result_path).name,
        file_path=job.result_path,
        mime_type=mime,
        width=width,
        height=height,
        checksum=hashlib.sha256(raw).hexdigest(),
        source="generated",
        generation_job_id=job.id,
    )
    db.add(asset)
    db.flush()
    job.result_asset_id = asset.id
    job.status = "saved"
    job.updated_at = datetime.utcnow()
    db.commit()
    return asset


# --- Custom model training ---


def run_training_job(db: Session, job_id: int) -> None:
    """Drive a CustomModel row through submit → poll → terminal.

    Picks the right TrainingProvider based on `kind` via the registry,
    so a `voice_clone` against a configured ElevenLabs key hits Voice
    Lab, `character_lora` against Replicate hits Replicate, everything
    else falls back to the mock provider that's deterministic + instant.

    Side effects on success:
      - cm.status = 'completed', cm.provider_model_id = trained id
      - For voice clones, the owning Avatar's `elevenlabs_voice_id` is
        set to the cloned voice so the existing TTS path uses it.
    """
    from time import sleep

    from ..providers import get_training
    from ..providers.base import TrainingAsset

    cm = db.get(models.CustomModel, job_id)
    if cm is None:
        raise ValueError(f"custom_model {job_id} not found")

    cm.status = "training"
    db.commit()

    try:
        provider = get_training(cm.kind)
        assets = (
            db.query(models.Asset)
            .filter(models.Asset.id.in_(cm.training_asset_ids_json or []))
            .all()
        )
        if not assets:
            raise ValueError("no training assets resolved")
        ta = [
            TrainingAsset(
                local_path=a.file_path, mime_type=a.mime_type, asset_id=a.id
            )
            for a in assets
        ]
        submitted = provider.submit(
            kind=cm.kind, name=cm.name or "untitled", assets=ta, config=cm.config_json or {}
        )
        cm.provider = submitted.provider
        cm.provider_job_id = submitted.provider_job_id
        db.commit()

        # Poll until terminal. Mock returns succeeded on the 2nd call;
        # real providers take minutes/hours.
        deadline = 60 * 60 * 2  # 2 hours
        elapsed = 0
        while True:
            st = provider.poll(submitted)
            cm.progress = max(cm.progress, st.progress)
            db.commit()
            if st.state == "succeeded":
                cm.status = "completed"
                cm.provider_model_id = st.provider_model_id
                cm.progress = 1.0
                cm.cost_usd = st.cost_usd
                cm.completed_at = datetime.utcnow()
                # Voice clones: write the voice id back onto the avatar so
                # the existing TTS rendering path uses the cloned voice.
                if cm.kind == "voice_clone" and cm.owner_kind == "avatar":
                    av = db.get(models.Avatar, cm.owner_id)
                    if av is not None:
                        av.elevenlabs_voice_id = st.provider_model_id
                        av.default_voice_provider = "elevenlabs"
                db.commit()
                if cm.cost_usd and not type(provider).__name__.startswith("Mock"):
                    from . import spend

                    spend.record(provider="replicate" if "Replicate" in type(provider).__name__ else "elevenlabs",
                                 kind="training", model=cm.kind, cost=cm.cost_usd,
                                 is_estimate=True, source="rate_card", ref=f"custom_model:{cm.id}",
                                 note="GPU seconds × rate")
                _log_provider_call(
                    db,
                    project_id=0,
                    provider=type(provider).__name__,
                    endpoint="training.complete",
                    request_summary={"kind": cm.kind, "asset_ids": cm.training_asset_ids_json},
                    response_summary={"model_id": cm.provider_model_id, "cost": cm.cost_usd},
                )
                return
            if st.state == "failed":
                cm.status = "failed"
                cm.error = st.error or "training failed"
                db.commit()
                _log_provider_call(
                    db,
                    project_id=0,
                    provider=type(provider).__name__,
                    endpoint="training.failed",
                    request_summary={"kind": cm.kind},
                    response_summary={"error": cm.error},
                    status="error",
                )
                return
            if elapsed > deadline:
                cm.status = "failed"
                cm.error = f"polling timed out after {deadline}s"
                db.commit()
                return
            sleep(2)
            elapsed += 2

    except Exception as e:  # noqa: BLE001
        cm.status = "failed"
        cm.error = str(e)[:500]
        db.commit()
        raise


def on_studio_clip_ready(db: Session, vjob: models.VideoJob) -> None:
    job = db.get(models.AssetGenerationJob, vjob.ref_id)
    if job is None:
        return
    data = Path(vjob.result_path).read_bytes()
    path = storage.save_generated_bytes(data, extension="mp4", subdir_name="clips")
    Path(vjob.result_path).unlink(missing_ok=True)
    job.result_path = str(path)
    job.status = "completed"
    db.commit()


def on_studio_clip_failed(db: Session, vjob: models.VideoJob) -> None:
    job = db.get(models.AssetGenerationJob, vjob.ref_id)
    if job is None:
        return
    job.status = "failed"
    job.error = vjob.error[:500]
    db.commit()

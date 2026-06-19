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
from . import storage
from .captions import chunks_to_timed
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


def _ensure_clip_for_shot(db: Session, project: models.VideoProject, shot: models.VideoShot) -> Path:
    """Submit + poll + download a shot's clip if it doesn't have one yet."""
    if shot.clip_path and Path(shot.clip_path).exists() and shot.status == "completed":
        return Path(shot.clip_path)

    video = get_video()
    refs = _resolve_references_for_shot(db, project, shot)
    settings_local = get_settings()
    model = settings_local.openrouter_video_model or "mock/video-default"

    shot.status = "submitted"
    shot.provider = "mock" if settings_local.video_is_mocked() else "openrouter"
    shot.provider_model = model
    db.commit()

    try:
        submitted = video.submit(
            model=model,
            prompt=shot.prompt,
            references=refs,
            negative_prompt=shot.negative_prompt or "",
            reference_strategy=shot.reference_strategy or "input_references",
            duration_seconds=shot.duration_seconds or 5.0,
        )
        shot.provider_job_id = submitted.job_id
        shot.polling_url = submitted.polling_url
        shot.status = "polling"
        db.commit()

        # Synchronous loop (workers run inside RQ jobs; for mock this is instant).
        # Real providers: we poll up to ~10 minutes.
        from time import sleep

        deadline = 60 * 10
        elapsed = 0
        while True:
            st = video.poll(submitted)
            if st.state == "succeeded":
                break
            if st.state == "failed":
                raise RuntimeError(st.error or "video provider returned failure")
            sleep(3)
            elapsed += 3
            if elapsed > deadline:
                raise RuntimeError("video job poll timed out")

        data = video.download(submitted)
        out_dir = storage.render_subdir(project.id)
        out_path = out_dir / f"shot_{shot.shot_order:02d}_{shot.id}.mp4"
        out_path.write_bytes(data)
        shot.clip_path = str(out_path)
        shot.status = "completed"
        db.commit()
        _log_provider_call(
            db,
            project_id=project.id,
            provider=submitted.provider,
            endpoint="video.submit+poll+download",
            request_summary={"model": submitted.model, "refs": len(refs), "shot": shot.id},
            response_summary={"bytes": len(data)},
        )
        return out_path
    except Exception as e:  # noqa: BLE001
        shot.status = "failed"
        shot.error = str(e)[:500]
        db.commit()
        _log_provider_call(
            db,
            project_id=project.id,
            provider="video",
            endpoint="video.submit",
            request_summary={"shot": shot.id, "model": model},
            response_summary={"error": str(e)[:500]},
            status="error",
        )
        raise


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

    settings_local = get_settings()
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

    try:
        _check_cancel(db, render)
        # 1. audio
        audio_path = generate_audio(db, project, plan=plan)
        if audio_path:
            render.audio_path = str(audio_path)
        render.status = "generating_shots"
        db.commit()
        _check_cancel(db, render)

        # 2. per-shot clips (skip end_card; we render the CTA card in compose)
        project = db.get(models.VideoProject, project.id)  # refresh
        clip_paths: list[Path] = []
        for shot in project.shots:
            _check_cancel(db, render)
            if shot.shot_type == "end_card":
                # End card rendered in compose() from cta_text. Skip provider call.
                shot.status = "completed"
                db.commit()
                continue
            clip = _ensure_clip_for_shot(db, project, shot)
            clip_paths.append(clip)

        if not clip_paths:
            raise RuntimeError("no clips were produced for any shot")

        render.status = "rendering"
        db.commit()
        _check_cancel(db, render)

        # 3. caption timings (over body duration only — end card is appended last)
        body_duration = sum(
            s.duration_seconds for s in project.shots if s.shot_type != "end_card"
        )
        timed = chunks_to_timed(plan.caption_chunks, max(body_duration, 1.0))

        # 4. compose
        brand = _brand_kit_for(db, project)
        out_dir = storage.render_subdir(project.id)
        outputs = compose(
            RenderInputs(
                project_id=project.id,
                out_dir=out_dir,
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
        render.final_video_path = str(outputs.final_video_path)
        render.thumbnail_path = str(outputs.thumbnail_path)
        render.render_log = outputs.log
        # Nothing is billed in mock mode; otherwise the actual matches the estimate
        # (we don't get per-call billing back from the providers in the MVP).
        render.actual_cost = 0.0 if settings_local.video_is_mocked() else render.estimated_cost
        render.status = "completed"
        project.status = "completed"
        db.commit()
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
        raise


def regenerate_shot(
    db: Session, project_id: int, shot_id: int, *, recompose: bool = True
) -> Path:
    project = db.get(models.VideoProject, project_id)
    shot = db.get(models.VideoShot, shot_id)
    if not project or not shot or shot.project_id != project.id:
        raise ValueError("shot not found in project")
    # Force regeneration of this clip only — other shots keep their clip_path,
    # so the next compose will reuse them.
    shot.clip_path = ""
    shot.status = "pending"
    shot.error = ""
    db.commit()
    clip = _ensure_clip_for_shot(db, project, shot)
    if recompose:
        try:
            recompose_project(db, project_id)
        except Exception:  # noqa: BLE001
            log.exception("recompose after shot regen failed")
    return clip


def regenerate_shots_bulk(
    db: Session, project_id: int, shot_ids: list[int], *, recompose: bool = True
) -> list[int]:
    """Regenerate several shots' clips in one pass, then optionally re-compose once.

    Returns the list of shot ids that were successfully regenerated. A shot that
    fails is marked failed and skipped; the recompose still runs for the rest as
    long as every body shot ends up with a clip on disk."""
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise ValueError(f"project {project_id} not found")
    by_id = {s.id: s for s in project.shots}
    regenerated: list[int] = []
    for sid in shot_ids:
        shot = by_id.get(sid)
        if shot is None or shot.shot_type == "end_card":
            continue
        shot.clip_path = ""
        shot.status = "pending"
        shot.error = ""
        db.commit()
        try:
            _ensure_clip_for_shot(db, project, shot)
            regenerated.append(sid)
        except Exception:  # noqa: BLE001
            log.exception("bulk regen failed for shot %s", sid)
    if recompose:
        try:
            recompose_project(db, project_id)
        except Exception:  # noqa: BLE001
            log.exception("recompose after bulk regen failed")
    return regenerated


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
        render.final_video_path = str(outputs.final_video_path)
        render.thumbnail_path = str(outputs.thumbnail_path)
        render.render_log = outputs.log
        render.status = "completed"
        project.status = "completed"
        db.commit()
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
                job.provider_model or settings.openrouter_image_model or "mock/image-default",
            )
            job.provider = "mock" if settings.image_is_mocked() else "openrouter"
            job.provider_model = model
            db.commit()
            data = image.generate(model=model, prompt=job.prompt, references=refs,
                                  negative_prompt=job.negative_prompt)
            path = storage.save_generated_bytes(data, extension="png", subdir_name="images")
            job.result_path = str(path)
            job.status = "completed"
        else:  # video_clip
            video = get_video()
            model = _resolve_model(
                db,
                job.provider_model or settings.openrouter_video_model or "mock/video-default",
            )
            job.provider = "mock" if settings.video_is_mocked() else "openrouter"
            job.provider_model = model
            db.commit()
            submitted = video.submit(
                model=model,
                prompt=job.prompt,
                references=refs,
                negative_prompt=job.negative_prompt or "",
                duration_seconds=5.0,
            )
            job.provider_job_id = submitted.job_id
            job.status = "polling"
            db.commit()
            # mock returns succeeded immediately; real provider would block here
            from time import sleep

            elapsed = 0
            while True:
                st = video.poll(submitted)
                if st.state == "succeeded":
                    break
                if st.state == "failed":
                    raise RuntimeError(st.error or "video provider failure")
                sleep(3)
                elapsed += 3
                if elapsed > 600:
                    raise RuntimeError("video job poll timed out")
            data = video.download(submitted)
            path = storage.save_generated_bytes(data, extension="mp4", subdir_name="clips")
            job.result_path = str(path)
            job.status = "completed"
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
    asset = models.Asset(
        owner_kind=job.owner_kind,
        avatar_id=job.owner_id if job.owner_kind == "avatar" else None,
        ingredient_id=job.owner_id if job.owner_kind == "ingredient" else None,
        asset_type=asset_type,
        original_filename=Path(job.result_path).name,
        file_path=job.result_path,
        mime_type="image/png",
        width=0,
        height=0,
        checksum="",
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

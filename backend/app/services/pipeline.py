from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from ..db import SessionLocal
from ..providers import (
    get_chat,
    get_tts,
    get_video,
    provider_label,
    routes_per_model,
    uses_webhooks,
)
from ..providers.base import CastContext, ImageRef
from ..schemas.storyboard import StoryboardPlan
from . import alerts, events, fingerprints, render_files, storage, takes
from .captions import CaptionTiming, chunks_to_timed
from .model_registry import active_model as _active_model
from .prompting import effective_negative
from .renderer import RenderInputs, RenderOutputs, compose
from .safety import UnsafeScriptError

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


def _pname(provider) -> str:
    """The adapter's class name for the provider-call log (not the safety wrapper's)."""
    from ..providers.registry import unwrap

    return type(unwrap(provider)).__name__


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
    # Shots keep asset-id order (what IN() gave before): a frame_images shot
    # pins refs[0] as its first frame, and the shot picker shows no order.
    return _asset_refs(db, reference_asset_ids(project, shot))


def reference_asset_ids(project: models.VideoProject, shot: models.VideoShot) -> list[int]:
    """The asset ids a shot's clip is made with: its own picks, else the
    first two pictures of each Cast member; sorted."""
    asset_ids: list = list(shot.reference_asset_ids_json or [])
    if not asset_ids:
        for cm in project.cast_members:
            owner = cm.avatar if cm.member_kind == "avatar" else cm.ingredient
            if owner is None:
                continue
            for a in owner.assets[:2]:
                asset_ids.append(a.id)
    return sorted(_asset_ids(asset_ids))


def _asset_ids(raw) -> list[int]:
    """Stored asset ids as ints (an API client may have stored "12")."""
    out = []
    for x in raw or []:
        text = str(x).strip()
        if text.isascii() and text.isdecimal():
            out.append(int(text))
    return out


def _asset_refs(db: Session, asset_ids: list) -> list[ImageRef]:
    """Assets as reference pictures, in the order given (Studio: the order
    the user picked them; IN() returns database order), each with its role
    (a Cast character, or an ingredient's kind) and name."""
    ids = _asset_ids(asset_ids)
    if not ids:
        return []
    rows = {r.id: r for r in db.query(models.Asset).filter(models.Asset.id.in_(ids)).all()}
    from .content_mode import adult_ok_avatars

    marked = adult_ok_avatars()
    refs: list[ImageRef] = []
    for aid in dict.fromkeys(ids):
        row = rows.get(aid)
        if row is None:
            continue
        role, name = "character" if row.owner_kind == "avatar" else "scene", ""
        if row.owner_kind == "avatar" and row.asset_type == "logo":
            role = "object"  # a character's logo is a thing to include, not a likeness
        if row.owner_kind == "avatar" and row.avatar is not None:
            name = row.avatar.name if role == "character" else f"{row.avatar.name}'s logo"
        elif row.owner_kind == "ingredient" and row.ingredient is not None:
            role = {"scene": "scene", "style": "style", "object": "object", "prop": "object"}.get(
                row.ingredient.kind, "scene")
            name = row.ingredient.name
        refs.append(ImageRef(url=storage.public_url_for_token(row.public_token),
                             role=role, name=name,  # type: ignore[arg-type]
                             adult_ok=role == "character" and _exempt_asset(row, marked)))
    return refs


def _exempt_asset(asset: Optional[models.Asset], marked: set[int]) -> bool:
    """Whether a Cast picture counts as its marked character ("adult content
    allowed"): any non-logo picture on a marked character's card, as long
    as the character's own text names no minor. How the picture was made
    doesn't matter (uploaded, generated, a cutout or upscale, from any
    picture, even one deleted since): the tick covers every picture the
    owner keeps on that card."""
    if asset is None or asset.owner_kind != "avatar" or asset.asset_type == "logo" or asset.avatar_id not in marked:
        return False
    from .content_mode import describes_minor

    return asset.avatar is not None and not describes_minor(asset.avatar)


def cast_avatar_of(asset: Optional[models.Asset]) -> Optional[int]:
    """The Cast character a picture shows: any non-logo picture on their
    card. None for a logo or an ingredient's picture."""
    if asset is None or asset.owner_kind != "avatar" or asset.asset_type == "logo" or asset.avatar_id is None:
        return None
    return int(asset.avatar_id)


def avatar_adult_ok(db: Session, avatar_id: int, marked: set[int]) -> bool:
    """Whether a Cast character's pictures skip the forced content filter:
    marked "adult content allowed" and its own text names no minor (as
    _exempt_asset, for a picture that is no longer on the card)."""
    if avatar_id not in marked:
        return False
    from .content_mode import describes_minor

    avatar = db.get(models.Avatar, avatar_id)
    return avatar is not None and not describes_minor(avatar)


def studio_cast_avatar(db: Session, job: Optional[models.AssetGenerationJob]) -> Optional[int]:
    """The Cast character a Studio picture shows when it is only a cutout
    or upscale of one of their pictures (followed back through each
    operation to the picture it started from), else None: a picture made
    in Studio is a new picture, not the character's. When the starting
    picture was deleted since, the operation's owner (copied from its
    source when it was made) says whose it was."""
    seen: set[int] = set()
    while job is not None and job.operation and job.id not in seen:
        seen.add(job.id)
        if job.source_asset_id:
            asset = db.get(models.Asset, job.source_asset_id)
            if asset is not None:
                return cast_avatar_of(asset)
        elif job.source_job_id:
            src = db.get(models.AssetGenerationJob, job.source_job_id)
            if src is not None:
                job = src
                continue
        return int(job.owner_id) if job.owner_kind == "avatar" and job.owner_id is not None else None
    return None


# ---------- public entry points ----------


def _check_plan(plan: StoryboardPlan) -> None:
    """The planner's own output gets the minors checks too (in adult mode a
    permissive model plans): every shot prompt, with its avoid list, and
    the spoken script."""
    from .safety import UnsafePromptError, check_prompt, check_script, steers_young

    check_script(plan.cleaned_voice_script or "")
    check_script(plan.hook or "")  # normally the script's first sentence (the schema sees to it)
    for shot in plan.shots:
        check_prompt(shot.visual_prompt or "")
        if steers_young(shot.visual_prompt or "", shot.negative_prompt or ""):
            raise UnsafePromptError(
                "This plan was refused: a shot's avoid list removes signs of adulthood from a sexual prompt.")
    # The AI safety check, once per distinct shot prompt (fills its cache,
    # so the clip calls later are hits), and on the script when it's sexual.
    from .minor_check import check_generation

    seen: set[tuple[str, str]] = set()
    for shot in plan.shots:
        key = (shot.visual_prompt or "", effective_negative("video", shot.negative_prompt or ""))
        if key[0].strip() and key not in seen:
            seen.add(key)
            check_generation(key[0], key[1], kind="video")
    check_generation(plan.cleaned_voice_script or "", kind="script")


def claim_held(db: Session, project_id: int, claim: str, token: Optional[str] = None) -> bool:
    """Whether the claim the API made when it queued this job still holds:
    the project is still `claim` and, when the job carries a token, it is
    still this claim's token. Not when the claim was cancelled, replaced by
    a newer one, or given up by the sweeper while the job waited in the
    queue: the job then does nothing (and pays for nothing)."""
    row = (db.query(models.VideoProject.status, models.VideoProject.claim_token)
           .filter(models.VideoProject.id == project_id).first())
    if row is None or row[0] != claim or (token and row[1] != token):
        log.info("project %s is no longer claimed by this job (%s, now %s); skipping it",
                 project_id, claim, row[0] if row else "gone")
        return False
    return True


# A hook-lab "Make videos" claim: its token starts with this, then the
# status the project goes back to, then the random part (dropped once the
# job takes the claim over). The project page and the sweeper read it, so
# the claim never ends as "cancelled" or "failed" on a finished video.
HOOK_CLAIM = "hl:"
_HOOK_PREVIOUS = ("draft", "planned", "completed", "failed", "cancelled")


def hook_claim_token(previous: str) -> str:
    import secrets

    return f"{HOOK_CLAIM}{previous}:{secrets.token_hex(12)}"


def hook_claim_previous(token: Optional[str]) -> Optional[str]:
    """The status a hook-lab claim goes back to; None when `token` is not
    a hook-lab claim's."""
    if not token or not token.startswith(HOOK_CLAIM):
        return None
    previous = token[len(HOOK_CLAIM):].split(":", 1)[0]
    return previous if previous in _HOOK_PREVIOUS else "completed"


def claim_marker(token: Optional[str]) -> str:
    """What claim_token holds once a job took the claim with `token` over."""
    previous = hook_claim_previous(token)
    return f"{HOOK_CLAIM}{previous}:" if previous else ""


def consume_claim(db: Session, project_id: int, claim: str, token: Optional[str]) -> bool:
    """Take over the claim atomically (conditional UPDATE, not committed:
    the caller commits it together with its first write). After this a
    cancel can no longer release the claim, and a second entry with the
    same token can't start. Without a token (older queue entries) it only
    checks the status. A hook-lab claim keeps its marker (without the
    random part), so it is still known as one while its job runs."""
    if not token:
        return claim_held(db, project_id, claim)
    done = (db.query(models.VideoProject)
            .filter(models.VideoProject.id == project_id, models.VideoProject.status == claim,
                    models.VideoProject.claim_token == token)
            .update({"claim_token": claim_marker(token)}, synchronize_session=False))
    if done != 1:
        log.info("project %s claim %s was cancelled or replaced; skipping its job", project_id, claim)
        return False
    return True


def generate_plan(
    db: Session, project_id: int, *, preserve_edits: bool = False, claim: Optional[str] = None,
    claim_token: Optional[str] = None,
) -> Optional[StoryboardPlan]:
    """`claim` / `claim_token`: the claim the API made ("planning"). When it
    no longer holds, nothing is done and None is returned. The claim stays
    cancellable while the planner runs; the plan is only written if it
    still holds then (consumed in the same commit), so a cancelled or
    replaced run never overwrites the project."""
    if claim and not claim_held(db, project_id, claim, claim_token):
        return None
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise ValueError(f"project {project_id} not found")
    project.status = "planning"
    events.plan_event(db, project, "started")
    db.commit()

    chat = None
    try:
        chat = get_chat()
        cast = _build_cast_context(db, project)
        plan = chat.generate_storyboard(project=_project_dict(project), cast=cast)
        _check_plan(plan)
    except Exception as e:  # noqa: BLE001
        db.rollback()
        # Cancelled (or replaced) while the planner ran: leave that state.
        if not claim or consume_claim(db, project_id, claim, claim_token):
            project = db.get(models.VideoProject, project_id)
            project.status = "failed"
            events.plan_event(db, project, "failed", error=str(e))
        db.commit()
        _log_provider_call(
            db,
            project_id=project.id,
            provider=type(chat).__name__ if chat is not None else "chat",
            endpoint="generate_storyboard",
            request_summary={"title": project.title, "mode": project.mode},
            response_summary={"error": str(e)[:500]},
            status="error",
        )
        raise

    # Publish only if the claim still holds, in the same commit as the plan.
    db.rollback()
    if claim and not consume_claim(db, project_id, claim, claim_token):
        db.rollback()
        return None
    project = db.get(models.VideoProject, project_id)
    new_plan = plan.model_dump()
    old_plan = project.generated_plan_json or {}
    if preserve_edits and old_plan:
        # "Keep my edits" keeps what the Voiceover & captions card edits:
        # the spoken script, its opening line (hook), the end-card text and
        # the captions.
        for key in ("cleaned_voice_script", "hook", "end_card_text", "caption_chunks"):
            if key in old_plan:
                new_plan[key] = old_plan[key]
        if "cleaned_voice_script" in old_plan and not str(old_plan.get("hook") or "").strip():
            # A plan from before the hook was stored: its hook is the kept
            # script's first sentence, not the new planner's.
            from .hook_lab import first_sentence

            new_plan["hook"] = first_sentence(str(old_plan.get("cleaned_voice_script") or ""))
    project.generated_plan_json = new_plan
    project.status = "planned"

    # Reconcile shots against the new plan.
    #
    # When `preserve_edits` is set we keep user-editable fields (prompt,
    # avoid text, duration, references, caption, model) on shots that match
    # an existing order (position in the storyboard as it is now), only
    # refreshing fields the planner is authoritative for. New plan
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
                # (prompt, negative_prompt, duration_seconds,
                # reference_asset_ids_json, caption_text) stay untouched.
                shot.shot_type = s.shot_type
                # Camera direction and reference strategy are part of what a
                # clip was made from: on a shot that has a clip (or one being
                # made) they stay, or "keep my edits" would mark the clip out
                # of date and the next render would buy it again.
                if not (shot.clip_path or shot.status in ("submitted", "running", "completed")):
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
    events.plan_event(db, project, "completed")
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

    refs = references if references is not None else _resolve_references_for_shot(db, project, shot)
    # Per-shot override (raw id or custom:<id> trained-LoRA selector) wins
    # over the deployment-wide default.
    model = _resolve_model(db, shot.model_override or _active_model("video"))
    # A sogni/ or runware/ model is served by its provider whatever video model is active.
    per_model = routes_per_model("video", model)
    video = get_video(model) if per_model else get_video()
    if purpose == "shot":
        takes.ensure_current_take(db, shot)  # the clip being replaced stays selectable
    shot.status = "submitted"
    shot.error = note
    shot.clip_path = ""
    shot.clip_fingerprint = ""
    shot.provider = provider_label("video", model)
    shot.provider_model = model
    db.commit()
    # What this clip is made from, kept with the job: the shot may be edited
    # again before the clip lands. Storyboard shots only (the ambient chain
    # passes its own references and never reuses by fingerprint).
    fingerprint = ""
    if purpose == "shot" and references is None:
        fingerprint = fingerprints.video_fingerprint(project, shot)

    # Storyboard shots: the shot's own avoid text plus the Settings video
    # defaults. Ambient keeps its own list (it is slow on purpose, and the
    # defaults' "slow motion" would fight it).
    negative = shot.negative_prompt or ""
    if purpose != "ambient_segment" and project.mode != "ambient_loop":
        negative = effective_negative("video", negative)

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
            negative_prompt=negative,
            reference_strategy=shot.reference_strategy or "input_references",
            duration_seconds=shot.duration_seconds or 5.0, settings=settings_extra,
        )

    try:
        job = video_jobs.submit(
            db, purpose=purpose, ref_id=shot.id, project_id=project.id, render_id=render_id,
            context={"after": after, **({"note": note} if note else {}),
                     "aspect": project.aspect_ratio or "",
                     **({"fingerprint": fingerprint} if fingerprint else {}), **(context or {})}, start=start,
            webhooks=uses_webhooks(model),
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
    db.refresh(job)  # a resumed render may have taken the job over meanwhile
    shot = db.get(models.VideoShot, job.ref_id)
    if shot is None:
        return
    path = Path(job.result_path)
    if job.purpose == "avatar":
        # Avatar models size the clip to their audio/script; trim to the shot.
        from .renderer import trim_clip

        # One file per job: an earlier take of this shot keeps its own file.
        trimmed = path.with_name(f"shot_{shot.shot_order:02d}_{shot.id}_{job.id}.mp4")
        trim_clip(path, trimmed, duration=float((job.context_json or {}).get("duration") or shot.duration_seconds or 5.0))
        path.unlink(missing_ok=True)
        path = trimmed
    ctx = job.context_json or {}
    shot.clip_path = str(path)
    shot.clip_fingerprint = ctx.get("fingerprint", "")
    if "aspect" in ctx:
        shot.clip_aspect_ratio = ctx.get("aspect") or ""
    else:  # a job submitted before the aspect was kept with it
        project = db.get(models.VideoProject, job.project_id) if job.project_id else None
        shot.clip_aspect_ratio = (project.aspect_ratio if project else "") or ""
    shot.status = "completed"
    shot.error = ctx.get("note", "")
    db.commit()
    try:
        takes.record(db, shot, job)
    except Exception:  # noqa: BLE001 — losing a take row must not stop the render
        db.rollback()
        log.exception("shot %s: could not record the take", shot.id)
        shot = db.get(models.VideoShot, job.ref_id)
    if _remake_outdated(db, job, shot):
        return
    _shot_progress(db, job)
    _after_shot(db, job)
    # The clip check runs after the render has moved on: advisory, never in its way.
    from . import inspector

    inspector.queue_auto_check(db, db.get(models.VideoShot, job.ref_id) or shot)


def _remake_outdated(db: Session, job: models.VideoJob, shot: models.VideoShot) -> bool:
    """A clip that was still being made when its shot (or, for lip-sync, the
    voiceover) was edited lands out of date. A render waiting on it must not
    be put together with it: make the shot again for that render instead.
    True when a new clip was submitted."""
    if (job.context_json or {}).get("after", "compose") != "compose" or not job.render_id:
        return False
    render = db.get(models.Render, job.render_id)
    if render is None or render.status != "generating_shots" or not fingerprints.shot_is_stale(shot):
        return False
    project = db.get(models.VideoProject, render.project_id)
    if project is None:
        return False
    log.info("shot %s: the clip that just landed was made from older settings; making it again", shot.id)
    try:
        note = ""
        if shot.shot_type == "talking_head":
            ok = submit_talking_head(
                db, project, shot, plan=StoryboardPlan.model_validate(project.generated_plan_json),
                audio_path=render.audio_path,
                start=_shot_start(project, shot), render_id=render.id,
                voice_fingerprint=render.voice_fingerprint,
            )
            if ok is True:
                return True
            note = ok or ""
            shot = db.get(models.VideoShot, shot.id)
        submit_shot_clip(db, project, shot, render_id=render.id, after="compose", note=note)
        return True
    except Exception as e:  # noqa: BLE001 — the render fails with the reason
        log.exception("remaking outdated shot %s failed", shot.id)
        shot = db.get(models.VideoShot, shot.id)
        shot.status = "failed"
        shot.error = f"could not remake the edited shot: {str(e)[:300]}"
        db.commit()
        return False


def on_shot_clip_failed(db: Session, job: models.VideoJob) -> None:
    db.refresh(job)  # a resumed render may have taken the job over meanwhile
    shot = db.get(models.VideoShot, job.ref_id)
    if shot is None:
        return
    newer = (db.query(models.VideoJob.id)
             .filter(models.VideoJob.ref_id == job.ref_id, models.VideoJob.id > job.id,
                     models.VideoJob.purpose.in_(("shot", "avatar")),
                     models.VideoJob.status != "failed").first())
    if newer is not None:
        # The shot was made again since (an old job that was stuck and is
        # failed late by the sweeper): its newer clip stands.
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


def _shot_progress(db: Session, job: models.VideoJob) -> None:
    """One progress event per landed clip of a render that is making clips:
    "Clips: 2 of 5 ready". Never in the render's way."""
    try:
        if not job.render_id:
            return
        render = db.get(models.Render, job.render_id)
        if render is None or render.status != "generating_shots":
            return
        project = db.get(models.VideoProject, render.project_id)
        body = _body_shots(project)
        done = sum(1 for s in body if s.status == "completed")
        events.render_event(db, render, project, "progress", detail=f"Clips: {done} of {len(body)} ready")
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("render progress event failed")


def _after_shot(db: Session, job: models.VideoJob) -> None:
    after = (job.context_json or {}).get("after", "compose")
    if after == "compose" and job.render_id:
        maybe_compose(db, job.render_id)
    elif after == "recompose" and job.project_id:
        maybe_recompose(db, job.project_id)
    if job.project_id:
        # A render that is waiting on this shot without owning its job (it
        # was resumed while the clip was still being made) composes too.
        waiting = [rid for (rid,) in db.query(models.Render.id).filter(
            models.Render.project_id == job.project_id,
            models.Render.status == "generating_shots",
            models.Render.hook_variant_id.is_(None),
            models.Render.id != (job.render_id or 0))]
        for rid in waiting:
            maybe_compose(db, rid)


def _body_shots(project: models.VideoProject) -> list[models.VideoShot]:
    return [s for s in project.shots if s.shot_type not in ("end_card", "ambient_segment")]


def maybe_compose(db: Session, render_id: int) -> None:
    """Compose once every body shot of the render's project has its clip.
    Exactly-once: the render moves generating_shots → rendering atomically."""
    from sqlalchemy import update as sa_update

    from ..workers.jobs import compose_render_job
    from ..workers.queue import enqueue

    render = db.get(models.Render, render_id)
    if render is None or render.status != "generating_shots" or render.hook_variant_id:
        return  # a hook-lab variant is composed from its own clips, never the project's
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
                     .values(status="rendering", updated_at=datetime.utcnow()))
    if res.rowcount == 1:
        events.render_event(db, render, project, "progress", detail="Putting the video together")
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
    events.render_event(db, render, project, "failed", error=error)
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
    after: str = "compose", voice_fingerprint: str = "",
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
    audio_url = None
    speech = dur
    if audio_path and Path(audio_path).exists():
        # Each talking-head shot speaks only its own part of the voiceover,
        # so lips, captions and the voice track stay aligned.
        seg = slice_audio(Path(audio_path), out_dir / f"voice_shot_{shot.id}.m4a", start=start, duration=dur)
        audio_url = public_media.publish(seg, "m4a")
        # The clip comes back as long as this audio, which is shorter than
        # the shot when the voiceover ends first: that's what is billed.
        from .renderer import _probe_duration

        speech = min(dur, _probe_duration(seg) or dur)
    script = _talking_head_script(project, shot, plan)
    takes.ensure_current_take(db, shot)  # the clip being replaced stays selectable
    shot.status = "submitted"
    shot.provider = "lipsync"
    shot.provider_model = getattr(avatar, "model", "")
    shot.error = ""
    shot.clip_path = ""
    shot.clip_fingerprint = ""
    db.commit()
    # The clip follows this voiceover: a new voiceover (or a new place in
    # the video) makes it out of date. `voice_fingerprint` is the
    # fingerprint of the audio in `audio_path` ("" when unknown).
    fingerprint = fingerprints.lipsync_fingerprint(
        project, shot, voice=voice_fingerprint if audio_url else "", start=start, portrait_id=portrait.id)

    def start_fn(callback: Optional[str]):
        return avatar.submit_avatar(
            portrait_url=storage.public_url_for_token(portrait.public_token), audio_url=audio_url,
            script=script, aspect_ratio=project.aspect_ratio, motion_prompt=shot.camera_direction or "",
            duration_seconds=dur, callback_url=callback,
        )

    try:
        video_jobs.submit(db, purpose="avatar", ref_id=shot.id, project_id=project.id, render_id=render_id,
                          context={"after": after, "duration": dur, "speech_seconds": round(speech, 2),
                                   "aspect": project.aspect_ratio or "",
                                   **({"fingerprint": fingerprint} if fingerprint else {})},
                          start=start_fn)
    except UnsafeScriptError:
        # A safety refusal is never worked around with the video model: the
        # voiceover carries the refused script. The render fails.
        db.rollback()
        raise
    except Exception as e:  # noqa: BLE001 — submit failed: use the video model
        log.warning("talking-head submit failed for shot %s, falling back: %s", shot.id, e)
        db.rollback()
        return f"lip-sync failed, used video model instead: {str(e)[:300]}"
    return True


def _talking_head_script(project: models.VideoProject, shot: models.VideoShot, plan: StoryboardPlan) -> str:
    """The text a talking-head shot speaks (sent with its lip-sync job)."""
    body = [s for s in project.shots if s.shot_type != "end_card"]
    return (plan.cleaned_voice_script if len(body) == 1 else "") or shot.caption_text \
        or plan.cleaned_voice_script or shot.prompt


def precheck_render(db: Session, project: models.VideoProject, plan: StoryboardPlan) -> None:
    """The safety checks every paid step of a render will meet, run before
    anything is paid for (the voiceover first of all): each body shot's clip
    prompt with its avoid list, the lip-sync text (motion + script) of
    talking-head shots, and the spoken script. Raises UnsafeScriptError.
    Verdicts are cached, so the checks at submit time are hits."""
    from ..providers import get_avatar
    from .minor_check import check_generation
    from .safety import check_prompt

    ambient = project.mode == "ambient_loop"
    avatar = None
    seen: set[tuple[str, str, str]] = set()

    def once(text: str, negative: str, kind: str) -> None:
        key = (text, negative, kind)
        if text.strip() and key not in seen:
            seen.add(key)
            check_generation(text, negative, kind=kind)

    for shot in _body_shots(project):
        if shot.status == "completed" and shot.clip_path and Path(shot.clip_path).exists() \
                and not fingerprints.shot_is_stale(shot):
            continue  # reused, not submitted
        negative = shot.negative_prompt or ""
        if not ambient:
            negative = effective_negative("video", negative)
        once(shot.prompt or "", negative, "video")
        if shot.shot_type == "talking_head":
            if avatar is None:
                avatar = get_avatar() or False
            if avatar and _hero_portrait(db, project) is not None:
                motion = shot.camera_direction or ""
                if motion.strip():
                    check_prompt(motion)
                script = _talking_head_script(project, shot, plan)
                once(" ".join(x for x in (motion, script) if x.strip()), "", "lipsync")
    if (project.voiceover_source or "tts").lower() == "tts":
        once(plan.cleaned_voice_script or "", "", "script")


def generate_audio(
    db: Session, project: models.VideoProject, *, plan: StoryboardPlan,
    render_id: Optional[int] = None,
) -> Optional[Path]:
    """Produce the project's voiceover track. Source is controlled by
    `project.voiceover_source`:

      - "upload": return the user-uploaded file from `voiceover_upload_path`.
      - "silent": return None (renderer will skip the voice track).
      - "tts" (default): call the TTS provider. An empty spoken script is
        the same as silent (providers reject empty text). A failure raises
        VoiceoverFailed: the caller must not go on to pay for clips that
        would end up in a video with no voice.

    The synthesized file is `voice_<render_id>.m4a` (`voice_<fingerprint>.m4a`
    without a render), never a shared name: an older render's audio_path
    keeps pointing at the audio it was made with.
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
    if source == "silent" or not (plan.cleaned_voice_script or "").strip():
        return None

    tts = get_tts()
    voice_id = fingerprints.tts_voice_id(db, project)
    try:
        audio_bytes = tts.synthesize(text=plan.cleaned_voice_script, voice_id=voice_id)
        out_dir = storage.render_subdir(project.id)
        name = (f"voice_{render_id}" if render_id
                else f"voice_{fingerprints.voice_fingerprint(db, project)[:16]}")
        audio_path = out_dir / f"{name}.m4a"
        audio_path.write_bytes(audio_bytes)
        _log_provider_call(
            db,
            project_id=project.id,
            provider=_pname(tts),
            endpoint="tts.synthesize",
            request_summary={"chars": len(plan.cleaned_voice_script), "voice_id": bool(voice_id)},
            response_summary={"bytes": len(audio_bytes)},
        )
        return audio_path
    except Exception as e:  # noqa: BLE001
        _log_provider_call(
            db,
            project_id=project.id,
            provider=_pname(tts),
            endpoint="tts.synthesize",
            request_summary={"chars": len(plan.cleaned_voice_script)},
            response_summary={"error": str(e)[:500]},
            status="error",
        )
        if isinstance(e, UnsafeScriptError):
            raise VoiceoverFailed(f"Voiceover not made: {e}") from e
        raise VoiceoverFailed(
            "Voiceover failed, so no clips were made and nothing was spent on video. "
            f"The voice provider said: {str(e)[:400] or type(e).__name__}. "
            "Retry, or set the voiceover to an uploaded file or to silent in Audio."
        ) from e


class VoiceoverFailed(RuntimeError):
    """The voiceover could not be synthesized."""


def voiceover_preview_path(project: models.VideoProject) -> Optional[Path]:
    """The preview voiceover's file, when one was made and is on disk."""
    path = str((project.voiceover_preview_json or {}).get("path") or "")
    return Path(path) if path and Path(path).is_file() else None


def _tts_cost(script: str) -> float:
    return len(script or "") / 1000.0 * get_settings().cost_tts_per_1k_chars


def _body_seconds(project: models.VideoProject) -> float:
    return float(sum(float(s.duration_seconds or 0) for s in _body_shots(project)))


END_CARD_SECONDS = 2.5  # renderer._make_end_card's default length


def voiceover_fit(project: models.VideoProject, audio_seconds: float) -> dict:
    """The voiceover's real length against the shots' total time, in plain words."""
    shots = round(_body_seconds(project), 2)
    end_card = END_CARD_SECONDS if (project.cta_text or "").strip() else 0.0
    diff = round(audio_seconds - shots, 1)
    if audio_seconds > shots + 0.5:
        verdict = "too_long"
        if audio_seconds <= shots + end_card:
            text = f"about {abs(diff):g} s longer than the shots: it runs over the end card"
        else:
            text = f"about {abs(diff):g} s longer than the video: the end will be cut"
    elif audio_seconds < shots - 1.5:
        verdict = "too_short"
        text = f"about {abs(diff):g} s shorter than the video: the last {abs(diff):g} s have no voice"
    else:
        verdict, text = "fits", "fits"
    return {"audio_seconds": round(audio_seconds, 2), "video_seconds": shots,
            "difference_seconds": diff, "fit": verdict, "fit_text": text}


def make_voiceover_preview(db: Session, project: models.VideoProject) -> dict:
    """Synthesize the current spoken script with the project's voice and keep
    it as the project's current voiceover: the next render uses it at no
    charge. When a voiceover made from exactly the same script, voice and
    voice model already exists, nothing is paid for and that one is returned."""
    from .renderer import _probe_duration

    if not project.generated_plan_json:
        raise ValueError("generate a storyboard plan first")
    if fingerprints.voice_source(project) != "tts":
        raise VoiceoverNotSynthesized(project)
    adopt_unfingerprinted_voiceover(db, project)
    fingerprint = fingerprints.voice_fingerprint(db, project)
    existing = _reusable_voiceover(db, project, fingerprint)
    if existing:
        return {"path": existing, "fingerprint": fingerprint, "cost_estimate": 0.0, "reused": True}
    script = str(project.generated_plan_json.get("cleaned_voice_script") or "")
    tts = get_tts()
    voice_id = fingerprints.tts_voice_id(db, project)
    out_dir = storage.render_subdir(project.id)
    try:
        data = tts.synthesize(text=script, voice_id=voice_id)
    except Exception as e:  # noqa: BLE001
        _log_provider_call(db, project_id=project.id, provider=_pname(tts), endpoint="tts.synthesize",
                           request_summary={"chars": len(script), "preview": True},
                           response_summary={"error": str(e)[:500]}, status="error")
        if isinstance(e, UnsafeScriptError):
            raise VoiceoverFailed(str(e)) from e
        raise VoiceoverFailed(f"The voice provider said: {str(e)[:400] or type(e).__name__}") from e
    # One file per fingerprint: a render that used an earlier preview keeps
    # pointing at the audio it was made with.
    path = out_dir / f"voice_preview_{fingerprint[:16]}.m4a"
    path.write_bytes(data)
    old = voiceover_preview_path(project)
    if old is not None and old != path and not db.query(models.Render.id).filter(
            models.Render.project_id == project.id, models.Render.audio_path == str(old)).first():
        old.unlink(missing_ok=True)
    cost = round(_tts_cost(script), 4)
    project.voiceover_preview_json = {
        "path": str(path), "fingerprint": fingerprint,
        "duration_seconds": round(_probe_duration(path), 2), "cost_estimate": cost,
        "created_at": datetime.utcnow().isoformat(),
    }
    db.commit()
    _log_provider_call(db, project_id=project.id, provider=_pname(tts), endpoint="tts.synthesize",
                       request_summary={"chars": len(script), "voice_id": bool(voice_id), "preview": True},
                       response_summary={"bytes": len(data)})
    return {"path": str(path), "fingerprint": fingerprint, "cost_estimate": cost, "reused": False}


def current_voiceover(db: Session, project: models.VideoProject) -> Optional[dict]:
    """The voiceover to play in the editor: whichever of the preview and the
    newest render's voiceover still matches the script and voice (the
    preview first), else the newer of the two. `current` says whether it
    still matches."""
    from .renderer import _probe_duration

    # A voiceover from before fingerprints existed is current until the
    # script or voice changes (same rule as Retry and as clips).
    adopt_unfingerprinted_voiceover(db, project)
    fp_now = fingerprints.voice_fingerprint(db, project) if fingerprints.voice_source(project) == "tts" else ""
    candidates: list[dict] = []
    preview = voiceover_preview_path(project)
    if preview is not None:
        meta = project.voiceover_preview_json or {}
        candidates.append({
            "source": "preview", "path": str(preview), "fingerprint": str(meta.get("fingerprint") or ""),
            "duration_seconds": float(meta.get("duration_seconds") or 0) or round(_probe_duration(preview), 2),
            "cost_estimate": float(meta.get("cost_estimate") or 0.0), "created_at": meta.get("created_at")})
    latest = _latest_audio_render(db, project.id)
    if latest is not None and str(preview or "") != latest.audio_path:
        candidates.append({
            "source": "render", "path": latest.audio_path, "fingerprint": latest.voice_fingerprint or "",
            "duration_seconds": round(_probe_duration(Path(latest.audio_path)), 2), "cost_estimate": 0.0,
            "created_at": latest.created_at.isoformat() if latest.created_at else None})
    if not candidates:
        return None
    for c in candidates:
        c["current"] = bool(fp_now) and c["fingerprint"] == fp_now
    for c in candidates:
        if c["current"]:
            return c
    return max(candidates, key=lambda c: c["created_at"] or "")


class VoiceoverNotSynthesized(ValueError):
    """The project's voiceover is an uploaded file or silent: nothing to synthesize."""

    def __init__(self, project: models.VideoProject) -> None:
        source = fingerprints.voice_source(project)
        msg = ("The voiceover is your uploaded file, so there is nothing to make."
               if source == "upload" else
               "This project is silent (or has no spoken script), so there is no voiceover to make.")
        super().__init__(msg)


def _latest_audio_render(db: Session, project_id: int) -> Optional[models.Render]:
    """The newest render that has a voiceover, if its file is still on disk.
    Hook-lab variants never count: their voiceover is the base one with
    another opening, never the project's current voiceover."""
    for r in (db.query(models.Render)
              .filter(models.Render.project_id == project_id, models.Render.audio_path != "",
                      models.Render.hook_variant_id.is_(None))
              .order_by(models.Render.id.desc()).limit(5)):
        if Path(r.audio_path).is_file():
            return r
    return None


def _reusable_voiceover(db: Session, project: models.VideoProject, fingerprint: str) -> Optional[str]:
    """The voiceover already on disk, when it was made from exactly the
    current spoken script, voice and voice model: the preview ("Make
    voiceover") first, then the newest render (not a hook-lab variant) whose
    voiceover matches. Each synthesis has its own file, so an older render's
    voiceover is still the audio it was made with."""
    preview = voiceover_preview_path(project)
    if preview is not None and fingerprint and (project.voiceover_preview_json or {}).get("fingerprint") == fingerprint:
        return str(preview)
    if not fingerprint:
        return None
    for r in (db.query(models.Render)
              .filter(models.Render.project_id == project.id, models.Render.audio_path != "",
                      models.Render.voice_fingerprint == fingerprint,
                      models.Render.hook_variant_id.is_(None))
              .order_by(models.Render.id.desc()).limit(20)):
        if Path(r.audio_path).is_file() and not audio_overwritten(db, r):
            return r.audio_path
    return None


def audio_overwritten(db: Session, render: models.Render) -> bool:
    """The render's voiceover file no longer holds its audio: a newer render
    (not a hook-lab variant) points at the same file with another
    fingerprint. Before each synthesis got its own file, every render wrote
    the shared `voice.m4a`, so only the newest render pointing at it still
    owns what is in it."""
    if not render.audio_path:
        return False
    newer = (db.query(models.Render.voice_fingerprint)
             .filter(models.Render.project_id == render.project_id,
                     models.Render.audio_path == render.audio_path,
                     models.Render.hook_variant_id.is_(None),
                     models.Render.id > render.id)
             .order_by(models.Render.id.desc()).first())
    return newer is not None and (newer[0] or "") != (render.voice_fingerprint or "")


def adopt_unfingerprinted_voiceover(db: Session, project: models.VideoProject) -> bool:
    """Retry on a project last rendered before voiceover fingerprints
    existed: its voice file has an unknown ("") fingerprint, which would
    never match and be bought again. Like a clip without a fingerprint, it
    counts as current: stamp it with the current fingerprint so the retry
    reuses it (and lip-synced clips cut from it stay in step). Returns
    True when a voiceover was adopted."""
    if fingerprints.voice_source(project) != "tts":
        return False
    latest = _latest_audio_render(db, project.id)
    if latest is None or latest.voice_fingerprint:
        return False
    if (project.voiceover_upload_path and Path(latest.audio_path) == Path(project.voiceover_upload_path)) \
            or is_voice_copy(Path(latest.audio_path)):
        return False  # that render used the uploaded file, not a synthesized voice
    latest.voice_fingerprint = fingerprints.voice_fingerprint(db, project)
    db.commit()
    return True


def _voiceover_changed(db: Session, project: models.VideoProject) -> bool:
    """The newest voiceover on disk no longer matches the spoken script,
    voice or voice model: the next render makes a new one."""
    latest = _latest_audio_render(db, project.id)
    return bool(
        fingerprints.voice_source(project) != "silent" and latest is not None
        and latest.voice_fingerprint
        and latest.voice_fingerprint != fingerprints.voice_fingerprint(db, project))


def _lipsync_available(db: Session, project: models.VideoProject) -> bool:
    from ..providers import get_avatar

    return (fingerprints.voice_source(project) != "silent" and get_avatar() is not None
            and _hero_portrait(db, project) is not None)


def _clip_follows_voice(db: Session, project: models.VideoProject, shot: models.VideoShot,
                        voice_fp: str) -> bool:
    """The shot's lip-synced clip was cut from the voiceover with this
    fingerprint (at the shot's current place and length)."""
    if not voice_fp or not shot.clip_fingerprint:
        return False
    portrait = _hero_portrait(db, project)
    return shot.clip_fingerprint == fingerprints.lipsync_fingerprint(
        project, shot, voice=voice_fp, start=_shot_start(project, shot),
        portrait_id=portrait.id if portrait else None)


def _lipsynced_clips(project: models.VideoProject) -> list[models.VideoShot]:
    return [s for s in _body_shots(project)
            if s.provider == "lipsync" and s.clip_path and s.clip_fingerprint]


def current_voiceover_made(db: Session, project: models.VideoProject) -> bool:
    """The voiceover for the current spoken script and voice is already on
    disk (from "Make voiceover" or a render)."""
    fp = fingerprints.voice_fingerprint(db, project)
    return bool(fp and _reusable_voiceover(db, project, fp))


def regen_needs_new_voiceover(db: Session, project: models.VideoProject,
                              shots: list[models.VideoShot], *,
                              recompose: bool = True) -> list[models.VideoShot]:
    """The shots a re-roll cannot fix: lip-synced shots while the voiceover
    is out of date.

    Without the new voiceover on disk, a re-roll lip-syncs to the old one, so
    the paid clip would still be out of date and the next render would buy it
    again. Only Generate video makes the new voiceover.

    With the new voiceover already made ("Make voiceover"), the re-roll cuts
    its clip from it, and a recompose puts that voiceover under the video.
    Any other lip-synced clip still cut from the old voiceover would then be
    out of step, so a recomposing re-roll is refused unless it takes those
    shots along too (they are returned with the re-rolled ones)."""
    talking = [s for s in shots if s.shot_type == "talking_head"]
    if not talking or not _voiceover_changed(db, project) or not _lipsync_available(db, project):
        return []
    voice_fp = fingerprints.voice_fingerprint(db, project)
    if not _reusable_voiceover(db, project, voice_fp):
        return talking
    if not recompose:
        return []  # the clip is cut from the new voiceover; nothing is composed yet
    picked = {s.id for s in shots}
    left_behind = [s for s in _lipsynced_clips(project)
                   if s.id not in picked and not _clip_follows_voice(db, project, s, voice_fp)]
    return talking + left_behind if left_behind else []


def _active_job_for(db: Session, shot_id: int) -> Optional[models.VideoJob]:
    """A clip for this shot that is still being made and can still finish.
    A job whose download was interrupted (stuck in "completing") does not
    count: adopting it would leave the render waiting with no end."""
    from . import video_jobs

    return (db.query(models.VideoJob)
            .filter(models.VideoJob.ref_id == shot_id,
                    models.VideoJob.purpose.in_(("shot", "avatar")),
                    video_jobs.still_in_progress())
            .order_by(models.VideoJob.id.desc()).first())


# Project / render states during which a planning, render or re-roll step is
# running (anything else: draft, planned, completed, failed, cancelled).
_IDLE_PROJECT = ("draft", "planned", "completed", "failed", "cancelled")
_DONE_RENDER = ("completed", "failed", "cancelled", "expired")


def busy_reason(db: Session, project: models.VideoProject) -> Optional[str]:
    """Why the storyboard can't be changed in a way that affects clips right
    now: a plan, render or re-roll is running. None when nothing is."""
    from . import video_jobs

    if (project.status or "draft") not in _IDLE_PROJECT:
        step = "planning" if project.status == "planning" else "making the video"
        return f"Wait until the current step ({step}) finishes."
    latest = (db.query(models.Render).filter(models.Render.project_id == project.id,
                                              models.Render.hook_variant_id.is_(None))
              .order_by(models.Render.id.desc()).first())
    if latest is not None and latest.status not in _DONE_RENDER:
        return "Wait until the current step (making the video) finishes."
    shot_ids = [s.id for s in project.shots]
    if shot_ids and (db.query(models.VideoJob.id)
                     .filter(models.VideoJob.ref_id.in_(shot_ids),
                             models.VideoJob.purpose.in_(("shot", "avatar")),
                             video_jobs.still_in_progress()).first()) is not None:
        return ("A clip for this project is still being made (and is already paid for). "
                "Wait until it is back.")
    return hook_lab_busy(db, project.id)


def hook_lab_busy(db: Session, project_id: int) -> Optional[str]:
    """Why hook-lab work blocks the storyboard: an opening's render or one
    of its clips is still being made. None when nothing is."""
    from . import video_jobs

    variant_running = (db.query(models.Render.id)
                       .filter(models.Render.project_id == project_id, models.Render.hook_variant_id.isnot(None),
                               models.Render.status.notin_(_DONE_RENDER + ("expired",))).first())
    if variant_running is not None or video_jobs.pending_for(
            db, project_id=project_id, purposes=video_jobs.HOOK_PURPOSES) > 0:
        return ("Other openings (hook lab) are being made for this project and are already paid for. "
                "Wait until they are back.")
    return None


def _video_rate(shot: models.VideoShot) -> float:
    """Rate-card price per second of a clip on the shot's video model."""
    from ..providers import runware, sogni

    model = (shot.model_override or _active_model("video") or "").strip()
    if model.startswith(sogni.PREFIX):
        return sogni.video_spend_rate_usd(model)
    if model.startswith(runware.PREFIX):
        return runware.video_rate_usd(model)
    return get_settings().cost_video_per_second


def render_plan(db: Session, project: models.VideoProject) -> dict:
    """What "Generate video" would do right now, before anything is paid
    for: whether a voiceover is made or reused, and which clips are reused,
    still being made, or made new (and why), with a rate-card price."""
    rates = get_settings()
    adopt_unfingerprinted_voiceover(db, project)  # an old voiceover is current, as on Retry
    source = fingerprints.voice_source(project)
    voice_fp = fingerprints.voice_fingerprint(db, project)
    script = str((project.generated_plan_json or {}).get("cleaned_voice_script") or "")
    if source == "silent":
        voiceover, voice_cost = "silent", 0.0
    elif source == "upload":
        voiceover, voice_cost = "upload", 0.0
    elif _reusable_voiceover(db, project, voice_fp):
        voiceover, voice_cost = "reuse", 0.0
    else:
        voiceover, voice_cost = "new", _tts_cost(script)
    # The finished video's voice no longer matches the spoken script / voice.
    voiceover_changed = _voiceover_changed(db, project)
    lipsync_on = _lipsync_available(db, project)

    from .inspector import flagged_shots

    body_ids = {s.id for s in _body_shots(project)}
    flagged = [f for f in flagged_shots(project) if f["shot_id"] in body_ids]
    shots: list[dict] = []
    clip_cost = 0.0
    for shot in _body_shots(project):
        has_clip = shot.status == "completed" and bool(shot.clip_path) and Path(shot.clip_path).exists()
        if _active_job_for(db, shot.id) is not None:
            action, reason = "in_flight", "already being made; it is kept and not paid for again"
        elif has_clip and not fingerprints.shot_is_stale(shot):
            action, reason = "reuse", ""
        elif has_clip and fingerprints.aspect_changed(shot, project):
            action, reason = "generate", "the video's shape (aspect ratio) changed"
        elif has_clip and shot.provider == "lipsync" and voiceover_changed:
            action, reason = "generate", "lip-sync follows the voiceover, which changed"
        elif has_clip:
            action = "generate"
            reason = ("its place or length in the voiceover changed" if shot.provider == "lipsync"
                      else "edited since this clip was made")
        else:
            action, reason = "generate", "no clip yet"
        cost = 0.0
        lipsync = shot.shot_type == "talking_head" and lipsync_on
        if action == "generate":
            seconds = float(shot.duration_seconds or 5.0)
            cost = seconds * (rates.cost_avatar_per_second if lipsync else _video_rate(shot))
            clip_cost += cost
        shots.append({"id": shot.id, "shot_order": shot.shot_order, "action": action, "reason": reason,
                      "lipsync": lipsync, "cost": round(cost, 4),
                      # A single re-roll would lip-sync to the old voiceover.
                      "needs_new_voiceover": lipsync and voiceover_changed})
    return {
        "voiceover": voiceover,
        "voiceover_changed": voiceover_changed,
        "voiceover_cost": round(voice_cost, 4),
        "shots": shots,
        "reuse": sum(1 for s in shots if s["action"] == "reuse"),
        "in_flight": sum(1 for s in shots if s["action"] == "in_flight"),
        "generate": sum(1 for s in shots if s["action"] == "generate"),
        "clip_cost": round(clip_cost, 4),
        "total": round(clip_cost + voice_cost, 4),
        "currency": "USD",
        "is_estimate": True,
        # Current clips the clip check flagged ("2 clips flagged by the check").
        "flagged": flagged,
        "flagged_count": len(flagged),
    }


def edit_impact(db: Session, project: models.VideoProject, *, op: str,
                shot_id: Optional[int] = None, after_shot_id: Optional[int] = None,
                duration: Optional[float] = None, shot_type: str = "b_roll") -> dict:
    """What adding (`op="add"`), duplicating or deleting a shot would put out
    of date, before it is done. Lip-synced clips are cut from their slice of
    the voiceover: a shot added or removed before them moves that slice, so
    the next Generate video makes them again (paid). Same rule and prices as
    render_plan. Also prices the new shot's own clip (add / duplicate)."""
    rates = get_settings()
    ordered = sorted(project.shots, key=lambda s: (s.shot_order, s.id))
    by_id = {s.id: s for s in ordered}
    lipsync_on = _lipsync_available(db, project)

    def rate(s: models.VideoShot) -> float:
        lipsync = s.shot_type == "talking_head" and lipsync_on
        return rates.cost_avatar_per_second if lipsync else _video_rate(s)

    new_shot: Optional[models.VideoShot] = None
    after: Optional[models.VideoShot] = None
    layout: list[Optional[models.VideoShot]] = list(ordered)  # None = the new shot
    if op == "delete":
        layout = [s for s in ordered if s.id != shot_id]
    else:
        if op == "duplicate":
            src = by_id.get(shot_id) if shot_id is not None else None
            if src is None:
                raise ValueError("shot not found in project")
            after = src
            new_shot = models.VideoShot(shot_type=src.shot_type, duration_seconds=src.duration_seconds,
                                        model_override=src.model_override)
        else:
            after = by_id.get(after_shot_id) if after_shot_id is not None else None
            new_shot = models.VideoShot(shot_type=shot_type, duration_seconds=float(duration or 5.0),
                                        model_override="")
        if after is not None and after.shot_type != "end_card":
            idx = ordered.index(after) + 1
        else:
            idx = len(ordered)
            while idx > 0 and ordered[idx - 1].shot_type == "end_card":
                idx -= 1
        layout.insert(idx, None)

    new_starts: dict[int, float] = {}
    t = 0.0
    for s in layout:
        if s is not None:
            new_starts[s.id] = t
        kind = new_shot.shot_type if s is None else s.shot_type
        if kind != "end_card":
            t += float((new_shot if s is None else s).duration_seconds or 0)

    stale: list[dict] = []
    if fingerprints.voice_source(project) != "silent":
        for s in _body_shots(project):
            if s.id not in new_starts or s.provider != "lipsync" or not s.clip_fingerprint:
                continue
            if s.status != "completed" or not s.clip_path or fingerprints.shot_is_stale(s):
                continue  # no clip, or already out of date (already priced by the render plan)
            if _active_job_for(db, s.id) is not None:
                continue
            if round(new_starts[s.id], 3) == round(_shot_start(project, s), 3):
                continue
            cost = float(s.duration_seconds or 5.0) * rate(s)
            stale.append({"id": s.id, "shot_order": s.shot_order, "cost": round(cost, 4)})
    new_cost = float(new_shot.duration_seconds or 5.0) * rate(new_shot) if new_shot is not None else 0.0
    stale_cost = sum(x["cost"] for x in stale)
    return {
        "stale_shots": stale,
        "stale_count": len(stale),
        "stale_cost": round(stale_cost, 4),
        "new_clip_cost": round(new_cost, 4),
        "currency": "USD",
        "is_estimate": True,
    }


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


def render_project(db: Session, project_id: int, *, claim: Optional[str] = None,
                   claim_token: Optional[str] = None) -> Optional[models.Render]:
    """`claim`: the status the API claimed the project with ("generating");
    when it no longer holds (cancelled, or failed by the sweeper while the
    job waited in the queue) nothing is done and None is returned. The claim
    is consumed in the same commit that creates the Render row, after which
    Cancel works through the render."""
    if claim and not claim_held(db, project_id, claim, claim_token):
        return None
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise ValueError(f"project {project_id} not found")
    try:
        if not project.generated_plan_json:
            raise ValueError("project has no storyboard plan yet")
        plan = StoryboardPlan.model_validate(project.generated_plan_json)
        from .cost import estimate_for_project

        estimate = estimate_for_project(db, project, plan)
    except Exception as e:
        # Nothing was made: give up the claim so Generate works again.
        db.rollback()
        if claim and not consume_claim(db, project_id, claim, claim_token):
            db.rollback()  # cancelled or replaced meanwhile: leave that state
            raise
        if project.status == "generating":
            project.status = "failed"
            events.emit(db, "render", "project", project.id, "failed",
                        f"Render failed: {events.plain_error(str(e))}", detail=str(e)[:600],
                        url=events.project_url(project), project_id=project.id)
            db.commit()
        raise
    if claim and not consume_claim(db, project_id, claim, claim_token):
        db.rollback()
        return None
    render = models.Render(
        project_id=project.id,
        status="generating_audio",
        estimated_cost=estimate["total"],
    )
    db.add(render)
    # Mark the project itself as generating so clients polling project_status keep
    # polling while a worker-backed render runs in the background.
    project.status = "generating"
    db.flush()
    events.render_event(db, render, project, "started", detail="Making the voiceover")
    db.commit()
    db.refresh(render)
    from . import spend

    spend.bind(project_id=project.id, render_id=render.id)  # voiceover etc. count toward this render

    try:
        _check_cancel(db, render)
        # 0. Every safety check this render's paid steps will meet, before
        #    the voiceover is paid for.
        precheck_render(db, project, plan)
        # 1. audio. An unchanged voiceover is reused (not paid for again,
        #    and lip-synced clips cut from it stay in step); a failed
        #    synthesis stops here, before any clip is submitted.
        adopt_unfingerprinted_voiceover(db, project)  # an old voiceover is reused, as on Retry
        voice_fp = fingerprints.voice_fingerprint(db, project)
        audio_path = None
        if fingerprints.voice_source(project) == "tts":
            audio_path = _reusable_voiceover(db, project, voice_fp)
        if audio_path is None:
            audio_path = generate_audio(db, project, plan=plan, render_id=render.id)
        if audio_path:
            render.audio_path = str(audio_path)
            render.voice_fingerprint = voice_fp
        render.status = "generating_shots"
        events.render_event(db, render, project, "progress", detail="Making clips")
        db.commit()
        _check_cancel(db, render)

        # 2. Submit every body shot at once (they generate in parallel at the
        #    provider); end cards are drawn by compose. Completion arrives by
        #    webhook / sweeper and maybe_compose() runs compose exactly once.
        project = db.get(models.VideoProject, project.id)  # refresh
        # Shots whose clip no longer matches them wait for a new one: mark
        # them first, so a clip that lands while the rest are still being
        # submitted can't compose the video with their old clips.
        for shot in _body_shots(project):
            if shot.status == "completed" and fingerprints.shot_is_stale(shot) \
                    and _active_job_for(db, shot.id) is None:
                takes.ensure_current_take(db, shot)  # the old clip stays selectable
                shot.status = "pending"
        db.commit()
        body = 0
        for shot in project.shots:
            _check_cancel(db, render)
            if shot.shot_type == "end_card":
                shot.status = "completed"
                db.commit()
                continue
            body += 1
            db.refresh(shot)
            job = _active_job_for(db, shot.id)
            if job is not None:
                # Still being made (a cancelled run, or a re-roll): never
                # submit and pay for it twice. This render waits for it.
                job.render_id = render.id
                job.context_json = {**(job.context_json or {}), "after": "compose"}
                db.commit()
                continue
            if shot.status == "completed" and shot.clip_path and Path(shot.clip_path).exists():
                continue  # reuse a clip that still matches the shot
            note = ""
            if shot.shot_type == "talking_head":
                ok = submit_talking_head(
                    db, project, shot, plan=plan, audio_path=render.audio_path,
                    start=_shot_start(project, shot), render_id=render.id,
                    voice_fingerprint=render.voice_fingerprint,
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
        events.render_event(db, render, project, "failed", error=str(e))
        db.commit()
        alerts.render_failed("render", project.id, project.title, str(e))
        raise


@dataclass
class ComposeClip:
    """One body clip of a compose and where it came from."""

    path: Path
    # The shot's length: a longer clip is cut to it. None = play it whole.
    seconds: Optional[float] = None
    shot_id: Optional[int] = None
    shot_order: Optional[int] = None
    shot_type: str = ""
    take_id: Optional[int] = None
    fingerprint: str = ""
    provider_model: str = ""
    # Plays for exactly `seconds`: held on its last frame when shorter.
    exact: bool = False
    # What the shot was made with, as the render recorded it (None: read
    # the shot row). The hook lab reads these, since a re-plan or a shot
    # delete can change or remove the row after the render.
    lipsynced: Optional[bool] = None
    reference_asset_ids: Optional[list[int]] = None
    reference_strategy: Optional[str] = None
    negative_prompt: Optional[str] = None


def clip_record(db: Session, project: models.VideoProject, clip: ComposeClip) -> dict:
    """What `clip`'s shot was made with, for the render's composition_json:
    whether it is lip-synced, its reference pictures, reference strategy
    and avoid list. The clip's own record when it has one (a hook-lab
    opening of a recorded base), else the shot row and take as they are."""
    shot = db.get(models.VideoShot, clip.shot_id) if clip.shot_id else None
    if shot is not None and shot.project_id != project.id:
        shot = None
    lipsynced = clip.lipsynced
    if lipsynced is None:
        lipsynced = False
        if clip.shot_type == "talking_head":
            take = db.get(models.ShotTake, clip.take_id) if clip.take_id else None
            lipsynced = (take.provider if take is not None else (shot.provider if shot else "")) == "lipsync"
    refs = clip.reference_asset_ids
    if refs is None:
        refs = reference_asset_ids(project, shot) if shot is not None else []
    strategy = clip.reference_strategy
    if strategy is None:
        strategy = (shot.reference_strategy if shot else "") or ""
    negative = clip.negative_prompt
    if negative is None:
        negative = (shot.negative_prompt if shot else "") or ""
    return {"lipsynced": bool(lipsynced), "reference_asset_ids": list(refs),
            "reference_strategy": strategy, "negative_prompt": negative}


def shot_clips(project: models.VideoProject) -> list[ComposeClip]:
    """The project's current body clips, in order (end cards are drawn by
    compose). Raises ValueError naming the shots that have no clip on disk."""
    clips: list[ComposeClip] = []
    missing: list[int] = []
    for shot in _body_shots(project):
        if not shot.clip_path or not Path(shot.clip_path).exists():
            missing.append(shot.shot_order)
            continue
        take = takes.current_take(shot)
        clips.append(ComposeClip(
            path=Path(shot.clip_path), seconds=float(shot.duration_seconds or 0) or None,
            shot_id=shot.id, shot_order=shot.shot_order, shot_type=shot.shot_type or "",
            take_id=take.id if take is not None else None, fingerprint=shot.clip_fingerprint or "",
            provider_model=shot.provider_model or ""))
    if missing:
        raise ValueError(f"shots not yet generated: {missing}")
    if not clips:
        raise ValueError("no shot clips available to recompose")
    return clips


def clip_seconds(clip: ComposeClip) -> float:
    """How long `clip` plays in the video: its file's length, cut to its
    shot's length when longer."""
    from .renderer import _probe_duration

    if clip.exact and clip.seconds:
        return float(clip.seconds)
    real = _probe_duration(clip.path) if clip.path.exists() else 0.0
    if clip.seconds and (not real or real > clip.seconds):
        return float(clip.seconds)
    return float(real or clip.seconds or 0.0)


def cover_voice(clips: list[ComposeClip], audio: Optional[Path], end_card: float, *,
                render_id: Optional[int] = None) -> list[ComposeClip]:
    """`clips` with the last one played longer when the voiceover runs past
    the body plus the end card: clips are cut to their shots' lengths and
    the video ends with the picture, so a long voiceover lost its last words
    (often the call to action). The last clip plays on (its own footage
    first, then held on its last frame) until the voice has finished."""
    from dataclasses import replace

    from .renderer import _probe_duration

    if not clips or audio is None or not audio.exists():
        return clips
    voice = _probe_duration(audio)
    short = voice - (sum(clip_seconds(c) for c in clips) + end_card)
    if short <= 0.05:
        return clips
    last = clips[-1]
    longer = round(clip_seconds(last) + short + 0.05, 3)
    log.warning("render %s: voiceover runs %.2f s past the clips; the last clip plays on to %.2f s",
                render_id, short, longer)
    return [*clips[:-1], replace(last, seconds=longer, exact=True)]


def base_captions(plan: StoryboardPlan, body_seconds: float) -> list[CaptionTiming]:
    """The plan's captions over the video's body (its clips, not the end card)."""
    return chunks_to_timed(plan.caption_chunks, max(body_seconds, 1.0))


def _compose(
    db: Session, project: models.VideoProject, render: models.Render, clips: list[ComposeClip],
    audio: Optional[Path], timed: list[CaptionTiming], *, plan: Optional[StoryboardPlan],
    voice_fingerprint: str = "", hook_window: Optional[float] = None,
    hook_variant_id: Optional[int] = None, hook_text: Optional[str] = None,
    script: Optional[str] = None, extra: Optional[dict] = None,
    settings: Optional[dict] = None,
) -> RenderOutputs:
    """Put one storyboard render together: compose `clips` (each cut to its
    shot length) with `audio`, the captions `timed`, the project's end card,
    music, brand and disclosure, in the render's own scratch dir
    (`tmp_<render id>/`, removed afterwards); move the outputs to
    `render_<id>.mp4/.jpg`; and record what was used on
    `render.composition_json`. Status, events and cost are the caller's.
    Used by compose_render, recompose_project and hook-lab variants."""
    from .hook_lab import first_sentence
    from .renderer import _probe_duration

    look = settings if settings is not None else compose_settings(db, project, plan)
    project_dir = storage.render_subdir(project.id)
    look = keep_music(look, project_dir, render.id)
    voice = voice_identity(db, project, audio, voice_fingerprint)
    audio = keep_voice(project, audio, project_dir, render.id)
    if audio is not None:
        render.audio_path = str(audio)
    work = render_files.scratch_dir(project_dir, render.id)
    clips = cover_voice(clips, audio, END_CARD_SECONDS if look["cta_text"] else 0.0, render_id=render.id)
    used = [clip_seconds(c) for c in clips]
    try:
        outputs = compose(
            RenderInputs(
                project_id=project.id,
                out_dir=work,
                clip_paths=[c.path for c in clips],
                clip_seconds=[c.seconds for c in clips],
                clip_exact=[c.exact for c in clips],
                audio_path=audio,
                caption_timings=timed,
                caption_style=look["caption_style"] or "clean_white",
                disclosure_text=look["disclosure_text"] or None,
                cta_text=look["cta_text"] or None,
                aspect_ratio=look["aspect_ratio"] or project.aspect_ratio,
                music_path=Path(look["music_path"]) if look["music_path"] else None,
                music_volume=float(look["music_volume"]),
                end_card_bg_color=look["end_card_bg_color"],
                end_card_text_color=look["end_card_text_color"],
                logo_path=Path(look["logo_path"]) if look["logo_path"] else None,
                caption_color=look["caption_color"] or None,
            )
        )
        render.render_log = outputs.log
        render_files.finalize(db, render, outputs.final_video_path, outputs.thumbnail_path)
    except BaseException:
        render_files.drop_scratch(project_dir, render.id)
        for own in own_copies(project_dir, render.id):  # its copies (keep_music, keep_voice)
            own.unlink(missing_ok=True)
        raise
    if script is None and plan is None:
        raise ValueError("a compose without a plan must say its script")
    spoken = plan.cleaned_voice_script if script is None else script
    final = Path(render.final_video_path)
    render.composition_json = {
        "version": 1,
        "shots": [{
            "shot_id": c.shot_id, "shot_order": c.shot_order, "shot_type": c.shot_type,
            "take_id": c.take_id, "clip_path": str(c.path), "fingerprint": c.fingerprint,
            "provider_model": c.provider_model, "planned_seconds": c.seconds,
            "seconds": round(u, 3), **clip_record(db, project, c),
        } for c, u in zip(clips, used, strict=True)],
        "audio_path": str(audio) if audio else "",
        "voice_fingerprint": voice_fingerprint,
        "voice": voice,
        "captions": [{"start": round(t.start, 3), "end": round(t.end, 3), "text": t.text} for t in timed],
        "body_seconds": round(sum(used), 3),
        "end_card_seconds": END_CARD_SECONDS if look["cta_text"] else 0.0,
        "duration_seconds": round(_probe_duration(final), 3) if final.exists() else None,
        "aspect_ratio": look["aspect_ratio"] or project.aspect_ratio or "",
        "avatar_id": project.primary_avatar_id,
        "script": spoken or "",
        "hook": (hook_text if hook_text is not None
                 else (plan.hook if script is None else first_sentence(spoken or ""))),
        "hook_window_seconds": hook_window,
        "hook_variant_id": hook_variant_id,
        "settings": look,
        **(extra or {}),
    }
    if hook_variant_id is not None:
        render.hook_variant_id = hook_variant_id
    db.commit()
    return outputs


def file_sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def keep_music(look: dict, project_dir: Path, render_id: int) -> dict:
    """`look` with its music bed pinned to this render: the project's
    music file keeps its name (music.<ext>) when music is made or uploaded
    again, so the render keeps its own copy
    (`render_<id>_music.<ext>`, a hard link when the disk allows it) and
    its checksum. Another opening of this render is composed with that
    copy, never with whatever music the project has by then. (The logo
    needs nothing: each upload is a new file.)"""
    import os
    import shutil

    src = Path(look.get("music_path") or "") if look.get("music_path") else None
    if src is None or not src.is_file():
        return look
    dst = project_dir / f"render_{render_id}_music{src.suffix}"
    if src != dst:
        dst.unlink(missing_ok=True)
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)
    return {**look, "music_path": str(dst), "music_sha256": file_sha256(dst)}


_VOICE_COPY = re.compile(r"render_\d+_voice\.\w+")


def own_copies(project_dir: Path, render_id: int) -> list[Path]:
    """The render's own copies of its music bed and uploaded voiceover
    (keep_music, keep_voice): removed with the render's files."""
    return [*project_dir.glob(f"render_{render_id}_music.*"), *project_dir.glob(f"render_{render_id}_voice.*")]


def is_voice_copy(path: Path) -> bool:
    """A render's own copy of an uploaded voiceover (keep_voice)."""
    return bool(_VOICE_COPY.fullmatch(path.name))


def keep_voice(project: models.VideoProject, audio: Optional[Path], project_dir: Path,
               render_id: int) -> Optional[Path]:
    """`audio`, pinned to this render when it is an uploaded voiceover: the
    upload keeps its name (voiceover.<ext>) when a new one is uploaded, so
    the render keeps its own copy (`render_<id>_voice.<ext>`, a hard link
    when the disk allows it), like keep_music does for the music. A
    synthesized voiceover needs nothing: each synthesis is a new file."""
    import os
    import shutil

    if audio is None or not audio.is_file():
        return audio
    upload = Path(project.voiceover_upload_path) if project.voiceover_upload_path else None
    if audio != upload and not is_voice_copy(audio):
        return audio
    dst = project_dir / f"render_{render_id}_voice{audio.suffix}"
    if audio != dst:
        dst.unlink(missing_ok=True)
        try:
            os.link(audio, dst)
        except OSError:
            shutil.copy2(audio, dst)
    return dst


def voice_identity(db: Session, project: models.VideoProject, audio: Optional[Path], voice_fp: str) -> dict:
    """What a render's voiceover was spoken with, recorded in its
    composition_json["voice"]: {"source": "upload"} for an uploaded
    recording, {"source": "tts", voice_id, default_voice, model, options}
    for a synthesized one, {"source": "silent"} without one. {} when it
    isn't known (an older voiceover from a render that didn't record it)."""
    if audio is None:
        return {"source": "silent"}
    upload = Path(project.voiceover_upload_path) if project.voiceover_upload_path else None
    if audio == upload or is_voice_copy(audio):
        return {"source": "upload"}
    if voice_fp and fingerprints.voice_source(project) == "tts" \
            and voice_fp == fingerprints.voice_fingerprint(db, project):
        return {"source": "tts", **fingerprints.tts_identity(db, project)}
    # An older voiceover kept under the clips: what the render that made it recorded.
    R = models.Render
    q = (db.query(R).filter(R.project_id == project.id, R.audio_path == str(audio),
                            R.hook_variant_id.is_(None), R.composition_json.isnot(None))
         .order_by(R.id.desc()))
    if voice_fp:
        q = q.filter(R.voice_fingerprint == voice_fp)
    for r in q.limit(20):
        voice = (r.composition_json or {}).get("voice")
        if voice:
            return dict(voice)
    return {}


def compose_settings(db: Session, project: models.VideoProject, plan: Optional[StoryboardPlan]) -> dict:
    """The project's compose look right now (CTA, captions, music, brand,
    disclosure), as recorded in composition_json["settings"]: a hook-lab
    variant is composed with its base render's, so only its opening differs."""
    brand = _brand_kit_for(db, project)
    music = _music_path_if_any(project)
    logo = Path(brand.logo_path) if brand and brand.logo_path and Path(brand.logo_path).exists() else None
    disclosure = _disclosure_for(project, plan, brand) if plan is not None else None
    return {
        "cta_text": project.cta_text or "",
        "caption_style": project.caption_style or "clean_white",
        "disclosure_text": disclosure or "",
        "aspect_ratio": project.aspect_ratio or "",
        "music_path": str(music) if music else "",
        "music_volume": float(project.music_volume or 0.25),
        "end_card_bg_color": brand.end_card_bg_color if brand else "#0E0E12",
        "end_card_text_color": brand.end_card_text_color if brand else "#FFFFFF",
        "logo_path": str(logo) if logo else "",
        "caption_color": (brand.primary_color if brand else "") or "",
    }


def compose_render(db: Session, render_id: int) -> models.Render:
    """FFmpeg composition for a storyboard render whose clips are all in
    (called once by maybe_compose)."""
    render = db.get(models.Render, render_id)
    project = db.get(models.VideoProject, render.project_id)
    plan = StoryboardPlan.model_validate(project.generated_plan_json)
    try:
        _check_cancel(db, render)
        clips = shot_clips(project)
        timed = base_captions(plan, sum(clip_seconds(c) for c in clips))
        _compose(db, project, render, clips, Path(render.audio_path) if render.audio_path else None,
                 timed, plan=plan, voice_fingerprint=render.voice_fingerprint or "")
        render.status = "completed"
        project.status = "completed"
        events.render_event(db, render, project, "completed")
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
    latest = _latest_audio_render(db, project.id)
    return latest.audio_path if latest is not None else None


def _submit_regen(db: Session, project: models.VideoProject, shot: models.VideoShot, *, after: str) -> None:
    """Re-roll one shot the same way a full render makes it: talking-head
    shots go to the lip-sync model with their slice of the latest voiceover
    (falling back to the video model, with a note, if that isn't possible);
    everything else goes to the video model."""
    note = ""
    if shot.shot_type == "talking_head" and project.generated_plan_json:
        voice_fp = fingerprints.voice_fingerprint(db, project)
        audio = _reusable_voiceover(db, project, voice_fp)
        if audio is None:
            latest = _latest_audio_render(db, project.id)
            audio, voice_fp = (latest.audio_path, latest.voice_fingerprint) if latest is not None else (None, "")
        if audio is not None:
            ok = submit_talking_head(
                db, project, shot, plan=StoryboardPlan.model_validate(project.generated_plan_json),
                audio_path=audio, start=_shot_start(project, shot), render_id=None, after=after,
                voice_fingerprint=voice_fp,
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
        .filter(models.Render.project_id == project_id, models.Render.hook_variant_id.is_(None))
        .order_by(models.Render.created_at.desc())
        .all()
    )
    for r in rows:
        if r.audio_path and Path(r.audio_path).exists():
            return Path(r.audio_path)
    return None


def _recompose_voiceover(db: Session, project: models.VideoProject) -> tuple[Optional[Path], str]:
    """The voiceover a recompose puts under the clips, and its fingerprint.

    The current one (made with "Make voiceover", or by the last render) when
    it is on disk, so a shot re-rolled against it stays in step and the
    captions match what is said. The last render's voiceover instead when the
    lip-synced clips were all cut from that one (nothing was re-rolled
    against the new voiceover yet): swapping it would put every one of them
    out of step. An uploaded voiceover is the upload file itself: each
    render plays its own copy of it (keep_voice), so a newly uploaded
    recording is found there, never under an older render."""
    old_audio = _latest_completed_audio(db, project.id)
    source_render = _latest_audio_render(db, project.id)
    # Same voiceover file as before: keep knowing what it was made from.
    old_fp = (source_render.voice_fingerprint
              if source_render is not None and old_audio and source_render.audio_path == str(old_audio)
              else "")
    voice_fp = fingerprints.voice_fingerprint(db, project)
    if fingerprints.voice_source(project) == "upload":
        current = project.voiceover_upload_path if voice_fp != old_fp else None
        if current is not None and old_audio is not None and Path(current) == old_audio:
            # An older render played the upload file itself: it holds the new recording now.
            return old_audio, voice_fp
    else:
        current = _reusable_voiceover(db, project, voice_fp) if voice_fp else None
    if current is None or (old_audio is not None and Path(current) == old_audio):
        return old_audio, old_fp
    clips = _lipsynced_clips(project)
    if (old_fp and any(_clip_follows_voice(db, project, s, old_fp) for s in clips)
            and not any(_clip_follows_voice(db, project, s, voice_fp) for s in clips)):
        return old_audio, old_fp
    return Path(current), voice_fp


def _spoken_texts(db: Session, project: models.VideoProject, audio: Optional[Path],
                  audio_fp: str) -> Optional[dict]:
    """{script, hook_text} to record for a recompose whose voiceover is not
    the current script's (the edited script has not been voiced yet, so the
    old voiceover is kept): what that audio says, from the render that made
    it, or empty so readers don't take the unspoken plan text for it. None
    when the audio is the current script's (the plan's text is right)."""
    if audio is None or fingerprints.voice_source(project) != "tts":
        return None
    if audio_fp and audio_fp == fingerprints.voice_fingerprint(db, project):
        return None
    R = models.Render
    q = (db.query(R).filter(R.project_id == project.id, R.audio_path == str(audio),
                            R.hook_variant_id.is_(None), R.composition_json.isnot(None))
         .order_by(R.id.desc()))
    if audio_fp:
        q = q.filter(R.voice_fingerprint == audio_fp)
    for r in q.limit(20):
        comp = r.composition_json or {}
        if comp.get("script"):
            return {"script": str(comp["script"]), "hook_text": str(comp.get("hook") or "")}
    return {"script": "", "hook_text": "", "extra": {"spoken_unknown": True}}


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

    try:
        clips = shot_clips(project)
    except ValueError:
        if project.status == "generating":
            # Claimed by Retry (or a re-roll's recompose): don't leave it spinning.
            project.status = "failed"
            db.commit()
        raise

    audio, audio_fp = _recompose_voiceover(db, project)

    render = models.Render(
        project_id=project.id,
        status="rendering",
        audio_path=str(audio) if audio else "",
        voice_fingerprint=audio_fp,
    )
    db.add(render)
    db.flush()
    events.render_event(db, render, project, "started", detail="Putting the video together")
    db.commit()
    db.refresh(render)

    try:
        timed = base_captions(plan, sum(clip_seconds(c) for c in clips))
        _check_cancel(db, render)
        spoken = _spoken_texts(db, project, audio, audio_fp)
        _compose(db, project, render, clips, audio, timed, plan=plan, voice_fingerprint=audio_fp,
                 **(spoken or {}))
        render.status = "completed"
        project.status = "completed"
        events.render_event(db, render, project, "completed")
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
        if project.status == "generating":
            project.status = "failed"  # nothing else will move it on
        events.render_event(db, render, project, "failed", error=str(e))
        db.commit()
        raise


STUDIO_DELETED = "deleted"  # tombstone: the row stays so its id is never reused
STUDIO_RUNNING = ("pending", "submitted", "polling")


def studio_failed_alert(error: str) -> None:
    """One ops alert per burst of Studio failures (a failed batch is one alert)."""
    alerts.send("studio-failed", "Studio job failed", (error or "")[:600])


def studio_first_frame(db: Session, job: models.AssetGenerationJob) -> Optional[ImageRef]:
    """The picture an "Animate this" clip starts from, at a URL the provider
    can fetch, or None when the job has none. Raises ValueError when the
    picture is gone."""
    if job.first_frame_job_id:
        src = db.get(models.AssetGenerationJob, job.first_frame_job_id)
        if (src is None or src.status == STUDIO_DELETED or src.output_kind != "image"
                or not src.result_path or not Path(src.result_path).exists()):
            raise ValueError("the picture this clip starts from was deleted")
        from . import public_media
        from .content_mode import adult_ok_avatars

        # A cutout or upscale of a Cast picture is still that character's
        # picture (a newly made Studio picture isn't).
        avatar_id = studio_cast_avatar(db, src)
        return ImageRef(url=public_media.publish(storage.reference_copy(Path(src.result_path))),
                        role="scene" if avatar_id is None else "character",
                        name="first frame", frame_type="first_frame",
                        adult_ok=avatar_id is not None and avatar_adult_ok(db, avatar_id, adult_ok_avatars()))
    if job.first_frame_asset_id:
        asset = db.get(models.Asset, job.first_frame_asset_id)
        if asset is None or not (asset.mime_type or "").startswith("image/"):
            raise ValueError("the picture this clip starts from was deleted")
        from .content_mode import adult_ok_avatars

        # A Cast character's picture is that character's likeness, first
        # frame or not: unless the character is marked "adult content
        # allowed", the content filter stays on for it.
        cast = asset.owner_kind == "avatar" and asset.asset_type != "logo"
        return ImageRef(url=storage.public_url_for_token(asset.public_token),
                        role="character" if cast else "scene",
                        name="first frame", frame_type="first_frame",
                        adult_ok=cast and _exempt_asset(asset, adult_ok_avatars()))
    return None


def _studio_job_deleted(db: Session, job_id: int) -> bool:
    status = db.query(models.AssetGenerationJob.status).filter(
        models.AssetGenerationJob.id == job_id).scalar()
    return status is None or status == STUDIO_DELETED


def _finish_studio_image(db: Session, job: models.AssetGenerationJob, data: bytes, *,
                         sized: bool = False) -> models.AssetGenerationJob:
    """Store a finished picture for a Studio job and complete it. Deleted
    in Studio while the provider worked: the picture is dropped (it was
    still billed). The write is conditional too, so a delete or cancel in
    between can't be undone by this result. `sized` also records the
    picture's real size (operation results: cutouts and upscales)."""
    if _studio_job_deleted(db, job.id):
        db.rollback()
        return job
    ext, _mime, w, h = storage.image_info(data)
    path = storage.save_generated_bytes(data, extension=ext, subdir_name="images")
    values = {"result_path": str(path), "status": "completed",
              "provider": job.provider, "provider_model": job.provider_model}
    if sized and w and h:
        values.update(result_width=w, result_height=h)
    done = db.query(models.AssetGenerationJob).filter(
        models.AssetGenerationJob.id == job.id,
        models.AssetGenerationJob.status.in_(STUDIO_RUNNING),
    ).update(values, synchronize_session=False)
    if done:
        events.studio_event(db, job, "completed")
    db.commit()
    if not done:
        Path(path).unlink(missing_ok=True)
    db.refresh(job)
    return job


def _run_studio_operation(db: Session, job: models.AssetGenerationJob) -> models.AssetGenerationJob:
    """Remove the background of, or upscale, the job's source picture (a
    Studio result or a Cast picture). No prompt, so no safety check: the
    operation makes nothing new, it only cuts out or enlarges."""
    from ..providers import get_image_op
    from . import image_ops
    from . import model_registry as reg

    kind = image_ops.kind_for(job.operation)
    model = job.provider_model or reg.active_model(kind)
    provider = get_image_op(kind, model)
    job.provider = reg.provider_for(kind, model)
    job.provider_model = model
    db.commit()
    raw, mime = image_ops.load_source(db, job)
    data = provider.run(job.operation, model, raw, mime, dict(job.options_json or {}))  # type: ignore[arg-type]
    return _finish_studio_image(db, job, data, sized=True)


def run_studio_job(db: Session, job_id: int) -> models.AssetGenerationJob:
    job = db.get(models.AssetGenerationJob, job_id)
    if job is None:
        raise ValueError(f"studio job {job_id} not found")
    # The worker's claim already set "submitted"; this only moves a job
    # started another way, and never undoes a Cancel that landed between
    # the claim and here.
    from sqlalchemy import update as _update

    moved = db.execute(_update(models.AssetGenerationJob)
                       .where(models.AssetGenerationJob.id == job_id,
                              models.AssetGenerationJob.status.in_(("pending", "submitted")))
                       .values(status="submitted")).rowcount
    db.commit()
    db.refresh(job)
    if moved != 1:
        log.info("studio job %s is %s; not starting it", job_id, job.status)
        return job
    try:
        if job.operation:
            return _run_studio_operation(db, job)
        # The user's order, each with its role and name (an ingredient was
        # always sent as a "scene" before, in database order).
        refs = _asset_refs(db, list(job.reference_asset_ids_json or []))

        if job.output_kind == "image":
            from ..providers import get_image

            model = _resolve_model(
                db,
                job.provider_model or _active_model("image"),
            )
            image = get_image(model) if routes_per_model("image", model) else get_image()
            job.provider = provider_label("image", model)
            job.provider_model = model
            db.commit()
            # Characters are portraits (Studio's 9:16 tiles); scenes and
            # objects are landscape. Without it Sogni made square images.
            from .image_settings import best_image_settings

            aspect = job.aspect_ratio or ("9:16" if job.owner_kind == "avatar" else "16:9")
            data = image.generate(model=model, prompt=job.prompt, references=refs,
                                  negative_prompt=effective_negative("image", job.negative_prompt or ""),
                                  settings=best_image_settings(model, aspect) or None)
            # Deleted in Studio while the provider worked: drop the image
            # (it was still billed). The write below is conditional too, so
            # a delete in between can't be undone by this result.
            return _finish_studio_image(db, job, data)
        else:  # video_clip — async: completes via webhook / sweeper
            from . import video_jobs

            model = _resolve_model(db, job.provider_model or _active_model("video"))
            video = get_video(model) if routes_per_model("video", model) else get_video()
            job.provider = provider_label("video", model)
            job.provider_model = model
            moved = db.query(models.AssetGenerationJob).filter(
                models.AssetGenerationJob.id == job.id,
                models.AssetGenerationJob.status.in_(("pending", "submitted")),
            ).update({"status": "polling", "provider": job.provider, "provider_model": model},
                     synchronize_session=False)
            db.commit()
            db.refresh(job)
            if not moved:
                return job  # cancelled or deleted before anything was sent

            negative = effective_negative("video", job.negative_prompt or "")
            settings_extra: dict = {}
            if job.aspect_ratio:  # unset: the model's own default, as before
                settings_extra["aspect_ratio"] = job.aspect_ratio
            strategy = "input_references"
            first = studio_first_frame(db, job)
            if first is not None:
                # "Animate this": the clip starts on that picture. Frame
                # images and input references don't mix, so it is the one
                # picture sent (it already shows whoever is in it).
                refs, strategy = [first], "frame_images"

            def start(callback):
                return video.submit(
                    model=model, prompt=job.prompt, references=refs,
                    negative_prompt=negative, reference_strategy=strategy,
                    duration_seconds=float(job.duration_seconds or 5.0),
                    settings={**settings_extra, **({"callback_url": callback} if callback else {})} or None,
                )

            if _studio_job_deleted(db, job.id):
                db.rollback()
                return job
            vj = video_jobs.submit(db, purpose="studio", ref_id=job.id, project_id=None, start=start,
                                   webhooks=uses_webhooks(model))
            job.provider_job_id = vj.provider_job_id
            db.refresh(job)
            return job
        db.commit()
        return job
    except Exception as e:  # noqa: BLE001
        db.rollback()
        # Only a job still running gets the failure; a deleted (or
        # cancelled) one keeps its state.
        failed = db.query(models.AssetGenerationJob).filter(
            models.AssetGenerationJob.id == job_id,
            models.AssetGenerationJob.status.in_(STUDIO_RUNNING),
        ).update({"status": "failed", "error": str(e)[:500]}, synchronize_session=False)
        if failed:
            row = db.get(models.AssetGenerationJob, job_id)
            if row is not None:
                events.studio_event(db, row, "failed", error=str(e))
        db.commit()
        if failed:
            studio_failed_alert(str(e))
        raise


def save_studio_result_as_asset(
    db: Session,
    job: models.AssetGenerationJob,
    *,
    asset_type: Optional[str] = None,
    owner_kind: Optional[str] = None,
    owner_id: Optional[int] = None,
) -> models.Asset:
    """File a completed studio image into a cast member's asset library.

    Defaults to the job's own owner. Each call makes a new asset owning its
    own copy of the image, so one result can go to several characters and
    deleting the asset never takes the Studio result with it (or the other
    way round)."""
    if job.status not in ("completed", "saved") or not job.result_path:
        raise ValueError("studio job is not completed")
    if job.output_kind != "image":
        # We only file image results into the visual asset library; clips stay in /studio.
        raise ValueError("only image studio results can be saved as cast assets")
    import hashlib
    import uuid

    from .s3 import mirror_to_s3

    kind = owner_kind or job.owner_kind
    oid = owner_id if owner_id is not None else job.owner_id
    if asset_type is None:
        asset_type = "lifestyle" if kind == "avatar" else "reference"
    src = Path(job.result_path)
    raw = src.read_bytes()
    ext, mime, width, height = storage.image_info(raw)
    # Same place uploads live, so the asset is an ordinary owned file.
    subdir = get_settings().data_path / "uploads" / kind / str(oid)
    subdir.mkdir(parents=True, exist_ok=True)
    dest = subdir / f"{uuid.uuid4().hex}.{ext}"
    dest.write_bytes(raw)
    mirror_to_s3(dest, content_type=mime)
    asset = models.Asset(
        owner_kind=kind,
        avatar_id=oid if kind == "avatar" else None,
        ingredient_id=oid if kind == "ingredient" else None,
        asset_type=asset_type,
        original_filename=src.name,
        file_path=str(dest),
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
    db.refresh(asset)
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
    events.training_event(db, cm, "started")
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
                events.training_event(db, cm, "completed")
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
                _training_failed(db, cm)
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
                _training_failed(db, cm)
                return
            sleep(2)
            elapsed += 2

    except Exception as e:  # noqa: BLE001
        cm.status = "failed"
        cm.error = str(e)[:500]
        _training_failed(db, cm)
        raise


def _training_failed(db: Session, cm: models.CustomModel) -> None:
    """Commit a failed training run, with its event and an ops alert."""
    events.training_event(db, cm, "failed", error=cm.error)
    db.commit()
    alerts.send(f"training-failed:{cm.id}", f"Training failed: {cm.name or cm.id}", (cm.error or "")[:600])


def on_studio_clip_ready(db: Session, vjob: models.VideoJob) -> None:
    job = db.get(models.AssetGenerationJob, vjob.ref_id)
    if job is None or job.status == STUDIO_DELETED:
        # The Studio job was deleted while the clip was rendering: nothing
        # will ever show this download, so don't leave it on disk.
        if vjob.result_path:
            Path(vjob.result_path).unlink(missing_ok=True)
        return
    if job.status != "polling":
        # Cancelled (or already finished): the clip was billed and its spend
        # recorded, but nothing shows it.
        if vjob.result_path:
            Path(vjob.result_path).unlink(missing_ok=True)
        return
    from sqlalchemy import update as _update

    from .renderer import _probe_duration, _probe_size

    data = Path(vjob.result_path).read_bytes()
    path = storage.save_generated_bytes(data, extension="mp4", subdir_name="clips")
    try:
        size = _probe_size(path)
        duration = _probe_duration(path)
        # Claimed atomically: two sweepers (or the handler and a sweeper)
        # applying the same clip save it once and announce it once.
        Job = models.AssetGenerationJob
        won = db.execute(_update(Job).where(Job.id == job.id, Job.status == "polling").values(
            result_path=str(path),
            result_width=size[0] if size else None, result_height=size[1] if size else None,
            result_duration_seconds=round(duration, 3) if duration > 0 else None,
            status="completed", error="", updated_at=datetime.utcnow(),
        )).rowcount
        if won != 1:
            db.rollback()
            Path(path).unlink(missing_ok=True)
            return
        db.refresh(job)
        events.studio_event(db, job, "completed")
        db.commit()
    except Exception:
        # Nothing saved: drop the copy and keep the provider's file, so the
        # sweeper's retry can still apply the clip.
        db.rollback()
        Path(path).unlink(missing_ok=True)
        raise
    Path(vjob.result_path).unlink(missing_ok=True)  # only once the clip is saved


def on_studio_clip_failed(db: Session, vjob: models.VideoJob) -> None:
    job = db.get(models.AssetGenerationJob, vjob.ref_id)
    if job is None or job.status != "polling":
        return  # deleted, cancelled or already finished
    job.status = "failed"
    job.error = vjob.error[:500]
    events.studio_event(db, job, "failed", error=vjob.error)
    db.commit()
    studio_failed_alert(vjob.error)

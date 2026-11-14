from __future__ import annotations

import io
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel
from slugify import slugify
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..schemas import ProjectStatusOut, RenderOut, ShotOut
from ..services import content_mode, provider_gate, render_files
from ..services.preflight import run_preflight
from ..services.ratelimit import rate_limit
from ..services.safety import UnsafeScriptError, check_script, validate_script
from ..services.spend import budget_guard, require_budget_for
from ..workers.jobs import (
    generate_plan_job,
    generate_video_job,
    recompose_project_job,
    regenerate_shot_job,
    regenerate_shots_bulk_job,
)
from ..workers.queue import enqueue
from .auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])

# Project states a plan or render may start from (anything else: a plan,
# render or re-roll is running).
_IDLE = ("draft", "planned", "completed", "failed", "cancelled")
_DONE_RENDER = ("completed", "failed", "cancelled", "expired")


def claim_project(db: Session, project: models.VideoProject, to: str, busy: str,
                  *, hook_lab: bool = False) -> tuple[str, str]:
    """Move the project to `to` ("generating" / "planning") before its job is
    queued, atomically and only from an idle state with no render running,
    so a double-click or a second tab can't queue a second paid run (409
    `busy`), and the editor keeps polling while the job waits for a worker.
    Returns the previous status (for release_project) and the claim's
    unique token, which the queued job must consume before it does anything.
    `hook_lab`: a hook-lab "Make videos" claim; its token says so and holds
    the previous status (pipeline.hook_claim_token)."""
    from ..services.pipeline import hook_claim_token

    latest = (db.query(models.Render.status).filter(models.Render.project_id == project.id)
              .order_by(models.Render.id.desc()).first())
    if latest is not None and latest[0] not in _DONE_RENDER:
        raise HTTPException(409, busy)
    previous = project.status or "draft"
    previous = previous if previous in _IDLE else "draft"
    token = hook_claim_token(previous) if hook_lab else uuid.uuid4().hex
    claimed = (db.query(models.VideoProject)
               .filter(models.VideoProject.id == project.id, models.VideoProject.status.in_(_IDLE))
               .update({"status": to, "claim_token": token, "updated_at": datetime.utcnow()},
                       synchronize_session=False))
    db.commit()
    if not claimed:
        raise HTTPException(409, busy)
    db.refresh(project)
    return previous, token


def release_project(db: Session, project_id: int, claimed: str, previous: str, token: str) -> None:
    """The job could not be queued: give the claim back."""
    db.rollback()
    db.query(models.VideoProject).filter(
        models.VideoProject.id == project_id, models.VideoProject.status == claimed,
        models.VideoProject.claim_token == token,
    ).update({"status": previous, "claim_token": ""}, synchronize_session=False)
    db.commit()


def _enqueue_claimed(db: Session, project: models.VideoProject, to: str, busy: str, func, *args, **kwargs) -> str:  # noqa: ANN001
    previous, token = claim_project(db, project, to, busy)
    try:
        job_id = enqueue(func, *args, claim=to, claim_token=token, **kwargs)
    except Exception:
        release_project(db, project.id, to, previous, token)
        raise
    # Kept while the claim holds, so the sweeper can tell a job waiting in a
    # busy queue from one whose queue entry is gone.
    db.query(models.VideoProject).filter(
        models.VideoProject.id == project.id, models.VideoProject.status == to,
    ).update({"queue_job_id": str(job_id)[:80]}, synchronize_session=False)
    db.commit()
    return job_id

# Public, token-gated router (no auth) for sharing finished renders.
public_router = APIRouter()


@public_router.get("/public-renders/{token}")
def public_render(token: str, db: Session = Depends(get_db)) -> FileResponse:
    r = db.query(models.Render).filter(models.Render.share_token == token).first()
    if r is None or r.status != "completed" or not r.final_video_path:
        raise HTTPException(404, "render not found")
    if not Path(r.final_video_path).exists():
        raise HTTPException(404, "render file missing")
    url = render_files.delivery_url(r.final_video_path, "video/mp4")
    if url:
        return RedirectResponse(url, status_code=302)
    return FileResponse(r.final_video_path, media_type="video/mp4")


@public_router.get("/public-renders/{token}/thumbnail")
def public_render_thumbnail(token: str, db: Session = Depends(get_db)) -> FileResponse:
    r = db.query(models.Render).filter(models.Render.share_token == token).first()
    if r is None or not r.thumbnail_path or not Path(r.thumbnail_path).exists():
        raise HTTPException(404, "thumbnail not found")
    url = render_files.delivery_url(r.thumbnail_path, "image/jpeg")
    if url:
        return RedirectResponse(url, status_code=302)
    return FileResponse(r.thumbnail_path, media_type="image/jpeg")


@public_router.get("/public-renders/{token}/meta")
def public_render_meta(token: str, db: Session = Depends(get_db)) -> dict:
    """Minimal metadata for the public share landing page (no auth)."""
    r = db.query(models.Render).filter(models.Render.share_token == token).first()
    if r is None or r.status != "completed":
        raise HTTPException(404, "render not found")
    project = r.project
    plan = project.generated_plan_json or {}
    return {
        "title": project.title or "AI-generated video",
        "aspect_ratio": project.aspect_ratio,
        "created_at": r.created_at.isoformat(),
        "disclosure": plan.get("disclosure_text") or "AI-generated virtual creator",
    }


@router.post("/projects/{project_id}/generate-plan", status_code=202, dependencies=[Depends(rate_limit("generation")), Depends(budget_guard)])
def generate_plan(
    project_id: int,
    preserve_edits: bool = False,
    db: Session = Depends(get_db),
) -> dict:
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    try:
        validate_script(project.original_script or "")
        # Everything the planner reads: in adult mode a permissive model plans.
        check_script("\n".join(x for x in (project.original_script, project.creative_direction,
                                            project.title, project.cta_text) if x))
    except UnsafeScriptError as e:
        raise HTTPException(422, str(e)) from e
    # A clean deployment: the script is voiced, so an explicit one is
    # refused before planning (the planned script is checked again).
    from ..services import explicit_check

    explicit_check.check_or_422(project.original_script, project.cta_text)
    provider_gate.require_model("chat", content_mode.chat_model())
    job_id = _enqueue_claimed(db, project, "planning", "Already planning this storyboard",
                              generate_plan_job, project_id, preserve_edits=preserve_edits)
    return {"project_id": project_id, "job_id": job_id, "status": "enqueued"}


@router.get("/projects/{project_id}/preflight")
def preflight(project_id: int, db: Session = Depends(get_db)) -> dict:
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    return run_preflight(db, project)


@router.get("/projects/{project_id}/cost-estimate")
def cost_estimate(project_id: int, db: Session = Depends(get_db)) -> dict:
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    if not project.generated_plan_json:
        raise HTTPException(409, "generate a storyboard plan first")
    from ..schemas.storyboard import StoryboardPlan
    from ..services.cost import estimate_for_project

    plan = StoryboardPlan.model_validate(project.generated_plan_json)
    # Music generation isn't priced here (we can't know intent); the
    # Studio/AudioPanel surfaces its cost separately.
    return estimate_for_project(db, project, plan)


@router.get("/projects/{project_id}/render-plan")
def render_plan(project_id: int, db: Session = Depends(get_db)) -> dict:
    """What "Generate video" would do right now, before anything is paid
    for: voiceover made or reused, and which clips are reused, still being
    made, or made new (with the reason and a rate-card price)."""
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    if not project.generated_plan_json:
        raise HTTPException(409, "generate a storyboard plan first")
    from ..services.pipeline import render_plan as _render_plan

    return _render_plan(db, project)


def _check_own_voiceover(project: models.VideoProject) -> None:
    """A clean deployment: an uploaded voiceover (one saved before the
    switch) can't be checked, so a render that would use it is refused."""
    if (project.voiceover_source == "upload" and project.voiceover_upload_path
            and Path(project.voiceover_upload_path).exists() and not content_mode.own_audio_allowed()):
        raise HTTPException(422, content_mode.VOICEOVER_UPLOAD_OFF)


def _check_shot_prompts(shots) -> None:  # noqa: ANN001 — VideoShot rows
    """A clean deployment: every clip prompt a render or re-roll will send
    gets the explicit-content check now (a 422), not in the worker after
    the voiceover is paid for. Cached, so the worker's checks are hits."""
    from ..services import explicit_check

    if explicit_check.active():
        explicit_check.check_prompts_or_422(*(s.prompt for s in shots if s.shot_type != "end_card"))


@router.post("/projects/{project_id}/generate-video", status_code=202, dependencies=[Depends(rate_limit("generation")), Depends(budget_guard)])
def generate_video(
    project_id: int,
    force: bool = False,
    db: Session = Depends(get_db),
) -> dict:
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    if not project.generated_plan_json:
        raise HTTPException(409, "generate a storyboard plan first")
    provider_gate.require_project(db, project)  # even with force: a switched-off provider is refused
    _check_own_voiceover(project)
    _check_shot_prompts(project.shots)
    if not force:
        result = run_preflight(db, project)
        if not result["ok"]:
            raise HTTPException(422, detail={"preflight": result})
    from ..services.pipeline import render_plan as _render_plan

    # The reuse-aware estimate counts against the cap, not only what is spent.
    require_budget_for(db, _render_plan(db, project)["total"])
    job_id = _enqueue_claimed(db, project, "generating", "Already making this video",
                              generate_video_job, project_id)
    return {"project_id": project_id, "job_id": job_id, "status": "enqueued"}


@router.post("/projects/{project_id}/recompose", status_code=202, dependencies=[Depends(rate_limit("generation"))])
def recompose_video(project_id: int, db: Session = Depends(get_db)) -> dict:
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    if not project.generated_plan_json:
        raise HTTPException(409, "generate a storyboard plan first")
    # Claimed like Generate: 409 while a render, a re-roll or another compose
    # of this project (a hook-lab variant too) is running, so two composes
    # never run at once.
    _refuse_while_composing(db, project)
    _check_own_voiceover(project)
    job_id = _enqueue_claimed(db, project, "generating", "Already making this video",
                              recompose_project_job, project_id)
    return {"project_id": project_id, "job_id": job_id, "status": "enqueued"}


def _refuse_while_composing(db: Session, project: models.VideoProject) -> None:
    """409 when any render of the project (not only the newest) is still
    being put together, e.g. a hook-lab variant composing next to it."""
    running = (db.query(models.Render.id)
               .filter(models.Render.project_id == project.id,
                       models.Render.status.in_(("rendering", "composing")))
               .first())
    if running is not None:
        raise HTTPException(409, "Already making this video")


@router.post("/projects/{project_id}/cancel")
def cancel_render(project_id: int, db: Session = Depends(get_db)) -> dict:
    """Signal the running pipeline to stop at the next cooperative checkpoint.

    The pipeline checks the render's status between shots / before compose; on
    seeing 'cancelled' it raises RenderCancelled, which the render path turns
    into a terminal cancelled state on both the render row and the project.

    Returns 200 with the latest render's state — the caller polls /status as
    usual to see the cancellation land (may take up to one provider call to
    propagate, e.g. one in-flight video poll cycle).

    Clips already submitted to the provider are not stopped: they finish and
    are billed. `clips_in_flight` says how many; a later resume waits for
    them instead of submitting them again.
    """
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    latest = (
        db.query(models.Render)
        # Hook-lab variants have their own progress (hook lab, events).
        .filter(models.Render.project_id == project_id, models.Render.hook_variant_id.is_(None))
        .order_by(models.Render.created_at.desc())
        .first()
    )
    from ..services import video_jobs
    from ..services.pipeline import hook_claim_previous

    if project.status == "generating" and hook_claim_previous(project.claim_token):
        # Held by the hook lab's "Make videos": its openings are stopped
        # there, one by one; this never cancels the finished video.
        raise HTTPException(409, "Other openings are being made for this video. Stop them in the hook lab.")
    if latest is None or latest.status in _DONE_RENDER:
        # Claimed (Generate / Retry / plan) but its job hasn't made a render
        # yet: still queued, or it stopped before it could. Give the claim
        # back, so the editor stops waiting and Generate works again; the
        # queued job sees the claim is gone and does nothing.
        released = _release_waiting_claim(db, project)
        if released:
            return {"render_id": latest.id if latest is not None else None, "status": "cancelled",
                    "action": "released", "clips_in_flight": video_jobs.pending_for(db, project_id=project_id)}
    if latest is None:
        raise HTTPException(409, "no render in flight")
    in_flight = video_jobs.pending_for(db, project_id=project_id)
    if latest.status in _DONE_RENDER:
        return {"render_id": latest.id, "status": latest.status, "action": "noop",
                "clips_in_flight": in_flight}
    latest.status = "cancelled"
    # Shots are submitted asynchronously, so there may be no worker left in
    # render_project to mirror this; in-flight clips that land later see a
    # cancelled render and don't compose.
    project.status = "cancelled"
    from ..services import events

    events.render_event(db, latest, project, "cancelled")
    db.commit()
    return {"render_id": latest.id, "status": "cancelled", "action": "signalled",
            "clips_in_flight": in_flight}


def _release_waiting_claim(db: Session, project: models.VideoProject) -> bool:
    """generating / planning with no render running → cancelled (atomic)."""
    was = project.status
    if was not in ("generating", "planning"):
        return False
    from ..services import events
    from ..services.pipeline import HOOK_CLAIM

    # Only while no worker has taken the claim over (its token is still
    # set): after that the job owns it and cancel goes through its render.
    # Never a hook-lab claim (the project's finished video stays as it is).
    done = (db.query(models.VideoProject)
            .filter(models.VideoProject.id == project.id, models.VideoProject.status == was,
                    models.VideoProject.claim_token != "",
                    ~models.VideoProject.claim_token.startswith(HOOK_CLAIM))
            .update({"status": "cancelled", "claim_token": "", "updated_at": datetime.utcnow()},
                    synchronize_session=False))
    if done != 1:
        db.rollback()
        return False
    db.refresh(project)
    if was == "planning":
        events.plan_event(db, project, "cancelled")
    else:
        kind = "ambient_loop" if project.mode == "ambient_loop" else "render"
        events.emit(db, kind, "project", project.id, "cancelled", f"Cancelled: {events._name(project)}",
                    url=events.project_url(project), project_id=project.id)
    db.commit()
    return True


@router.post("/projects/{project_id}/retry", status_code=202, dependencies=[Depends(rate_limit("generation")), Depends(budget_guard)])
def retry_render(project_id: int, db: Session = Depends(get_db)) -> dict:
    """Smart retry after a failed render. Picks the cheapest viable path:

      - If every body shot has a clip that still matches it AND the last
        render's voiceover is intact and current → just recompose (no provider
        calls).
      - Otherwise → resume the pipeline. render_project reuses clips that
        still match their shot, waits for clips that are still being made
        (never submitting those twice) and reuses an unchanged voiceover, so
        only failed, missing or edited shots are generated. Failed shots are
        reset to 'pending' so they actually retry.
    """
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    if not project.generated_plan_json:
        raise HTTPException(409, "generate a storyboard plan first")

    body_shots = [s for s in project.shots if s.shot_type != "end_card"]
    if not body_shots:
        raise HTTPException(409, "no body shots to render — re-plan the storyboard first")
    # Before anything is reset: a render already running refuses a second one.
    running = (db.query(models.Render.status).filter(models.Render.project_id == project.id)
               .order_by(models.Render.id.desc()).first())
    if (project.status or "draft") not in _IDLE or (running is not None and running[0] not in _DONE_RENDER):
        raise HTTPException(409, "Already making this video")

    # Reset any failed shots so the pipeline's idempotent check doesn't skip them.
    for s in body_shots:
        if s.status == "failed":
            s.status = "pending"
            s.clip_path = ""
            s.error = ""
    db.commit()

    from ..services.pipeline import adopt_unfingerprinted_voiceover
    from ..services.pipeline import render_plan as _render_plan

    # A voiceover made before fingerprints existed counts as current here
    # (same rule as clips): Retry finishes the failed render, it does not
    # buy the voiceover again.
    adopt_unfingerprinted_voiceover(db, project)
    plan = _render_plan(db, project)
    all_clips = (
        all(s.clip_path and Path(s.clip_path).exists() for s in body_shots)
        and plan["generate"] == 0 and plan["in_flight"] == 0
        and plan["voiceover"] != "new" and not plan["voiceover_changed"]
    )
    latest = (
        db.query(models.Render)
        # Hook-lab variants have their own progress (hook lab, events).
        .filter(models.Render.project_id == project_id, models.Render.hook_variant_id.is_(None))
        .order_by(models.Render.created_at.desc())
        .first()
    )
    have_audio = bool(latest and latest.audio_path and Path(latest.audio_path).exists())

    busy = "Already making this video"
    if all_clips and have_audio:
        job_id = _enqueue_claimed(db, project, "generating", busy, recompose_project_job, project_id)
        action = "recompose"
        voiceover, voiceover_cost = "reuse", 0.0
    else:
        voiceover, voiceover_cost = plan["voiceover"], plan["voiceover_cost"]
        provider_gate.require_project(db, project)  # only the shots it resubmits
        require_budget_for(db, plan["total"])
        job_id = _enqueue_claimed(db, project, "generating", busy, generate_video_job, project_id)
        action = "resume"
    return {
        "project_id": project_id,
        "job_id": job_id,
        "action": action,
        "status": "enqueued",
        # Clips still being made from the earlier run: waited for, not resubmitted.
        "clips_in_flight": plan["in_flight"],
        # What happens to the voiceover: "reuse", "new" (paid), "upload" or "silent".
        "voiceover": voiceover,
        "voiceover_cost": voiceover_cost,
    }


def _refuse_stale_voice_reroll(db: Session, project: models.VideoProject, shots: list,
                               *, recompose: bool = True) -> None:
    """409 when a re-roll would leave lip-sync out of step with the
    voiceover: either it would be cut from a voiceover that is out of date
    (paid for twice), or the recompose would put the new voiceover under
    other clips still cut from the old one."""
    from ..services.pipeline import current_voiceover_made, regen_needs_new_voiceover

    blocked = regen_needs_new_voiceover(db, project, shots, recompose=recompose)
    if not blocked:
        return
    orders = ", ".join(str(s.shot_order) for s in sorted(blocked, key=lambda s: s.shot_order))
    one = len(blocked) == 1
    if current_voiceover_made(db, project):
        why = ("The new voiceover is made, but re-composing now would put it under lip-synced clips "
               "still cut from the old one, so the video would be out of step. Re-roll "
               f"{'it' if one else 'them'} together, or use Generate video: it makes "
               f"{'this clip' if one else 'these clips'} with the new voiceover.")
    else:
        why = ("A re-roll would use the old voiceover and be paid for twice. Use Generate video: it "
               f"makes the new voiceover and {'this clip' if one else 'these clips'} together.")
    raise HTTPException(
        409,
        f"Shot{'' if one else 's'} {orders} {'is' if one else 'are'} lip-synced to the voiceover, "
        f"and the spoken script or voice changed since the voiceover was made. {why}",
    )


def _refuse_while_hook_lab(db: Session, project_id: int) -> None:
    """409 while other openings are being made: a re-roll's recompose would
    otherwise race them for the project."""
    from ..services.pipeline import hook_lab_busy

    reason = hook_lab_busy(db, project_id)
    if reason:
        raise HTTPException(409, reason)


class BulkRegenIn(BaseModel):
    shot_ids: list[int]


@router.post("/projects/{project_id}/shots/regenerate-bulk", status_code=202, dependencies=[Depends(rate_limit("generation")), Depends(budget_guard)])
def regenerate_shots_bulk(
    project_id: int, body: BulkRegenIn, db: Session = Depends(get_db)
) -> dict:
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    valid = {s.id for s in project.shots if s.shot_type != "end_card"}
    targets = [sid for sid in body.shot_ids if sid in valid]
    if not targets:
        raise HTTPException(422, "no regenerable shots in the selection")
    _refuse_while_hook_lab(db, project_id)
    _refuse_stale_voice_reroll(db, project, [s for s in project.shots if s.id in set(targets)])
    provider_gate.require_project(db, project, shot_ids=set(targets))
    _check_shot_prompts([s for s in project.shots if s.id in set(targets)])
    job_id = enqueue(regenerate_shots_bulk_job, project_id, targets)
    return {
        "project_id": project_id,
        "job_id": job_id,
        "status": "enqueued",
        "shot_ids": targets,
    }


@router.get("/projects/{project_id}/status", response_model=ProjectStatusOut)
def project_status(project_id: int, db: Session = Depends(get_db)) -> ProjectStatusOut:
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    latest = (
        db.query(models.Render)
        # Hook-lab variants have their own progress (hook lab, events).
        .filter(models.Render.project_id == project_id, models.Render.hook_variant_id.is_(None))
        .order_by(models.Render.created_at.desc())
        .first()
    )
    from ..services.hook_lab import RUNNING
    from ..services.pipeline import hook_claim_previous

    # Held by the hook lab's "Make videos", not by a render of its own.
    making = project.status == "generating" and hook_claim_previous(project.claim_token) is not None
    making_base = None
    if making:
        # The version whose openings are being made, so the project page
        # links the hook lab to it (not to the newest version).
        making_base = (
            db.query(models.HookVariant.base_render_id)
            .filter(models.HookVariant.project_id == project_id, models.HookVariant.status.in_(RUNNING))
            .order_by(models.HookVariant.updated_at.desc(), models.HookVariant.id.desc())
            .limit(1)
            .scalar()
        )
    return ProjectStatusOut(
        project_id=project.id,
        project_status=project.status,
        hook_lab_making=making,
        hook_lab_base_render_id=making_base,
        latest_render=render_out(db, latest) if latest else None,
        shots=[ShotOut.model_validate(s) for s in project.shots],
    )


@router.post("/projects/{project_id}/shots/{shot_id}/regenerate", status_code=202, dependencies=[Depends(rate_limit("generation")), Depends(budget_guard)])
def regenerate_shot(
    project_id: int,
    shot_id: int,
    recompose: bool = True,
    db: Session = Depends(get_db),
) -> dict:
    shot = db.get(models.VideoShot, shot_id)
    if shot is None or shot.project_id != project_id:
        raise HTTPException(404, "shot not found in project")
    _refuse_while_hook_lab(db, project_id)
    _refuse_stale_voice_reroll(db, shot.project, [shot], recompose=recompose)
    provider_gate.require_model("video", shot.model_override or None, db)
    _check_shot_prompts([shot])
    job_id = enqueue(regenerate_shot_job, project_id, shot_id, recompose)
    return {
        "shot_id": shot_id,
        "job_id": job_id,
        "status": "enqueued",
        "will_recompose": recompose,
    }


@router.get("/projects/{project_id}/shots/{shot_id}/clip")
def shot_clip(project_id: int, shot_id: int, db: Session = Depends(get_db)) -> FileResponse:
    """Stream a single shot's generated clip for in-editor preview."""
    shot = db.get(models.VideoShot, shot_id)
    if shot is None or shot.project_id != project_id:
        raise HTTPException(404, "shot not found in project")
    if not shot.clip_path or not Path(shot.clip_path).exists():
        raise HTTPException(404, "clip not generated yet")
    return FileResponse(shot.clip_path, media_type="video/mp4")


def render_outs(db: Session, renders: list[models.Render]) -> list[RenderOut]:
    """Renders with their version number and label ("Version 3 · opening B",
    "Failed attempt"): the same numbering everywhere."""
    from ..services import render_versions

    info = render_versions.describe(db, renders)
    vids = {r.hook_variant_id for r in renders if r.hook_variant_id}
    bases = dict(db.query(models.HookVariant.id, models.HookVariant.base_render_id)
                 .filter(models.HookVariant.id.in_(vids)).all()) if vids else {}
    return [RenderOut.model_validate(r).model_copy(update={
        "version": info[r.id]["version"], "version_label": info[r.id]["version_label"],
        "hook_base_render_id": bases.get(r.hook_variant_id) if r.hook_variant_id else None})
        for r in renders]


def render_out(db: Session, render: models.Render) -> RenderOut:
    return render_outs(db, [render])[0]


@router.get("/projects/{project_id}/renders", response_model=list[RenderOut])
def list_renders(project_id: int, db: Session = Depends(get_db)) -> list[RenderOut]:
    if db.get(models.VideoProject, project_id) is None:
        raise HTTPException(404, "project not found")
    return render_outs(db, (
        db.query(models.Render)
        .filter(models.Render.project_id == project_id)
        .order_by(models.Render.created_at.desc())
        .all()
    ))


@router.get("/projects/{project_id}/renders/export")
def export_renders(project_id: int, db: Session = Depends(get_db)) -> StreamingResponse:
    """Bundle every completed render's MP4 (+ thumbnail) for a project into a
    single downloadable zip, newest version first."""
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    renders = (
        db.query(models.Render)
        .filter(models.Render.project_id == project_id, models.Render.status == "completed")
        .order_by(models.Render.created_at.desc())
        .all()
    )
    completed = [r for r in renders if r.final_video_path and Path(r.final_video_path).exists()]
    if not completed:
        raise HTTPException(404, "no completed renders to export")
    from ..services import render_versions

    info = render_versions.describe(db, completed)

    slug = slugify(project.title or f"project-{project_id}") or f"project-{project_id}"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for r in completed:
            # The same number the render page and Publishing show.
            v = info[r.id]
            base = f"{slug}_v{v['version'] or r.id}"
            if v["opening"]:
                base += f"_opening_{slugify(v['opening'])}"
            zf.write(r.final_video_path, arcname=f"{base}.mp4")
            if r.thumbnail_path and Path(r.thumbnail_path).exists():
                zf.write(r.thumbnail_path, arcname=f"{base}.jpg")
    buf.seek(0)
    headers = {"Content-Disposition": f'attachment; filename="{slug}_renders.zip"'}
    return StreamingResponse(buf, media_type="application/zip", headers=headers)


@router.get("/renders/recent")
def recent_renders(limit: int = 12, db: Session = Depends(get_db)) -> list[dict]:
    """Most-recent completed renders across all projects, for the dashboard gallery."""
    limit = max(1, min(limit, 48))
    rows = (
        db.query(models.Render, models.VideoProject.title)
        .join(models.VideoProject, models.Render.project_id == models.VideoProject.id)
        .filter(models.Render.status == "completed", models.Render.final_video_path != "")
        .order_by(models.Render.created_at.desc())
        .limit(limit)
        .all()
    )
    out: list[dict] = []
    for r, title in rows:
        if not (r.final_video_path and Path(r.final_video_path).exists()):
            continue
        out.append({
            "render_id": r.id,
            "project_id": r.project_id,
            "project_title": title or "Untitled",
            "share_token": r.share_token,
            "created_at": r.created_at.isoformat(),
        })
    return out


@router.get("/renders/{render_id}", response_model=RenderOut)
def get_render(render_id: int, db: Session = Depends(get_db)) -> RenderOut:
    r = db.get(models.Render, render_id)
    if r is None:
        raise HTTPException(404, "render not found")
    return render_out(db, r)


@router.get("/renders/{render_id}/download")
def download_render(render_id: int, db: Session = Depends(get_db)) -> FileResponse:
    r = db.get(models.Render, render_id)
    if r is None or not r.final_video_path or not Path(r.final_video_path).exists():
        raise HTTPException(404, "render not ready")
    url = render_files.delivery_url(r.final_video_path, "video/mp4", download_name=f"avatar-video-{render_id}.mp4")
    if url:
        # Hour-long MP4s are 50–500 MB; let S3 (or a CDN in front) serve them.
        return RedirectResponse(url, status_code=302)
    return FileResponse(
        r.final_video_path,
        media_type="video/mp4",
        filename=f"avatar-video-{render_id}.mp4",
    )


@router.get("/renders/{render_id}/thumbnail")
def download_thumbnail(render_id: int, db: Session = Depends(get_db)) -> FileResponse:
    r = db.get(models.Render, render_id)
    if r is None or not r.thumbnail_path or not Path(r.thumbnail_path).exists():
        raise HTTPException(404, "thumbnail not ready")
    return FileResponse(r.thumbnail_path, media_type="image/jpeg")

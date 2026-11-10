from __future__ import annotations

import io
import zipfile
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
from ..services.spend import budget_guard
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
    provider_gate.require_model("chat", content_mode.chat_model())
    job_id = enqueue(generate_plan_job, project_id, preserve_edits=preserve_edits)
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
    if not force:
        result = run_preflight(db, project)
        if not result["ok"]:
            raise HTTPException(422, detail={"preflight": result})
    job_id = enqueue(generate_video_job, project_id)
    return {"project_id": project_id, "job_id": job_id, "status": "enqueued"}


@router.post("/projects/{project_id}/recompose", status_code=202, dependencies=[Depends(rate_limit("generation"))])
def recompose_video(project_id: int, db: Session = Depends(get_db)) -> dict:
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    if not project.generated_plan_json:
        raise HTTPException(409, "generate a storyboard plan first")
    job_id = enqueue(recompose_project_job, project_id)
    return {"project_id": project_id, "job_id": job_id, "status": "enqueued"}


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
        .filter(models.Render.project_id == project_id)
        .order_by(models.Render.created_at.desc())
        .first()
    )
    if latest is None:
        raise HTTPException(409, "no render in flight")
    from ..services import video_jobs

    in_flight = video_jobs.pending_for(db, project_id=project_id)
    if latest.status in ("completed", "failed", "cancelled"):
        return {"render_id": latest.id, "status": latest.status, "action": "noop",
                "clips_in_flight": in_flight}
    latest.status = "cancelled"
    # Shots are submitted asynchronously, so there may be no worker left in
    # render_project to mirror this; in-flight clips that land later see a
    # cancelled render and don't compose.
    project.status = "cancelled"
    db.commit()
    return {"render_id": latest.id, "status": "cancelled", "action": "signalled",
            "clips_in_flight": in_flight}


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
        .filter(models.Render.project_id == project_id)
        .order_by(models.Render.created_at.desc())
        .first()
    )
    have_audio = bool(latest and latest.audio_path and Path(latest.audio_path).exists())

    if all_clips and have_audio:
        job_id = enqueue(recompose_project_job, project_id)
        action = "recompose"
        voiceover, voiceover_cost = "reuse", 0.0
    else:
        voiceover, voiceover_cost = plan["voiceover"], plan["voiceover_cost"]
        provider_gate.require_project(db, project)  # only the shots it resubmits
        job_id = enqueue(generate_video_job, project_id)
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


def _refuse_stale_voice_reroll(db: Session, project: models.VideoProject, shots: list) -> None:
    """409 when a re-roll would lip-sync a shot to a voiceover that is out
    of date: the paid clip would still not match, and the next Generate
    video would buy it again."""
    from ..services.pipeline import regen_needs_new_voiceover

    blocked = regen_needs_new_voiceover(db, project, shots)
    if not blocked:
        return
    orders = ", ".join(str(s.shot_order) for s in sorted(blocked, key=lambda s: s.shot_order))
    one = len(blocked) == 1
    raise HTTPException(
        409,
        f"Shot{'' if one else 's'} {orders} {'is' if one else 'are'} lip-synced to the voiceover, "
        "and the spoken script or voice changed since the voiceover was made. A re-roll would use "
        "the old voiceover and be paid for twice. Use Generate video: it makes the new voiceover "
        f"and {'this clip' if one else 'these clips'} together.",
    )


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
    _refuse_stale_voice_reroll(db, project, [s for s in project.shots if s.id in set(targets)])
    provider_gate.require_project(db, project, shot_ids=set(targets))
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
        .filter(models.Render.project_id == project_id)
        .order_by(models.Render.created_at.desc())
        .first()
    )
    return ProjectStatusOut(
        project_id=project.id,
        project_status=project.status,
        latest_render=RenderOut.model_validate(latest) if latest else None,
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
    _refuse_stale_voice_reroll(db, shot.project, [shot])
    provider_gate.require_model("video", shot.model_override or None, db)
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


@router.get("/projects/{project_id}/renders", response_model=list[RenderOut])
def list_renders(project_id: int, db: Session = Depends(get_db)) -> list[models.Render]:
    if db.get(models.VideoProject, project_id) is None:
        raise HTTPException(404, "project not found")
    return (
        db.query(models.Render)
        .filter(models.Render.project_id == project_id)
        .order_by(models.Render.created_at.desc())
        .all()
    )


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

    slug = slugify(project.title or f"project-{project_id}") or f"project-{project_id}"
    buf = io.BytesIO()
    total = len(completed)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for idx, r in enumerate(completed):
            version = total - idx  # newest = highest version number
            base = f"{slug}_v{version}"
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
def get_render(render_id: int, db: Session = Depends(get_db)) -> models.Render:
    r = db.get(models.Render, render_id)
    if r is None:
        raise HTTPException(404, "render not found")
    return r


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

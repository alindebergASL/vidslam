"""Hook lab: other openings (first spoken line, optionally shot 1) of a
finished storyboard video, made without paying for the whole video again.

GET    /api/projects/{id}/hook-lab               base versions, their openings, window, shot 1 options
POST   /api/projects/{id}/hook-lab/suggest       opening ideas from the planner (rate-limited, ~$0.001)
POST   /api/projects/{id}/hook-lab/variants      create drafts (no spend) + their price
POST   /api/projects/{id}/hook-lab/plan          price of making some drafts (no spend)
POST   /api/projects/{id}/hook-lab/make          make + compose them (guarded, claimed)
GET    /api/hook-variants/{id}                   one opening
PATCH  /api/hook-variants/{id}                   edit a draft (or a failed one)
DELETE /api/hook-variants/{id}                   remove (?delete_render=true for a finished one)
POST   /api/hook-variants/{id}/cancel           stop one being made (it fails; clips in flight are billed)
POST   /api/hook-variants/{id}/preview-audio     make its voice to listen to (paid TTS)
GET    /api/hook-variants/{id}/audio             its voiceover (?raw=true: the opening alone)
GET    /api/renders/{id}/opening-audio           the base voiceover's first `seconds`
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..services import content_mode, events, hook_lab, provider_gate, render_versions, spend, takes
from ..services.ratelimit import rate_limit
from ..services.spend import budget_guard, require_budget_for
from ..workers.queue import enqueue
from .auth import require_auth
from .generation import claim_project, release_project

router = APIRouter(dependencies=[Depends(require_auth)])


# ---------- inputs ----------


class SuggestIn(BaseModel):
    base_render_id: int
    n: int = Field(3, ge=1, le=hook_lab.MAX_PER_REQUEST)


class VariantSpec(BaseModel):
    hook_text: str = Field(min_length=1, max_length=300)
    style: str = "custom"
    shot1_source: str = "keep"
    shot1_take_id: Optional[int] = None
    shot1_prompt: str = Field("", max_length=2000)


class VariantsIn(BaseModel):
    base_render_id: int
    variants: list[VariantSpec] = Field(min_length=1, max_length=hook_lab.MAX_PER_REQUEST)
    window_seconds: Optional[float] = None


class VariantIdsIn(BaseModel):
    variant_ids: list[int] = Field(min_length=1, max_length=hook_lab.MAX_PER_BASE)


class VariantPatch(BaseModel):
    hook_text: Optional[str] = Field(None, min_length=1, max_length=300)
    style: Optional[str] = None
    shot1_source: Optional[str] = None
    shot1_take_id: Optional[int] = None
    shot1_prompt: Optional[str] = Field(None, max_length=2000)
    window_seconds: Optional[float] = None


# ---------- helpers ----------


def _err(e: hook_lab.HookLabError) -> HTTPException:
    return HTTPException(e.status, str(e))


def _project(db: Session, project_id: int) -> models.VideoProject:
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    if project.mode == "ambient_loop":
        raise HTTPException(409, "Other openings work on storyboard videos only.")
    return project


def _base(db: Session, project_id: int, render_id: int) -> models.Render:
    try:
        return hook_lab.check_base(db, db.get(models.Render, render_id), project_id)
    except hook_lab.HookLabError as e:
        raise _err(e) from e


def _variant(db: Session, variant_id: int) -> models.HookVariant:
    v = db.get(models.HookVariant, variant_id)
    if v is None or v.status == hook_lab.ARCHIVED:
        raise HTTPException(404, "opening not found")
    return v


def _audio_seconds(path: str) -> float:
    from ..services.renderer import _probe_duration

    return _probe_duration(Path(path)) if path and Path(path).is_file() else 0.0


def _check_window(db: Session, base: models.Render, window: float) -> float:
    lo, hi = hook_lab.window_limits(db, base)
    if not (lo <= window <= hi + 0.005):
        raise HTTPException(422, f"The opening must end between {lo:.1f} s and {hi:.1f} s.")
    return round(min(float(window), hi), 2)


def _check_spec(spec: VariantSpec | VariantPatch) -> None:
    if spec.style is not None and spec.style not in hook_lab.STYLES:
        raise HTTPException(422, f"style must be one of {', '.join(hook_lab.STYLES)}")
    if spec.shot1_source is not None and spec.shot1_source not in hook_lab.SOURCES:
        raise HTTPException(422, f"shot1_source must be one of {', '.join(hook_lab.SOURCES)}")


def _check_opening(db: Session, base: models.Render, hook: str) -> None:
    """The minors check, on the opening alone and inside the script it joins."""
    try:
        hook_lab.check_opening(db, base, hook)
    except hook_lab.HookLabError as e:
        raise _err(e) from e


def _publish_state(db: Session, render_ids: set[int]) -> dict[int, dict]:
    if not render_ids:
        return {}
    out: dict[int, dict] = {}
    for job in (db.query(models.PublishJob).filter(models.PublishJob.render_id.in_(render_ids))
                .order_by(models.PublishJob.id)):
        st = out.setdefault(job.render_id, {"published": False, "jobs": []})
        st["jobs"].append({"id": job.id, "status": job.status, "external_url": job.external_url or ""})
        if job.status != "failed":
            st["published"] = True
    return out


def _published(db: Session, render_id: Optional[int]) -> bool:
    if not render_id:
        return False
    return db.query(models.PublishJob.id).filter(models.PublishJob.render_id == render_id,
                                                 models.PublishJob.status != "failed").first() is not None


def variant_out(db: Session, v: models.HookVariant, *, base_hook: str = "",
                renders: Optional[dict[int, models.Render]] = None,
                labels: Optional[dict[int, dict]] = None,
                publish: Optional[dict[int, dict]] = None) -> dict:
    render = (renders or {}).get(v.render_id) if v.render_id else None
    if render is None and v.render_id:
        render = db.get(models.Render, v.render_id)
    if labels is None:
        labels = render_versions.describe(db, [render]) if render is not None else {}
    if publish is None:
        publish = _publish_state(db, {v.render_id} if v.render_id else set())
    done = render is not None and render.status == "completed"
    has_audio = bool(v.audio_path) and Path(v.audio_path).is_file()
    pub = publish.get(v.render_id or 0, {"published": False, "jobs": []})
    return {
        "id": v.id, "project_id": v.project_id, "base_render_id": v.base_render_id, "label": v.label,
        "style": v.style, "style_label": hook_lab.STYLE_LABELS.get(v.style, v.style),
        "hook_text": v.hook_text, "window_seconds": v.window_seconds,
        "shot1_source": v.shot1_source, "shot1_take_id": v.shot1_take_id, "shot1_prompt": v.shot1_prompt or "",
        "status": v.status, "error": v.error or "",
        "render_id": v.render_id,
        "render_status": render.status if render is not None else None,
        "version_label": (labels.get(render.id) or {}).get("version_label", "") if render is not None else "",
        "video_url": f"/api/renders/{render.id}/download" if done and render.final_video_path else None,
        "thumbnail_url": (f"/api/renders/{render.id}/thumbnail"
                          if done and render.thumbnail_path and Path(render.thumbnail_path).is_file() else None),
        "cost_estimate": round(float(v.cost_estimate or 0), 4),
        "actual_cost": round(float(render.actual_cost or 0), 4) if done else None,
        "audio_url": (f"/api/hook-variants/{v.id}/audio?v={(v.audio_fingerprint or 'x')[:12]}"
                      if has_audio else None),
        **hook_lab.fit_estimate(v.hook_text, base_hook, float(v.window_seconds or 0)),
        "published": pub["published"], "publish_jobs": pub["jobs"],
        "created_at": v.created_at.isoformat() if v.created_at else None,
        "updated_at": v.updated_at.isoformat() if v.updated_at else None,
    }


def _shot1(db: Session, project: models.VideoProject, clips: list) -> Optional[dict]:
    from ..services.pipeline import _lipsync_available

    if not clips:
        return None
    first = clips[0]
    shot = db.get(models.VideoShot, first.shot_id) if first.shot_id else None
    shot_takes = []
    if shot is not None:
        shot_takes = [takes.out(project.id, shot, t) for t in sorted(shot.takes, key=lambda t: t.id, reverse=True)
                      if t.clip_path and Path(t.clip_path).is_file()]
    talking = first.shot_type == "talking_head"
    return {
        "shot_id": first.shot_id, "shot_order": first.shot_order, "shot_type": first.shot_type,
        # The shot's planned length (a held last clip plays longer, but a new
        # shot 1 or a take only has to fill the planned slot).
        "seconds": first.planned_seconds or first.seconds, "take_id": first.take_id,
        "lipsynced": hook_lab._lipsynced(db, first),
        "prompt": shot.prompt if shot is not None else "",
        "clip_url": (f"/api/projects/{project.id}/shots/{shot.id}/takes/{first.take_id}/clip"
                     if shot is not None and first.take_id else None),
        "takes": shot_takes,
        "can_redo_talking_head": talking and _lipsync_available(db, project),
        "single_shot": len(clips) == 1,
    }


def lab_state(db: Session, project: models.VideoProject) -> dict:
    """Every finished version that can get other openings, with its
    openings, where its own opening ends, and shot 1's options."""
    R = models.Render
    bases = [r for r in (db.query(R).filter(R.project_id == project.id, R.status == "completed",
                                            R.hook_variant_id.is_(None))
                         .order_by(R.created_at.desc(), R.id.desc()))
             if r.audio_path and Path(r.audio_path).is_file()]
    variants = (db.query(models.HookVariant)
                .filter(models.HookVariant.project_id == project.id,
                        models.HookVariant.status != hook_lab.ARCHIVED)
                .order_by(models.HookVariant.id).all())
    for v in variants:
        hook_lab.sync(db, v)
    render_ids = {v.render_id for v in variants if v.render_id}
    renders = {r.id: r for r in db.query(R).filter(R.id.in_(render_ids))} if render_ids else {}
    labels = render_versions.describe(db, bases + list(renders.values()))
    publish = _publish_state(db, render_ids | {b.id for b in bases})
    out = []
    for b in bases:
        hook = hook_lab.base_hook(db, b)
        script = hook_lab.base_script(db, b)
        try:
            window: Optional[float] = hook_lab.find_hook_window(b, script)
        except hook_lab.HookLabError:
            window = None
        reuse_error = ""
        shot1 = None
        try:
            hook_lab.check_base(db, b, project.id)
            clips = hook_lab.base_clips(db, b)
            shot1 = _shot1(db, project, clips)
            hook_lab.base_settings(db, b)
        except hook_lab.HookLabError as e:
            reuse_error = str(e)
        mine = [v for v in variants if v.base_render_id == b.id]
        used_labels = {lbl for (lbl,) in db.query(models.HookVariant.label)
                       .filter(models.HookVariant.base_render_id == b.id)}
        room = max(0, min(hook_lab.MAX_PER_BASE - len(mine), len(hook_lab.LABELS) - len(used_labels)))
        comp = b.composition_json or {}
        info = labels.get(b.id) or {}
        pub = publish.get(b.id, {"published": False, "jobs": []})
        out.append({
            "render_id": b.id, "version": info.get("version"), "version_label": info.get("version_label", ""),
            "created_at": b.created_at.isoformat() if b.created_at else None,
            "video_url": f"/api/renders/{b.id}/download",
            "thumbnail_url": (f"/api/renders/{b.id}/thumbnail"
                              if b.thumbnail_path and Path(b.thumbnail_path).is_file() else None),
            "duration_seconds": comp.get("duration_seconds"),
            "audio_seconds": round(_audio_seconds(b.audio_path), 2),
            "hook_text": hook, "script": script,
            "window_seconds": window,
            "opening_audio_url": (f"/api/renders/{b.id}/opening-audio?seconds={window}" if window else None),
            "reusable": not reuse_error, "reuse_error": reuse_error,
            "room": room, "max_per_base": hook_lab.MAX_PER_BASE,
            "shot1": shot1,
            "published": pub["published"], "publish_jobs": pub["jobs"],
            "variants": [variant_out(db, v, base_hook=hook, renders=renders, labels=labels, publish=publish)
                         for v in mine],
        })
    return {
        "project_id": project.id,
        "bases": out,
        "styles": [{"key": k, "label": hook_lab.STYLE_LABELS[k]} for k in hook_lab.STYLES],
        "max_per_request": hook_lab.MAX_PER_REQUEST,
        "suggest_cost_estimate": 0.001,
        "tts_cost_per_1k_chars": spend_rate(),
        "youtube_note": hook_lab.YOUTUBE_NOTE,
    }


def spend_rate() -> float:
    from ..config import get_settings

    return get_settings().cost_tts_per_1k_chars


# ---------- routes ----------


@router.get("/projects/{project_id}/hook-lab")
def get_hook_lab(project_id: int, db: Session = Depends(get_db)) -> dict:
    return lab_state(db, _project(db, project_id))


@router.post("/projects/{project_id}/hook-lab/suggest",
             dependencies=[Depends(rate_limit("generation")), Depends(budget_guard)])
def suggest(project_id: int, body: SuggestIn, db: Session = Depends(get_db)) -> dict:
    project = _project(db, project_id)
    base = _base(db, project_id, body.base_render_id)
    live = db.query(models.HookVariant.id).filter(models.HookVariant.base_render_id == base.id,
                                                  models.HookVariant.status != hook_lab.ARCHIVED).count()
    used = db.query(models.HookVariant.id).filter(models.HookVariant.base_render_id == base.id).count()
    if live >= hook_lab.MAX_PER_BASE or used >= len(hook_lab.LABELS):
        raise HTTPException(409, f"This version already has as many other openings as it can "
                                 f"({hook_lab.MAX_PER_BASE}); remove one first.")
    provider_gate.require_model("chat", content_mode.chat_model())
    try:
        with spend.scope(project_id=project.id):
            ideas = hook_lab.suggest_hooks(db, project, base, body.n)
    except hook_lab.HookLabError as e:
        raise _err(e) from e
    hook = hook_lab.base_hook(db, base)
    try:
        window = hook_lab.find_hook_window(base)
    except hook_lab.HookLabError:
        window = None
    return {"base_render_id": base.id, "original": {"text": hook, "window_seconds": window},
            "suggestions": ideas, "cost_estimate": 0.001}


@router.post("/projects/{project_id}/hook-lab/variants", status_code=201)
def create_variants(project_id: int, body: VariantsIn, db: Session = Depends(get_db)) -> dict:
    """Drafts only: nothing is made or paid for. Returns them with their price."""
    project = _project(db, project_id)
    base = _base(db, project_id, body.base_render_id)
    for spec in body.variants:
        _check_spec(spec)
        _check_opening(db, base, " ".join(spec.hook_text.split()))
    try:
        hook_lab.base_clips(db, base)  # its clips are still there to reuse
    except hook_lab.HookLabError as e:
        raise _err(e) from e
    live = db.query(models.HookVariant.id).filter(models.HookVariant.base_render_id == base.id,
                                                  models.HookVariant.status != hook_lab.ARCHIVED).count()
    if live + len(body.variants) > hook_lab.MAX_PER_BASE:
        raise HTTPException(409, f"A version can have at most {hook_lab.MAX_PER_BASE} other openings.")
    if body.window_seconds is not None:
        window = _check_window(db, base, body.window_seconds)
    else:
        try:
            window = hook_lab.find_hook_window(base)
        except hook_lab.HookLabError as e:
            raise _err(e) from e
    made: list[models.HookVariant] = []
    try:
        for spec in body.variants:
            v = models.HookVariant(
                project_id=project.id, base_render_id=base.id, label=hook_lab.next_label(db, base.id),
                style=spec.style, hook_text=" ".join(spec.hook_text.split()), window_seconds=window,
                shot1_source=spec.shot1_source,
                shot1_take_id=spec.shot1_take_id if spec.shot1_source == "take" else None,
                shot1_prompt=spec.shot1_prompt.strip() if spec.shot1_source == "new" else "",
                status="draft", error="")
            db.add(v)
            db.flush()  # the next label sees this one
            made.append(v)
        plan = hook_lab.variant_plan(db, made)
        bad = [r for r in plan["variants"] if not r["ok"]]
        if bad:
            raise HTTPException(422, f"Opening {bad[0]['label']}: {bad[0]['error']}")
        for v, row in zip(made, plan["variants"], strict=True):
            v.cost_estimate = row["total"]
        db.commit()
    except hook_lab.HookLabError as e:
        db.rollback()
        raise _err(e) from e
    except HTTPException:
        db.rollback()
        raise
    hook = hook_lab.base_hook(db, base)
    return {"variants": [variant_out(db, v, base_hook=hook) for v in made], "plan": plan}


def _owned(db: Session, project_id: int, ids: list[int]) -> list[models.HookVariant]:
    ids = list(dict.fromkeys(ids))
    variants = (db.query(models.HookVariant)
                .filter(models.HookVariant.id.in_(ids), models.HookVariant.project_id == project_id,
                        models.HookVariant.status != hook_lab.ARCHIVED).all())
    if len(variants) != len(ids):
        raise HTTPException(404, "opening not found")
    return sorted(variants, key=lambda v: ids.index(v.id))


@router.post("/projects/{project_id}/hook-lab/plan")
def plan(project_id: int, body: VariantIdsIn, db: Session = Depends(get_db)) -> dict:
    _project(db, project_id)
    return hook_lab.variant_plan(db, _owned(db, project_id, body.variant_ids))


@router.post("/projects/{project_id}/hook-lab/make", status_code=202,
             dependencies=[Depends(rate_limit("generation")), Depends(budget_guard)])
def make(project_id: int, body: VariantIdsIn, db: Session = Depends(get_db)) -> dict:
    """Make the openings' audio and clips (paid) and compose each one. The
    estimate counts against the budget; the project and the openings are
    claimed atomically, so a double click never makes them twice."""
    from ..workers.jobs import make_hook_variants_job

    project = _project(db, project_id)
    variants = _owned(db, project_id, body.variant_ids)
    for v in variants:
        hook_lab.sync(db, v)
        if v.status not in hook_lab.EDITABLE:
            what = "is already being made" if v.status in hook_lab.RUNNING else "is already made"
            raise HTTPException(409, f"Opening {v.label} {what}.")
    priced = hook_lab.variant_plan(db, variants)
    bad = [r for r in priced["variants"] if not r["ok"]]
    if bad:
        raise HTTPException(422, f"Opening {bad[0]['label']}: {bad[0]['error']}")
    long = [r for r in priced["variants"] if r.get("too_long_seconds")]
    if long:
        raise HTTPException(422, f"Opening {long[0]['label']} is about {long[0]['too_long_seconds']:.1f} s "
                                 "longer than the original; shorten it, or move where the opening ends.")
    # Every provider the run will call must be on (refused before anything is paid).
    if any(r["tts_cost"] > 0 for r in priced["variants"]):
        provider_gate.require_model("tts")
    for v, r in zip(variants, priced["variants"], strict=True):
        if v.shot1_source == "new":
            first = (db.get(models.Render, v.base_render_id).composition_json or {}).get("shots") or [{}]
            shot = db.get(models.VideoShot, first[0].get("shot_id")) if first[0].get("shot_id") else None
            provider_gate.require_model("video", (shot.model_override if shot else "") or None, db)
        if r["shot1"] and r["shot1"]["lipsync"] or r["extra_lipsync"]:
            provider_gate.require_model("lipsync")
    require_budget_for(db, priced["total"])
    busy = "This video is busy right now (a render or other openings are being made). Try again when it is done."
    previous, token = claim_project(db, project, "generating", busy, hook_lab=True)
    ids = [v.id for v in variants]
    claimed = (db.query(models.HookVariant)
               .filter(models.HookVariant.id.in_(ids), models.HookVariant.status.in_(hook_lab.EDITABLE),
                       or_(models.HookVariant.voicing_until.is_(None),
                           models.HookVariant.voicing_until < datetime.utcnow()))
               .update({"status": "making", "error": "", "render_id": None}, synchronize_session=False))
    if claimed != len(ids):
        release_project(db, project.id, "generating", previous, token)
        raise HTTPException(409, "These openings are already being made, or one's voice is still being "
                                 "made for Listen; try again in a moment.")
    for v, r in zip(variants, priced["variants"], strict=True):
        db.query(models.HookVariant).filter(models.HookVariant.id == v.id).update(
            {"cost_estimate": r["total"]}, synchronize_session=False)
    db.commit()
    try:
        job_id = enqueue(make_hook_variants_job, project.id, ids, claim="generating", claim_token=token,
                         previous=previous)
    except Exception:
        db.query(models.HookVariant).filter(models.HookVariant.id.in_(ids),
                                            models.HookVariant.status == "making").update(
            {"status": "draft"}, synchronize_session=False)
        db.commit()
        release_project(db, project.id, "generating", previous, token)
        raise
    db.query(models.VideoProject).filter(
        models.VideoProject.id == project.id, models.VideoProject.status == "generating",
    ).update({"queue_job_id": str(job_id)[:80]}, synchronize_session=False)
    db.commit()
    return {"project_id": project.id, "job_id": job_id, "status": "enqueued", "variant_ids": ids,
            "plan": priced}


@router.get("/hook-variants/{variant_id}")
def get_variant(variant_id: int, db: Session = Depends(get_db)) -> dict:
    v = _variant(db, variant_id)
    hook_lab.sync(db, v)
    base = db.get(models.Render, v.base_render_id)
    return variant_out(db, v, base_hook=hook_lab.base_hook(db, base) if base else "")


@router.patch("/hook-variants/{variant_id}")
def patch_variant(variant_id: int, body: VariantPatch, db: Session = Depends(get_db)) -> dict:
    v = _variant(db, variant_id)
    if v.status not in hook_lab.EDITABLE:
        raise HTTPException(409, "Only an opening that isn't made yet (or failed) can be changed.")
    _check_spec(body)
    base = _base(db, v.project_id, v.base_render_id)
    fields = body.model_fields_set
    audio_changed = False
    if "hook_text" in fields and body.hook_text is not None:
        text = " ".join(body.hook_text.split())
        _check_opening(db, base, text)
        audio_changed |= text != v.hook_text
        v.hook_text = text
    if "style" in fields and body.style is not None:
        audio_changed |= body.style != v.style and "original" in (body.style, v.style)
        v.style = body.style
    if "window_seconds" in fields and body.window_seconds is not None:
        w = _check_window(db, base, body.window_seconds)
        audio_changed |= w != v.window_seconds
        v.window_seconds = w
    if "shot1_source" in fields and body.shot1_source is not None:
        v.shot1_source = body.shot1_source
    if "shot1_take_id" in fields:
        v.shot1_take_id = body.shot1_take_id
    if "shot1_prompt" in fields and body.shot1_prompt is not None:
        v.shot1_prompt = body.shot1_prompt.strip()
    if v.shot1_source != "take":
        v.shot1_take_id = None
    if v.shot1_source != "new":
        v.shot1_prompt = ""
    if audio_changed:
        v.audio_path, v.audio_fingerprint = "", ""
    if v.status == "failed":
        v.status, v.error = "draft", ""
    try:
        row = hook_lab.variant_plan(db, [v])["variants"][0]
    except hook_lab.HookLabError as e:
        db.rollback()
        raise _err(e) from e
    if not row["ok"]:
        db.rollback()
        raise HTTPException(422, row["error"])
    v.cost_estimate = row["total"]
    db.commit()
    return {**variant_out(db, v, base_hook=hook_lab.base_hook(db, base)), "plan": row}


@router.post("/hook-variants/{variant_id}/cancel")
def cancel_variant(variant_id: int, db: Session = Depends(get_db)) -> dict:
    """Stop an opening that is waiting for its clips: it fails ("Cancelled")
    and can be changed and made again. Clips already at a provider finish
    and are billed, but are not used."""
    v = _variant(db, variant_id)
    hook_lab.sync(db, v)
    try:
        hook_lab.cancel(db, v)
    except hook_lab.HookLabError as e:
        raise _err(e) from e
    base = db.get(models.Render, v.base_render_id)
    return variant_out(db, v, base_hook=hook_lab.base_hook(db, base) if base else "")


@router.delete("/hook-variants/{variant_id}")
def delete_variant(variant_id: int, delete_render: bool = False, db: Session = Depends(get_db)) -> dict:
    """A draft or failed opening goes. A finished one keeps its video unless
    `delete_render` (refused while it is published): it only leaves the
    hook lab (archived), so its video and numbers stay in history/Results."""
    from ..services import render_files

    v = _variant(db, variant_id)
    if v.status in hook_lab.RUNNING:
        raise HTTPException(409, "This opening is being made; wait until it is done.")
    render = db.get(models.Render, v.render_id) if v.render_id else None
    project = db.get(models.VideoProject, v.project_id)
    if v.status == "completed" and render is not None and render.status == "completed" and not delete_render:
        v.status = hook_lab.ARCHIVED
        db.commit()
        return {"id": variant_id, "deleted": True, "render_kept": True, "render_id": render.id}
    if render is not None and _published(db, render.id):
        raise HTTPException(409, "This opening's video is on YouTube, so it is kept.")
    files = [v.audio_path] if v.audio_path and v.audio_path != (
        db.get(models.Render, v.base_render_id).audio_path if v.base_render_id else "") else []
    # Its renders from earlier tries (failed or cancelled) go too: once the
    # opening is gone they would read as ordinary storyboard renders. One
    # that finished or is on YouTube stays, and so does the opening
    # (archived), so that render keeps saying it is an opening.
    earlier = (db.query(models.Render)
               .filter(models.Render.hook_variant_id == v.id,
                       models.Render.id != (render.id if render is not None else -1))
               .order_by(models.Render.id).all())
    kept = [r for r in earlier if r.status == "completed" or _published(db, r.id)]
    for r in [r for r in earlier if r not in kept] + ([render] if render is not None else []):
        files += [r.final_video_path, r.thumbnail_path]
        if r.final_video_path:  # its own copies (pipeline.keep_music, keep_voice)
            from ..services.pipeline import own_copies

            files += [str(p) for p in own_copies(Path(r.final_video_path).parent, r.id)]
        (db.query(models.HookVariant).filter(models.HookVariant.render_id == r.id)
         .update({"render_id": None}, synchronize_session=False))
        # Only failed uploads are left (a published one refused above).
        db.query(models.PublishJob).filter(models.PublishJob.render_id == r.id).delete(
            synchronize_session=False)
        db.delete(r)
    if kept:
        v.status = hook_lab.ARCHIVED
        v.render_id = kept[-1].id
        files = [f for f in files if f != v.audio_path]  # a kept render may play it
    else:
        db.delete(v)
    events.emit(db, "hook_lab", "hook_variant", variant_id, "cancelled",
                f"Opening {v.label} removed: {events._name(project)}", url=events.hook_lab_url(project),
                project_id=project.id)
    db.commit()
    for f in files:
        if f:
            Path(f).unlink(missing_ok=True)
    render_files.prune(db, project.id)
    return {"id": variant_id, "deleted": True, "render_kept": bool(kept), "render_id": kept[-1].id if kept else None}


VOICING_FOR = timedelta(minutes=3)  # longer than any TTS call; a crashed Listen frees it then


@router.post("/hook-variants/{variant_id}/preview-audio",
             dependencies=[Depends(rate_limit("generation")), Depends(budget_guard)])
def preview_audio(variant_id: int, db: Session = Depends(get_db)) -> dict:
    """Make the opening's voice (paid TTS, ~$0.01) and the whole voiceover
    with it, to listen to before making the video. `fits` false: the
    opening is too long for the original's time (`overflow_seconds`); its
    voice alone is at `url` so it can still be heard."""
    v = _variant(db, variant_id)
    if v.status not in hook_lab.EDITABLE:
        raise HTTPException(409, "This opening is being made or is already made.")
    base = _base(db, v.project_id, v.base_render_id)
    project = db.get(models.VideoProject, v.project_id)
    _check_opening(db, base, v.hook_text)  # before any voice is made
    cost = 0.0
    if not hook_lab.is_original(db, v, base) and not hook_lab.raw_hook_path(db, project, v).is_file():
        cost = round(hook_lab.tts_cost(v.hook_text), 4)
        provider_gate.require_model("tts")
        require_budget_for(db, cost)
    now = datetime.utcnow()
    held = (db.query(models.HookVariant)
            .filter(models.HookVariant.id == v.id, models.HookVariant.status.in_(hook_lab.EDITABLE),
                    or_(models.HookVariant.voicing_until.is_(None), models.HookVariant.voicing_until < now))
            .update({"voicing_until": now + VOICING_FOR}, synchronize_session=False))
    db.commit()
    if held != 1:
        raise HTTPException(409, "This opening's voice is already being made, or the opening is being made.")
    db.refresh(v)
    try:
        with spend.scope(project_id=project.id):
            made = hook_lab.make_variant_audio(db, v)
    except hook_lab.HookTooLong as e:
        db.rollback()
        return {"url": f"/api/hook-variants/{v.id}/audio?raw=true", "fits": False,
                "overflow_seconds": e.overflow, "message": str(e), "cost": cost,
                "window_seconds": v.window_seconds}
    except hook_lab.HookLabError as e:
        db.rollback()
        raise _err(e) from e
    finally:
        db.query(models.HookVariant).filter(models.HookVariant.id == v.id).update(
            {"voicing_until": None}, synchronize_session=False)
        db.commit()
    db.refresh(v)
    return {"url": f"/api/hook-variants/{v.id}/audio?v={(v.audio_fingerprint or 'x')[:12]}", "fits": True,
            "overflow_seconds": 0.0, "message": "", "cost": made["cost"], "reused": made["reused"],
            "hook_seconds": made.get("hook_seconds"), "tempo": made.get("tempo"),
            "window_seconds": v.window_seconds, "duration_seconds": round(_audio_seconds(made["path"]), 2)}


@router.get("/hook-variants/{variant_id}/audio")
def variant_audio(variant_id: int, raw: bool = False, db: Session = Depends(get_db)) -> FileResponse:
    v = _variant(db, variant_id)
    if raw:
        project = db.get(models.VideoProject, v.project_id)
        path = hook_lab.raw_hook_path(db, project, v)
    else:
        path = Path(v.audio_path or "")
    if not path.is_file():
        raise HTTPException(404, "no audio yet")
    return FileResponse(path, media_type="audio/mp4")


@router.get("/renders/{render_id}/opening-audio")
def opening_audio(render_id: int, seconds: float = Query(..., gt=0, le=120),
                  db: Session = Depends(get_db)) -> FileResponse:
    """The first `seconds` of a version's voiceover (to hear where its
    opening ends while moving the window), plus half a second."""
    from ..services import storage
    from ..services.renderer import slice_audio

    r = db.get(models.Render, render_id)
    if r is None or not r.audio_path or not Path(r.audio_path).is_file():
        raise HTTPException(404, "no voiceover for this version")
    import hashlib

    ms = int(round(seconds * 1000))
    src = Path(r.audio_path)
    tag = hashlib.sha1(f"{src}:{src.stat().st_mtime_ns}".encode()).hexdigest()[:10]
    dst = storage.render_subdir(r.project_id) / f"opening_{r.id}_{tag}_{ms}.m4a"
    if not dst.is_file():
        slice_audio(Path(r.audio_path), dst, start=0.0, duration=seconds + 0.5)
    return FileResponse(dst, media_type="audio/mp4")

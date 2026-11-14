from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..schemas import (
    AssetOut,
    CastMemberIn,
    PlanEditIn,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
    ShotOut,
    ShotUpdate,
)
from ..schemas.api import (
    ShotCreate,
    ShotEditImpactOut,
    ShotEditOut,
    TakeFromStudioIn,
    TakeFromStudioOut,
    TakeOut,
    TakesOut,
)
from ..schemas.storyboard import StoryboardPlan
from ..services import takes as takes_svc
from ..services.captions import chunk_script
from .auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])


def _apply_cast(db: Session, project: models.VideoProject, cast: list[CastMemberIn]) -> None:
    for cm in list(project.cast_members):
        db.delete(cm)
    db.flush()
    for c in cast:
        if c.member_kind == "avatar" and c.avatar_id:
            db.add(
                models.ProjectCastMember(
                    project_id=project.id,
                    member_kind="avatar",
                    avatar_id=c.avatar_id,
                    role=c.role or "host",
                )
            )
        elif c.member_kind == "ingredient" and c.ingredient_id:
            db.add(
                models.ProjectCastMember(
                    project_id=project.id,
                    member_kind="ingredient",
                    ingredient_id=c.ingredient_id,
                    role=c.role or "scene",
                )
            )


def _cast_key(cast) -> list[tuple]:
    out = []
    for c in cast:
        if c.member_kind == "avatar" and c.avatar_id:
            out.append(("avatar", c.avatar_id, c.role or "host"))
        elif c.member_kind == "ingredient" and c.ingredient_id:
            out.append(("ingredient", c.ingredient_id, c.role or "scene"))
    return sorted(out)


@router.get("/projects", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db)) -> list[models.VideoProject]:
    from sqlalchemy import func

    projects = db.query(models.VideoProject).order_by(models.VideoProject.created_at.desc()).all()
    # One grouped query for every card's thumbnail, instead of one per project.
    latest = dict(
        db.query(models.Render.project_id, func.max(models.Render.id))
        .filter(models.Render.status == "completed", models.Render.thumbnail_path != "")
        .group_by(models.Render.project_id)
        .all()
    )
    for p in projects:
        p.latest_render_id = latest.get(p.id)
    return projects


@router.post("/projects", response_model=ProjectOut, status_code=201)
def create_project(body: ProjectCreate, db: Session = Depends(get_db)) -> models.VideoProject:
    primary_id = body.primary_avatar_id
    if primary_id is None:
        for c in body.cast:
            if c.member_kind == "avatar" and c.avatar_id:
                primary_id = c.avatar_id
                break
    p = models.VideoProject(
        title=body.title or "Untitled Project",
        original_script=body.original_script,
        mode=body.mode,
        aspect_ratio=body.aspect_ratio,
        target_duration_seconds=body.target_duration_seconds,
        cta_text=body.cta_text,
        caption_style=body.caption_style,
        include_disclosure=body.include_disclosure,
        disclosure_text=body.disclosure_text,
        creative_direction=body.creative_direction,
        voiceover_source=body.voiceover_source,
        music_volume=body.music_volume,
        primary_avatar_id=primary_id,
        brand_kit_id=body.brand_kit_id,
        status="draft",
    )
    db.add(p)
    db.flush()
    _apply_cast(db, p, body.cast)
    db.commit()
    db.refresh(p)
    return p


@router.get("/projects/{project_id}", response_model=ProjectOut)
def get_project(project_id: int, db: Session = Depends(get_db)) -> models.VideoProject:
    p = db.get(models.VideoProject, project_id)
    if p is None:
        raise HTTPException(404, "project not found")
    return p


def refuse_while_busy(db: Session, project: models.VideoProject, what: str) -> None:
    """409 while a plan, render or re-roll runs: `what` would change clips
    or timing under it (a render could hang, lose a file, or pay for clips
    that are out of date before they land)."""
    from ..services.pipeline import busy_reason

    why = busy_reason(db, project)
    if why:
        raise HTTPException(409, f"{what} can't be changed while the video is being made. {why}")


# Settings a clip or the voiceover is made from: changing one mid-render
# would make the clips in flight out of date before they land (paid twice).
_CLIP_FIELDS = {
    "aspect_ratio": "The aspect ratio",
    "mode": "The mode",
    "voice_id": "The voice",
    "voiceover_source": "The voiceover",
    "primary_avatar_id": "The main character",
    "generated_plan_json": "The storyboard",
}


def _refuse_clip_changes_while_busy(db: Session, project: models.VideoProject, body: ProjectUpdate) -> None:
    """Title, captions, disclosure, end card, brand kit, music volume: fine
    at any time. Fields clips are made from: only when nothing is running."""
    for field, label in _CLIP_FIELDS.items():
        if field not in body.model_fields_set:
            continue
        new = getattr(body, field)
        if field == "voice_id":
            new = (new or "").strip() or None
        elif new is None:
            continue  # not sent (None means "leave as is" for these fields)
        if new != getattr(project, field):
            refuse_while_busy(db, project, label)


def _adopt_old_voiceover(db: Session, project: models.VideoProject) -> None:
    """Before the script or voice changes: a voiceover from before
    fingerprints existed is known to match what it is changed *from*, so it
    shows as out of date afterwards (and as current until then)."""
    from ..services.pipeline import adopt_unfingerprinted_voiceover

    adopt_unfingerprinted_voiceover(db, project)


def _check_shown_words(project: models.VideoProject, body: ProjectUpdate) -> None:
    """A clean deployment: the end card, the disclosure and a whole posted
    storyboard (its script, opening, end card and captions) are shown in the
    video or voiced, so a changed one gets the explicit-content check (a 422)."""
    from ..services import explicit_check

    if not explicit_check.active():
        return
    texts: list[str] = []
    for field in ("cta_text", "disclosure_text"):
        new = getattr(body, field)
        if new is not None and new != getattr(project, field):
            texts.append(new)
    plan = body.generated_plan_json
    if plan is not None and plan != project.generated_plan_json:
        texts.extend(explicit_check.plan_texts(plan))
        if isinstance(plan.get("disclosure_text"), str):
            texts.append(plan["disclosure_text"])
    if texts:
        explicit_check.check_or_422(*texts)


@router.patch("/projects/{project_id}", response_model=ProjectOut)
def update_project(
    project_id: int, body: ProjectUpdate, db: Session = Depends(get_db)
) -> models.VideoProject:
    p = db.get(models.VideoProject, project_id)
    if p is None:
        raise HTTPException(404, "project not found")
    _refuse_clip_changes_while_busy(db, p, body)
    _check_shown_words(p, body)
    if body.voiceover_source == "upload" != p.voiceover_source:
        from ..services import content_mode

        if not content_mode.own_audio_allowed():
            raise HTTPException(422, content_mode.VOICEOVER_UPLOAD_OFF)
    _adopt_old_voiceover(db, p)
    for k, v in body.model_dump(exclude_none=True).items():
        if k != "voice_id":
            setattr(p, k, v)
    if "voice_id" in body.model_fields_set:  # null / "" = back to the character's voice
        p.voice_id = (body.voice_id or "").strip() or None
    db.commit()
    db.refresh(p)
    return p


@router.patch("/projects/{project_id}/cast", response_model=ProjectOut)
def update_cast(
    project_id: int, cast: list[CastMemberIn], db: Session = Depends(get_db)
) -> models.VideoProject:
    p = db.get(models.VideoProject, project_id)
    if p is None:
        raise HTTPException(404, "project not found")
    if _cast_key(p.cast_members) != _cast_key(cast):
        # Shots without their own references are made from the cast's pictures.
        refuse_while_busy(db, p, "The cast")
    _apply_cast(db, p, cast)
    # The main character must be in the cast: a removed one would keep
    # lending its face and voice to talking-head shots.
    avatars = [c.avatar_id for c in cast if c.member_kind == "avatar" and c.avatar_id]
    if p.primary_avatar_id not in avatars:
        p.primary_avatar_id = avatars[0] if avatars else None
    db.commit()
    db.refresh(p)
    return p


@router.patch("/projects/{project_id}/shots/{shot_id}", response_model=ShotOut)
def update_shot(
    project_id: int, shot_id: int, body: ShotUpdate, db: Session = Depends(get_db)
) -> models.VideoShot:
    s = db.get(models.VideoShot, shot_id)
    if s is None or s.project_id != project_id:
        raise HTTPException(404, "shot not found in project")
    changes = body.model_dump(exclude_none=True)
    if "prompt" in changes or "negative_prompt" in changes:
        # The safety check now (a 422), not when the clip is sent.
        from ..services.minor_check import check_or_422
        from ..services.prompting import effective_negative

        prompt = changes.get("prompt", s.prompt) or ""
        negative = changes.get("negative_prompt", s.negative_prompt) or ""
        check_or_422(prompt, effective_negative("video", negative), kind="video")
    if changes.get("caption_text") and changes["caption_text"] != s.caption_text:
        # A clean deployment: the caption is shown and may be lip-synced.
        from ..services import explicit_check

        explicit_check.check_or_422(changes["caption_text"])
    if "duration_seconds" in changes and float(changes["duration_seconds"]) != float(s.duration_seconds or 0):
        # A new length moves every later shot's slice of the voiceover.
        refuse_while_busy(db, s.project, "Shot lengths")
    for k, v in changes.items():
        setattr(s, k, v)
    db.commit()
    db.refresh(s)
    return s


@router.patch("/projects/{project_id}/plan", response_model=ProjectOut)
def edit_plan(
    project_id: int, body: PlanEditIn, db: Session = Depends(get_db)
) -> models.VideoProject:
    """Edit the generated storyboard's spoken voiceover script, end-card text, or
    disclosure — the values the renderer actually uses for TTS/captions/end card —
    without re-running the planner. Optionally resync captions to the new script."""
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    if not project.generated_plan_json:
        raise HTTPException(409, "generate a storyboard plan first")

    plan = StoryboardPlan.model_validate(project.generated_plan_json)
    script = _edited_script(plan, body)
    if script is not None and script != plan.cleaned_voice_script:
        from ..services.minor_check import check_or_422

        check_or_422(script, kind="script")  # it is voiced and lip-synced
        from ..services import explicit_check

        explicit_check.check_or_422(script)  # a clean deployment: no explicit voiceover
        refuse_while_busy(db, project, "The spoken script")  # the voiceover is made from it
        _adopt_old_voiceover(db, project)
    if script is not None:
        from ..services.hook_lab import first_sentence

        plan.cleaned_voice_script = script
        plan.hook = first_sentence(script)
    if body.end_card_text is not None:
        if body.end_card_text != plan.end_card_text:
            from ..services import explicit_check

            explicit_check.check_or_422(body.end_card_text)  # a clean deployment: shown on screen
        plan.end_card_text = body.end_card_text
        project.cta_text = body.end_card_text  # keep the project's end card in sync
    if body.disclosure_text is not None:
        if body.disclosure_text != plan.disclosure_text:
            from ..services import explicit_check

            explicit_check.check_or_422(body.disclosure_text)  # a clean deployment: shown on screen
        plan.disclosure_text = body.disclosure_text
    project.generated_plan_json = plan.model_dump()
    if body.resync_captions:
        # The shots as they are now (added, deleted, resized), not the
        # planner's original list in the plan JSON.
        _resync_captions(project)
    db.commit()
    db.refresh(project)
    return project


def _edited_script(plan: StoryboardPlan, body: PlanEditIn) -> Optional[str]:
    """The spoken script after this edit (None: unchanged). A hook that
    differs from the stored one and from the script's opening line
    replaces that line; otherwise the script is taken as sent."""
    from ..services.hook_lab import first_sentence, replace_first_sentence

    script = body.cleaned_voice_script
    hook = (body.hook or "").strip() if body.hook is not None else None
    base = script if script is not None else plan.cleaned_voice_script
    if hook and hook != plan.hook and hook != first_sentence(base or ""):
        return replace_first_sentence(base or "", hook)
    return script


class ReorderIn(BaseModel):
    shot_ids: list[int]


@router.post("/projects/{project_id}/shots/reorder", response_model=ProjectOut)
def reorder_shots(
    project_id: int, body: ReorderIn, db: Session = Depends(get_db)
) -> models.VideoProject:
    """Set shot_order from the given ordering of shot ids. Ids not belonging to
    the project are ignored; omitted shots keep a stable order after the listed ones."""
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    refuse_while_busy(db, project, "The shot order")
    by_id = {s.id: s for s in project.shots}
    order = 1
    for sid in body.shot_ids:
        shot = by_id.pop(sid, None)
        if shot is not None:
            shot.shot_order = order
            order += 1
    # Any shots not mentioned keep their relative order at the end.
    for shot in sorted(by_id.values(), key=lambda s: s.shot_order):
        shot.shot_order = order
        order += 1
    db.commit()
    db.refresh(project)
    return project


@router.get("/projects/{project_id}/cast-assets", response_model=list[AssetOut])
def cast_assets(project_id: int, db: Session = Depends(get_db)) -> list[models.Asset]:
    """Flat list of every asset owned by any cast member of this project,
    used to populate per-shot reference pickers in the UI."""
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    out: list[models.Asset] = []
    for cm in project.cast_members:
        if cm.member_kind == "avatar" and cm.avatar is not None:
            out.extend(cm.avatar.assets)
        elif cm.member_kind == "ingredient" and cm.ingredient is not None:
            out.extend(cm.ingredient.assets)
    return out


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: int, db: Session = Depends(get_db)) -> None:
    import shutil

    from ..services import storage

    p = db.get(models.VideoProject, project_id)
    if p is None:
        raise HTTPException(404, "project not found")
    # Rows that reference the project (or its renders) without an ORM
    # cascade. SQLite never enforced these FKs, so deletes left orphans;
    # Postgres refuses the delete outright.
    render_ids = [r.id for r in p.renders]
    if render_ids:
        db.query(models.PublishJob).filter(models.PublishJob.render_id.in_(render_ids)).delete(
            synchronize_session=False)
    db.query(models.VideoJob).filter(models.VideoJob.project_id == project_id).delete(
        synchronize_session=False)
    # Hook-lab variants point at the renders and the renders back at them.
    db.query(models.Render).filter(models.Render.project_id == project_id).update(
        {"hook_variant_id": None}, synchronize_session=False)
    db.query(models.HookVariant).filter(models.HookVariant.project_id == project_id).delete(
        synchronize_session=False)
    # Provider logs are an audit trail: keep them, detach from the project.
    db.query(models.ProviderLog).filter(models.ProviderLog.project_id == project_id).update(
        {"project_id": None}, synchronize_session=False)
    db.delete(p)
    db.commit()
    # Drop the project's render directory (final MP4s, audio, per-shot clips,
    # uploaded voiceover/music). The DB row is gone; the files would otherwise
    # accumulate forever on disk.
    render_dir = storage.render_subdir(project_id)
    try:
        shutil.rmtree(render_dir, ignore_errors=True)
    except OSError:
        pass


@router.get("/projects/{project_id}/publish-meta")
def publish_meta(project_id: int, render_id: Optional[int] = None, db: Session = Depends(get_db)) -> dict:
    """Draft YouTube title / description / tags / flags for this project,
    or (`render_id`) for one of its renders: that render's own script and
    opening (a hook-lab variant's hook)."""
    from ..services import publish_meta as pm

    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    render = None
    if render_id is not None:
        render = db.get(models.Render, render_id)
        if render is None or render.project_id != project.id:
            raise HTTPException(404, "render not found in project")
    return pm.draft(project, render)


# ---------- add / duplicate / delete a shot ----------


def _project_with_plan(db: Session, project_id: int) -> models.VideoProject:
    project = db.get(models.VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    if not project.generated_plan_json:
        raise HTTPException(409, "generate a storyboard plan first")
    return project


def _shot_in(project: models.VideoProject, shot_id: int) -> models.VideoShot:
    for s in project.shots:
        if s.id == shot_id:
            return s
    raise HTTPException(404, "shot not found in project")


def _place(project: models.VideoProject, new: models.VideoShot, after: Optional[models.VideoShot]) -> None:
    """Number the shots 1..n with `new` right after `after`, or at the end
    of the body shots (an end card stays last)."""
    ordered = sorted((s for s in project.shots if s is not new), key=lambda s: (s.shot_order, s.id))
    if after is not None and after.shot_type != "end_card":
        idx = ordered.index(after) + 1
    else:
        idx = len(ordered)
        while idx > 0 and ordered[idx - 1].shot_type == "end_card":
            idx -= 1
    ordered.insert(idx, new)
    for i, s in enumerate(ordered, start=1):
        s.shot_order = i


def _renumber(project: models.VideoProject) -> None:
    for i, s in enumerate(sorted(project.shots, key=lambda s: (s.shot_order, s.id)), start=1):
        s.shot_order = i


def _resync_captions(project: models.VideoProject) -> bool:
    """Spread the captions over the shots' new total time (what "Save +
    resync captions" does), from the spoken script."""
    plan = StoryboardPlan.model_validate(project.generated_plan_json)
    total = sum(float(s.duration_seconds or 0) for s in project.shots
                if s.shot_type not in ("end_card", "ambient_segment"))
    plan.caption_chunks = chunk_script(plan.cleaned_voice_script, max(total, 1.0))
    project.generated_plan_json = plan.model_dump()
    return True


def _edit_out(db: Session, project: models.VideoProject, shot: Optional[models.VideoShot],
              resynced: bool, discarded: int = 0) -> ShotEditOut:
    db.refresh(project)
    return ShotEditOut(project=ProjectOut.model_validate(project),
                       shot=ShotOut.model_validate(shot) if shot is not None else None,
                       captions_resynced=resynced, clips_discarded=discarded)


@router.get("/projects/{project_id}/shots/edit-impact", response_model=ShotEditImpactOut)
def shot_edit_impact(project_id: int, op: Literal["add", "duplicate", "delete"],
                     shot_id: Optional[int] = None, after_shot_id: Optional[int] = None,
                     duration_seconds: float = Query(5.0, ge=1.0, le=60.0),
                     shot_type: str = "b_roll", db: Session = Depends(get_db)) -> dict:
    """Before adding, duplicating or deleting a shot: which lip-synced clips
    it would put out of date (their slice of the voiceover moves) and what
    the next Generate video would pay to make them again. Nothing changes."""
    from ..services.pipeline import edit_impact

    project = _project_with_plan(db, project_id)
    if op in ("duplicate", "delete"):
        if shot_id is None:
            raise HTTPException(422, "shot_id is required")
        _shot_in(project, shot_id)
    if after_shot_id is not None:
        _shot_in(project, after_shot_id)
    return edit_impact(db, project, op=op, shot_id=shot_id, after_shot_id=after_shot_id,
                       duration=duration_seconds, shot_type=shot_type)


@router.post("/projects/{project_id}/shots", response_model=ShotEditOut, status_code=201)
def add_shot(project_id: int, body: ShotCreate, db: Session = Depends(get_db)) -> ShotEditOut:
    """A new shot with no clip (nothing is generated or paid for here)."""
    project = _project_with_plan(db, project_id)
    refuse_while_busy(db, project, "Shots")
    after = _shot_in(project, body.after_shot_id) if body.after_shot_id is not None else None
    if (body.prompt or "").strip() and body.shot_type != "end_card":
        from ..services.minor_check import check_or_422
        from ..services.prompting import effective_negative

        # The same early check as editing a shot: a refusal now, not after
        # the voiceover and other clips of the next render are paid for.
        check_or_422(body.prompt, effective_negative("video", ""), kind="video")
    shot = models.VideoShot(
        project_id=project.id, shot_type=body.shot_type, prompt=body.prompt,
        duration_seconds=float(body.duration_seconds), reference_asset_ids_json=[], status="pending",
    )
    project.shots.append(shot)
    _place(project, shot, after)
    resynced = _resync_captions(project)
    db.commit()
    db.refresh(shot)
    return _edit_out(db, project, shot, resynced)


@router.post("/projects/{project_id}/shots/{shot_id}/duplicate", response_model=ShotEditOut, status_code=201)
def duplicate_shot(project_id: int, shot_id: int, db: Session = Depends(get_db)) -> ShotEditOut:
    """A copy of the shot's settings, right after it, with no clip."""
    project = _project_with_plan(db, project_id)
    refuse_while_busy(db, project, "Shots")
    src = _shot_in(project, shot_id)
    if src.shot_type == "end_card":
        raise HTTPException(409, "The end card can't be duplicated: a video has one end card.")
    shot = models.VideoShot(
        project_id=project.id, shot_type=src.shot_type, prompt=src.prompt,
        negative_prompt=src.negative_prompt, duration_seconds=src.duration_seconds,
        reference_asset_ids_json=list(src.reference_asset_ids_json or []),
        reference_strategy=src.reference_strategy, caption_text=src.caption_text,
        camera_direction=src.camera_direction, model_override=src.model_override, status="pending",
    )
    project.shots.append(shot)
    _place(project, shot, src)
    resynced = _resync_captions(project)
    db.commit()
    db.refresh(shot)
    return _edit_out(db, project, shot, resynced)


@router.delete("/projects/{project_id}/shots/{shot_id}", response_model=ShotEditOut)
def delete_shot(project_id: int, shot_id: int, db: Session = Depends(get_db)) -> ShotEditOut:
    """Remove a shot and its kept clips. Refused (409) while its clip is
    still being made, for the last body shot, and for the end card while
    the project has end-card text (the end card is drawn from that text)."""
    from ..services.pipeline import _active_job_for

    project = _project_with_plan(db, project_id)
    shot = _shot_in(project, shot_id)
    if _active_job_for(db, shot.id) is not None:
        raise HTTPException(409, f"Shot {shot.shot_order}'s clip is still being made (and is already paid "
                                 "for). Wait until it is back, then delete the shot.")
    refuse_while_busy(db, project, "Shots")
    if shot.shot_type == "end_card":
        if (project.cta_text or "").strip():
            raise HTTPException(409, "The end card shows your end-card text. Clear the end-card text in "
                                     "Project settings to drop it; deleting this row alone would not.")
    else:
        body = [s for s in project.shots if s.shot_type not in ("end_card", "ambient_segment")]
        if len(body) <= 1:
            raise HTTPException(409, "A video needs at least one shot. Add another shot before deleting this one.")
    paths = {t.clip_path for t in shot.takes if t.clip_path}
    if shot.clip_path:
        paths.add(shot.clip_path)
    project.shots.remove(shot)  # its takes go with it (cascade)
    db.flush()
    discarded = sum(1 for p in paths if takes_svc.remove_file(db, p, project_id=project.id))
    _renumber(project)
    resynced = _resync_captions(project) if shot.shot_type != "end_card" else False
    db.commit()
    return _edit_out(db, project, None, resynced, discarded)


# ---------- takes: earlier clips of a shot ----------


def _shot_of(db: Session, project_id: int, shot_id: int) -> models.VideoShot:
    shot = db.get(models.VideoShot, shot_id)
    if shot is None or shot.project_id != project_id:
        raise HTTPException(404, "shot not found in project")
    return shot


def _take_of(shot: models.VideoShot, take_id: int) -> models.ShotTake:
    for t in shot.takes:
        if t.id == take_id:
            return t
    raise HTTPException(404, "take not found for this shot")


@router.get("/projects/{project_id}/shots/{shot_id}/takes", response_model=TakesOut)
def list_takes(project_id: int, shot_id: int, db: Session = Depends(get_db)) -> TakesOut:
    """Every kept clip of the shot, newest first."""
    shot = _shot_of(db, project_id, shot_id)
    takes_svc.ensure_current_take(db, shot)  # a clip from before takes existed is take 1
    rows = sorted(shot.takes, key=lambda t: t.id, reverse=True)
    return TakesOut(takes=[TakeOut(**takes_svc.out(project_id, shot, t)) for t in rows], keep=takes_svc.KEEP)


@router.get("/projects/{project_id}/shots/{shot_id}/takes/{take_id}/clip")
def take_clip(project_id: int, shot_id: int, take_id: int, db: Session = Depends(get_db)) -> FileResponse:
    shot = _shot_of(db, project_id, shot_id)
    take = _take_of(shot, take_id)
    if not take.clip_path or not Path(take.clip_path).is_file():
        raise HTTPException(404, "this take's file is gone")
    return FileResponse(take.clip_path, media_type="video/mp4")


@router.post("/projects/{project_id}/shots/{shot_id}/takes/{take_id}/use", response_model=ShotOut)
def use_take(project_id: int, shot_id: int, take_id: int, db: Session = Depends(get_db)) -> models.VideoShot:
    """Make this take the shot's clip. Nothing is generated or paid for;
    Re-compose (free) puts the video together with it."""
    from ..services.pipeline import _active_job_for

    shot = _shot_of(db, project_id, shot_id)
    take = _take_of(shot, take_id)
    if _active_job_for(db, shot.id) is not None:
        raise HTTPException(409, "A new clip for this shot is still being made. Wait until it is back, "
                                 "then pick the take you want.")
    if not take.clip_path or not Path(take.clip_path).is_file():
        raise HTTPException(409, "This take's file is gone, so it can't be used.")
    takes_svc.ensure_current_take(db, shot)  # keep the clip being swapped out
    takes_svc.use(db, shot, take)
    db.refresh(shot)
    return shot


@router.post("/projects/{project_id}/shots/{shot_id}/takes/from-studio", response_model=TakeFromStudioOut,
             status_code=201)
def take_from_studio(project_id: int, shot_id: int, body: TakeFromStudioIn,
                     db: Session = Depends(get_db)) -> TakeFromStudioOut:
    """Use a finished Studio clip as this shot's clip: copied into the
    project as a new take (trimmed to the shot when longer) and made
    current. Free; Re-compose puts it in the video. A clip with pictures of
    an uploaded video (or a join with one) has the stretch used checked
    first, about a frame a second (video_ops.check_clip_used_whole): 422
    refused, 503 the check couldn't run; that check is a vision call
    costing a fraction of a cent, so it is refused (402) past the budget's
    hard stop, before anything is read."""
    from ..services import inspector, video_ops
    from ..services.pipeline import STUDIO_DELETED, _active_job_for
    from ..services.spend import budget_guard

    shot = _shot_of(db, project_id, shot_id)
    job = db.get(models.AssetGenerationJob, body.studio_job_id)
    if job is None or job.status == STUDIO_DELETED:
        raise HTTPException(404, "That Studio clip was deleted.")
    if job.output_kind != "video_clip":
        raise HTTPException(422, "Only a Studio clip can be used in a shot, not a picture.")
    if job.status not in ("completed", "saved") or not job.result_path or not Path(job.result_path).is_file():
        raise HTTPException(409, "That Studio clip isn't finished yet.")
    if not takes_svc._is_storyboard_shot(shot):
        raise HTTPException(409, "Only a storyboard shot can use a Studio clip (not the end card).")
    if _active_job_for(db, shot.id) is not None:
        raise HTTPException(409, "A new clip for this shot is still being made. Wait until it is back, "
                                 "then use the Studio clip.")
    if video_ops.has_upload(db, job):
        budget_guard()
    try:
        take, warning, seconds = takes_svc.import_studio_clip(db, shot, job)
    except takes_svc.StudioImportError as e:
        raise HTTPException(e.status, str(e)) from e
    db.refresh(shot)
    inspector.queue_auto_check(db, shot)
    return TakeFromStudioOut(**takes_svc.out(project_id, shot, take), warning=warning, duration_seconds=seconds)


@router.delete("/projects/{project_id}/shots/{shot_id}/takes/{take_id}", status_code=204)
def delete_take(project_id: int, shot_id: int, take_id: int, db: Session = Depends(get_db)) -> None:
    shot = _shot_of(db, project_id, shot_id)
    take = _take_of(shot, take_id)
    if shot.clip_path and take.clip_path == shot.clip_path:
        raise HTTPException(409, "This is the clip the shot uses now. Pick another take first.")
    take_ids, paths = takes_svc.in_use(db, project_id)
    if take.id in take_ids or (take.clip_path and take.clip_path in paths):
        raise HTTPException(409, "Another opening (hook lab) uses this take, so it is kept. Remove that "
                                 "opening first.")
    takes_svc.delete(db, shot, take)

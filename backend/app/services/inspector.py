"""Clip check: catch an off-model or broken storyboard clip before the video
is put together or more money is spent on it.

When a shot's clip lands (pipeline.on_shot_clip_ready → takes.record), a
background job pulls 4 frames from it and asks a vision-capable chat model
to judge them against the shot's description (and avoid text) and, when
the shot shows a cast member, that character's main picture. The verdict
is kept on the take (ShotTake.inspection_json) so it follows the take.

The check is advisory: it never blocks or fails a render, and anything
that goes wrong reads "unsure" ("couldn't check this clip"), never
"problem". A refusal is never shown as a result.

Settings (app_settings "inspector"): auto_check (default on) and the
model (None = default_model()).
"""
from __future__ import annotations

import base64
import io
import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from PIL import Image
from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings

log = logging.getLogger("avs.inspector")

SETTINGS_KEY = "inspector"
FRAMES = 4
EDGE_SKIP = 0.2  # seconds skipped at each end of the clip (fades, codec warm-up)
LONG_SIDE = 512  # frames and the reference picture are downscaled to this
ISSUE_KINDS = ("likeness", "anatomy", "text", "prompt_mismatch", "artifact", "other")
VERDICTS = ("ok", "problem", "unsure")
MAX_ISSUES = 6
COULDNT = "couldn't check this clip"
OVER_BUDGET = "not checked: monthly budget reached"
SWITCHED_OFF = "not checked: the clip check's model is switched off"
ADULT_MODE = "not checked: adult mode is on"
# Shown wherever a check would run while adult mode is on: the vision
# models refuse (and are billed) or moralise, so clips aren't sent at all.
ADULT_MODE_WHY = "Clip check is off while adult mode is on."

# Rough per-check token use, for a price from the catalog's per-token rates.
_TOKENS_PER_IMAGE = 450
_TOKENS_TEXT_IN = 600
_TOKENS_OUT = 250


# ---------- settings ----------


def _stored() -> dict:
    from .model_registry import get_setting

    return dict(get_setting(SETTINGS_KEY) or {})


def auto_check() -> bool:
    return bool(_stored().get("auto_check", True))


def adult_mode() -> bool:
    """Adult mode is on: no clip is sent for checking (never raises)."""
    try:
        from .content_mode import adult_mode as on

        return on()
    except Exception:  # noqa: BLE001 — unreadable: treat as on here (send nothing)
        log.warning("adult mode unreadable; not checking clips")
        return True


def _vision_entries() -> list[dict]:
    from .model_catalog import get_snapshot

    snap = get_snapshot() or {}
    return [e for e in snap.get("entries") or []
            if e.get("kind") == "chat" and "image" in [str(x).lower() for x in e.get("input_modalities") or []]]


def is_vision(model: str) -> Optional[bool]:
    """True/False from the catalog's input modalities; None = not listed."""
    from .model_catalog import find

    e = find("chat", model)
    if e is None or not e.get("input_modalities"):
        return None
    return "image" in [str(x).lower() for x in e["input_modalities"]]


def default_model() -> str:
    """The configured default when the catalog lists it as taking images
    (or the catalog hasn't been polled); else a current vision model from
    the catalog: the newest fast Google vision model, else the planner when it
    takes images, else the configured default anyway."""
    s = get_settings()
    if s.mock_providers:
        return "mock/vision"
    want = s.inspector_model
    entries = _vision_entries()
    if not entries or is_vision(want) is not False and any(e["id"] == want for e in entries):
        return want
    flash = sorted((e for e in entries if e["id"].startswith("google/gemini") and "flash" in e["id"]
                    and "image" not in e["id"].split("/", 1)[1]),
                   key=lambda e: e.get("created") or 0, reverse=True)
    if flash:
        return flash[0]["id"]
    from .model_registry import active_model

    planner = active_model("chat")
    if is_vision(planner):
        return planner
    return want


def selected_model() -> Optional[str]:
    m = _stored().get("model")
    return str(m) if m else None


def active_model() -> str:
    return selected_model() or default_model()


def cost_per_check(model: Optional[str] = None) -> float:
    """Rough USD for one check: the catalog's per-token / per-image rates
    for ~5 pictures and a short reply, else the configured rate card."""
    m = model or active_model()
    s = get_settings()
    if m.startswith("mock/") or s.mock_providers:
        return 0.0
    from .model_catalog import find

    pricing = (find("chat", m) or {}).get("pricing") or {}
    if not pricing.get("prompt") and not pricing.get("image"):
        return round(s.cost_clip_check, 4)
    images = FRAMES + 1
    cost = (pricing.get("prompt", 0.0) * (_TOKENS_TEXT_IN + images * _TOKENS_PER_IMAGE)
            + pricing.get("completion", 0.0) * _TOKENS_OUT
            + pricing.get("image", 0.0) * images + pricing.get("request", 0.0))
    return round(max(cost, 0.0001), 4)


def placeholder(model: Optional[str] = None) -> bool:
    """True when no real vision model would answer (no OpenRouter key outside
    mock mode): a made-up "looks right" must never be shown."""
    from .model_registry import provider_for

    m = model or active_model()
    return (provider_for("chat", m) == "mock" and not get_settings().mock_providers
            and not m.startswith("mock/"))


def settings_out() -> dict:
    from .model_registry import provider_for

    m = active_model()
    return {
        "auto_check": auto_check(),
        "model": m,
        "default_model": default_model(),
        "is_default": selected_model() is None,
        "provider": provider_for("chat", m),
        "takes_images": is_vision(m),
        # No real model would answer (no OpenRouter key): checks don't run.
        "available": not placeholder(m),
        # Adult mode on: no clip is checked (automatic or manual).
        "adult_mode": adult_mode(),
        "cost_per_check": cost_per_check(m),
        "currency": "USD",
    }


def save_settings(*, auto: Optional[bool] = None, model: Optional[str] = None,
                  reset_model: bool = False) -> dict:
    from .model_registry import put_setting

    v = _stored()
    if auto is not None:
        v["auto_check"] = bool(auto)
    if reset_model:
        v.pop("model", None)
    elif model:
        v["model"] = model.strip()
    put_setting(SETTINGS_KEY, v)
    return settings_out()


def model_choices() -> list[dict]:
    """Chat models the catalog lists as taking pictures, newest first."""
    rows = sorted(_vision_entries(), key=lambda e: e.get("created") or 0, reverse=True)
    return [{"id": e["id"], "name": e.get("name") or e["id"], "usable": bool(e.get("usable", True)),
             "cost_per_check": cost_per_check(e["id"])} for e in rows]


# ---------- frames ----------


def _probe_duration(path: Path) -> float:
    from .renderer import _probe_duration as probe

    return probe(path)


def frame_times(duration: float, n: int = FRAMES) -> list[float]:
    """n timestamps spread evenly over the clip, skipping EDGE_SKIP at each end."""
    lo, hi = EDGE_SKIP, duration - EDGE_SKIP
    if hi <= lo:
        return [max(duration / 2.0, 0.0)] * n
    return [round(lo + (hi - lo) * i / (n - 1), 3) for i in range(n)]


def _jpeg(im: Image.Image) -> bytes:
    im = im.convert("RGB")
    im.thumbnail((LONG_SIDE, LONG_SIDE))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=82)
    return buf.getvalue()


def _frame_at(clip: Path, t: float) -> Optional[bytes]:
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(clip), "-frames:v", "1",
           "-f", "image2pipe", "-c:v", "png", "-"]
    proc = subprocess.run(cmd, capture_output=True, timeout=60)
    if proc.returncode != 0 or not proc.stdout:
        return None
    try:
        return _jpeg(Image.open(io.BytesIO(proc.stdout)))
    except OSError:
        return None


def extract_frames(clip: Path, n: int = FRAMES) -> list[bytes]:
    """n JPEG frames (long side ≤ LONG_SIDE) spread over the clip."""
    duration = _probe_duration(clip)
    if duration <= 0:
        raise RuntimeError("could not read the clip")
    frames = [f for f in (_frame_at(clip, t) for t in frame_times(duration, n)) if f]
    if not frames:
        raise RuntimeError("could not read frames from the clip")
    return frames


def _data_url(jpeg: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")


# ---------- the character to compare against ----------


def _main_picture(avatar: models.Avatar) -> Optional[models.Asset]:
    images = [a for a in avatar.assets if (a.mime_type or "").startswith("image/") and a.asset_type != "logo"]
    heroes = [a for a in images if a.asset_type == "hero"]
    return (heroes or images or [None])[0]


def character_for(db: Session, project: models.VideoProject,
                  shot: models.VideoShot) -> Optional[tuple[str, models.Asset]]:
    """(name, main picture) of the cast member the shot shows: one of the
    shot's reference pictures belongs to a character, or it is a talking
    head / hero shot (the main character)."""
    from .pipeline import _asset_ids

    avatar: Optional[models.Avatar] = None
    ids = _asset_ids(shot.reference_asset_ids_json)
    if ids:
        for a in db.query(models.Asset).filter(models.Asset.id.in_(ids)).all():
            if a.owner_kind == "avatar" and a.asset_type != "logo" and a.avatar is not None:
                avatar = a.avatar
                break
    if avatar is None and shot.shot_type in ("talking_head", "hero"):
        avatar = db.get(models.Avatar, project.primary_avatar_id) if project.primary_avatar_id else None
        if avatar is None:
            for cm in project.cast_members:
                if cm.member_kind == "avatar" and cm.avatar is not None:
                    avatar = cm.avatar
                    break
    if avatar is None:
        return None
    pic = _main_picture(avatar)
    if pic is None or not pic.file_path or not Path(pic.file_path).is_file():
        return None
    return avatar.name, pic


# ---------- the question and the answer ----------

SYSTEM = """You check one short AI-generated video clip for a creator before it goes into their video. You get the shot's description, what it must avoid, and frames from the clip in time order (and, when the shot shows a cast member, that person's main picture first, as the likeness reference).
Look for: a face or person that doesn't match the reference picture (likeness); broken hands, extra or missing fingers or limbs, melting or warped bodies (anatomy); garbled or unwanted text, captions, logos or watermarks (text); the clip not showing what the description asks for, or showing something it must avoid (prompt_mismatch); glitches, smearing, flicker, frozen or corrupted frames (artifact); anything else that would make the creator re-make the clip (other).
Judge only what you can see. Minor style differences are fine. Nudity, sexual or adult content, violence and other mature themes are allowed when the description asks for them: they are never an issue, and you never comment on content policy or how explicit the clip is. Report only defects in how the clip was made. When it looks right, say ok; when something is clearly wrong, say problem; when you can't tell, say unsure.
Reply with JSON only, no prose: {"verdict": "ok" | "problem" | "unsure", "issues": [{"kind": "likeness" | "anatomy" | "text" | "prompt_mismatch" | "artifact" | "other", "detail": "..."}], "summary": "..."}
Each detail is one short plain sentence a non-expert understands, naming the person or the moment when it helps (for example "face doesn't match Maya", "six fingers on the left hand at the end"). summary is one short sentence. If you won't look at these images for any reason, reply with exactly DECLINED."""

# SYSTEM on a clean deployment (ADULT_FEATURES=false): the app is for a
# general audience, so nudity or sexual content in a clip is reported, never
# waved through.
_PERMISSIVE_RULE = ("Nudity, sexual or adult content, violence and other mature themes are allowed when the "
                    "description asks for them: they are never an issue, and you never comment on content "
                    "policy or how explicit the clip is. Report only defects in how the clip was made.")
_CLEAN_RULE = ("This app is for a general audience: nudity, sexual or explicit content is always an issue "
               "(kind other), even when the description asks for it; say problem and name it in one short "
               "plain sentence. Otherwise report only defects in how the clip was made.")
SYSTEM_CLEAN = SYSTEM.replace(_PERMISSIVE_RULE, _CLEAN_RULE)


def system_prompt() -> str:
    """The clip check's instructions: the clean ones on a clean deployment."""
    from . import content_mode

    return SYSTEM if content_mode.features() else SYSTEM_CLEAN


def build_text(shot: models.VideoShot, times: list[float], character: Optional[str],
               avoid: str) -> str:
    lines = [f"Shot description: {shot.prompt.strip() or '(none)'}"]
    if avoid.strip():
        lines.append(f"Must avoid: {avoid.strip()}")
    if shot.camera_direction:
        lines.append(f"Camera: {shot.camera_direction}")
    n = 1
    if character:
        lines.append(f"Image 1: {character}'s main picture (the likeness reference). The clip should show {character}.")
        n = 2
    stamps = ", ".join(f"{t:.1f} s" for t in times)
    lines.append(f"Images {n}-{n + len(times) - 1}: frames from the clip at {stamps}.")
    return "\n".join(lines)


def _clean(text: object, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


# An "issue" about what the clip shows rather than how it was made (nudity,
# explicitness, policy) is a content judgement, never a defect: dropped.
_CONTENT_JUDGEMENT = re.compile(
    r"\b(nud(e|ity)|naked|explicit|sexual(ly)?|sexy|porn\w*|nsfw|adult (content|material|themes?)|"
    r"topless|lewd|obscen\w*|inappropriate|"
    r"(content|safety|usage) polic(y|ies)|guidelines?|not (allowed|permitted|appropriate)|"
    r"can't (help|assist)|cannot (help|assist)|i won't)\b", re.I)


def content_judgement(text: str) -> bool:
    """An issue that only judges what the clip shows: dropped, except on a
    clean deployment, where nudity or sexual content is an issue to report."""
    from . import content_mode

    if not content_mode.features():
        return False
    return bool(_CONTENT_JUDGEMENT.search(text or ""))


def parse_reply(reply: str) -> dict:
    """The model's reply as {verdict, issues, summary}. A refusal, DECLINED
    or anything unparseable reads "unsure" ("couldn't check this clip")."""
    from ..providers.openrouter_chat import extract_json
    from .prompting import refusal

    unsure = {"verdict": "unsure", "issues": [], "summary": COULDNT}
    text = (reply or "").strip()
    if not text or refusal(text) is not None:
        return unsure
    try:
        data = extract_json(text)
    except ValueError:
        return unsure
    if not isinstance(data, dict):
        return unsure
    verdict = str(data.get("verdict") or "").strip().lower()
    if verdict not in VERDICTS:
        return unsure
    issues = []
    dropped = 0  # issues that only judged what the clip shows
    for it in data.get("issues") or []:
        if not isinstance(it, dict):
            continue
        detail = _clean(it.get("detail"), 200)
        if not detail:
            continue
        if content_judgement(detail):
            dropped += 1
            continue
        kind = str(it.get("kind") or "other").strip().lower()
        issues.append({"kind": kind if kind in ISSUE_KINDS else "other", "detail": detail})
        if len(issues) >= MAX_ISSUES:
            break
    summary = _clean(data.get("summary"), 300)
    if verdict == "problem" and not issues and dropped:
        return unsure  # flagged only for what it shows: not a verdict on the clip
    if content_judgement(summary):
        if verdict == "problem" and not issues:
            return unsure  # flagged only for what it shows: not a verdict on the clip
        summary = issues[0]["detail"] if issues else ""
    if verdict == "problem" and not issues:
        issues = [{"kind": "other", "detail": summary or "something looks wrong in this clip"}]
    if verdict == "unsure" and not summary:
        summary = COULDNT
    if not summary:
        summary = issues[0]["detail"] if issues else "Looks right."
    return {"verdict": verdict, "issues": issues, "summary": summary}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _result(verdict: str, summary: str, model: str, *, issues: Optional[list] = None,
            auto: bool, cost: float = 0.0) -> dict:
    return {"verdict": verdict, "issues": issues or [], "summary": summary, "checked_at": _now(),
            "model": model, "auto": auto, "cost_estimate": cost}


def _store(db: Session, take: models.ShotTake, result: dict) -> dict:
    take.inspection_json = result
    db.commit()
    return result


def check_take(db: Session, take: models.ShotTake, *, auto: bool = False) -> dict:
    """Check `take`'s clip and store the verdict on it. Never raises: any
    failure is stored as "unsure"."""
    model = active_model()
    shot = take.shot
    project = shot.project
    try:
        from ..providers.registry import get_vision

        clip = Path(take.clip_path or "")
        if not take.clip_path or not clip.is_file():
            return _store(db, take, _result("unsure", COULDNT, model, auto=auto))
        frames = extract_frames(clip)
        duration = _probe_duration(clip)
        times = frame_times(duration, len(frames))
        who = character_for(db, project, shot)
        images = []
        if who is not None:
            try:
                images.append(_data_url(_jpeg(Image.open(who[1].file_path))))
            except OSError:  # a picture Pillow can't read: check against the description only
                who = None
        images += [_data_url(f) for f in frames]
        # The shot's own avoid text only: the Settings defaults ("slow
        # motion", "blurry"...) are generic and would read as mismatches.
        text = build_text(shot, times, who[0] if who else None, shot.negative_prompt or "")
        cost = cost_per_check(model)
        reply = get_vision(model).inspect_images(system=system_prompt(), text=text, images=images,
                                                 note="clip check", estimate=cost)
        parsed = parse_reply(reply)
        return _store(db, take, _result(parsed["verdict"], parsed["summary"], model,
                                         issues=parsed["issues"], auto=auto, cost=cost))
    except Exception as e:  # noqa: BLE001 — advisory: an error is "couldn't check", never a failure
        log.warning("clip check of take %s failed: %s", take.id, e)
        db.rollback()
        take = db.get(models.ShotTake, take.id)
        if take is None:
            return _result("unsure", COULDNT, model, auto=auto)
        return _store(db, take, _result("unsure", COULDNT, model, auto=auto))


def run_auto(db: Session, take_id: int) -> Optional[dict]:
    """The automatic check after a clip lands: skipped (nothing stored)
    when switched off; noted, not run, over the monthly budget or when the
    model's provider is switched off."""
    from . import spend
    from .model_registry import switched_off

    take = db.get(models.ShotTake, take_id)
    if take is None or not auto_check():
        return None
    shot = take.shot
    if shot is None or not shot.clip_path or shot.clip_path != take.clip_path:
        return None  # replaced meanwhile: the newer clip gets its own check
    model = active_model()
    if adult_mode():
        return _store(db, take, _result("skipped", ADULT_MODE, model, auto=True))
    if spend.over_budget(db):
        return _store(db, take, _result("skipped", OVER_BUDGET, model, auto=True))
    if switched_off("chat", model):
        return _store(db, take, _result("skipped", SWITCHED_OFF, model, auto=True))
    if placeholder(model):
        return None  # no key: nothing to ask
    with spend.scope(project_id=shot.project_id, ref=f"take:{take.id}"):
        return check_take(db, take, auto=True)


def queue_auto_check(db: Session, shot: models.VideoShot) -> None:
    """Queue the automatic check of the shot's current take, off the
    render's path: its own low-priority RQ queue, or a background thread
    (with its own session) when jobs run inline, so a landing clip, the
    sweeper and a webhook never wait for frames or the vision model. Never
    raises."""
    try:
        if not auto_check():
            return
        from .takes import current_take

        take = current_take(shot)
        if take is None:
            return
        from ..workers.jobs import inspect_take_job
        from ..workers.queue import enqueue_low

        enqueue_low(inspect_take_job, take.id)
    except Exception:  # noqa: BLE001
        log.exception("could not queue the clip check for shot %s", getattr(shot, "id", "?"))


# ---------- reading ----------


def flagged_shots(project: models.VideoProject) -> list[dict]:
    """Body shots whose current clip the check flagged ("problem")."""
    out = []
    for s in project.shots:
        ins = s.inspection
        if ins and ins.get("verdict") == "problem":
            first = (ins.get("issues") or [{}])[0].get("detail") or ins.get("summary") or ""
            out.append({"shot_id": s.id, "shot_order": s.shot_order, "summary": ins.get("summary") or "",
                        "first_issue": first})
    return sorted(out, key=lambda r: r["shot_order"])

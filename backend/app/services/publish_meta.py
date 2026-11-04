"""Draft YouTube metadata for a project: title, description, tags,
category and disclosure flags, inside YouTube's documented limits
(title ≤ 100 chars, description ≤ 5000 bytes, tags ≤ 500 chars total,
no '<' or '>' anywhere)."""
from __future__ import annotations

from typing import Optional

from .. import models

TITLE_MAX = 100
DESC_MAX_BYTES = 5000
TAGS_MAX_CHARS = 500
# YouTube category ids: 10 = Music, 22 = People & Blogs (widely used ids;
# verify against videoCategories.list for your region if a call 400s).
CATEGORY_MUSIC = "10"
CATEGORY_PEOPLE = "22"


def _clean(text: str) -> str:
    return (text or "").replace("<", "‹").replace(">", "›").strip()


def _fit_title(t: str) -> str:
    t = _clean(t)
    return t if len(t) <= TITLE_MAX else t[: TITLE_MAX - 1].rstrip() + "…"


def _fit_desc(d: str) -> str:
    d = _clean(d)
    b = d.encode("utf-8")
    if len(b) <= DESC_MAX_BYTES:
        return d
    return b[: DESC_MAX_BYTES - 3].decode("utf-8", "ignore") + "…"


def fit_tags(tags: list[str]) -> list[str]:
    """De-duplicate and keep tags while the YouTube total stays ≤ 500 chars
    (commas count; a tag with a space counts as quoted)."""
    out, total, seen = [], 0, set()
    for t in tags:
        t = _clean(t).replace(",", " ")[:100]
        if not t or t.lower() in seen:
            continue
        cost = len(t) + (2 if " " in t else 0) + (1 if out else 0)
        if total + cost > TAGS_MAX_CHARS:
            break
        out.append(t)
        seen.add(t.lower())
        total += cost
    return out


def _hours(seconds: int) -> str:
    if seconds >= 3600 and seconds % 3600 == 0:
        h = seconds // 3600
        return f"{h} Hour{'s' if h > 1 else ''}"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} Hours"
    return f"{max(1, round(seconds / 60))} Minutes"


def draft(project: models.VideoProject) -> dict:
    if project.mode == "ambient_loop":
        return _ambient(project)
    return _storyboard(project)


def _ambient(project: models.VideoProject) -> dict:
    from ..schemas.ambient import AmbientConfig
    from .ambient import _MOOD_BY_ID, _PRESET_BY_ID

    cfg = AmbientConfig.model_validate(project.ambient_config_json or {})
    preset = _PRESET_BY_ID.get(cfg.scene_preset)
    mood = _MOOD_BY_ID.get(cfg.music_mood)
    scene = preset.label if preset and preset.id != "custom" else (project.title or "Ambient Scene")
    length = _hours(int(project.target_duration_seconds))
    music_label = (mood.label if mood and mood.id != "none" else "")
    uses = "Sleep, Relaxation, Meditation & Study"
    res = " 4K" if cfg.output_resolution == "4k" else ""
    title = f"{scene}{res} — {length}" + (f" of {music_label} Music" if music_label else " Ambience") + f" for {uses}"
    if len(title) > TITLE_MAX:
        title = f"{scene}{res} — {length} Ambience for Sleep & Relaxation"
    lines = [
        f"{length} of {scene.lower()} ambience" + (f" with soft {music_label.lower()} music." if music_label else "."),
        (preset.prompt if preset and preset.prompt else cfg.scene_prompt) or "",
        "",
        "Perfect for: sleep · relaxation · meditation · yoga · spa · study · focus · background ambience.",
        "",
        "🌿 Seamlessly looped, no ads in the middle, gentle fade in and out.",
        "",
        "This video's visuals and music were created with AI.",
    ]
    tags = [scene, f"{scene} ambience", f"{length.lower()} {scene.lower()}", "ambient", "relaxing",
            "sleep music" if music_label else "sleep sounds", "relaxation", "meditation", "spa",
            "study music", "calm", "background ambience", "nature sounds"]
    if music_label:
        tags.insert(2, f"{music_label} music")
    if res:
        tags.append("4K")
    return {
        "title": _fit_title(title),
        "description": _fit_desc("\n".join(lines)),
        "tags": fit_tags(tags),
        "category_id": CATEGORY_MUSIC if music_label else CATEGORY_PEOPLE,
        "contains_synthetic_media": True,
        "made_for_kids": False,
    }


def _storyboard(project: models.VideoProject) -> dict:
    plan = project.generated_plan_json or {}
    hook = plan.get("hook") or ""
    script = plan.get("cleaned_voice_script") or project.original_script or ""
    disclosure = plan.get("disclosure_text") or project.disclosure_text or "AI-generated video"
    lines = [hook, "", script]
    if project.cta_text:
        lines += ["", project.cta_text]
    lines += ["", f"{disclosure}."]
    words = [w.strip(".,!?").lower() for w in (project.title or "").split() if len(w) > 3]
    return {
        "title": _fit_title(project.title or hook or "New video"),
        "description": _fit_desc("\n".join(l for l in lines if l is not None)),
        "tags": fit_tags(words + ["shorts" if project.aspect_ratio == "9:16" else "video", "ai video"]),
        "category_id": CATEGORY_PEOPLE,
        "contains_synthetic_media": True,
        "made_for_kids": False,
    }

"""Pre-generation quality checks. Run before kicking off a real-provider video job
so the user doesn't burn API budget on a project that's structurally broken."""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from . import storage

CheckStatus = Literal["ok", "warn", "fail"]

# ~150 words/min ≈ 2.5 words/sec. Soft cap = 1.2× target duration's worth of words,
# hard cap = 2× — beyond that the TTS will outrun the visuals.
_WORDS_PER_SECOND = 2.5


@dataclass
class CheckResult:
    id: str
    label: str
    status: CheckStatus
    message: str
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "status": self.status,
            "message": self.message,
            "detail": self.detail,
        }


def _check_avatar_hero(db: Session, project: models.VideoProject) -> CheckResult:
    avatars: list[models.Avatar] = []
    for cm in project.cast_members:
        if cm.member_kind == "avatar" and cm.avatar is not None:
            avatars.append(cm.avatar)
    if not avatars:
        return CheckResult(
            id="avatar_hero",
            label="Avatar hero image",
            status="fail",
            message="Project has no characters in its cast.",
            detail="Add at least one character in Cast → Characters, then attach it to this project.",
        )
    missing = [a.name for a in avatars if not any(x.asset_type == "hero" for x in a.assets)]
    if missing:
        return CheckResult(
            id="avatar_hero",
            label="Avatar hero image",
            status="fail",
            message=f"Missing hero image for: {', '.join(missing)}.",
            detail="Open the character in Cast and upload an asset with type 'hero'.",
        )
    return CheckResult(
        id="avatar_hero",
        label="Avatar hero image",
        status="ok",
        message=f"All {len(avatars)} character(s) have a hero image.",
    )


def _check_script_length(db: Session, project: models.VideoProject) -> CheckResult:
    script = (project.original_script or "").strip()
    if not script:
        return CheckResult(
            id="script_length",
            label="Script length",
            status="fail",
            message="Script is empty.",
        )
    word_count = len([w for w in script.split() if w])
    target = max(project.target_duration_seconds or 25, 5)
    recommended = int(target * _WORDS_PER_SECOND)
    soft_cap = int(recommended * 1.2)
    hard_cap = int(recommended * 2)
    if word_count > hard_cap:
        return CheckResult(
            id="script_length",
            label="Script length",
            status="fail",
            message=f"Script is {word_count} words; recommended ≤ {recommended} for {target}s.",
            detail=(
                f"Voiceover will overrun the visuals. Trim to about {recommended} words, "
                f"or raise the target duration above {int(word_count / _WORDS_PER_SECOND)}s."
            ),
        )
    if word_count > soft_cap:
        return CheckResult(
            id="script_length",
            label="Script length",
            status="warn",
            message=f"Script is {word_count} words; comfortable max is {recommended}.",
            detail="Voiceover may feel rushed. Trim a few sentences or extend the duration.",
        )
    return CheckResult(
        id="script_length",
        label="Script length",
        status="ok",
        message=f"{word_count} words fits a {target}s voiceover.",
    )


def _check_cta(db: Session, project: models.VideoProject) -> CheckResult:
    cta = (project.cta_text or "").strip()
    if cta:
        return CheckResult(
            id="cta_or_disabled",
            label="CTA / end card",
            status="ok",
            message=f"End card will read: “{cta[:60]}”.",
        )
    return CheckResult(
        id="cta_or_disabled",
        label="CTA / end card",
        status="warn",
        message="No CTA text set; the video will end on the last shot.",
        detail="Add CTA text on the project to append a 2.5s branded end card, or ignore to skip.",
    )


def _check_providers(db: Session, project: models.VideoProject) -> CheckResult:
    s = get_settings()
    if s.mock_providers:
        return CheckResult(
            id="providers_configured",
            label="Providers",
            status="ok",
            message="MOCK_PROVIDERS=true — running without external API calls.",
        )
    from . import provider_keys

    missing: list[str] = []
    if not provider_keys.has("openrouter"):
        missing.append("OPENROUTER_API_KEY (chat + image + video)")
    if not provider_keys.has("elevenlabs"):
        # ElevenLabs is optional: mock TTS will be used.
        pass
    if missing:
        return CheckResult(
            id="providers_configured",
            label="Providers",
            status="fail",
            message="Required provider keys are missing.",
            detail="Missing: " + ", ".join(missing) + ". Add them in Settings → API keys (or .env), or enable MOCK_PROVIDERS.",
        )
    detail_bits = []
    if not provider_keys.has("elevenlabs"):
        detail_bits.append("ElevenLabs not configured — mock silent audio will be used")
    msg = "OpenRouter configured."
    if detail_bits:
        return CheckResult(
            id="providers_configured",
            label="Providers",
            status="warn",
            message=msg,
            detail="; ".join(detail_bits),
        )
    return CheckResult(
        id="providers_configured",
        label="Providers",
        status="ok",
        message=msg + " ElevenLabs configured.",
    )


def _check_output_writable(db: Session, project: models.VideoProject) -> CheckResult:
    target = storage.render_subdir(project.id)
    try:
        with tempfile.NamedTemporaryFile(dir=target, prefix=".preflight-", delete=True):
            pass
    except OSError as e:
        return CheckResult(
            id="output_writable",
            label="Output folder",
            status="fail",
            message=f"Cannot write to {target}.",
            detail=str(e),
        )
    # Bonus: warn on low disk space (< 100 MB).
    try:
        stats = os.statvfs(target)
        free_mb = (stats.f_bavail * stats.f_frsize) / (1024 * 1024)
    except OSError:
        free_mb = -1
    if free_mb >= 0 and free_mb < 100:
        return CheckResult(
            id="output_writable",
            label="Output folder",
            status="warn",
            message=f"Only {free_mb:.0f} MB free in {target}.",
            detail="Renders typically need 5–50 MB; consider clearing old projects.",
        )
    return CheckResult(
        id="output_writable",
        label="Output folder",
        status="ok",
        message=f"{target} is writable.",
    )


def _check_models(db: Session, project: models.VideoProject) -> CheckResult:
    """With live providers, a kind whose active model is still a mock/*
    placeholder (nothing chosen in Settings → Models, no OPENROUTER_*_MODEL)
    silently produces placeholder output while the rest is billed."""
    from . import model_registry as reg

    if get_settings().mock_providers:
        return CheckResult(id="models_selected", label="Models", status="ok",
                           message="Mock mode — placeholder models are expected.")
    from .content_mode import chat_model

    # The planner that will run (the adult text model in adult mode).
    mocked = {k: (chat_model() if k == "chat" else reg.active_model(k)) for k in ("chat", "video", "image")}
    mocked = {k: m for k, m in mocked.items() if m.startswith("mock/")}
    if not mocked:
        return CheckResult(id="models_selected", label="Models", status="ok",
                           message="A real model is selected for planning, video and images.")
    names = {"chat": "planner", "video": "video", "image": "image"}
    blocking = [k for k in mocked if k in ("chat", "video")]
    return CheckResult(
        id="models_selected", label="Models",
        status="fail" if blocking else "warn",
        message="No real " + " / ".join(names[k] for k in mocked) + " model is selected.",
        detail="These would produce mock placeholders: "
               + ", ".join(f"{names[k]} → {m}" for k, m in mocked.items())
               + ". Pick one in Settings → Models, or set "
               + ", ".join(f"OPENROUTER_{k.upper()}_MODEL" for k in mocked) + ".",
    )


def _check_voice(db: Session, project: models.VideoProject) -> CheckResult:
    """ElevenLabs needs a voice: the avatar's, or ELEVENLABS_DEFAULT_VOICE_ID.
    Without one the TTS call fails, and the render stops there (before any
    clip is paid for) — so this blocks rather than warns."""
    from . import fingerprints
    from . import model_registry as reg

    s = get_settings()
    source = (project.voiceover_source or "tts").lower()
    if (project.generated_plan_json and source != "silent"
            and fingerprints.voice_source(project) == "silent"):
        # Nothing to say: the render goes ahead without a voice, so no voice
        # is needed. Said here so it is not a surprise in the finished video.
        return CheckResult(id="voice_selected", label="Voice", status="warn",
                           message="The spoken script is empty, so the video is made without a voice. "
                                   "No voiceover is paid for.",
                           detail="Write the spoken script in Voiceover & captions if you want a voice.")
    if source != "tts" or s.mock_providers or reg.provider_for("tts") != "elevenlabs":
        return CheckResult(id="voice_selected", label="Voice", status="ok",
                           message="Voiceover source doesn't need an ElevenLabs voice.")
    avatar = db.get(models.Avatar, project.primary_avatar_id) if project.primary_avatar_id else None
    if (avatar and avatar.elevenlabs_voice_id) or s.elevenlabs_default_voice_id:
        return CheckResult(id="voice_selected", label="Voice", status="ok",
                           message="An ElevenLabs voice is set.")
    return CheckResult(
        id="voice_selected", label="Voice", status="fail",
        message="No ElevenLabs voice is set, so the voiceover can't be made. "
                "The render would stop before any clip is generated.",
        detail="Give the avatar a voice, set ELEVENLABS_DEFAULT_VOICE_ID, pick an "
               "OpenRouter voice model in Settings → Models, or set the voiceover "
               "to an uploaded file or to silent in Audio.",
    )


def run_preflight(db: Session, project: models.VideoProject) -> dict:
    checks = [
        _check_avatar_hero(db, project),
        _check_script_length(db, project),
        _check_cta(db, project),
        _check_providers(db, project),
        _check_models(db, project),
        _check_voice(db, project),
        _check_output_writable(db, project),
    ]
    has_fail = any(c.status == "fail" for c in checks)
    has_warn = any(c.status == "warn" for c in checks)
    return {
        "ok": not has_fail,
        "summary": "fail" if has_fail else ("warn" if has_warn else "ok"),
        "checks": [c.to_dict() for c in checks],
    }

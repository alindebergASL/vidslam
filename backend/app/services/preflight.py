"""Pre-generation quality checks. Run before kicking off a real-provider video job
so the user doesn't burn API budget on a project that's structurally broken."""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
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
    missing: list[str] = []
    if not s.openrouter_api_key:
        missing.append("OPENROUTER_API_KEY (chat + image + video)")
    if not s.elevenlabs_api_key:
        # ElevenLabs is optional: mock TTS will be used.
        pass
    if missing:
        return CheckResult(
            id="providers_configured",
            label="Providers",
            status="fail",
            message="Required provider keys are missing.",
            detail="Missing: " + ", ".join(missing) + ". Set them in .env or enable MOCK_PROVIDERS.",
        )
    detail_bits = []
    if not s.elevenlabs_api_key:
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


def run_preflight(db: Session, project: models.VideoProject) -> dict:
    checks = [
        _check_avatar_hero(db, project),
        _check_script_length(db, project),
        _check_cta(db, project),
        _check_providers(db, project),
        _check_output_writable(db, project),
    ]
    has_fail = any(c.status == "fail" for c in checks)
    has_warn = any(c.status == "warn" for c in checks)
    return {
        "ok": not has_fail,
        "summary": "fail" if has_fail else ("warn" if has_warn else "ok"),
        "checks": [c.to_dict() for c in checks],
    }

from __future__ import annotations

import time

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models as models_pkg
from ..config import get_settings
from ..db import get_db
from ..providers import get_image, get_video, provider_status
from ..providers.base import ModelInfo
from ..services import provider_keys
from .auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])
settings = get_settings()


def _to_dict(models: list[ModelInfo]) -> list[dict]:
    return [{"id": m.id, "name": m.name, "description": m.description} for m in models]


@router.get("/status")
def status() -> dict:
    return provider_status()


def _trained_lora_entries(db: Session) -> list[dict]:
    """The user's completed character/style LoRA adapters as picker entries.
    Shared by the image- and video-model listings so a trained identity
    adapter is selectable in the Studio AND per-shot in the editor."""
    trained = (
        db.query(models_pkg.CustomModel)
        .filter(
            models_pkg.CustomModel.status == "completed",
            models_pkg.CustomModel.kind.in_(("character_lora", "style_lora")),
        )
        .order_by(models_pkg.CustomModel.completed_at.desc())
        .all()
    )
    return [
        {
            "id": f"custom:{cm.id}",
            "name": cm.name or f"#{cm.id}",
            "description": (
                f"{cm.kind.replace('_', ' ').title()} · "
                f"trained {cm.completed_at.date().isoformat() if cm.completed_at else 'recently'} · "
                f"{cm.provider or 'mock'}"
            ),
        }
        for cm in trained
    ]


def _catalog_entries(kind: str) -> list[dict]:
    """Picker entries from the polled model catalog (empty if never polled)."""
    from ..services import model_catalog

    snap = model_catalog.get_snapshot() or {}
    out = []
    for e in snap.get("entries") or []:
        if e.get("kind") != kind:
            continue
        bits = []
        if e.get("max_seconds"):
            bits.append(f"up to {e['max_seconds']}s")
        frames = e.get("frame_images") or []
        if "last_frame" in frames:
            bits.append("first+last frame")
        elif "first_frame" in frames:
            bits.append("first frame")
        if e.get("provider") == "sogni":
            bits = []  # the Sogni name already says how long and which frames
        name = e.get("name") or e["id"]
        out.append({
            "id": e["id"],
            "name": f"{name} ({' · '.join(bits)})" if bits else name,
            "description": ("" if e.get("usable") else "needs API key · ") + (e.get("description") or ""),
        })
    return out


def _merged(kind: str, provider_list) -> list[dict]:
    """Provider's own list (always includes whatever it can actually serve,
    e.g. the mock model) plus the polled catalog, de-duplicated."""
    try:
        base = _to_dict(provider_list())
    except Exception:  # noqa: BLE001 — the catalog may still have entries
        base = []
    seen = {m["id"] for m in base}
    extra = [m for m in _catalog_entries(kind) if m["id"] not in seen]
    if not base and not extra:
        raise HTTPException(502, f"failed to list {kind} models")
    return base + extra


@router.get("/openrouter/video-models")
def video_models(db: Session = Depends(get_db)) -> list[dict]:
    return _trained_lora_entries(db) + _merged("video", lambda: get_video().list_models())


@router.get("/openrouter/image-models")
def image_models(db: Session = Depends(get_db)) -> list[dict]:
    """Base image models from the provider, plus the user's completed
    character/style LoRA adapters. Trained models are surfaced inline so
    the Studio picker shows "Naina LoRA v1" alongside the catalog without
    needing a second dropdown — pick it and the next generate-image call
    passes the adapter id through.
    """
    # Custom adapters surface first since users almost always want their
    # own model over the base when they've trained one.
    return _trained_lora_entries(db) + _merged("image", lambda: get_image().list_models())


@router.get("/caption-styles")
def caption_styles() -> list[str]:
    from ..services.captions import available_styles

    return available_styles()


def _probe(name: str, url: str, headers: dict) -> dict:
    """Hit a cheap auth-only endpoint and report ok/latency without generating anything."""
    t0 = time.monotonic()
    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.get(url, headers=headers)
        latency = int((time.monotonic() - t0) * 1000)
        if r.status_code == 200:
            return {"group": name, "mode": "live", "ok": True,
                    "message": f"authenticated (HTTP 200, {latency}ms)", "latency_ms": latency}
        if r.status_code in (401, 403):
            return {"group": name, "mode": "live", "ok": False,
                    "message": f"auth rejected (HTTP {r.status_code}) — check the API key",
                    "latency_ms": latency}
        return {"group": name, "mode": "live", "ok": False,
                "message": f"unexpected HTTP {r.status_code}", "latency_ms": latency}
    except Exception as e:  # noqa: BLE001
        return {"group": name, "mode": "live", "ok": False, "message": str(e)[:200]}


@router.post("/health-check")
def health_check() -> dict:
    """Validate provider credentials with lightweight, no-generation calls.

    OpenRouter (chat/image/video share one key) is checked via GET /models;
    ElevenLabs (tts/music) via GET /voices. In mock mode each group reports
    'mock' without making any external request."""
    s = get_settings()
    results: list[dict] = []
    or_key = provider_keys.resolve("openrouter")
    el_key = provider_keys.resolve("elevenlabs")

    if s.mock_providers or not or_key:
        results.append({
            "group": "openrouter",
            "mode": "mock",
            "ok": True,
            "message": "mock mode — no key needed (chat / image / video)",
        })
    else:
        results.append(_probe(
            "openrouter",
            f"{s.openrouter_base_url.rstrip('/')}/models",
            {"Authorization": f"Bearer {or_key}"},
        ))

    if s.mock_providers or not el_key:
        results.append({
            "group": "elevenlabs",
            "mode": "mock",
            "ok": True,
            "message": "mock mode — no key needed (tts / music)",
        })
    else:
        results.append(_probe(
            "elevenlabs",
            f"{s.elevenlabs_base_url.rstrip('/')}/voices",
            {"xi-api-key": el_key},
        ))

    return {"ok": all(r["ok"] for r in results), "results": results}


@router.get("/elevenlabs/voices")
def elevenlabs_voices() -> list[dict]:
    """Return the configured TTS provider's available voices.

    In MOCK_PROVIDERS mode this returns the mock provider's single voice;
    when ElevenLabs is configured it fetches the live `/v1/voices` list.
    Frontend uses this to populate the per-avatar voice picker."""
    from ..providers import get_tts

    try:
        return get_tts().list_voices()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"failed to list voices: {e}") from e

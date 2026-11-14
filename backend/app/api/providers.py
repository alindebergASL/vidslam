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
from ..services.prices import price_text
from .auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])
settings = get_settings()


def _to_dict(models: list[ModelInfo]) -> list[dict]:
    return [{"id": m.id, "name": m.name, "description": m.description} for m in models]


@router.get("/status")
def status() -> dict:
    return provider_status()


def _trained_lora_entries(db: Session, kind: str) -> list[dict]:
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
    # Not offered while the provider that trained them, or the one that runs
    # them, is switched off (exactly the check a generate request gets).
    from ..services import provider_gate

    def _servable(cm) -> bool:  # noqa: ANN001
        try:
            provider_gate.require_model(kind, f"custom:{cm.id}", db)
        except provider_keys.ProviderUnavailable:
            return False
        return True

    trained = [cm for cm in trained if _servable(cm)]
    return [
        {
            "id": f"custom:{cm.id}",
            "name": cm.name or f"#{cm.id}",
            "price_text": "",
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
        if e.get("kind") != kind or e.get("enabled") is False:
            continue  # a provider switched off in Settings isn't offered
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
            "price_text": price_text(e, kind),
        })
    return out


def _priced(kind: str, entries: list[dict]) -> list[dict]:
    """Each picker entry with `price_text` ("about $0.04 per image", "about
    $0.30 per 5 s"; "" when unknown), from the catalog's pricing."""
    from ..services import model_catalog

    snap = model_catalog.get_snapshot() or {}
    by_id = {e.get("id"): e for e in snap.get("entries") or [] if e.get("kind") == kind}
    return [{**e, "price_text": e.get("price_text") or price_text(by_id.get(e.get("id")), kind)}
            for e in entries]


def _merged(kind: str, provider_list) -> list[dict]:
    """Provider's own list (always includes whatever it can actually serve,
    e.g. the mock model) plus the polled catalog, de-duplicated."""
    switched_off = False
    try:
        base = _to_dict(provider_list())
    except provider_keys.ProviderUnavailable:
        base, switched_off = [], True  # the active model's provider is off
    except Exception:  # noqa: BLE001 — the catalog may still have entries
        base = []
    seen = {m["id"] for m in base}
    extra = [m for m in _catalog_entries(kind) if m["id"] not in seen]
    if not base and not extra and not switched_off:
        raise HTTPException(502, f"failed to list {kind} models")
    entries = base + extra
    from ..services import content_mode

    if not content_mode.features():
        # A clean deployment lists no adult-only model, including the ones a
        # provider reports live (they would be refused anyway).
        entries = [m for m in entries if not content_mode.adult_entry(m)]
    return _priced(kind, entries)


@router.get("/openrouter/video-models")
def video_models(db: Session = Depends(get_db)) -> list[dict]:
    """Each entry also says whether the model can start from a picture
    (`first_frame`), for Studio's "Animate this", and whether it can make
    its own sound (`supports_audio`, with `audio_price_text`: its price
    with sound), for Studio's Sound switch."""
    entries = _trained_lora_entries(db, "video") + _merged("video", lambda: get_video().list_models())
    from ..services import model_catalog

    catalog = {e.get("id"): e for e in (model_catalog.get_snapshot() or {}).get("entries") or []
               if e.get("kind") == "video"}
    frames = {i: e.get("frame_images") or [] for i, e in catalog.items() if "frame_images" in e}
    out = []
    for e in entries:
        row = {**e, "first_frame": _first_frame(e["id"], frames), "supports_audio": _supports_audio(
            e["id"], catalog.get(e["id"]))}
        if row["supports_audio"]:
            row["audio_price_text"] = price_text(catalog.get(e["id"]), "video", audio=True) or e.get("price_text", "")
        out.append(row)
    return out


def _supports_audio(model_id: str, entry: dict | None) -> bool:
    """Whether a video model makes its own sound when asked: a Sogni model
    that takes generateAudio, a Runware model with a sound field, any other
    one whose catalog entry says generate_audio (trained adapters: no)."""
    from ..providers import runware, sogni

    if model_id.startswith("custom:"):
        return False
    if sogni.is_sogni(model_id):
        spec = sogni.video_model(model_id)
        return bool(spec and spec.audio_toggle)
    if runware.is_runware(model_id):
        return runware.makes_audio(model_id)
    return (entry or {}).get("generate_audio") is True


def _first_frame(model_id: str, catalog_frames: dict) -> bool:
    """From the polled catalog when it lists the model, else the provider's
    own capabilities (trained adapters: no)."""
    if model_id.startswith("custom:"):
        return False
    if model_id in catalog_frames:
        return "first_frame" in catalog_frames[model_id]
    from ..providers import video_capabilities

    try:
        return bool(video_capabilities(model_id).first_frame)
    except Exception:  # noqa: BLE001
        return False


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
    entries = _trained_lora_entries(db, "image") + _merged("image", lambda: get_image().list_models())
    # A Sogni model given reference pictures runs on one of Sogni's editors:
    # `with_pictures` says which, so Studio can show and pick it.
    from ..providers import sogni

    return [{**e, "with_pictures": sogni.with_pictures(e["id"])} if sogni.is_sogni(e["id"]) else e
            for e in entries]


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

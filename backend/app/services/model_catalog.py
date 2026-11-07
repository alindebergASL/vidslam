"""Live model catalog: polls OpenRouter (and Gemini, when keyed) for the
models available right now, groups them by capability, and remembers the
previous poll so newly appeared models can be flagged in the UI.

Sources
  OpenRouter GET /models          chat / image / audio-output (tts) models
  OpenRouter GET /videos/models   video generators + avatar (lip-sync) models
  Gemini     GET /models          Lyria music models (needs GEMINI_API_KEY)
  static                          ElevenLabs voice + music, Lyria fallbacks

Both OpenRouter listings are public, so the catalog can be browsed before
any key is configured; entries carry `usable` to say whether the app can
actually call them with the current keys.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Optional

import httpx

from ..config import get_settings
from ..providers.base import expand_durations
from . import model_registry as reg
from . import provider_keys

log = logging.getLogger("avs.catalog")
CATALOG_KEY = "catalog.snapshot"

# Lyria models documented for the Gemini API (aistudio.google.com/docs/models).
# Used when no key is set or the Gemini listing can't be read.
_LYRIA_STATIC = [
    ("google/lyria-3.5", "Lyria 3.5", "Full tracks of a couple of minutes, 44.1 kHz stereo MP3. ~$0.08/song."),
    ("google/lyria-3-clip-preview", "Lyria 3 Clip (preview)", "Always 30 s, MP3. ~$0.04/clip."),
    ("google/lyria-3-pro-preview", "Lyria 3 Pro (preview)", "Previous-generation full tracks. ~$0.08/song."),
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _or_headers() -> dict:
    s = get_settings()
    h = {"HTTP-Referer": s.openrouter_site_url, "X-Title": s.openrouter_app_title}
    key = provider_keys.resolve("openrouter")
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def _price(pricing: dict | None) -> dict:
    """Keep OpenRouter's per-unit USD strings as floats (per token / image / …)."""
    out = {}
    for k, v in (pricing or {}).items():
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0:
            out[k] = f
    return out


def _classify_text_model(m: dict) -> list[str]:
    mid = (m.get("id") or "").lower()
    arch = m.get("architecture") or {}
    outs = [x.lower() for x in (arch.get("output_modalities") or [])]
    if not outs:
        # Older shape: "text+image->text"
        modality = (arch.get("modality") or "").lower()
        outs = modality.split("->", 1)[1].split("+") if "->" in modality else ["text"]
    kinds = []
    # Music generators also report audio output; keep them out of Voice.
    if ("audio" in outs or "music" in outs) and ("lyria" in mid or "music" in mid):
        return ["music"]
    if "image" in outs:
        kinds.append("image")
    if "audio" in outs or "speech" in outs:
        kinds.append("tts")
    if outs == ["text"] or (not kinds and "text" in outs):
        kinds.append("chat")
    return kinds


def _paged(client: httpx.Client, url: str, params: dict) -> list[dict]:
    """GET a listing, following offset pagination (limit ≤ 1000)."""
    rows: list[dict] = []
    offset = 0
    for _ in range(20):
        r = client.get(url, headers=_or_headers(), params={**params, "limit": 1000, "offset": offset})
        r.raise_for_status()
        body = r.json()
        page = body.get("data", []) or []
        rows += page
        total = body.get("total_count")
        nxt = (body.get("links") or {}).get("next")
        if not page or (total is not None and len(rows) >= total) or (total is None and not nxt):
            break
        offset += len(page)
    return rows


def _fetch_openrouter_models(client: httpx.Client) -> list[dict]:
    base = get_settings().openrouter_base_url.rstrip("/")
    usable = provider_keys.has("openrouter")
    out = []
    for m in _paged(client, f"{base}/models", {"output_modalities": "all"}):
        mid = m.get("id") or ""
        if not mid or mid.endswith(":batch"):
            # ":batch" routes are for OpenRouter's asynchronous Batch API and
            # can't serve the app's interactive calls.
            continue
        arch = m.get("architecture") or {}
        for kind in _classify_text_model(m):
            out.append({
                "id": mid,
                "kind": kind,
                "name": m.get("name") or mid,
                "provider": "openrouter",
                "description": (m.get("description") or "")[:400],
                "created": m.get("created"),
                "context_length": m.get("context_length"),
                "input_modalities": arch.get("input_modalities") or [],
                "output_modalities": arch.get("output_modalities") or [],
                "pricing": _price(m.get("pricing")),
                "supported_parameters": m.get("supported_parameters") or [],
                "supported_voices": m.get("supported_voices") or [],
                "usable": usable,
            })
    # The Image API has its own listing; merge in anything it adds.
    try:
        seen = {e["id"] for e in out if e["kind"] == "image"}
        for m in _paged(client, f"{base}/images/models", {}):
            mid = m.get("id") or ""
            if mid and mid not in seen:
                out.append({
                    "id": mid, "kind": "image", "name": m.get("name") or mid,
                    "provider": "openrouter", "description": (m.get("description") or "")[:400],
                    "created": m.get("created"), "pricing": _price(m.get("pricing")),
                    "usable": usable,
                })
    except httpx.HTTPError as e:
        log.info("images/models listing unavailable: %s", e)
    return out


def _fetch_openrouter_videos(client: httpx.Client) -> list[dict]:
    base = get_settings().openrouter_base_url.rstrip("/")
    r = client.get(f"{base}/videos/models", headers=_or_headers())
    r.raise_for_status()
    usable = provider_keys.has("openrouter")
    out = []
    for m in r.json().get("data", []) or []:
        mid = m.get("id") or ""
        durations = expand_durations(m.get("supported_durations"))
        frames = m.get("supported_frame_images") or []
        if "avatar" in mid:
            kind = "lipsync"
        elif durations:
            kind = "video"
        else:
            continue  # edit / upscale endpoints, not generators
        out.append({
            "id": mid,
            "kind": kind,
            "name": m.get("name") or mid,
            "provider": "openrouter",
            "description": (m.get("description") or "")[:400],
            "created": m.get("created"),
            "durations": durations,
            "max_seconds": max(durations) if durations else None,
            "frame_images": frames,
            "resolutions": m.get("supported_resolutions") or [],
            "aspect_ratios": m.get("supported_aspect_ratios") or [],
            "generate_audio": m.get("generate_audio"),
            "pricing_skus": m.get("pricing_skus") or {},
            "usable": usable,
        })
    return out


def _fetch_lyria(client: httpx.Client) -> list[dict]:
    s = get_settings()
    gemini_key = provider_keys.resolve("gemini")
    usable = bool(gemini_key)
    found: dict[str, dict] = {}
    if gemini_key:
        url = f"{s.gemini_base_url.rstrip('/')}/models"
        token = None
        for _ in range(10):  # paginate defensively
            params = {"pageSize": 1000}
            if token:
                params["pageToken"] = token
            r = client.get(url, headers={"x-goog-api-key": gemini_key}, params=params)
            r.raise_for_status()
            data = r.json()
            for m in data.get("models", []) or []:
                name = (m.get("name") or "").removeprefix("models/")
                if "lyria" in name and "realtime" not in name:
                    found[f"google/{name}"] = {
                        "id": f"google/{name}",
                        "kind": "music",
                        "name": m.get("displayName") or name,
                        "provider": "gemini",
                        "description": (m.get("description") or "")[:400],
                        "usable": True,
                    }
            token = data.get("nextPageToken")
            if not token:
                break
    if not found:
        for mid, name, desc in _LYRIA_STATIC:
            found[mid] = {"id": mid, "kind": "music", "name": name, "provider": "gemini",
                          "description": desc, "usable": usable}
    return list(found.values())


def _static_entries() -> list[dict]:
    s = get_settings()
    el = provider_keys.has("elevenlabs")
    return [
        {"id": "elevenlabs", "kind": "tts", "name": "ElevenLabs voices", "provider": "elevenlabs",
         "description": f"Per-avatar ElevenLabs voices (model {s.elevenlabs_model_id}).", "usable": el},
        {"id": "elevenlabs", "kind": "music", "name": f"ElevenLabs Music (default: {s.elevenlabs_music_model_id or 'music_v2_5'})",
         "provider": "elevenlabs",
         "description": "Instrumental beds of 3 s–5 min (force_instrumental). Paid ElevenLabs plans only.",
         "usable": el},
        {"id": "elevenlabs/music_v2_5", "kind": "music", "name": "ElevenLabs Music v2.5", "provider": "elevenlabs",
         "description": "Current Eleven Music model. 3 s–5 min per call.", "usable": el},
        {"id": "elevenlabs/music_v2", "kind": "music", "name": "ElevenLabs Music v2", "provider": "elevenlabs",
         "description": "Previous Eleven Music model.", "usable": el},
    ]


_refresh_lock = threading.Lock()


def refresh(*, client: Optional[httpx.Client] = None) -> dict:
    """Poll every source, store and return the new snapshot. A failing
    source keeps its entries from the previous snapshot and reports the
    error, so one outage never empties the picker."""
    with _refresh_lock:
        prev = reg.get_setting(CATALOG_KEY) or {}
        prev_entries = prev.get("entries") or []
        prev_ids = {(e["kind"], e["id"]) for e in prev_entries}
        own = client is None
        client = client or httpx.Client(timeout=30.0)
        entries: list[dict] = []
        errors: dict[str, str] = {}
        sources = {
            "openrouter_models": (_fetch_openrouter_models, {"chat", "image", "tts", "music"}, "openrouter"),
            "openrouter_videos": (_fetch_openrouter_videos, {"video", "lipsync"}, "openrouter"),
            "gemini_models": (_fetch_lyria, {"music"}, "gemini"),
        }
        try:
            for name, (fn, kinds, provider) in sources.items():
                try:
                    entries += fn(client)
                except Exception as e:  # noqa: BLE001
                    log.warning("catalog source %s failed: %s", name, e)
                    errors[name] = str(e)[:300]
                    entries += [x for x in prev_entries
                                if x.get("kind") in kinds and x.get("provider") == provider]
        finally:
            if own:
                client.close()
        entries += _static_entries()
        # A Lyria model served by both OpenRouter and Google appears once,
        # as the OpenRouter entry (one key for everything); the Google route
        # stays available as a fallback for it.
        or_music = {e["id"] for e in entries if e["kind"] == "music" and e["provider"] == "openrouter"}
        for e in entries:
            if e["kind"] == "music" and e["provider"] == "openrouter":
                e["also_via"] = "gemini"
        entries = [e for e in entries
                   if not (e["kind"] == "music" and e["provider"] == "gemini" and e["id"] in or_music)]
        # Flag models that weren't in the previous poll (none on the first poll).
        for e in entries:
            e["new"] = bool(prev_ids) and (e["kind"], e["id"]) not in prev_ids
        snap = {
            "fetched_at": _now_iso(),
            "entries": entries,
            "errors": errors,
            "counts": {k: sum(1 for e in entries if e["kind"] == k) for k in reg.KINDS},
            "new_count": sum(1 for e in entries if e["new"]),
        }
        reg.put_setting(CATALOG_KEY, snap)
        _alert_missing_active(snap, prev_entries)
        return snap


def _alert_missing_active(snap: dict, prev_entries: list[dict]) -> None:
    """Alert when a model someone selected was listed last poll but isn't
    now (retired / renamed) and its source answered this time."""
    from . import alerts

    if not prev_entries:
        return
    now = {(e["kind"], e["id"]) for e in snap["entries"]}
    before = {(e["kind"], e["id"]) for e in prev_entries}
    failed = snap.get("errors") or {}
    for kind in reg.KINDS:
        sel = reg.selected(kind)
        if not sel or sel.startswith("mock/"):
            continue
        if (kind, sel) in before and (kind, sel) not in now and not failed:
            alerts.send(
                f"model-gone:{kind}:{sel}",
                f"Active {kind} model {sel} is no longer listed",
                "Jobs using it will likely fail. Pick a replacement in Settings → Models.",
                severity="warning",
            )


def _live_usable(snap: Optional[dict]) -> Optional[dict]:
    """`usable` is baked in at poll time; re-derive it from the keys now
    configured, so a key saved in Settings → API keys (or cleared) shows at
    once without waiting for the next poll."""
    if not snap:
        return snap
    known = set(provider_keys.KEYS)
    have: dict[str, bool] = {}

    def _has(p: str) -> bool:
        if p not in have:
            have[p] = provider_keys.has(p)
        return have[p]

    for e in snap.get("entries") or []:
        p = e.get("provider")
        if p not in known:
            continue
        alt = e.get("also_via")
        e["usable"] = _has(p) or bool(alt in known and _has(alt))
    return snap


def get_snapshot() -> Optional[dict]:
    return _live_usable(reg.get_setting(CATALOG_KEY))


def find(kind: str, model_id: str) -> Optional[dict]:
    snap = get_snapshot() or {}
    for e in snap.get("entries") or []:
        if e["kind"] == kind and e["id"] == model_id:
            return e
    return None


def is_stale(snap: Optional[dict], hours: float) -> bool:
    if not snap or not snap.get("fetched_at"):
        return True
    try:
        t = datetime.fromisoformat(snap["fetched_at"])
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - t).total_seconds() > hours * 3600


# ---------- background poller ----------

_stop = threading.Event()
_thread: Optional[threading.Thread] = None


def _loop(hours: float) -> None:
    # First poll shortly after boot if the stored snapshot is stale.
    if _stop.wait(5):
        return
    while not _stop.is_set():
        try:
            if is_stale(get_snapshot(), hours):
                snap = refresh()
                log.info("model catalog refreshed: %s (new: %s)", snap["counts"], snap["new_count"])
        except Exception:  # noqa: BLE001
            log.exception("model catalog refresh failed")
        if _stop.wait(max(60.0, hours * 3600 / 6)):  # check ~6× per interval
            return


def start_poller() -> bool:
    global _thread
    hours = get_settings().model_catalog_refresh_hours
    if hours <= 0 or (_thread and _thread.is_alive()):
        return False
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(hours,), name="model-catalog", daemon=True)
    _thread.start()
    return True


def stop_poller() -> None:
    _stop.set()

"""Sogni (sogni.ai) image + video models over the REST workflow API.

Model ids are ``sogni/<selector>`` with Sogni's tool-level selectors
(``sogni/krea-2-turbo``, ``sogni/ltx25``, ``sogni/seedance2-5`` …), not the
worker ids the public model catalog lists. The prefix is stripped before
anything is sent.

Every generation is a one-step durable workflow (docs.sogni.ai,
api-reference/workflows):

  POST /v1/creative-agent/workflows   {"input": {"title", "steps": [{"id",
                                       "toolName", "arguments"}]},
                                       "media_references": [...], ...}
                                      -> 201 data.workflow.workflowId
  GET  /v1/creative-agent/workflows/{id}         -> snapshot (data.workflow)
  GET  /v1/creative-agent/workflows/{id}/events  -> persisted event log

Top-level body keys are snake_case and unknown keys are a 400. Sogni has no
webhooks for REST workflows, so video jobs are polled by video_jobs' sweeper.
Result URLs are signed for ~48 h: they're downloaded straight away, as is,
without our Authorization header.

Billing is in Spark (1 Spark = $0.005); the workflow snapshot carries no
per-job cost, so spend is recorded from the price table below, as an estimate.
"""
from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional

import httpx

from ..config import get_settings
from ..services import provider_keys
from .base import (
    ImageProvider,
    ImageRef,
    JobStatus,
    ModelInfo,
    SubmittedJob,
    VideoCapabilities,
    VideoProvider,
)

log = logging.getLogger("avs.sogni")

PREFIX = "sogni/"
STEP_ID = "s1"
USER_AGENT = "vidslam/1.0"
APP_SOURCE = "vidslam"
WORKFLOWS = "/v1/creative-agent/workflows"

# Patched in tests so retries and polling don't sleep.
_sleep = time.sleep


def is_sogni(model: Optional[str]) -> bool:
    return bool(model) and str(model).startswith(PREFIX)


def selector(model: str) -> str:
    """`sogni/krea-2-turbo` -> `krea-2-turbo` (bare selectors pass through)."""
    return model[len(PREFIX):] if is_sogni(model) else model


# ---------- models + prices (one place) ----------

# 1 Spark = $0.005 (docs.sogni.ai pricing).
SPARK_USD = 0.005

# Families group models in the pickers. "Next-Gen" is Sogni's own category
# (the "Next-Gen Diffusion" model picker in app.sogni.ai).
NEXT_GEN = "Next-Gen"
OPEN = "open"
COMMUNITY = "SDXL community"
VENDOR = "vendor"
FAMILY_ORDER = {NEXT_GEN: 0, OPEN: 1, VENDOR: 2, COMMUNITY: 3}


@dataclass(frozen=True)
class ImageModel:
    selector: str
    name: str
    note: str
    spark: float  # per image at ~1024px; documented where noted, else an estimate
    premium: bool = False  # vendor model: always bills (bought) Premium Spark
    family: str = OPEN
    uncensored: bool = False  # Sogni tags it uncensored (needs the content filter off)
    keep: bool = False  # offer it even when the published schema doesn't list it yet
    # Takes a source image (sourceImageIndex + starting_image_strength).
    # Sogni's schema documents it for Z-Image, Z-Image Turbo and Krea 2
    # Turbo, but through the workflow API the uploaded image isn't applied:
    # tested 2026-11-07 on Krea 2 Turbo, MOSS-Image 8-Step and Chroma v46
    # Flash at strength 0.35 and 0.75, nothing of the source came through
    # and Sogni's billing request showed startingImageStrength null. So no
    # model is marked yet, and ambient's "same place" stills go to the
    # fallback picture model instead.
    img2img: bool = False


@dataclass(frozen=True)
class VideoModel:
    selector: str
    name: str
    note: str
    min_s: int
    max_s: int
    spark_per_second: float  # estimate at 720p unless noted
    animate: bool = False  # in animate_photo: exact first frame
    last_frame: bool = False  # also pins the last frame (frameRole "both"): closed loops
    references: bool = False  # takes loose reference images (referenceImageIndices)
    negative: bool = False  # accepts negativePrompt (LTX / WAN only)
    premium: bool = False
    text: bool = True  # in generate_video: works without a picture
    flf_only: bool = False  # needs first AND last frame (MiniMax H3 flf2v)
    needs_reference: bool = False  # needs at least one reference picture (MiniMax H3 r2v)
    uncensored: bool = False
    audio_toggle: bool = True  # accepts generateAudio (HappyHorse doesn't)


# Prices: documented figures from docs.sogni.ai/pricing and the model pages
# (Nov 2026) where marked "documented"; the rest are estimates. Vendor models
# bill Premium Spark, which Free Spark can't pay for.
_IMAGES: tuple[ImageModel, ...] = (
    # Next-Gen Diffusion
    ImageModel("krea-2-turbo", "Krea 2 Turbo", "Fast and cheap. 2.43 Spark at 1024px (documented).", 2.43,
               family=NEXT_GEN),
    ImageModel("moss-image", "MOSS-Image", "OpenMOSS 5B, 50 steps. ~8.9 Spark at 1024px (documented); more "
               "at larger sizes.", 8.86, family=NEXT_GEN, uncensored=True),
    ImageModel("moss-image-8step", "MOSS-Image 8-Step", "Fast distilled MOSS-Image. ~1.3 Spark at 1024px "
               "(documented). Punchier colour than the 50-step model.", 1.25, family=NEXT_GEN, uncensored=True),
    ImageModel("dark-beast-krea2", "Dark Beast Krea 2", "Community fine-tune of Krea 2. 5.87 Spark at 1024px "
               "(documented).", 5.87, family=NEXT_GEN, uncensored=True),
    ImageModel("dark-beast-z-turbo", "Dark Beast Z-Image Turbo", "Community fine-tune of Z-Image Turbo. 1.60 "
               "Spark at 1024px (documented).", 1.60, family=NEXT_GEN, uncensored=True),
    ImageModel("z-turbo", "Z-Image Turbo", "Fast photoreal. 1.60 Spark at 1024px (documented).", 1.60,
               family=NEXT_GEN, uncensored=True),
    ImageModel("z-image", "Z-Image", "4.00 Spark at 1024px (documented).", 4.00, family=NEXT_GEN),
    ImageModel("chroma-detail", "Chroma v48 Detail", "Detail-calibrated Chroma. 4.18 Spark at 1024px "
               "(documented).", 4.18, family=NEXT_GEN, uncensored=True),
    ImageModel("chroma1-hd", "Chroma1 HD", "Final high-resolution Chroma. Estimate ~4 Spark.", 4.0,
               family=NEXT_GEN, uncensored=True),
    ImageModel("chroma-v46-flash", "Chroma v46 Flash", "Fast Chroma. 2.20 Spark at 1024px (documented).", 2.20,
               family=NEXT_GEN, uncensored=True),
    ImageModel("qwen-2512", "Qwen Image 2512", "Good with text in images. 4.15 Spark at 1024px (documented).",
               4.15, family=NEXT_GEN),
    ImageModel("qwen-2512-lightning", "Qwen Image 2512 Lightning", "Faster Qwen. 2.07 Spark at 1024px "
               "(documented).", 2.07, family=NEXT_GEN),
    # other open models
    ImageModel("flux-3-klein", "FLUX.3 Klein", "Small FLUX.3. 1.80 Spark at 1024px (documented).", 1.80),
    # vendor models: bought Premium Spark only
    ImageModel("gpt-image-2", "GPT Image 2 (via Sogni)", "Premium Spark. Estimate ~12 Spark.", 12.0,
               premium=True, family=VENDOR),
    ImageModel("gpt-image-2.5-sunburst", "GPT Image 2.5 Sunburst (via Sogni)", "Premium Spark. Estimate ~30 "
               "Spark.", 30.0, premium=True, family=VENDOR),
    ImageModel("gpt-image-2.5-flare", "GPT Image 2.5 Flare (via Sogni)", "Premium Spark. Estimate ~20 Spark.",
               20.0, premium=True, family=VENDOR),
    ImageModel("gpt-image-2.6-sunburst", "GPT Image 2.6 Sunburst (via Sogni)", "Premium Spark. Estimate ~30 "
               "Spark.", 30.0, premium=True, family=VENDOR),
    ImageModel("gpt-image-2.6-flare", "GPT Image 2.6 Flare (via Sogni)", "Premium Spark. Estimate ~20 Spark.",
               20.0, premium=True, family=VENDOR),
    # Not in the published schema, but Sogni knows it: on Free Spark it
    # answers 402 "requires Premium Spark Points" naming this model (Nov 2026).
    ImageModel("seedream-5-0-pro-uncensored", "Seedream 5.0 Pro (uncensored)", "Premium Spark; needs the "
               "one-time likeness agreement in the Sogni app. Estimate ~10 Spark.", 10.0, premium=True,
               family=VENDOR, uncensored=True, keep=True),
    # community checkpoints (SDXL / Pony / Illustrious): ~1 Spark each (documented tier)
    ImageModel("one-obsession-v22", "One Obsession v2.2", "Anime community checkpoint. 1.07 Spark "
               "(documented).", 1.07, family=COMMUNITY),
    ImageModel("pony-v7", "CyberRealistic Pony v7", "Community checkpoint. ~1 Spark.", 1.0, family=COMMUNITY,
               uncensored=True),
    ImageModel("hyphoria-real", "Hyphoria Real", "Photoreal community checkpoint. ~1 Spark.", 1.0,
               family=COMMUNITY),
)

# Durations from the docs: LTX 2-20; Seedance 2.0/Mini 4-15; Seedance 2.5
# 4-30; HappyHorse 3-15; Wan 3 2-30; MiniMax H3 5.17-15.08 (a 17-frame grid
# at 24 fps, so 6-15 whole seconds). Wan 2.2 has no documented range: 2-10.
# animate/text below describe the bundled listing (they only apply when no
# listing can be read; otherwise the tools that list a model decide).
# negativePrompt: LTX and Wan 2.2 only (the manifest: not H3, Seedance,
# HappyHorse or Wan 3). generateAudio: LTX, MiniMax H3, Seedance, Wan 3
# (not Wan 2.2 or HappyHorse). "uncensored" only where Sogni says so.
_H3 = dict(min_s=6, max_s=15)
_I2V = dict(animate=True, text=False, **_H3)
_FLF = dict(animate=True, text=False, last_frame=True, flf_only=True, **_H3)
_R2V = dict(references=True, needs_reference=True, **_H3)
_VIDEOS: tuple[VideoModel, ...] = (
    VideoModel("ltx25", "LTX-2.5", "Sogni's default video model. Text, first frame, or first + last frame "
               "(closed loops). 2–20 s.", 2, 20, 7.4, animate=True, last_frame=True, negative=True),
    VideoModel("ltx23", "LTX-2.3", "Previous LTX. Text or first frame. 2–20 s. ~17.5 Spark per 5 s.", 2, 20, 3.5,
               animate=True, negative=True),
    VideoModel("wan22", "Wan 2.2", "Text or first frame, no audio. 2–10 s (assumed).", 2, 10, 4.0, animate=True,
               negative=True, audio_toggle=False),
    # MiniMax H3: text (t2v), first frame (i2v), first + last frame (flf2v), reference (r2v)
    VideoModel("minimax-h3-t2v", "MiniMax H3", "Text to video, 20 steps. ~16 Spark/s (documented).",
               spark_per_second=16.0, **_H3),
    VideoModel("minimax-h3-t2v-turbo", "MiniMax H3 Turbo", "Text to video, 4-step. ~6 Spark/s.",
               spark_per_second=6.0, **_H3),
    VideoModel("minimax-h3-fasth3-t2v-turbo", "MiniMax H3 FastH3", "Text to video, fastest H3. 4 Spark/s "
               "(documented).", spark_per_second=4.0, **_H3),
    VideoModel("minimax-h3-fasth3-t2v-turbo-2stage", "MiniMax H3 FastH3 Two-Stage", "Text to video, rendered "
               "then upscaled 2x. 16 Spark/s at 2K (documented).", spark_per_second=16.0, **_H3),
    VideoModel("minimax-h3-i2v", "MiniMax H3", "Needs a first frame. ~16 Spark/s (documented).",
               spark_per_second=16.0, **_I2V),
    VideoModel("minimax-h3-i2v-turbo", "MiniMax H3 Turbo", "Needs a first frame. 4-step. ~6 Spark/s "
               "(documented).", spark_per_second=6.0, **_I2V),
    VideoModel("minimax-h3-fasth3-i2v-turbo", "MiniMax H3 FastH3", "Needs a first frame. Fastest H3. "
               "4 Spark/s (documented).", spark_per_second=4.0, **_I2V),
    VideoModel("minimax-h3-fasth3-i2v-turbo-2stage", "MiniMax H3 FastH3 Two-Stage", "Needs a first frame. "
               "Upscaled 2x. 16 Spark/s at 2K (documented).", spark_per_second=16.0, **_I2V),
    VideoModel("minimax-h3-flf2v", "MiniMax H3", "First and last frame: closed loops (one picture is both "
               "ends). ~16 Spark/s (documented).", spark_per_second=16.0, **_FLF),
    VideoModel("minimax-h3-flf2v-turbo", "MiniMax H3 Turbo", "First and last frame, 4-step. ~6 Spark/s "
               "(documented).", spark_per_second=6.0, **_FLF),
    VideoModel("minimax-h3-fasth3-flf2v-turbo", "MiniMax H3 FastH3", "First and last frame: closed loops on "
               "Free Spark. 4 Spark/s (documented).", spark_per_second=4.0, **_FLF),
    VideoModel("minimax-h3-fasth3-flf2v-turbo-2stage", "MiniMax H3 FastH3 Two-Stage", "First and last frame, "
               "upscaled 2x. 16 Spark/s at 2K (documented).", spark_per_second=16.0, **_FLF),
    VideoModel("minimax-h3-r2v", "MiniMax H3 Reference", "Needs at least one reference picture (a loose "
               "guide, not an exact frame). ~16 Spark/s plus reference input.", spark_per_second=16.0, **_R2V),
    VideoModel("minimax-h3-r2v-turbo", "MiniMax H3 Turbo Reference", "Needs a reference picture. ~6 Spark/s.",
               spark_per_second=6.0, **_R2V),
    VideoModel("minimax-h3-r2v-2stage", "MiniMax H3 Two-Stage Reference", "Needs a reference picture. ~16+ "
               "Spark/s.", spark_per_second=18.0, **_R2V),
    VideoModel("minimax-h3-r2v-balanced-2stage", "MiniMax H3 Balanced Two-Stage Reference", "Needs a reference "
               "picture. ~10+ Spark/s.", spark_per_second=12.0, **_R2V),
    VideoModel("minimax-h3-fasth3-r2v-turbo", "MiniMax H3 FastH3 Reference", "Needs a reference picture. "
               "~6 Spark/s.", spark_per_second=6.0, **_R2V),
    VideoModel("minimax-h3-fasth3-r2v-turbo-2stage", "MiniMax H3 FastH3 Two-Stage Reference", "Needs a "
               "reference picture. 6-26 Spark/s by size.", spark_per_second=12.0, **_R2V),
    # vendor models: bought Premium Spark only
    VideoModel("seedance2-5", "Seedance 2.5", "Premium Spark. 4–30 s. Picture is a loose reference, not an exact "
               "first frame. ~60 Spark/s at 720p.", 4, 30, 60.1, references=True, premium=True),
    VideoModel("seedance2-5-uncensored", "Seedance 2.5 Uncensored", "Premium Spark; needs the likeness "
               "agreement. 4–30 s. Loose picture reference. ~69 Spark/s at 720p (documented).", 4, 30, 69.3,
               references=True, premium=True, uncensored=True),
    VideoModel("seedance2", "Seedance 2.0", "Premium Spark. 4–15 s. Loose picture reference. ~39 Spark/s.",
               4, 15, 39.3, references=True, premium=True),
    VideoModel("seedance2-mini", "Seedance 2.0 Mini", "Premium Spark. 4–15 s. Loose picture reference. ~20 Spark/s.",
               4, 15, 19.7, references=True, premium=True),
    VideoModel("seedance2-mini-uncensored", "Seedance 2.0 Mini Uncensored", "Premium Spark; needs the likeness "
               "agreement. 4–15 s. ~38 Spark/s at 720p (documented).", 4, 15, 37.8, references=True,
               premium=True, uncensored=True),
    # HappyHorse: Sogni lists i2v and r2v under both video tools, but i2v
    # needs its first frame and r2v 1-9 reference pictures (its schema text).
    VideoModel("happyhorse-1.1-i2v", "HappyHorse 1.1", "Premium Spark. Needs a first frame. 3–15 s. ~182 Spark "
               "per clip.", 3, 15, 36.4, animate=True, text=False, premium=True, audio_toggle=False),
    VideoModel("happyhorse-1.1-t2v", "HappyHorse 1.1 Text", "Premium Spark. Text to video. 3–15 s. ~182 Spark "
               "per 5 s at 720p (documented).", 3, 15, 36.4, premium=True, audio_toggle=False),
    VideoModel("happyhorse-1.1-r2v", "HappyHorse 1.1 Reference", "Premium Spark. Needs 1-9 reference pictures "
               "(loose guides). 3–15 s.", 3, 15, 36.4, references=True, needs_reference=True, premium=True,
               audio_toggle=False),
    VideoModel("wan3.0-video", "Wan 3.0 (via Sogni)", "Premium Spark. First frame. 2–30 s. Estimate ~40 Spark/s.",
               2, 30, 40.0, animate=True, premium=True),
    VideoModel("wan3.0-spicy-video", "Wan 3.0 Enhanced (uncensored, via Sogni)", "Premium Spark; needs the "
               "likeness agreement. First frame. 2–30 s. 32 Spark/s at 720p (documented).", 2, 30, 32.0,
               animate=True, premium=True, uncensored=True),
)
_IMAGE_INFO = {m.selector: m for m in _IMAGES}
_VIDEO_INFO = {m.selector: m for m in _VIDEOS}


# ---------- which selectors Sogni accepts (synced) ----------

# Sogni has no endpoint that lists workflow model selectors; its published
# tool schemas (@sogni-ai/sogni-protocol, manifests/generation-tools.json)
# carry them as enums, and the API validates against the same enums. The
# list is refreshed with the model catalog (SOGNI_TOOLS_MANIFEST_URL), kept
# in DATA_DIR, and falls back to the copy bundled here.
LISTED_TOOLS = ("generate_image", "generate_video", "animate_photo")
_BUNDLED = Path(__file__).with_name("sogni_tools.json")
_SELECTOR_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_listed: Optional[dict] = None
_listed_key: object = None  # which cache file state _listed was read from
_models: Optional[tuple] = None  # (listing, image_models, video_models, by selector, by selector)
_listed_lock = threading.Lock()


def _reset_listed() -> None:
    """Forget the loaded list (tests)."""
    global _listed, _listed_key, _models
    with _listed_lock:
        _listed = _listed_key = _models = None


def _cache_path() -> Path:
    return Path(get_settings().data_dir) / "sogni_tools.json"


def _valid_listing(data: object) -> bool:
    """All three tools listed, each a list of selectors. A tool that comes
    back empty is far more likely a schema change we can't parse than Sogni
    dropping it, so that's refused rather than kept as the last good list."""
    tools = data.get("tools") if isinstance(data, dict) else None
    return (isinstance(tools, dict) and all(tools.get(t) for t in LISTED_TOOLS)
            and all(isinstance(v, list) and all(isinstance(s, str) for s in v) for v in tools.values()))


def _read_listing(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if _valid_listing(data) else None


def _version(data: dict, field: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", str(data.get(field) or "")))


def _newer(synced: Optional[dict], bundled: Optional[dict]) -> Optional[dict]:
    """The synced list unless the bundled one is newer: a later package
    release (a CDN that lagged behind a deploy), then a later manifest
    version, then (nothing to compare) a later fetch."""
    if not synced or not bundled:
        return synced or bundled
    for field in ("package", "version"):
        vs, vb = _version(synced, field), _version(bundled, field)
        if vs and vb and len(vs) == len(vb) and vs != vb:
            return synced if vs > vb else bundled
    return synced if str(synced.get("fetched_at") or "") >= str(bundled.get("fetched_at") or "") else bundled


def _cache_state() -> object:
    """Identifies the synced file as it is now (None: syncing is off or
    there's no file), so every process picks up another one's sync."""
    if not get_settings().sogni_tools_manifest_url:
        return None
    try:
        path = _cache_path()
        st = path.stat()
    except OSError:
        return None
    return (str(path), st.st_mtime_ns, st.st_size)


def listed() -> dict:
    """{"tools": {tool: [selector, ...]}, "source": ..., "fetched_at": ...}:
    the newer of the last successful sync (DATA_DIR, shared by every
    process) and the snapshot bundled here."""
    global _listed, _listed_key
    key = _cache_state()
    with _listed_lock:
        if _listed is not None and key == _listed_key:
            return _listed
        _listed = _newer(_read_listing(Path(key[0])) if key else None, _read_listing(_BUNDLED)) or {"tools": {}}
        _listed_key = key
        return _listed


def parse_manifest(doc: dict) -> dict[str, list[str]]:
    """The model enums of the tools we use, from a generation-tools manifest."""
    tools: dict[str, list[str]] = {}
    for t in (doc or {}).get("tools") or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else {}
        name = t.get("name") or fn.get("name")
        if name not in LISTED_TOOLS:
            continue
        params = t.get("parameters") or t.get("input_schema") or fn.get("parameters") or {}
        key = "model" if name == "generate_image" else "videoModel"
        enum = ((params.get("properties") or {}).get(key) or {}).get("enum") or []
        tools[name] = [s for s in enum if isinstance(s, str) and _SELECTOR_RE.fullmatch(s)]
    return tools


def refresh_listed(client: Optional[httpx.Client] = None) -> dict:
    """Fetch Sogni's current tool manifest and keep its model lists. Raises
    (and keeps the previous list) when it can't be read or looks wrong."""
    global _listed, _listed_key
    url = get_settings().sogni_tools_manifest_url
    if not url:
        return listed()
    own = client is None
    client = client or httpx.Client(timeout=20.0)
    try:
        r = client.get(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        r.raise_for_status()
        doc = r.json()
    finally:
        if own:
            client.close()
    tools = parse_manifest(doc)
    # The release actually served (jsDelivr names it; else a pinned @version).
    pinned = re.search(r"@(\d[^/]*)/", url)
    package = r.headers.get("x-jsd-version") or (pinned.group(1) if pinned else "")
    data = {"source": url, "package": package, "version": str((doc or {}).get("version") or ""),
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tools": tools}
    if not _valid_listing(data):
        counts = ", ".join(f"{t} {len(tools.get(t) or [])}" for t in LISTED_TOOLS)
        raise ValueError(f"Sogni tool manifest is missing model lists ({counts})")
    # Written whole, then swapped in: readers never see half a file. If it
    # can't be stored, nothing changes anywhere (every process reads the
    # file) and the catalog reports the error.
    path = _cache_path()
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise OSError(f"couldn't store the Sogni model list: {e}") from e
    with _listed_lock:
        _listed = _listed_key = None  # re-read: the same choice every process makes
    return data


def _pretty(sel: str) -> str:
    words = re.sub(r"[-_]+", " ", sel).split()
    return " ".join(w.upper() if w in {"xl", "sdxl", "hd"} else (w if any(c.isdigit() for c in w) else
                                                                    w.capitalize()) for w in words)


def _generic_image(sel: str) -> ImageModel:
    """A selector Sogni lists that we have no notes for yet."""
    if sel.startswith(("gpt-image", "seedream", "nano-banana", "imagen", "ideogram", "midjourney")):
        return ImageModel(sel, f"{_pretty(sel)} (via Sogni)", "Newly listed by Sogni. Premium Spark; price "
                          "is an estimate.", 20.0, premium=True, family=VENDOR)
    if sel.endswith("-xl") or "pony" in sel or "sdxl" in sel:
        return ImageModel(sel, _pretty(sel), "Community checkpoint. ~1 Spark.", 1.0, family=COMMUNITY)
    return ImageModel(sel, _pretty(sel), "Newly listed by Sogni; price is an estimate.", 5.0)


def _generic_video(sel: str) -> VideoModel:
    """A selector Sogni lists that we have no notes for yet (which tools
    list it still decides first frame / text, in video_models)."""
    flf = "flf2v" in sel
    r2v = "r2v" in sel
    i2v = "i2v" in sel  # needs its first frame, even if listed under generate_video too
    vendor = sel.startswith(("seedance", "happyhorse", "wan3", "kling", "veo", "sora", "runway"))
    note = "Newly listed by Sogni; length and price are estimates."
    if sel.startswith("minimax-h3"):
        return VideoModel(sel, _pretty(sel), note, spark_per_second=16.0, last_frame=flf, flf_only=flf,
                          references=r2v, needs_reference=r2v, text=not (i2v or flf), **_H3)
    return VideoModel(sel, _pretty(sel), ("Premium Spark. " if vendor else "") + note, 2, 10,
                      40.0 if vendor else 8.0, last_frame=flf, flf_only=flf, premium=vendor, text=not (i2v or flf),
                      references=r2v or sel.startswith("seedance"), needs_reference=r2v,
                      audio_toggle=not sel.startswith(("happyhorse", "wan2")))


def _build_images(tools: dict) -> tuple[ImageModel, ...]:
    sels = list(dict.fromkeys(tools.get("generate_image") or []))
    if not sels:  # no listing at all: the curated table
        models = list(_IMAGES)
    else:
        seen = set(sels)
        models = [_IMAGE_INFO.get(s) or _generic_image(s) for s in sels]
        models += [m for m in _IMAGES if m.keep and m.selector not in seen]
    pos = {m.selector: i for i, m in enumerate(_IMAGES)}
    return tuple(sorted(models, key=lambda m: (FAMILY_ORDER.get(m.family, 9), pos.get(m.selector, 999))))


def _build_videos(tools: dict) -> tuple[VideoModel, ...]:
    gen = list(tools.get("generate_video") or [])
    ani = list(tools.get("animate_photo") or [])
    if not gen and not ani:  # no listing at all: the curated table, as bundled
        return _VIDEOS
    out = []
    for sel in dict.fromkeys(gen + ani):
        base = _VIDEO_INFO.get(sel) or _generic_video(sel)
        # Listed under animate_photo = takes an exact first frame, except
        # reference models (their pictures are loose guides); listed under
        # generate_video = works from text, unless our notes say it needs
        # a picture (HappyHorse i2v is listed there too).
        animate = sel in ani and not base.needs_reference
        out.append(replace(base, animate=animate, text=sel in gen and base.text,
                           last_frame=animate and base.last_frame, flf_only=animate and base.flf_only))
    pos = {m.selector: i for i, m in enumerate(_VIDEOS)}
    return tuple(sorted(out, key=lambda m: (m.premium, pos.get(m.selector, 999))))


def _model_tables() -> tuple:
    """(image models, video models, images by selector, videos by selector)
    for the current listing, built once per listing."""
    global _models
    data = listed()
    cached = _models
    if cached is not None and cached[0] is data:
        return cached[1:]
    images, videos = _build_images(data["tools"]), _build_videos(data["tools"])
    cached = (data, images, videos, {m.selector: m for m in images}, {m.selector: m for m in videos})
    _models = cached
    return cached[1:]


def image_models() -> tuple[ImageModel, ...]:
    """Every image model Sogni accepts on generate_image, Next-Gen first."""
    return _model_tables()[0]


def video_models() -> tuple[VideoModel, ...]:
    """Every video model on generate_video or animate_photo. Which tool
    lists it decides whether it takes an exact first frame (animate_photo)
    and whether it works from text alone (generate_video)."""
    return _model_tables()[1]


def video_frames(m: VideoModel) -> str:
    """What a video model starts from, in a few words."""
    if m.flf_only:
        return "first + last frame, needs a picture"
    if m.last_frame:
        return "first + last frame"
    if m.animate and not m.text:
        return "first frame, needs a picture"
    if m.animate:
        return "first frame"
    if m.needs_reference:
        return "needs a reference picture"
    if m.references:
        return "reference pictures"
    return "text only"


def display_name(m: "ImageModel | VideoModel") -> str:
    """How pickers show a Sogni model: name, family and what it needs."""
    bits = ["Sogni"]
    if isinstance(m, ImageModel) and m.family in (NEXT_GEN, COMMUNITY):
        bits.append(m.family)
    if isinstance(m, VideoModel):
        bits += [f"{m.min_s}–{m.max_s}s", video_frames(m)]
    if m.premium:
        bits.append("Premium Spark")
    if m.uncensored:
        bits.append("uncensored")
    return f"{m.name} ({' · '.join(bits)})"


def image_model(model: str) -> Optional[ImageModel]:
    return _model_tables()[2].get(selector(model))


def video_model(model: str) -> Optional[VideoModel]:
    return _model_tables()[3].get(selector(model))


def image_cost_usd(model: str) -> float:
    """Estimated USD for one image; COST_IMAGE_PER_ITEM for unknown models.
    (Our notes still price a model Sogni has stopped listing: its last jobs.)"""
    m = image_model(model) or _IMAGE_INFO.get(selector(model))
    return round(m.spark * SPARK_USD, 6) if m else get_settings().cost_image_per_item


def video_rate_usd(model: str) -> float:
    """Estimated USD per second of video; COST_VIDEO_PER_SECOND if unknown."""
    m = video_model(model) or _VIDEO_INFO.get(selector(model))
    return round(m.spark_per_second * SPARK_USD, 6) if m else get_settings().cost_video_per_second


def video_cost_usd(model: str, seconds: float) -> float:
    return round(video_rate_usd(model) * max(float(seconds or 0.0), 0.0), 6)


def takes_source_image(model: str) -> bool:
    m = image_model(model)
    return bool(m and m.img2img)


def capabilities_for(model: str) -> VideoCapabilities:
    m = video_model(model)
    if m is None:
        return VideoCapabilities(durations=[5], first_frame=False, last_frame=False, known=False)
    return model_capabilities(m)


def model_capabilities(m: VideoModel) -> VideoCapabilities:
    return VideoCapabilities(durations=list(range(m.min_s, m.max_s + 1)),
                             first_frame=m.animate, last_frame=m.animate and m.last_frame,
                             needs_first_frame=m.animate and not m.text,
                             both_frames_only=m.animate and m.flf_only,
                             needs_reference=m.needs_reference)


# ---------- HTTP client ----------


class SogniError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, code: Any = None,
                 retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.retry_after = retry_after


def _retry_after(r: httpx.Response, body: dict) -> Optional[float]:
    for raw in (r.headers.get("Retry-After"), body.get("retryAfter"),
                (body.get("details") or {}).get("retryAfterSeconds")
                if isinstance(body.get("details"), dict) else None):
        try:
            if raw is not None:
                return max(float(raw), 0.0)
        except (TypeError, ValueError):
            continue
    return None


class SogniClient:
    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key or provider_keys.resolve("sogni")
        if not self.api_key:
            raise RuntimeError("SOGNI_API_KEY missing for the Sogni provider (Settings → API keys)")

    @property
    def base(self) -> str:
        return get_settings().sogni_base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": USER_AGENT,
            "X-App-Source": APP_SOURCE,
            "Accept": "application/json",
        }

    @staticmethod
    def _json_ok(r: httpx.Response) -> dict:
        """Parse a 2xx body; a proxy/HTML page with a 2xx status becomes a
        SogniError rather than a bare ValueError (and no raw HTML in text)."""
        try:
            body = r.json()
        except ValueError:
            raise SogniError(f"Sogni {r.status_code}: unexpected non-JSON response",
                             status=r.status_code) from None
        if not isinstance(body, dict):
            raise SogniError(f"Sogni {r.status_code}: unexpected response shape", status=r.status_code)
        return body

    @staticmethod
    def _workflow_of(body: dict) -> dict:
        data = body.get("data")
        if not isinstance(data, dict):
            data = {}
        wf = data.get("workflow")
        return wf if isinstance(wf, dict) else data

    def _scrub(self, text: str) -> str:
        return text.replace(self.api_key, "***") if self.api_key else text

    def error_for(self, r: httpx.Response) -> SogniError:
        """A readable error from Sogni's envelope {status:"error", errorCode,
        message, details?, retryAfter?}. Never contains the key."""
        try:
            body = r.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        code = body.get("errorCode")
        msg = str(body.get("message") or (r.text or "").strip()[:200] or r.reason_phrase or "error")
        if r.status_code == 402:
            vendor = (body.get("details") or {}).get("vendorModels") if isinstance(body.get("details"), dict) else None
            msg = ("not enough Sogni credit (Spark)"
                   + (f" for premium models {', '.join(map(str, vendor))}" if vendor else "")
                   + f": {msg}")
        label = f"Sogni {r.status_code}" + (f" {code}" if code is not None else "")
        return SogniError(self._scrub(f"{label}: {msg}")[:600], status=r.status_code, code=code,
                          retry_after=_retry_after(r, body))

    def start(self, *, title: str, tool: str, arguments: dict,
              media_references: Optional[list[dict]] = None) -> dict:
        """Start a one-step workflow; returns the workflow snapshot. One fresh
        Idempotency-Key per call, reused only when retrying this same start
        (network errors, 429 within the wait cap, 5xx)."""
        body: dict = {
            "input": {"title": title[:120] or "vidslam", "steps": [
                {"id": STEP_ID, "toolName": tool, "arguments": arguments},
            ]},
            "token_type": "auto",
            "app_source": APP_SOURCE,
            "safe_content_filter": bool(get_settings().sogni_safe_content_filter),
        }
        if media_references:
            body["media_references"] = media_references
        headers = {**self._headers(), "Content-Type": "application/json",
                   "Idempotency-Key": str(uuid.uuid4())}
        cap = get_settings().sogni_max_retry_after_seconds
        attempts = 3
        for attempt in range(attempts):
            last = attempt == attempts - 1
            try:
                with httpx.Client(timeout=60.0) as client:
                    r = client.post(f"{self.base}{WORKFLOWS}", headers=headers, json=body)
            except httpx.TransportError as e:
                if last:
                    raise SogniError(f"Sogni unreachable: {type(e).__name__}") from None
                _sleep(1.0 + attempt + random.random())
                continue
            if r.status_code < 400:
                wf = self._workflow_of(self._json_ok(r))
                if not wf.get("workflowId"):
                    raise SogniError("Sogni start returned no workflowId")
                return wf
            err = self.error_for(r)
            if r.status_code == 429 and not last:
                wait = err.retry_after if err.retry_after is not None else 2.0 + random.random() * 3
                if wait > cap:
                    raise SogniError(f"{err} (retry after {int(wait)} s)", status=429,
                                     code=err.code, retry_after=wait)
                _sleep(wait)
                continue
            if r.status_code >= 500 and not last:
                _sleep(2.0 * (attempt + 1))
                continue
            raise err
        raise SogniError("Sogni start failed")  # pragma: no cover — loop always returns/raises

    def get(self, workflow_id: str) -> dict:
        with httpx.Client(timeout=30.0) as client:
            r = client.get(f"{self.base}{WORKFLOWS}/{workflow_id}", headers=self._headers())
        if r.status_code >= 400:
            raise self.error_for(r)
        return self._workflow_of(self._json_ok(r))

    def events(self, workflow_id: str) -> list[dict]:
        with httpx.Client(timeout=30.0) as client:
            r = client.get(f"{self.base}{WORKFLOWS}/{workflow_id}/events", headers=self._headers())
        if r.status_code >= 400:
            raise self.error_for(r)
        body = self._json_ok(r)
        data = body.get("data", body)
        if isinstance(data, dict):
            data = data.get("events") or []
        return [e for e in data or [] if isinstance(e, dict)]

    def artifact_url(self, wf: dict, kind: str) -> Optional[str]:
        """Where the result is: steps[i].artifacts, then workflow.artifacts for
        our step, then artifact_created events (the docs and the protocol
        schema disagree on the first two)."""
        url = find_artifact(wf, kind)
        if url:
            return url
        wid = wf.get("workflowId")
        if not wid:
            return None
        try:
            events = self.events(str(wid))
        except Exception as e:  # noqa: BLE001
            log.info("sogni events for %s unavailable: %s", wid, e)
            return None
        for ev in events:
            if ev.get("type") != "artifact_created":
                continue
            d = ev.get("data") or {}
            art = d.get("artifact") if isinstance(d.get("artifact"), dict) else d
            if _artifact_ok(art, kind, step_ok=True):
                return str(art["url"])
        return None

    @staticmethod
    def download(url: str) -> bytes:
        """Fetch a signed result URL as given: no auth header, no rewriting."""
        with httpx.Client(timeout=300.0, follow_redirects=True) as client:
            r = client.get(url, headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            return r.content


def _artifact_ok(a: Any, kind: str, *, step_ok: bool = False) -> bool:
    if not isinstance(a, dict) or not a.get("url"):
        return False
    if a.get("kind") and a.get("kind") != kind:
        return False
    sid = a.get("stepId")
    return step_ok or sid in (None, "", STEP_ID)


def find_artifact(wf: dict, kind: str) -> Optional[str]:
    steps = [s for s in wf.get("steps") or [] if isinstance(s, dict)]
    ours = [s for s in steps if s.get("id") == STEP_ID] or steps
    for s in ours:
        for a in s.get("artifacts") or []:
            if _artifact_ok(a, kind, step_ok=True):
                return str(a["url"])
    for a in wf.get("artifacts") or []:
        if _artifact_ok(a, kind):
            return str(a["url"])
    return None


def workflow_state(wf: dict) -> tuple[str, Optional[str]]:
    """(queued|running|done|failed, error) for a snapshot. "done" still needs
    an artifact to count as succeeded (partial_failure may have one)."""
    status = str(wf.get("status") or "").lower()
    if status in ("completed", "partial_failure"):
        return "done", None
    if status in ("failed", "cancelled"):
        return "failed", _failure_message(wf, status)
    if status == "waiting_for_user":
        # Never auto-approve a cost or release a safety pause: a one-step job
        # waiting on a person has failed as far as the pipeline is concerned.
        if wf.get("awaitingCostApproval"):
            return "failed", "Sogni is waiting for cost approval (not approved automatically)"
        return "failed", f"Sogni is waiting for: {wf.get('waitingReason') or 'user input'}"
    if status == "running":
        return "running", None
    return "queued", None


def _failure_message(wf: dict, status: str) -> str:
    err = wf.get("error")
    if isinstance(err, dict):
        err = err.get("message") or err.get("code")
    if not err:
        for s in wf.get("steps") or []:
            e = (s or {}).get("error") if isinstance(s, dict) else None
            if e:
                err = e.get("message") if isinstance(e, dict) else e
                break
    return f"Sogni workflow {status}" + (f": {str(err)[:300]}" if err else "")


def _duration_of(wf: dict) -> Optional[float]:
    for a in wf.get("artifacts") or []:
        if isinstance(a, dict) and a.get("durationSeconds"):
            return float(a["durationSeconds"])
    steps = ((wf.get("input") or {}).get("steps") or wf.get("steps") or [])
    for s in steps:
        if isinstance(s, dict):
            d = (s.get("arguments") or {}).get("duration")
            if d:
                return float(d)
    return None


# ---------- image ----------


class SogniImageProvider(ImageProvider):
    """generate_image on Sogni: start, poll, download. Synchronous."""

    def __init__(self, *, api_key: Optional[str] = None, model: Optional[str] = None) -> None:
        self.client = SogniClient(api_key)
        self.model = model or f"{PREFIX}krea-2-turbo"

    def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(id=f"{PREFIX}{m.selector}", name=display_name(m), modality="image",
                          description=m.note) for m in image_models()]

    def generate(
        self,
        *,
        model: str,
        prompt: str,
        references: list[ImageRef],
        negative_prompt: str = "",
        settings: dict | None = None,
    ) -> bytes:
        model = model or self.model
        settings = settings or {}
        args: dict = {
            "prompt": prompt,
            "model": selector(model),
            "numberOfVariations": 1,
            "outputFormat": "png",
        }
        if negative_prompt:
            args["negativePrompt"] = negative_prompt
        if settings.get("aspect_ratio"):
            args["aspectRatio"] = str(settings["aspect_ratio"])
        media: list[dict] = []
        # A frame or a "scene" reference (same place, other time of day — the
        # ambient drift/anchor variants) is the img2img source; character /
        # style / object references have no input on generate_image.
        spec = image_model(model)
        source = next((r for r in references or [] if r.frame_type), None) or next(
            (r for r in references or [] if r.role == "scene"), None)
        if source is None and settings.get("img2img") and references:
            source = references[0]
        if source is not None and not (spec and spec.img2img):
            # No source image is applied on generate_image (see
            # ImageModel.img2img): send text only rather than unused fields.
            log.info("sogni %s: no image-to-image through the workflow API; ignoring the source image", model)
            source = None
            references = []
        if source is not None:
            media.append({"kind": "image", "url": source.url})
            args["sourceImageIndex"] = -1
            args["starting_image_strength"] = float(
                settings.get("starting_image_strength") or get_settings().sogni_image_strength)
        elif references:
            # generate_image has no identity-reference input.
            log.info("sogni %s: ignoring %d reference image(s) (no reference input)", model, len(references))
        wf = self.client.start(title=prompt[:80], tool="generate_image", arguments=args,
                               media_references=media or None)
        wf = self._wait(wf)
        url = self.client.artifact_url(wf, "image")
        if not url:
            raise SogniError(f"Sogni workflow {wf.get('status')} without an image")
        data = self.client.download(url)
        from ..services import spend

        spend.record_rate_card("sogni", "image", model, image_cost_usd(model),
                               note="Sogni Spark price-table estimate")
        return data

    def _wait(self, wf: dict) -> dict:
        wid = str(wf["workflowId"])
        deadline = time.monotonic() + get_settings().sogni_image_timeout_seconds
        delay = 2.0
        while True:
            state, err = workflow_state(wf)
            if state == "done":
                return wf
            if state == "failed":
                raise SogniError(err or "Sogni workflow failed")
            if time.monotonic() > deadline:
                raise SogniError(f"Sogni image timed out after {int(get_settings().sogni_image_timeout_seconds)} s "
                                 f"(workflow {wid} still {wf.get('status')})")
            _sleep(delay)
            delay = min(delay * 1.5, 5.0)
            try:
                wf = self.client.get(wid)
            except SogniError as e:
                if e.status == 429 or e.status >= 500:
                    if e.retry_after:
                        _sleep(min(e.retry_after, 30.0))
                    continue
                raise
            except httpx.TransportError:
                continue


# ---------- video ----------


class SogniVideoProvider(VideoProvider):
    """generate_video / animate_photo on Sogni. Async: submit, then the
    video_jobs sweeper polls (no webhooks on Sogni)."""

    def __init__(self, *, api_key: Optional[str] = None, model: Optional[str] = None) -> None:
        self.client = SogniClient(api_key)
        self.model = model or f"{PREFIX}ltx25"

    def list_models(self) -> list[ModelInfo]:
        out = []
        for m in video_models():
            out.append(ModelInfo(id=f"{PREFIX}{m.selector}", name=display_name(m),
                                 modality="video", description=m.note))
        return out

    def capabilities(self, model: str) -> VideoCapabilities:
        return capabilities_for(model or self.model)

    def submit(
        self,
        *,
        model: str,
        prompt: str,
        references: list[ImageRef],
        negative_prompt: str = "",
        reference_strategy: str = "input_references",
        duration_seconds: float = 5.0,
        settings: dict | None = None,
    ) -> SubmittedJob:
        model = model or self.model
        sel = selector(model)
        spec = video_model(model)
        caps = self.capabilities(model)
        settings = settings or {}  # callback_url is ignored: Sogni has no webhooks
        duration = caps.snap(duration_seconds)
        args: dict = {"prompt": prompt, "videoModel": sel, "duration": duration}
        if spec is None or spec.audio_toggle:
            args["generateAudio"] = bool(settings.get("generate_audio", False))
        if settings.get("aspect_ratio"):
            args["aspectRatio"] = str(settings["aspect_ratio"])
        if negative_prompt and (spec.negative if spec else sel.startswith(("ltx", "wan2"))):
            args["negativePrompt"] = negative_prompt
        media: list[dict] = []
        tool = "generate_video"
        refs = list(references or [])
        first = last = None
        if reference_strategy == "frame_images" and refs:
            for i, r in enumerate(refs):
                ft = r.frame_type or ("first_frame" if i == 0 else "last_frame")
                if ft == "first_frame" and first is None:
                    first = r
                elif ft == "last_frame" and last is None:
                    last = r
        if first is None and refs and spec is not None and spec.animate and not spec.text:
            first = refs[0]  # a picture-only model: a loose reference is its first frame
        if first is None and spec is not None and spec.animate and not spec.text:
            raise SogniError(f"{spec.name} needs a starting picture (it makes video from a first frame)")
        if spec is not None and spec.needs_reference and not refs:
            raise SogniError(f"{spec.name} needs at least one reference picture")
        if first is not None and caps.first_frame:
            tool = "animate_photo"
            media.append({"kind": "image", "url": first.url})
            args["sourceImageIndex"] = -1  # -1 = the first media_references image
            args["frameRole"] = "start"
            if last is None and spec is not None and spec.flf_only:
                last = first  # first + last frame only: the picture is both ends
            if last is not None and caps.last_frame:
                # "both" interpolates first -> last; the end image is always named.
                args["frameRole"] = "both"
                if last.url == first.url:
                    args["endImageIndex"] = -1
                else:
                    media.append({"kind": "image", "url": last.url})
                    args["endImageIndex"] = -2
        else:
            loose = [first] if first is not None else refs
            if loose and spec is not None and spec.references:
                media += [{"kind": "image", "url": r.url} for r in loose[:9]]
                args["referenceImageIndices"] = [-(i + 1) for i in range(len(media))]
                if first is not None:
                    # MiniMax H3 names references <Picture N>, HappyHorse
                    # [Image N], Seedance @ImageN (Sogni's tool schema).
                    tag = ("<Picture 1>" if sel.startswith("minimax-h3") else
                           "[Image 1]" if sel.startswith("happyhorse") else "@Image1")
                    args["prompt"] = f"Use {tag} as the first frame. {prompt}"
            elif loose:
                log.info("sogni %s: ignoring %d reference image(s) (model takes none)", model, len(loose))
        wf = self.client.start(title=prompt[:80], tool=tool, arguments=args, media_references=media or None)
        wid = str(wf["workflowId"])
        return SubmittedJob(provider="sogni", model=model, job_id=wid,
                            polling_url=f"{self.client.base}{WORKFLOWS}/{wid}",
                            raw={"duration": duration, "tool": tool})

    def poll(self, job: SubmittedJob) -> JobStatus:
        wf = self.client.get(job.job_id)
        state, err = workflow_state(wf)
        if state == "done":
            url = self.client.artifact_url(wf, "video")
            if not url:
                return JobStatus(state="failed", error=f"Sogni workflow {wf.get('status')} without a video")
            secs = _duration_of(wf) or float(job.raw.get("duration") or 5.0)
            # An estimate from the Spark price table; spend records it as one.
            return JobStatus(state="succeeded", progress=1.0, result_url=url,
                             cost=video_cost_usd(job.model or self.model, secs))
        if state == "failed":
            return JobStatus(state="failed", error=err)
        if state == "running":
            return JobStatus(state="running")
        return JobStatus(state="queued")

    def download(self, job: SubmittedJob, result_url: str | None = None) -> bytes:
        if not result_url:
            st = self.poll(job)
            if st.state != "succeeded" or not st.result_url:
                raise RuntimeError(f"Sogni job not ready: {st.state} {st.error or ''}".strip())
            result_url = st.result_url
        return SogniClient.download(result_url)

"""Runware (runware.ai): image and video models behind one REST endpoint.

Model ids are "runware/<AIR>", e.g. "runware/runware:107@1" (FLUX.1 Krea
dev) or "runware/lightricks:ltx@2.3-fast". Every request is a JSON array of
tasks POSTed with a Bearer key. Images are synchronous; videos are async
(submit, then the video_jobs sweeper polls getResponse; Runware's webhooks
are unsigned, so they aren't used).

Requests are built from each model's published JSON schema (Runware rejects
unknown fields): the curated models' schemas ship in runware_models.json
(scripts/runware_snapshot.py), any other model's schema is fetched from the
public docs the first time it's used and cached. Each candidate request is
validated against the schema before it is sent, so a request Runware would
reject is never paid for.

Never resend a task that may have run: after a 5xx or a timeout the client
asks getResponse about the same taskUUID instead (a resend is a second paid
generation). 429/503 mean the task didn't run and are retried.

Content: Runware's own check (safety.checkContent) is off unless switched on
in Settings or the request carries a Cast member's picture. Prompts involving
minors are refused before anything is sent (services/safety.check_prompt).
Vendor models (Google, Kling, Alibaba Wan, ...) stay moderated by their vendor.
"""
from __future__ import annotations

import copy
import json
import logging
import math
import re
import threading
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator, Optional

import httpx

from ..config import get_settings
from .base import (
    ImageOp,
    ImageOpProvider,
    ImageProvider,
    ImageRef,
    JobStatus,
    ModelInfo,
    SubmittedJob,
    VideoCapabilities,
    VideoProvider,
)

log = logging.getLogger("avs.runware")
_sleep = time.sleep  # tests patch this

PREFIX = "runware/"
BUNDLE = Path(__file__).with_name("runware_models.json")
OPEN_AIR_PREFIXES = ("runware:", "rundiffusion:", "civitai:")
IMAGE_CAPS = {"text-to-image", "edit"}
VIDEO_CAPS = {"text-to-video", "image-to-video"}
_DROP_KEYS = {"description", "title", "examples", "x-docs", "$comment", "markdownDescription", "deprecated"}
# Error codes that mean "refused for its content" (not billed by Runware).
_CONTENT_HINTS = ("nsfw", "policy", "blocked", "moderat", "safety", "sensitive", "inappropriate")
# Output long sides per video resolution label.
_RES_LONG = {"360p": 640, "480p": 854, "540p": 960, "720p": 1280, "768p": 1366, "1080p": 1920,
             "1440p": 2560, "2k": 2048, "4k": 3840}


def is_runware(model: Optional[str]) -> bool:
    return bool(model) and str(model).startswith(PREFIX)


def air(model: str) -> str:
    return model[len(PREFIX):] if is_runware(model) else model


def is_open(model_air: str, arch: Optional[str] = None) -> bool:
    """Runware-hosted open weights: no vendor moderation."""
    return bool(arch) or model_air.startswith(OPEN_AIR_PREFIXES)


# ---------- model data ----------


@lru_cache(maxsize=1)
def bundle() -> dict:
    return json.loads(BUNDLE.read_text())


def _data_dir() -> Path:
    d = get_settings().data_path / "runware"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def index_row(e: dict) -> Optional[dict]:
    """A live image or video model from Runware's index, or None."""
    caps = set(e.get("capabilities") or e.get("caps") or [])
    if e.get("status", "live") != "live" or not e.get("air"):
        return None
    if not caps & (IMAGE_CAPS | VIDEO_CAPS | {"image-to-image"}):
        return None
    return {k: v for k, v in {
        "air": e["air"], "id": e["id"], "name": e.get("name") or e["id"],
        "caps": sorted(caps), "arch": e.get("architecture") or e.get("arch"),
        "creator": e.get("creator"),
    }.items() if v not in (None, "")}


_index_memo: dict = {}


def index_rows() -> list[dict]:
    """The synced index when there is one, else the bundled copy."""
    path = _data_dir() / "index.json" if _data_ok() else None
    try:
        stamp = path.stat().st_mtime_ns if path is not None else None
    except OSError:
        stamp = None
    if stamp is not None and _index_memo.get("stamp") == (str(path), stamp):
        return _index_memo["rows"]
    synced = _read_json(path) if stamp is not None else None
    rows = (synced or {}).get("rows")
    rows = rows if isinstance(rows, list) and rows else bundle()["index"]
    if stamp is not None:
        _index_memo.update(stamp=(str(path), stamp), rows=rows)
    return rows


def _data_ok() -> bool:
    try:
        return get_settings().data_path.exists()
    except Exception:  # noqa: BLE001
        return False


def refresh_index(client: Optional[httpx.Client] = None) -> int:
    """Sync the public model index (If-None-Match). Returns the row count.
    Raises on failure; the last good list (or the bundled one) stays."""
    s = get_settings()
    path = _data_dir() / "index.json"
    prev = _read_json(path) or {}
    headers = {"If-None-Match": prev["etag"]} if prev.get("etag") and prev.get("rows") else {}
    own = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        r = client.get(s.runware_models_index_url, headers=headers)
    finally:
        if own:
            client.close()
    if r.status_code == 304:
        return len(prev.get("rows") or [])
    r.raise_for_status()
    rows = [x for x in (index_row(e) for e in r.json() if isinstance(e, dict)) if x]
    if not rows:
        raise RuntimeError("Runware's model index came back empty")
    path.write_text(json.dumps({"etag": r.headers.get("etag", ""), "rows": rows}))
    return len(rows)


CHECK_PER_REFRESH = 25


def _checked() -> dict:
    """{air: "" (the app can drive it) | why not} for models that came in
    through the live index after the bundle was made."""
    path = _data_dir() / "checked.json" if _data_ok() else None
    data = _read_json(path) if path else None
    return data if isinstance(data, dict) else {}


def check_new_models(limit: int = CHECK_PER_REFRESH) -> int:
    """Smoke-test models the live index added since the bundle (the same
    test the snapshot script runs), so pickers don't offer one the app
    can't build a request for. Up to `limit` per call; returns how many."""
    b = bundle()
    known = {r.get("air") for r in b["index"]} | set(b["schemas"]) | set(b.get("unusable") or [])
    done = _checked()
    todo = [r["air"] for r in index_rows() if r.get("air") not in known and r.get("air") not in done]
    for a in todo[:max(0, limit)]:
        try:
            done[a] = smoke_build(PREFIX + a) or ""
        except Exception as e:  # noqa: BLE001 — couldn't read its schema: try again next refresh
            log.info("runware: couldn't check %s yet (%s)", a, str(e)[:200])
    if any(a in done for a in todo[:limit]):
        (_data_dir() / "checked.json").write_text(json.dumps(done))
    if len(todo) > limit:
        log.info("runware: %d new models left to check next refresh", len(todo) - limit)
    return min(len(todo), max(0, limit))


def kind_of(caps) -> Optional[str]:
    c = set(caps or [])
    if c & VIDEO_CAPS:
        # Talking-head / dubbing models need an audio input the app doesn't send.
        if "audio-to-video" in c and "text-to-video" not in c:
            return None
        return "video"
    if c & IMAGE_CAPS:
        return "image"
    # Picture operations (image-to-image only): Studio's "Remove background"
    # and "Upscale".
    if "image-to-image" in c and "remove-background" in c:
        return "bg_removal"
    if "image-to-image" in c and "upscale" in c:
        return "upscale"
    return None


def _strip(o: Any) -> Any:
    if isinstance(o, dict):
        return {k: _strip(v) for k, v in o.items() if k not in _DROP_KEYS}
    if isinstance(o, list):
        return [_strip(v) for v in o]
    return o


_schema_lock = threading.Lock()
_schemas: dict[str, dict] = {}


def _safe_name(a: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", a)


def schema(model: str, *, fetch: bool = True) -> Optional[dict]:
    """{"id", "name", "capabilities", "modes", "pricing", "request"} for a
    model: bundled, cached on disk, or (fetch=True) read from the docs."""
    a = air(model)
    hit = bundle()["schemas"].get(a)
    if hit:
        return hit
    with _schema_lock:
        if a in _schemas:
            return _schemas[a]
    path = _data_dir() / "schemas" / f"{_safe_name(a)}.json" if _data_ok() else None
    cached = _read_json(path) if path else None
    if cached and cached.get("request"):
        with _schema_lock:
            _schemas[a] = cached
        return cached
    if not fetch:
        return None
    row = next((r for r in index_rows() if r.get("air") == a), None)
    if row is None:
        return None
    url = f"{get_settings().runware_docs_models_base.rstrip('/')}/{row['id']}/schema.json"
    try:
        r = httpx.get(url, timeout=30.0)
        r.raise_for_status()
        doc = r.json()
        info = doc.get("info") or {}
        req = doc["components"]["schemas"]["RequestBody"]
        out = {"id": row["id"], "name": row.get("name") or row["id"],
               "capabilities": info.get("x-capabilities") or row.get("caps") or [],
               "modes": info.get("x-modes") or [], "pricing": info.get("x-pricing") or {},
               "request": _strip(req.get("items", req))}
    except Exception as e:  # noqa: BLE001
        raise RunwareError(f"Couldn't read Runware's description of {a} ({e})") from e
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out))
    with _schema_lock:
        _schemas[a] = out
    return out


def _props(sch: dict) -> dict:
    return (sch.get("request") or {}).get("properties") or {}


def _sub(props: dict, *path: str) -> Optional[dict]:
    """The schema of a nested property (e.g. "settings", "audio")."""
    spec: Optional[dict] = {"properties": props}
    for name in path:
        spec = ((spec or {}).get("properties") or {}).get(name)
        if not isinstance(spec, dict):
            return None
    return spec


def display_name(model: str) -> str:
    a = air(model)
    sch = bundle()["schemas"].get(a)
    if sch:
        return sch.get("name") or a
    row = next((r for r in index_rows() if r.get("air") == a), None)
    return (row or {}).get("name") or a


# ---------- capabilities and prices ----------


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _durations(request: Optional[dict]) -> list[int]:
    """Every clip length (whole seconds) the request schema allows: the
    duration property's enum/const/range, including those nested in
    oneOf/anyOf and in allOf rules (e.g. "with frames, 5 s only")."""
    found: set[int] = set()
    defaults: set[int] = set()

    def spec_values(spec: Any) -> None:
        if not isinstance(spec, dict):
            return
        for x in spec.get("enum") or []:
            if _num(x):
                found.add(int(round(float(x))))
        if _num(spec.get("const")):
            found.add(int(round(float(spec["const"]))))
        lo, hi = spec.get("minimum"), spec.get("maximum")
        if _num(lo) and _num(hi) and hi >= lo:
            found.update(range(max(1, math.ceil(float(lo))), int(math.floor(float(hi))) + 1))
        if _num(spec.get("default")):
            defaults.add(int(round(float(spec["default"]))))
        for k in ("oneOf", "anyOf", "allOf"):
            for sub in spec.get(k) or []:
                spec_values(sub)

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            props = o.get("properties")
            if isinstance(props, dict) and "duration" in props:
                spec_values(props["duration"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(request or {})
    return sorted(found or defaults)


# When a schema states no lengths: common ones, checked against it per request.
_FALLBACK_DURATIONS = [4, 5, 6, 8, 10]


def capabilities_for(model: str) -> VideoCapabilities:
    sch = schema(model)
    if sch is None:
        return VideoCapabilities(known=False)
    modes = sch.get("modes") or []
    frames: set = set()
    for m in modes:
        frames.update(str(f) for f in (m.get("frames") or []))
    has_text = any(m.get("id") == "text" for m in modes) or not modes
    first = "first" in frames
    refs_only = any("referenceImages" in (m.get("inputs") or []) for m in modes)
    return VideoCapabilities(
        durations=_durations(sch.get("request")) or list(_FALLBACK_DURATIONS),
        first_frame=first, last_frame="last" in frames,
        needs_first_frame=not has_text and first,
        needs_reference=not has_text and not first and refs_only,
    )


def _rates(sch: Optional[dict], unit: str) -> list[dict]:
    return [r for r in ((sch or {}).get("pricing") or {}).get("rates") or []
            if r.get("unit") == unit and isinstance(r.get("amount"), (int, float))]


def _mp(text: str) -> Optional[float]:
    m = re.search(r"(\d{3,4})\s*[x×]\s*(\d{3,4})", text or "")
    return int(m.group(1)) * int(m.group(2)) / 1e6 if m else None


def image_cost_usd(model: str, megapixels: float = 2.1) -> float:
    """Pre-flight estimate of one still (the real cost comes back with it)."""
    sch = schema(model, fetch=False)
    out = _rates(sch, "output")
    if out:
        return round(float(out[0]["amount"]), 4)
    per_mp = _rates(sch, "outputMegapixel")
    if per_mp:
        return round(float(per_mp[0]["amount"]) * megapixels, 4)
    measured = [m for m in ((sch or {}).get("pricing") or {}).get("measured") or []
                if isinstance(m.get("price"), (int, float))]
    if measured:
        m = measured[0]
        base = _mp(str(m.get("configuration") or "")) or 1.05
        return round(float(m["price"]) * max(1.0, megapixels / base), 4)
    return get_settings().cost_image_per_item


OP_TASKS = {"remove_background": "removeBackground", "upscale": "upscale"}
OP_KIND_OF = {"remove_background": "bg_removal", "upscale": "upscale"}


def _label_mp(label: Any) -> Optional[float]:
    m = re.search(r"(\d+(?:\.\d+)?)\s*MP", str(label or ""))
    return float(m.group(1)) if m else None


# Runware's size tiers ("16 MP output") and its measured prices (a 1024 x
# 1024 input) count megapixels in powers of two: 1 MP = 1024 x 1024 pixels.
_MP_PIXELS = 1024 * 1024
# A size this close to a tier still counts as that tier (rounding, a few
# pixels of padding), so a 4096 x 4096 result is the 16 MP tier, not 24.
_TIER_SLACK = 1.02


def op_cost_usd(model: str, op: str, *, factor: int = 2, out_megapixels: Optional[float] = None) -> float:
    """Pre-flight estimate of one picture operation. Prices from the model's
    schema: a flat or output-size-tiered rate per picture, a rate per output
    megapixel, or Runware's measured price for the same upscale factor (made
    on a ~1 MP input, so scaled up for larger pictures). `out_megapixels` is
    width x height / 1e6 of the result."""
    sch = schema(model, fetch=False)
    out = _rates(sch, "output")
    if out:
        tiers = [(mp, float(r["amount"])) for r in out if (mp := _label_mp(r.get("label"))) is not None]
        if tiers:
            if out_megapixels:
                want = out_megapixels * 1e6 / _MP_PIXELS
                fit = sorted(t for t in tiers if t[0] * _TIER_SLACK >= want)
                return round(fit[0][1] if fit else max(a for _, a in tiers), 4)
            return round(min(a for _, a in tiers), 4)
        return round(float(out[0]["amount"]), 4)
    per_mp = _rates(sch, "outputMegapixel")
    if per_mp:
        mp = out_megapixels or (1.05 * factor * factor if op == "upscale" else 1.05)
        return round(float(per_mp[0]["amount"]) * mp, 4)
    measured = [m for m in ((sch or {}).get("pricing") or {}).get("measured") or []
                if isinstance(m.get("price"), (int, float))]
    if measured:
        if op != "upscale":
            return round(float(measured[0]["price"]), 4)
        # How many ~1 MP inputs this picture is (never less than one).
        size = 1.0
        if out_megapixels:
            size = out_megapixels * 1e6 / _MP_PIXELS / (factor * factor)
            size = 1.0 if size <= _TIER_SLACK else size

        def fac(m: dict) -> Optional[int]:
            hit = re.search(r"upscaleFactor\s*(\d+)", str(m.get("configuration") or ""))
            return int(hit.group(1)) if hit else None
        same = [m for m in measured if fac(m) == factor]
        if same:
            return round(float(same[0]["price"]) * size, 4)
        base = measured[0]
        bf = fac(base) or 2
        return round(float(base["price"]) * (factor / bf) ** 2 * size, 4)
    return get_settings().cost_image_per_item


def upscale_factors(model: str) -> list[int]:
    """Which of ×2 and ×4 the model takes (both when its schema isn't known
    yet, or when it has no factor setting)."""
    sch = schema(model, fetch=False)
    spec = _props(sch).get("upscaleFactor") if sch else None
    if not isinstance(spec, dict):
        return [2, 4]
    return [f for f in (2, 4) if _allowed(spec, f)]


def input_limits(model: str) -> dict:
    """The most the model takes as its input picture: {"area", "width",
    "height"} in pixels (missing = no limit), from its schema."""
    sch = schema(model, fetch=False)
    out: dict = {}
    for c in ((sch or {}).get("request") or {}).get("x-constraints") or []:
        if "inputs.image" in (c.get("parameters") or []) and isinstance(c.get("maximum"), (int, float)):
            if c.get("operation") in ("area", "width", "height"):
                out[c["operation"]] = int(c["maximum"])
    return out


def fit_to_limits(width: int, height: int, limits: dict) -> tuple[int, int]:
    """(w, h) scaled down (aspect kept) to fit the model's input limits."""
    scale = 1.0
    if limits.get("area") and width * height > limits["area"]:
        scale = min(scale, math.sqrt(limits["area"] / (width * height)))
    if limits.get("width") and width > limits["width"]:
        scale = min(scale, limits["width"] / width)
    if limits.get("height") and height > limits["height"]:
        scale = min(scale, limits["height"] / height)
    if scale >= 1.0:
        return width, height
    return max(1, int(width * scale)), max(1, int(height * scale))


def video_resolution() -> str:
    r = (get_settings().runware_video_resolution or "1080p").lower()
    return r if r in _RES_LONG else "1080p"


def video_rate_usd(model: str, resolution: Optional[str] = None) -> float:
    """Price per second of silent output at `resolution` (estimate)."""
    rates = _rates(schema(model, fetch=False), "durationSecond")
    if not rates:
        return get_settings().cost_video_per_second
    res = (resolution or video_resolution()).lower()

    def silent(r: dict) -> bool:
        label = str(r.get("label") or "").lower()
        return "with audio" not in label and "· audio" not in label

    for pick in (lambda r: res in str(r.get("label") or "").lower() and silent(r),
                 lambda r: res in str(r.get("label") or "").lower(),
                 lambda r: not r.get("label")):
        hit = [r for r in rates if pick(r)]
        if hit:
            return float(min(h["amount"] for h in hit))
    quiet = [r for r in rates if silent(r)] or rates
    return float(max(r["amount"] for r in quiet))  # no label for this resolution: err high


def video_cost_usd(model: str, seconds: float) -> float:
    return round(video_rate_usd(model) * max(1.0, float(seconds or 0)), 4)


# ---------- request building ----------


def _validator(model_air: str, request: dict):
    from jsonschema import Draft202012Validator

    key = (model_air, id(request))
    v = _validators.get(key)
    if v is None:
        v = Draft202012Validator(request)
        _validators[key] = v
    return v


_validators: dict = {}


def schema_error(sch: dict, task: dict) -> Optional[str]:
    """None when `task` satisfies the model's request schema, else why not."""
    from jsonschema.exceptions import best_match

    err = best_match(_validator(task.get("model", ""), sch["request"]).iter_errors(task))
    if err is None:
        return None
    where = "/".join(str(p) for p in err.absolute_path)
    return f"{where + ': ' if where else ''}{err.message}"[:300]


def _allowed(spec: Optional[dict], value: Any) -> bool:
    if not isinstance(spec, dict):
        return False
    from jsonschema import Draft202012Validator

    return Draft202012Validator(spec).is_valid(value)


def _put(task: dict, props: dict, path: tuple[str, ...], value: Any) -> bool:
    """Set task[path] = value when the schema has that field and allows it."""
    spec = _sub(props, *path)
    if spec is None:
        return False
    if value == "max":
        value = spec.get("maximum")
        if value is None:
            return False
    if not _allowed(spec, value):
        return False
    node = task
    for name in path[:-1]:
        node = node.setdefault(name, {})
    node[path[-1]] = value
    return True


# Defaults that cost money or take control away, set where a model has them.
_QUIET = (
    (("settings", "audio"), False), (("providerSettings", "google", "generateAudio"), False),
    (("providerSettings", "alibaba", "audio"), False), (("providerSettings", "vidu", "audio"), False),
    (("settings", "enhancePrompt"), False), (("settings", "promptExtend"), False),
    (("settings", "promptExpansion"), False), (("providerSettings", "alibaba", "promptExtend"), False),
)
# Vendor safety knobs at their most permissive (the vendor still moderates).
_PERMISSIVE = (
    (("providerSettings", "bfl", "safetyTolerance"), "max"), (("settings", "safetyTolerance"), "max"),
    (("settings", "moderation"), "low"), (("settings", "safetyFilter"), False),
)


def _target(aspect: str, long_side: int) -> tuple[int, int]:
    try:
        w, h = (float(x) for x in str(aspect or "16:9").split(":"))
        if w <= 0 or h <= 0:
            raise ValueError
    except ValueError:
        w, h = 16.0, 9.0
    if w >= h:
        return long_side, int(round(long_side * h / w))
    return int(round(long_side * w / h)), long_side


def _snap(v: int, spec: dict, multiple: Optional[int] = None) -> Optional[int]:
    m = int(multiple or spec.get("multipleOf") or 1) or 1
    lo = int(math.ceil(float(spec.get("minimum") or m) / m) * m)
    hi_raw = spec.get("maximum")
    hi = int(math.floor(float(hi_raw) / m) * m) if hi_raw is not None else 10 ** 6
    if lo > hi:
        return None
    return min(max(int(round(v / m)) * m, lo), hi)


def _fit_scale(w: int, h: int, ws: dict, hs: dict) -> float:
    """The factor that brings w×h inside both sides' minimum..maximum while
    keeping its shape (shrink first; grow only when a side is too small)."""
    def lim(spec: dict, key: str) -> Optional[float]:
        v = spec.get(key)
        return float(v) if isinstance(v, (int, float)) and v > 0 else None

    k = 1.0
    for v, spec in ((w, ws), (h, hs)):
        hi = lim(spec, "maximum")
        if hi is not None and v * k > hi:
            k = hi / v
    if k == 1.0:
        for v, spec in ((w, ws), (h, hs)):
            lo = lim(spec, "minimum")
            if lo is not None and v * k < lo:
                k = lo / v
    return k


def _size_pairs(request: dict) -> list[tuple[int, int]]:
    """Every fixed (width, height) the schema lists (oneOf/const rules)."""
    out: list[tuple[int, int]] = []

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            p = o.get("properties")
            if isinstance(p, dict) and isinstance(p.get("width"), dict) and isinstance(p.get("height"), dict):
                w, h = p["width"].get("const"), p["height"].get("const")
                if isinstance(w, int) and isinstance(h, int) and (w, h) not in out:
                    out.append((w, h))
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(request)
    return out


def _nearest(pairs: list[tuple[int, int]], w: int, h: int) -> list[tuple[int, int]]:
    """Pairs ordered by closeness to w×h: aspect ratio first, then size."""
    ar = math.log(w / h)
    return sorted(pairs, key=lambda p: (round(abs(math.log(p[0] / p[1]) - ar), 2),
                                        abs(p[0] * p[1] - w * h)))


def _resolutions(spec: Optional[dict], preferred: list[str]) -> list[str]:
    if not spec:
        return []
    enum = spec.get("enum") or [a.get("const") for a in spec.get("oneOf") or spec.get("anyOf") or []
                                if isinstance(a, dict) and isinstance(a.get("const"), str)]
    enum = [str(e) for e in enum if isinstance(e, str)]
    picked = [r for r in preferred if r in enum]
    return picked or enum[-1:]


def _size_variants(sch: dict, aspect: str, long_side: int, resolutions: list[str],
                   frames: bool) -> Iterator[dict]:
    """Size fields to try, best first: a resolution preset, an exact listed
    size, a snapped width×height, or none (the model's default / the frame's)."""
    props = _props(sch)
    w, h = _target(aspect, long_side)
    variants: list[dict] = []
    res = [{"resolution": r} for r in _resolutions(props.get("resolution"), resolutions)]
    ar_spec = props.get("aspectRatio")
    if ar_spec and _allowed(ar_spec, aspect):
        res = [{**r, "aspectRatio": aspect} for r in res] or [{"aspectRatio": aspect}]
    pairs = [{"width": pw, "height": ph} for pw, ph in _nearest(_size_pairs(sch["request"]), w, h)[:3]]
    snapped: list[dict] = []
    ws, hs = props.get("width"), props.get("height")
    if isinstance(ws, dict) and isinstance(hs, dict):
        # The property's own multiple first; some models state theirs only
        # in an allOf rule, so common multiples follow.
        for m in (None, 64, 32, 16):
            # Scale both sides together to fit the limits, then snap: clamping
            # each side alone would change the picture's shape.
            k = _fit_scale(w, h, ws, hs)
            sw, sh = _snap(round(w * k), ws, m), _snap(round(h * k), hs, m)
            if sw and sh and {"width": sw, "height": sh} not in snapped:
                snapped.append({"width": sw, "height": sh})
    order = (res, pairs, snapped) if frames else (snapped, pairs, res)
    for group in order:
        variants += group
    variants.append({})
    seen = []
    for v in variants:
        if v not in seen:
            seen.append(v)
            yield v


def _pick_refs(refs: list[ImageRef], limit: int) -> list[ImageRef]:
    """Up to `limit` pictures in the user's order; when some must go,
    frames and Cast people are kept first, then scenes, objects, styles."""
    if len(refs) <= limit:
        return list(refs)
    rank = {"character": 1, "scene": 2, "object": 3, "style": 4}
    keep = sorted(range(len(refs)), key=lambda i: (0 if refs[i].frame_type else rank.get(refs[i].role, 5), i))
    chosen = sorted(keep[:max(0, limit)])
    return [refs[i] for i in chosen]


def _name_refs(prompt: str, used: list[ImageRef]) -> str:
    if len(used) < 2:
        return prompt
    names = [f"image {i + 1} is {'the ' + r.role if not r.name else r.name}" for i, r in enumerate(used)]
    return f"{prompt}\n({'; '.join(names)}.)"


def _choose(sch: dict, bases: list[dict], variants: list[dict], label: str) -> dict:
    """The first base × variant the schema accepts. `bases` go from all our
    defaults set to fewer (a model's rules can forbid one in some modes)."""
    first_err = None
    for base in bases:
        for v in variants:
            task = {**copy.deepcopy(base), **v}
            err = schema_error(sch, task)
            if err is None:
                return task
            first_err = first_err or err
    raise RunwareError(f"{label}: couldn't build a request Runware accepts ({first_err})")


def _fit_text(text: str, spec: Optional[dict], *, terms: bool = False) -> str:
    """`text` within the field's maxLength: cut at the last comma (for
    lists of terms) or space before the limit."""
    limit = (spec or {}).get("maxLength")
    text = (text or "").strip()
    if not isinstance(limit, int) or len(text) <= limit:
        return text
    cut = text[:limit]
    sep = cut.rfind(",") if terms else -1
    if sep < limit // 2:
        sep = cut.rfind(" ")
    return (cut[:sep] if sep > limit // 2 else cut).rstrip(" ,")


def _bases(sch: dict, task_type: str, model_air: str, prompt: str, negative: str,
           check_content: bool, cast: bool, *, pictures: bool = False) -> list[dict]:
    """The request without its size/frames, with our defaults (quiet: no
    audio or prompt rewriting; permissive: vendor safety knobs at their
    loosest), then without the permissive knobs, then without either."""
    props = _props(sch)
    full = _fit_text(prompt, props.get("positivePrompt"))
    if len(full) < len(prompt.strip()):
        log.warning("runware %s: prompt cut to the model's %s characters", model_air, len(full))
    task: dict = {"taskType": task_type, "taskUUID": str(uuid.uuid4()), "model": model_air,
                  "positivePrompt": full}
    for key, value in (("includeCost", True), ("numberResults", 1), ("outputType", "URL")):
        if key in props:
            _put(task, props, (key,), value)
    neg = _fit_text(negative, props.get("negativePrompt"), terms=True)
    if neg and "negativePrompt" in props and _allowed(props["negativePrompt"], neg) and len(neg) >= 2:
        task["negativePrompt"] = neg
    if not _put(task, props, ("safety", "checkContent"), bool(check_content)) and (cast or pictures):
        # Rule: a Cast member's picture, or any picture with a sexual
        # prompt, never goes out without the check.
        if cast:
            raise RunwareError(f"{model_air} has no content check, so it can't be sent pictures of your Cast")
        raise RunwareError(f"{model_air} has no content check, so it can't be sent a picture with a "
                           "sexual prompt; remove the picture or pick another model")
    quiet = copy.deepcopy(task)
    for path, value in _QUIET:
        _put(quiet, props, path, value)
    loud = copy.deepcopy(quiet)
    if _adult_mode():  # vendor safety knobs stay at the vendor's defaults otherwise
        for path, value in _PERMISSIVE:
            _put(loud, props, path, value)
    out: list[dict] = []
    for t in (loud, quiet, task):
        if t not in out:
            out.append(t)
    return out


def build_image_task(model: str, prompt: str, references: list[ImageRef], *, negative_prompt: str = "",
                     settings: Optional[dict] = None, check_content: Optional[bool] = None) -> dict:
    """The imageInference task for one still (validated)."""
    s = get_settings()
    settings = settings or {}
    a = air(model)
    sch = schema(model)
    if sch is None:
        raise RunwareError(f"{a} isn't in Runware's model list")
    props = _props(sch)
    inputs = (_sub(props, "inputs") or {}).get("properties") or {}
    refs = list(references or [])
    used: list[ImageRef] = []
    extra: dict = {}
    if refs and "referenceImages" in inputs:
        used = _pick_refs(refs, int(inputs["referenceImages"].get("maxItems") or 4))
        extra = {"inputs": {"referenceImages": [r.url for r in used]}}
    elif refs and "seedImage" in inputs:
        src = next((r for r in refs if r.frame_type or r.role == "scene"), None)
        if src is not None:
            used = [src]
            extra = {"inputs": {"seedImage": src.url}}
            strength = float(settings.get("image_strength") or s.runware_image_strength)
            if "strength" in props:
                extra["strength"] = round(min(max(strength, 0.0), 1.0), 2)
    if len(used) < len(refs):
        log.warning("runware %s: %d reference picture(s) not used (the model takes %s)", a,
                    len(refs) - len(used), "fewer" if used else "none of this kind")
    # A Cast member's likeness, or a prompt about children, always goes
    # through Runware's content check.
    cast = any(r.role == "character" for r in used)
    sent = _name_refs(prompt, used)
    pictures = _pictures_need_check(used, sent)
    checked = _content_check(check_content, cast, sent, negative_prompt) or pictures
    bases = _bases(sch, "imageInference", a, sent, negative_prompt, checked, cast, pictures=pictures)
    for base in bases:
        for key, value in (("deliveryMethod", "sync"), ("outputFormat", "JPG"), ("outputQuality", 95)):
            if key in props:
                _put(base, props, (key,), value)
        base.update(copy.deepcopy(extra))
    variants = list(_size_variants(sch, str(settings.get("aspect_ratio") or "16:9"),
                                   s.runware_image_long_side, ["2K", "2k", "1K", "1k"], frames=bool(used)))
    return _choose(sch, bases, variants, display_name(model))


_DATA_URI_PLACEHOLDER = "data:image/png;base64,iVBORw0KGgo="


def build_operation_task(model: str, op: ImageOp, image_bytes: bytes, mime: str, options: Optional[dict] = None,
                         *, keep_alpha: bool = False) -> dict:
    """The removeBackground / upscale task for one picture (validated). The
    picture goes inline as a data URI, so it needs no public URL. Removal
    always comes back as PNG (keeps the transparency); an upscale as PNG
    when the picture has transparency (`keep_alpha`), else JPG."""
    import base64

    if op not in OP_TASKS:
        raise RunwareError(f"unknown picture operation {op!r}")
    a = air(model)
    sch = schema(model)
    if sch is None:
        raise RunwareError(f"{a} isn't in Runware's model list")
    if kind_of(sch.get("capabilities")) != OP_KIND_OF[op]:
        raise RunwareError(f"{display_name(model)} can't {'remove backgrounds' if op == 'remove_background' else 'upscale'}")
    props = _props(sch)
    task: dict = {"taskType": OP_TASKS[op], "taskUUID": str(uuid.uuid4()), "model": a,
                  "inputs": {"image": _DATA_URI_PLACEHOLDER}}
    for key, value in (("includeCost", True), ("outputType", "URL"), ("deliveryMethod", "sync")):
        if key in props:
            _put(task, props, (key,), value)
    fmt = "PNG" if op == "remove_background" or keep_alpha else "JPG"
    if "outputFormat" in props and not _put(task, props, ("outputFormat",), fmt) and fmt == "PNG":
        raise RunwareError(f"{display_name(model)} can't return a PNG, so the transparency would be lost")
    if fmt == "JPG" and "outputQuality" in props:
        _put(task, props, ("outputQuality",), 95)
    if op == "upscale":
        factor = int((options or {}).get("factor") or 2)
        if "upscaleFactor" in props and not _put(task, props, ("upscaleFactor",), factor):
            raise RunwareError(f"{display_name(model)} can't upscale ×{factor}; pick another factor or model")
    err = schema_error(sch, task)
    if err:
        raise RunwareError(f"Couldn't build a valid request for {display_name(model)}: {err}")
    task["inputs"]["image"] = f"data:{mime or 'image/png'};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    return task


def _frames(refs: list[ImageRef], strategy: str) -> tuple[Optional[ImageRef], Optional[ImageRef]]:
    first = last = None
    if strategy == "frame_images":
        for i, r in enumerate(refs):
            ft = r.frame_type or ("first_frame" if i == 0 else "last_frame")
            if ft == "first_frame" and first is None:
                first = r
            elif ft == "last_frame" and last is None:
                last = r
    return first, last


def build_video_task(model: str, prompt: str, references: list[ImageRef], *, negative_prompt: str = "",
                     reference_strategy: str = "input_references", duration_seconds: float = 5.0,
                     settings: Optional[dict] = None,
                     check_content: Optional[bool] = None) -> tuple[dict, int]:
    """(videoInference task, the duration it asks for)."""
    settings = settings or {}
    a = air(model)
    sch = schema(model)
    if sch is None:
        raise RunwareError(f"{a} isn't in Runware's model list")
    props = _props(sch)
    caps = capabilities_for(model)
    inputs = (_sub(props, "inputs") or {}).get("properties") or {}
    refs = list(references or [])
    first, last = _frames(refs, reference_strategy)
    if first is None and refs and caps.needs_first_frame:
        first = refs[0]  # a picture-only model: a loose reference is its first frame
    if first is None and caps.needs_first_frame:
        raise RunwareError(f"{display_name(model)} needs a starting picture (it makes video from a first frame)")
    used: list[ImageRef] = []
    frame_variants: list[dict] = [{}]
    if first is not None and caps.first_frame and "frameImages" in inputs:
        pinned = [(first, "first")] + ([(last, "last")] if last is not None and caps.last_frame else [])
        used = [r for r, _ in pinned]
        frame_variants = [{"inputs": {"frameImages": [{"image": r.url, "frame": f} for r, f in pinned]}},
                          {"inputs": {"frameImages": [r.url for r, _ in pinned]}}]
    elif refs and "referenceImages" in inputs:
        loose = _pick_refs(refs, int(inputs["referenceImages"].get("maxItems") or 4))
        used = loose
        frame_variants = [{"inputs": {"referenceImages": [r.url for r in loose]}}]
    if len(used) < len(refs):
        # Like Sogni: a model that takes no loose pictures makes the clip from
        # the prompt (storyboard shots attach the Cast to every shot).
        log.warning("runware %s: %d reference picture(s) not used (the model takes %s)", a,
                    len(refs) - len(used), "fewer" if used else "only pinned frames")
    cast = any(r.role == "character" for r in used)
    sent = _name_refs(prompt, used if not first else [])
    pictures = _pictures_need_check(used, sent)
    checked = _content_check(check_content, cast, sent, negative_prompt) or pictures
    bases = _bases(sch, "videoInference", a, sent, negative_prompt, checked, cast, pictures=pictures)
    loop = first is not None and last is not None and first.url == last.url
    for base in bases:
        _put(base, props, ("deliveryMethod",), "async")
        if loop:
            _put(base, props, ("providerSettings", "bytedance", "cameraFixed"), True)
        if settings.get("generate_audio"):
            for path, _ in _QUIET[:4]:
                _put(base, props, path, True)
    want = caps.snap(duration_seconds)
    durations = sorted(caps.durations or [want], key=lambda d: (d < want, abs(d - want)))
    res = video_resolution()
    resolutions = [res] + [r for r in ("1080p", "720p") if r != res]
    sizes = list(_size_variants(sch, str(settings.get("aspect_ratio") or "16:9"), _RES_LONG[res],
                                resolutions, frames=bool(used and first is not None)))
    variants = []
    for d in durations[:6]:
        for fv in frame_variants:
            for sv in sizes:
                v = {**fv, **sv}
                if "duration" in props:
                    v["duration"] = d
                variants.append(v)
    task = _choose(sch, bases, variants, display_name(model))
    got = int(task.get("duration") or want)
    if got != want:
        log.warning("runware %s: asked for %s s, the model takes %s s here", a, want, got)
    return task, got


# ---------- HTTP client ----------


class RunwareError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, code: Any = None, refused: bool = False,
                 final: bool = False, task_failed: bool = False, pending: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.pending = pending  # sent and maybe still running (and billing) at Runware
        self.refused = refused  # declined for its content
        self.final = final  # retrying won't help (no credit, bad key)
        self.task_failed = task_failed  # the task itself failed (not the request about it)

    @property
    def transient(self) -> bool:
        """A failure to *ask* about a task (network, overload, server error,
        a key just cleared), not a failure of the task: ask again later."""
        return not self.task_failed and (self.status == 0 or self.status in (401, 403, 408, 429)
                                         or self.status >= 500)


def _error_items(body: Any) -> list[dict]:
    """Runware errors in either shape: {"errors": [...]} (items possibly
    wrapping {"error": {...}}), or a top-level {"error": ...}."""
    if not isinstance(body, dict):
        return []
    items = body.get("errors")
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        items = []
    if body.get("error"):
        items = [*items, body["error"] if isinstance(body["error"], dict) else {"message": str(body["error"])}]
    out = []
    for it in items:
        if isinstance(it, dict) and isinstance(it.get("error"), dict):
            it = {**it, **it["error"]}
        if isinstance(it, dict):
            out.append(it)
        elif it:
            out.append({"message": str(it)})
    return out


def describe_error(body: Any, status: int = 0) -> tuple[str, str, bool]:
    """(code, message, refused-for-content) of a Runware error body."""
    items = _error_items(body)
    it = items[0] if items else {}
    code = str(it.get("code") or "")
    msg = str(it.get("message") or (body.get("message") if isinstance(body, dict) else "") or "").strip()
    refused = any(h in code.lower() for h in _CONTENT_HINTS)
    if not msg:
        msg = code or f"HTTP {status}"
    return code, msg, refused


def _retry_after(r: httpx.Response, body: Any) -> Optional[float]:
    for raw in (r.headers.get("Retry-After"),
                next((it.get("retryAfter") for it in _error_items(body) if it.get("retryAfter")), None)):
        try:
            if raw is not None:
                return max(0.0, float(raw))
        except (TypeError, ValueError):
            continue
    return None


class RunwareClient:
    def __init__(self, api_key: Optional[str] = None) -> None:
        if api_key is None:
            from ..services import provider_keys

            api_key = provider_keys.resolve("runware")
        self.api_key = api_key or ""
        self.base = get_settings().runware_base_url.rstrip("/")

    def _scrub(self, text: str) -> str:
        return text.replace(self.api_key, "***") if self.api_key else text

    def error_for(self, status: int, body: Any) -> RunwareError:
        code, msg, refused = describe_error(body, status)
        msg = self._scrub(msg)
        if refused:
            text = (f"Runware refused this request for its content ({code}): {msg}. "
                    "Vendor models stay moderated by their vendor; the open models (marked open) don't.")
        elif status == 401 or code in ("invalidApiKey", "missingApiKey"):
            text = "Runware rejected the API key (Settings → API keys)."
        elif status == 402 or "credit" in code.lower() or "balance" in code.lower():
            text = f"Runware account is out of credit ({msg})."
        else:
            text = f"Runware error {status or ''} {code}: {msg}".replace("  ", " ")
        return RunwareError(text, status=status, code=code, refused=refused,
                            final=refused or status in (401, 402, 403))

    def post(self, tasks: list[dict], *, timeout: float = 60.0) -> tuple[int, Any]:
        """POST a task array. Retries only 429/503 (the tasks didn't run)."""
        if not self.api_key:
            raise RunwareError("No Runware API key (Settings → API keys).", status=401, final=True)
        cap = get_settings().runware_max_retry_after_seconds
        for attempt in range(4):
            r = httpx.post(self.base, json=tasks, timeout=timeout,
                           headers={"Authorization": f"Bearer {self.api_key}"})
            try:
                body = r.json()
            except ValueError:
                body = {"errors": [{"code": f"http{r.status_code}", "message": r.text[:300]}]}
            if r.status_code in (429, 503) and attempt < 3:
                wait = _retry_after(r, body)
                _sleep(min(cap, wait if wait is not None else 2.0 * (attempt + 1)))
                continue
            return r.status_code, body
        return r.status_code, body  # pragma: no cover — loop always returns

    @staticmethod
    def _row(body: Any, task_uuid: str) -> Optional[dict]:
        rows = body.get("data") if isinstance(body, dict) else None
        for row in rows or []:
            if isinstance(row, dict) and row.get("taskUUID") == task_uuid:
                return row
        return None

    def get_response(self, task_uuid: str) -> Optional[dict]:
        """The task's latest row, None while Runware has nothing to report.
        Raises RunwareError: with task_failed=True when the task failed or
        isn't known; otherwise (5xx, overload, auth) the question failed and
        can be asked again (err.transient)."""
        status, body = self.post([{"taskType": "getResponse", "taskUUID": task_uuid}], timeout=30.0)
        row = self._row(body, task_uuid)
        if row is not None and str(row.get("status") or "").lower() not in ("error", "failed"):
            return row
        if row is not None:
            err = self.error_for(status, row if _error_items(row) else body)
            err.task_failed = True
            raise err
        if status == 0 or status >= 500 or status in (401, 403, 408, 429):
            raise self.error_for(status, body)  # transient: ask again later
        if status >= 400 or _error_items(body):
            err = self.error_for(status, body)  # e.g. taskNotFound
            err.task_failed = True
            raise err
        return None

    def run(self, task: dict, *, timeout: float) -> dict:
        """Send one task and return its finished row. After a timeout or a
        5xx it asks getResponse instead of resending (no second charge)."""
        uid = task["taskUUID"]
        deadline = time.monotonic() + timeout
        try:
            status, body = self.post([task], timeout=min(150.0, timeout))
        except (httpx.TimeoutException, httpx.RemoteProtocolError, httpx.ReadError) as e:
            log.warning("runware %s: no answer to the request (%s); asking for its result", uid, type(e).__name__)
            return self.wait(uid, deadline)
        row = self._row(body, uid)
        if status in (429, 503) and row is None:
            raise self.error_for(status, body)  # overloaded even after retries: it didn't run
        if status >= 500 and row is None:
            log.warning("runware %s: HTTP %s; asking for its result instead of resending", uid, status)
            return self.wait(uid, deadline)
        if row is None:
            raise self.error_for(status, body)
        if str(row.get("status") or "").lower() == "processing":
            return self.wait(uid, deadline)
        if str(row.get("status") or "").lower() in ("error", "failed"):
            raise self.error_for(status, row)
        return row

    def wait(self, task_uuid: str, deadline: float) -> dict:
        """Ask about a task until it's done. A failed question (network,
        overload, server error) is asked again; only the task failing, or
        the deadline, ends the wait early."""
        delay = 2.0
        while True:
            try:
                row = self.get_response(task_uuid)
            except RunwareError as e:
                if not e.transient:
                    raise
                log.warning("runware %s: couldn't get its result yet (%s); asking again", task_uuid, e)
                row = None
            except httpx.TransportError as e:
                log.warning("runware %s: couldn't get its result yet (%s); asking again",
                            task_uuid, type(e).__name__)
                row = None
            if row is not None and str(row.get("status") or "success").lower() not in ("processing", "pending"):
                return row
            if time.monotonic() + delay > deadline:
                raise RunwareError(f"Runware task {task_uuid} didn't finish in time", pending=True)
            _sleep(delay)
            delay = min(delay * 1.5, 10.0)

    @staticmethod
    def download(url: str) -> bytes:
        """Output URLs are public (no auth header) and live for days."""
        r = httpx.get(url, timeout=180.0, follow_redirects=True)
        r.raise_for_status()
        if not r.content:
            raise RunwareError("Runware returned an empty file")
        return r.content


def _adult_mode() -> bool:
    from ..services.content_mode import adult_mode

    return adult_mode()


def check_content_default() -> bool:
    """Runware's content check: always on unless adult mode is; then as set
    in Settings (else RUNWARE_CHECK_CONTENT)."""
    if not _adult_mode():
        return True
    try:
        from ..services import provider_keys

        return provider_keys.option("runware", "check_content")
    except Exception:  # noqa: BLE001 — no database (scripts): the config value
        return bool(get_settings().runware_check_content)


def mentions_minor(text: str) -> bool:
    from ..services.safety import mentions_minor as _m

    return _m(text)


def _content_check(explicit: Optional[bool], cast: bool, sent: str, negative: str) -> bool:
    """Refuse the exact prompt that will be sent (reference names
    included), then decide Runware's content check: the setting, forced on
    for a Cast member's picture, a prompt that mentions a child or a minor's
    age, or a sexual prompt whose "avoid" list removes signs of adulthood."""
    from ..services.safety import check_prompt, steers_young

    check_prompt(sent)
    default = check_content_default() if explicit is None else explicit
    return bool(default or cast or mentions_minor(sent) or steers_young(sent, negative))


def _pictures_need_check(used: list[ImageRef], sent: str) -> bool:
    """A sexual prompt sent with any reference picture (not only a Cast
    member's) always goes through Runware's content check: nobody has
    checked who is in the picture."""
    from ..services.safety import is_sexual

    return bool(used) and is_sexual(sent)


def _guard(prompt: str) -> None:
    """Before any paid request: stop once the monthly budget's hard cap is
    reached (a batch of jobs would otherwise keep spending past it). The
    minors check runs on the exact text sent (_content_check)."""
    _budget_guard()


def _budget_guard() -> None:
    """The monthly budget's hard cap, before any paid request."""
    from ..db import SessionLocal
    from ..services import spend

    db = SessionLocal()
    try:
        msg = spend.over_budget(db) or _over_with_in_flight(db)
    finally:
        db.close()
    if msg:
        raise RunwareError(msg, status=402, final=True)


def _over_with_in_flight(db) -> Optional[str]:  # noqa: ANN001
    """The hard cap counting Runware videos still being made: their spend is
    only recorded when they finish, so a render submitting every shot at
    once would otherwise pass the check on each one."""
    from .. import models
    from ..services import spend
    from ..services.video_jobs import ACTIVE

    b = spend.get_budget(db)
    if not b["hard_stop"] or b["monthly_usd"] <= 0 or get_settings().mock_providers:
        return None
    pending = 0.0
    for model, ctx in db.query(models.VideoJob.model, models.VideoJob.context_json).filter(
            models.VideoJob.provider == "runware", models.VideoJob.status.in_(ACTIVE)):
        try:
            pending += video_cost_usd(model, float((ctx or {}).get("duration") or 5.0))
        except Exception:  # noqa: BLE001 — an unknown model: count nothing
            continue
    if pending <= 0:
        return None
    spent = spend.month_total(db)
    if spent + pending >= b["monthly_usd"]:
        return (f"Monthly budget of ${b['monthly_usd']:.2f} reached (${spent:.2f} spent this month, "
                f"plus about ${pending:.2f} of Runware videos still being made). "
                "Raise it in Settings → Spend to keep generating.")
    return None


# ---------- catalog ----------


def catalog_entries(usable: bool) -> list[dict]:
    """Picker entries: the curated models first (with durations, frames and
    prices from their schemas), then every other live model in the index."""
    b = bundle()
    out: list[dict] = []
    rows = {r["air"]: r for r in index_rows()}
    curated = [a for a in b["curated"] if a in b["schemas"]]
    unusable = set(b.get("unusable") or [])
    bundled_index = {r.get("air") for r in b["index"]}
    checked = _checked()
    # Hidden: models the snapshot or check_new_models found the app can't
    # drive. Not checked yet: shown, as before (a refresh checks them).
    later = [a for a in rows if a not in b["schemas"] and a not in unusable
             and (a in bundled_index or not checked.get(a))]
    for a in curated + later:
        sch = b["schemas"].get(a)
        row = rows.get(a) or {}
        kind = kind_of((sch or {}).get("capabilities") or row.get("caps"))
        if kind is None:
            continue
        model = f"{PREFIX}{a}"
        open_ = is_open(a, row.get("arch"))
        name = (sch or {}).get("name") or row.get("name") or a
        tag = "open" if open_ else "moderated"
        e: dict = {"id": model, "kind": kind, "name": f"{name} · Runware {tag}", "provider": "runware",
                   "description": ("Runware-hosted open weights: no vendor moderation." if open_ else
                                   "Runs on the vendor's API through Runware (the vendor moderates it)."),
                   "uncensored": open_, "suggested": a in curated, "usable": usable}
        if kind in OP_KIND_OF.values():
            out.append(_op_entry(a, model, kind, name, sch, a in curated, usable))
            continue
        if kind == "image":
            usd = image_cost_usd(model) if sch else None
            e["notes"] = " · ".join(x for x in (tag, f"~${usd:.3f} per image" if usd else "") if x)
            e["pricing"] = {"image": usd} if usd else {}
        else:
            caps = capabilities_for(model) if sch else None
            frames = []
            if caps and caps.first_frame:
                frames.append("first_frame")
            if caps and caps.last_frame:
                frames.append("last_frame")
            rate = video_rate_usd(model) if sch else None
            bits = [tag]
            if caps:
                bits.append(f"{min(caps.durations)}–{max(caps.durations)} s")
            if rate:
                bits.append(f"~${rate:.3f}/s at {video_resolution()}")
            e.update({"notes": " · ".join(bits), "durations": caps.durations if caps else [],
                      "max_seconds": caps.max_seconds if caps else None, "frame_images": frames,
                      "resolutions": [], "aspect_ratios": [], "generate_audio": None,
                      "pricing_skus": {}, "price_per_second": rate})
        out.append(e)
    return out


# Plain-language notes for the curated picture-operation models.
_OP_NOTES = {
    "runware:112@5": "Cheap and good for most pictures.",
    "runware:109@1": "Cheap; an older remover.",
    "bria:2@1": "Cleanest edges (hair, fur); costs more.",
    "runware:504@1": "Cheap and sharp, ×2 or ×4.",
    "runware:500@1": "Adds fine detail by redrawing; slower.",
    "topazlabs:wonder@4": "Top quality; priced by output size.",
}


def _op_entry(a: str, model: str, kind: str, name: str, sch: Optional[dict], suggested: bool,
              usable: bool) -> dict:
    """A picture-operation model for Settings → Models and Studio's picker,
    priced for a 1 MP (1024 × 1024) picture."""
    from ..services.prices import money

    e: dict = {"id": model, "kind": kind, "name": name, "provider": "runware",
               "description": _OP_NOTES.get(a) or ("Removes the background, keeping transparency."
                                                   if kind == "bg_removal" else "Makes the picture larger."),
               "uncensored": False, "suggested": suggested, "usable": usable}
    if kind == "bg_removal":
        usd = op_cost_usd(model, "remove_background", out_megapixels=1.05) if sch else None
        e["pricing"] = {"image": usd} if usd else {}
        e["price_text"] = f"about {money(usd)} per picture" if usd else ""
        e["notes"] = f"~{money(usd)} per picture" if usd else ""
        return e
    factors = upscale_factors(model)
    e["factors"] = factors
    prices = {f"x{f}": op_cost_usd(model, "upscale", factor=f, out_megapixels=1.05 * f * f)
              for f in factors} if sch else {}
    e["pricing"] = prices
    if prices:
        bits = [f"~{money(v)} ×{k[1:]}" for k, v in prices.items()]
        e["notes"] = " · ".join(bits) + " (1 MP picture)"
        e["price_text"] = "about " + ", ".join(f"{money(v)} for ×{k[1:]}" for k, v in prices.items())
    else:
        e["notes"], e["price_text"] = "", ""
    return e


def _model_infos(kind: str) -> list[ModelInfo]:
    return [ModelInfo(id=e["id"], name=e["name"], modality=kind, description=e.get("notes") or "")
            for e in catalog_entries(True) if e["kind"] == kind and e.get("suggested")]


# ---------- providers ----------


class RunwareImageProvider(ImageProvider):
    """imageInference, synchronous (polls if Runware is still working)."""

    def __init__(self, *, api_key: Optional[str] = None, model: Optional[str] = None) -> None:
        self.client = RunwareClient(api_key)
        self.model = model or f"{PREFIX}runware:107@1"

    def list_models(self) -> list[ModelInfo]:
        return _model_infos("image")

    def generate(self, *, model: str, prompt: str, references: list[ImageRef],
                 negative_prompt: str = "", settings: dict | None = None) -> bytes:
        model = model or self.model
        _guard(prompt)
        task = build_image_task(model, prompt, list(references or []), negative_prompt=negative_prompt,
                                settings=settings)
        from ..services import spend

        try:
            row = self.client.run(task, timeout=get_settings().runware_image_timeout_seconds)
        except RunwareError as e:
            if e.pending:
                # Sent, and Runware may still finish (and bill) it: count the
                # estimate so the ledger and the monthly cap see it.
                spend.record(provider="runware", kind="image", model=model, cost=image_cost_usd(model),
                             is_estimate=True, source="rate_card", generation_id=task["taskUUID"],
                             note="didn't finish in time; Runware may still bill it")
            raise
        # Billed now, whether or not the download works: record it first.

        cost = row.get("cost")
        if isinstance(cost, (int, float)):
            spend.record(provider="runware", kind="image", model=model, cost=float(cost),
                         generation_id=task["taskUUID"])
        else:
            spend.record(provider="runware", kind="image", model=model, cost=image_cost_usd(model),
                         is_estimate=True, source="rate_card", generation_id=task["taskUUID"],
                         note="Runware reported no cost")
        url = row.get("imageURL")
        if not url:
            raise RunwareError(f"Runware returned no image for {air(model)}")
        if row.get("NSFWContent") is not None:
            log.info("runware %s: NSFWContent=%s", air(model), row.get("NSFWContent"))
        return RunwareClient.download(url)


def _prepared_input(model: str, image_bytes: bytes, mime: str) -> tuple[bytes, str, int, int, bool]:
    """The picture as it will be sent: scaled down to the model's input
    limits, and a large opaque PNG re-encoded as JPEG (a smaller request).
    Returns (bytes, mime, width, height, has_alpha)."""
    import io

    from PIL import Image

    with Image.open(io.BytesIO(image_bytes)) as img:
        img.load()
        alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
        w, h = img.size
        tw, th = fit_to_limits(w, h, input_limits(model))
        big_png = not alpha and (mime or "").lower() == "image/png" and len(image_bytes) > 4_000_000
        if (tw, th) == (w, h) and not big_png:
            return image_bytes, mime or "image/png", w, h, alpha
        out = img.convert("RGBA" if alpha else "RGB")
        if (tw, th) != (w, h):
            out = out.resize((tw, th), Image.LANCZOS)
    buf = io.BytesIO()
    if alpha:
        out.save(buf, format="PNG")
        return buf.getvalue(), "image/png", tw, th, True
    out.save(buf, format="JPEG", quality=95)
    return buf.getvalue(), "image/jpeg", tw, th, False


def keeps_transparency(model: str) -> bool:
    """Whether the model says it returns transparent pictures (Runware's
    'transparency' capability: the background removers do, upscalers don't)."""
    sch = schema(model, fetch=False)
    return "transparency" in ((sch or {}).get("capabilities") or [])


def restore_alpha(source: bytes, result: bytes) -> bytes:
    """`result` (an upscale of `source`) with `source`'s transparency put
    back: an upscaler that ignores the alpha channel fills the cut-out
    background in, so the alpha is enlarged here (Lanczos) to the result's
    size and applied. A result that already has transparency is kept."""
    import io

    from PIL import Image

    with Image.open(io.BytesIO(source)) as src:
        src.load()
        alpha = src.convert("RGBA").getchannel("A")
    with Image.open(io.BytesIO(result)) as res:
        res.load()
        if res.mode in ("RGBA", "LA", "P") and "A" in res.convert("RGBA").getbands():
            lo, _hi = res.convert("RGBA").getchannel("A").getextrema()
            if lo < 255:
                return result
        out = res.convert("RGBA")
    out.putalpha(alpha.resize(out.size, Image.LANCZOS))
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


class RunwareImageOpProvider(ImageOpProvider):
    """removeBackground / upscale on one picture, synchronous (polls if
    Runware is still working).

    Cast pictures are allowed here without Runware's content check (these
    models have none): the operation makes nothing new from a prompt, it
    only cuts out or enlarges the user's own picture, so the rule that a
    Cast likeness is only sent with the check on (_bases) doesn't apply."""

    def __init__(self, *, api_key: Optional[str] = None, model: Optional[str] = None) -> None:
        self.client = RunwareClient(api_key)
        self.model = model or ""

    def run(self, op: ImageOp, model: str, image_bytes: bytes, mime: str, options: dict) -> bytes:
        model = model or self.model
        _budget_guard()
        data, mime, w, h, alpha = _prepared_input(model, image_bytes, mime)
        factor = int((options or {}).get("factor") or 2) if op == "upscale" else 1
        task = build_operation_task(model, op, data, mime, options, keep_alpha=alpha)
        estimate = op_cost_usd(model, op, factor=factor, out_megapixels=w * h * factor * factor / 1e6)
        label = "remove background" if op == "remove_background" else "upscale"
        from ..services import spend

        try:
            row = self.client.run(task, timeout=get_settings().runware_image_timeout_seconds)
        except RunwareError as e:
            if e.pending:
                spend.record(provider="runware", kind="image", model=model, cost=estimate, is_estimate=True,
                             source="rate_card", generation_id=task["taskUUID"],
                             note=f"{label}; didn't finish in time, Runware may still bill it")
            raise
        cost = row.get("cost")
        if isinstance(cost, (int, float)):
            spend.record(provider="runware", kind="image", model=model, cost=float(cost),
                         generation_id=task["taskUUID"], note=label)
        else:
            spend.record(provider="runware", kind="image", model=model, cost=estimate, is_estimate=True,
                         source="rate_card", generation_id=task["taskUUID"],
                         note=f"{label}; Runware reported no cost")
        url = row.get("imageURL")
        if not url:
            raise RunwareError(f"Runware returned no picture for {air(model)}")
        result = RunwareClient.download(url)
        if alpha and op == "upscale" and not keeps_transparency(model):
            # Upscalers don't list 'transparency': keep a cut-out a cut-out.
            result = restore_alpha(data, result)
        return result


class RunwareVideoProvider(VideoProvider):
    """videoInference, async: submit, then the sweeper polls getResponse."""

    def __init__(self, *, api_key: Optional[str] = None, model: Optional[str] = None) -> None:
        self.client = RunwareClient(api_key)
        self.model = model or f"{PREFIX}lightricks:ltx@2.3-fast"

    def list_models(self) -> list[ModelInfo]:
        return _model_infos("video")

    def capabilities(self, model: str) -> VideoCapabilities:
        return capabilities_for(model or self.model)

    def submit(self, *, model: str, prompt: str, references: list[ImageRef], negative_prompt: str = "",
               reference_strategy: str = "input_references", duration_seconds: float = 5.0,
               settings: dict | None = None) -> SubmittedJob:
        model = model or self.model
        _guard(prompt)
        task, duration = build_video_task(
            model, prompt, list(references or []), negative_prompt=negative_prompt,
            reference_strategy=reference_strategy, duration_seconds=duration_seconds,
            settings=settings)
        uid = task["taskUUID"]
        try:
            status, body = self.client.post([task], timeout=60.0)
        except (httpx.TimeoutException, httpx.RemoteProtocolError, httpx.ReadError) as e:
            # It may have been accepted: polling decides (never resend).
            log.warning("runware %s: no acknowledgement (%s); polling decides", uid, type(e).__name__)
            status, body = 0, {}
        if status in (429, 503) or 400 <= status < 500:
            raise self.client.error_for(status, body)  # it didn't run
        if 0 < status < 400 and self.client._row(body, uid) is None:
            raise self.client.error_for(status, body)
        if status >= 500:
            log.warning("runware %s: HTTP %s on submit; polling decides", uid, status)
        log.info("runware %s submitted: %s", uid, json.dumps(body)[:300] if body else "(no body)")
        return SubmittedJob(provider="runware", model=model, job_id=uid, polling_url="",
                            raw={"duration": duration})

    def poll(self, job: SubmittedJob) -> JobStatus:
        try:
            row = self.client.get_response(job.job_id)
        except RunwareError as e:
            if e.task_failed:
                return JobStatus(state="failed", error=str(e))
            raise  # couldn't ask (5xx, overload, key): the sweeper asks again until the deadline
        if row is None:
            return JobStatus(state="queued")
        st = str(row.get("status") or "").lower()
        url = row.get("videoURL")
        cost = row.get("cost") if isinstance(row.get("cost"), (int, float)) else None
        if url and st in ("", "success", "succeeded", "completed"):
            return JobStatus(state="succeeded", progress=1.0, result_url=url, cost=cost)
        if st in ("success", "succeeded", "completed"):
            return JobStatus(state="failed", error=f"Runware finished {job.job_id} without a video")
        progress = row.get("progress")
        p = float(progress) / (100.0 if float(progress or 0) > 1 else 1.0) if isinstance(progress, (int, float)) else 0.0
        return JobStatus(state="running", progress=min(max(p, 0.0), 0.99))

    def download(self, job: SubmittedJob, result_url: str | None = None) -> bytes:
        if not result_url:
            st = self.poll(job)
            if st.state != "succeeded" or not st.result_url:
                raise RuntimeError(f"Runware job not ready: {st.state} {st.error or ''}".strip())
            result_url = st.result_url
        return RunwareClient.download(result_url)


_no_schema_until: dict[str, float] = {}


def takes_reference(model: str) -> bool:
    """Whether a still model applies reference pictures (an edit model with
    inputs.referenceImages). Image-to-image-only models keep the layout but
    barely change the light, so they don't count for "same place" stills.
    Fetches a model's schema on first use (as the request builder would), so
    a model nobody has run yet isn't wrongly sent without its reference."""
    if get_settings().mock_providers:
        sch = schema(model, fetch=False)
    elif time.monotonic() < _no_schema_until.get(air(model), 0.0):
        sch = None  # failed to fetch a moment ago: don't wait 30 s again
    else:
        try:
            sch = schema(model)
        except Exception:  # noqa: BLE001 — the docs unreachable: no reference this time
            _no_schema_until[air(model)] = time.monotonic() + 600
            sch = None
    inputs = (_sub(_props(sch), "inputs") or {}).get("properties") or {} if sch else {}
    return "referenceImages" in inputs


def is_video_model(model: str) -> bool:
    a = air(model)
    sch = bundle()["schemas"].get(a)
    if sch:
        return kind_of(sch.get("capabilities")) == "video"
    row = next((r for r in index_rows() if r.get("air") == a), None)
    return bool(row) and kind_of(row.get("caps")) == "video"


def smoke_build(model: str) -> Optional[str]:
    """None when the app can build a valid request for this model the way
    it uses it (text, or a first frame / a reference where the model needs
    one); else why not. The snapshot script hides models that fail."""
    from .base import ImageRef

    sch = schema(model)
    if sch is None:
        return "no schema"
    caps = set(sch.get("capabilities") or [])
    pic = ImageRef(url="https://example.com/picture.jpg", role="scene", frame_type="first_frame")
    tries: list = []
    kind = kind_of(caps)
    if kind == "image":
        if "text-to-image" in caps:
            tries.append(lambda: build_image_task(model, "a quiet street at dusk", [], check_content=False))
        tries.append(lambda: build_image_task(model, "the same street at night", [pic], check_content=False))
    elif kind == "video":
        tries.append(lambda: build_video_task(model, "rain falls on a quiet street", [],
                                              duration_seconds=5, check_content=False))
        tries.append(lambda: build_video_task(model, "rain falls on a quiet street", [pic],
                                              reference_strategy="frame_images", duration_seconds=5,
                                              check_content=False))
    elif kind in OP_KIND_OF.values():
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (64, 64), (90, 120, 150)).save(buf, format="PNG")
        op = "remove_background" if kind == "bg_removal" else "upscale"
        tries.append(lambda: build_operation_task(model, op, buf.getvalue(), "image/png",
                                                  {"factor": (upscale_factors(model) or [2])[0]}))
    else:
        return "not an image or video model"
    err = None
    for t in tries:
        try:
            t()
            return None
        except RunwareError as e:
            err = err or str(e)
    return err or "nothing to try"

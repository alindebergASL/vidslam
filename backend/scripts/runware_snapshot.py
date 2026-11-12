"""Regenerate app/providers/runware_models.json from Runware's public model
docs (no API key needed):

    python scripts/runware_snapshot.py            # from the backend/ folder

It keeps:
- the live image and video models from the index (for the catalog);
- the request schema of each curated model (CURATED below), without its
  descriptions, plus its modes, capabilities and prices. These schemas
  drive the payload builder in providers/runware.py and the contract tests;
- "unusable": the other models the app can't build a request for (they need
  inputs it never sends, e.g. a video to edit or an audio track), checked
  with the app's own builder, so the catalog doesn't offer them. Run it
  where the backend imports (e.g. inside the backend container) for this;
  elsewhere the list is left empty.

Any other model's schema is fetched the first time it is used (and cached).
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

INDEX_URL = "https://runware.ai/docs/models/index.json"
SCHEMA_URL = "https://runware.ai/docs/models/{id}/schema.json"
OUT = Path(__file__).resolve().parents[1] / "app" / "providers" / "runware_models.json"

# Suggested first, in picker order. Open = Runware-hosted open weights
# (no vendor moderation; Runware's own content check follows Settings in
# adult mode and is always on outside it). None of the generation models
# here documents a vendor safety setting (providers/runware._PERMISSIVE;
# checked 2026-11-12), so adult mode loosens nothing on them. If a new
# snapshot adds one, tests/test_runware_vendor_knobs.py says so.
CURATED = [
    # photoreal stills
    "runware:107@1",          # FLUX.1 Krea [dev] (open)
    "runware:111@1",          # FLUX.1 dev SRPO (open)
    "rundiffusion:130@100",   # Juggernaut Pro Flux (open)
    "runware:z-image@turbo",  # Z-Image-Turbo (open)
    "krea:krea@2-raw",        # Krea 2 Raw (open)
    "alibaba:qwen-image@2512",  # Qwen-Image-2512 (open)
    # edits with reference pictures
    "alibaba:qwen-image-edit@2511",
    "runware:108@22",         # Qwen-Image-Edit-Plus (open)
    "runware:400@2",          # FLUX.2 klein 9B (open)
    "runware:400@1",          # FLUX.2 dev (open)
    "runware:106@1",          # FLUX.1 Kontext dev (open)
    # video: first + last frame
    "lightricks:ltx@2.3-fast",  # open
    "lightricks:ltx@2.3",       # open
    "google:veo@3.1-lite",
    "vidu:4@2",                 # Vidu Q3 Turbo
    "alibaba:wan@2.7",
    "luma:ray@3.2",
    "klingai:6@1",              # Kling 2.5 Turbo Pro
    "bytedance:2@1",            # Seedance 1.0 Pro
    # picture operations: remove background
    "runware:112@5",            # BiRefNet General (the default)
    "runware:109@1",            # RemBG v1.4
    "bria:2@1",                 # Bria RMBG v2.0
    # picture operations: upscale
    "runware:504@1",            # Real-ESRGAN (the default)
    "runware:500@1",            # Clarity
    "topazlabs:wonder@4",       # Topaz Labs Wonder 4
]

IMAGE_CAPS = {"text-to-image", "image-to-image", "edit"}
VIDEO_CAPS = {"text-to-video", "image-to-video"}
DROP_KEYS = {"description", "title", "examples", "x-docs", "$comment", "markdownDescription", "deprecated"}


def _get(url: str) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": "vidslam-runware-snapshot"})
    with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 — fixed https URLs
        return json.loads(r.read())


def strip(o: object) -> object:
    if isinstance(o, dict):
        return {k: strip(v) for k, v in o.items() if k not in DROP_KEYS}
    if isinstance(o, list):
        return [strip(v) for v in o]
    return o


def index_row(e: dict) -> dict | None:
    caps = set(e.get("capabilities") or [])
    if e.get("status") != "live" or not e.get("air"):
        return None
    if not caps & (IMAGE_CAPS | VIDEO_CAPS):
        return None
    return {k: v for k, v in {
        "air": e["air"], "id": e["id"], "name": e.get("name") or e["id"],
        "caps": sorted(caps), "arch": e.get("architecture"), "creator": e.get("creator"),
    }.items() if v not in (None, "")}


def schema_entry(doc: dict) -> dict:
    info = doc.get("info") or {}
    request = doc["components"]["schemas"]["RequestBody"]
    items = request.get("items", request)
    return {
        "capabilities": info.get("x-capabilities") or [],
        "modes": info.get("x-modes") or [],
        "pricing": info.get("x-pricing") or {},
        "request": strip(items),
    }


def smoke_test(rows: list[dict], curated: dict) -> list[str]:
    """AIRs of index models the app's request builder can't serve."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from app.providers import runware
    except Exception as e:  # noqa: BLE001 — not inside the backend: skip
        print(f"not checking which models the app can use ({e})", file=sys.stderr)
        return []
    bad = []
    for r in rows:
        a = r["air"]
        if a in curated:
            continue
        try:
            runware._schemas[a] = {"id": r["id"], "name": r["name"],
                                   **schema_entry(_get(SCHEMA_URL.format(id=r["id"])))}
            why = runware.smoke_build(runware.PREFIX + a)
        except Exception as e:  # noqa: BLE001 — a schema we can't read is unusable too
            why = str(e)
        if why:
            bad.append(a)
            print(f"unusable: {a} ({why[:120]})", file=sys.stderr)
    for a in curated:
        why = runware.smoke_build(runware.PREFIX + a)
        if why:
            raise SystemExit(f"curated model {a} fails the builder: {why}")
    return sorted(bad)


def main() -> int:
    index = _get(INDEX_URL)
    by_air = {e.get("air"): e for e in index if e.get("air")}
    rows = [r for r in (index_row(e) for e in index) if r]
    schemas = {}
    for air in CURATED:
        e = by_air.get(air)
        if not e or e.get("status") != "live":
            print(f"skipping {air}: not live in the index", file=sys.stderr)
            continue
        schemas[air] = {"id": e["id"], "name": e.get("name") or e["id"],
                        **schema_entry(_get(SCHEMA_URL.format(id=e["id"])))}
    unusable = smoke_test(rows, schemas)
    out = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "curated": [a for a in CURATED if a in schemas],
        "index": rows,
        "schemas": schemas,
        "unusable": unusable,
    }
    OUT.write_text(json.dumps(out, separators=(",", ":"), sort_keys=True) + "\n")
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB, {len(rows)} models, {len(schemas)} schemas)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

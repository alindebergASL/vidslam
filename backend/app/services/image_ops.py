"""Studio picture operations: remove a background or upscale a finished
Studio picture or a Cast picture. Each run is a new Studio job (operation
set, the source recorded) that the worker runs like any other image job.

The checks here run before anything is queued (the API turns OpError into
its status code) and again where it matters in the worker (the source can
be deleted while the job waits)."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from .. import models
from . import model_registry as reg
from . import storage
from .prices import money

OPS = ("remove_background", "upscale")
KIND_OF = {"remove_background": "bg_removal", "upscale": "upscale"}
FACTORS = (2, 4)
STUDIO_DELETED = "deleted"


class OpError(Exception):
    """A request that can't run; `status` is the HTTP status to answer."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def kind_for(op: str) -> str:
    if op not in KIND_OF:
        raise OpError(422, "op must be remove_background or upscale")
    return KIND_OF[op]


def factor_of(job_or_options) -> int:
    opts = job_or_options if isinstance(job_or_options, dict) else (job_or_options.options_json or {})
    try:
        return int(opts.get("factor") or 2)
    except (TypeError, ValueError):
        return 2


# ---------- sources ----------


def source_job(db: Session, job_id: int) -> models.AssetGenerationJob:
    """A finished Studio picture whose file is still there."""
    src = db.get(models.AssetGenerationJob, job_id)
    if src is None or src.status == STUDIO_DELETED:
        raise OpError(404, "That picture was deleted")
    if src.output_kind != "image":
        raise OpError(422, "Only pictures can have their background removed or be upscaled, not clips")
    if src.status not in ("completed", "saved"):
        raise OpError(409, "That picture isn't finished yet")
    if not src.result_path or not Path(src.result_path).exists():
        raise OpError(409, "That picture's file is missing")
    return src


def source_asset(db: Session, asset_id: int) -> models.Asset:
    """A Cast (or ingredient) picture whose file is still there."""
    asset = db.get(models.Asset, asset_id)
    if asset is None:
        raise OpError(404, "That picture was deleted")
    if not (asset.mime_type or "").startswith("image/"):
        raise OpError(422, "Only pictures can have their background removed or be upscaled")
    if not asset.file_path or not Path(asset.file_path).exists():
        raise OpError(409, "That picture's file is missing")
    return asset


def asset_owner(asset: models.Asset) -> tuple[str, int]:
    if asset.owner_kind == "avatar" and asset.avatar_id is not None:
        return "avatar", int(asset.avatar_id)
    if asset.owner_kind == "ingredient" and asset.ingredient_id is not None:
        return "ingredient", int(asset.ingredient_id)
    raise OpError(422, "That picture doesn't belong to anyone in the Cast")


def _source_path(db: Session, job: models.AssetGenerationJob) -> Path:
    """The picture an operation job works on; ValueError when it's gone."""
    if job.source_job_id:
        src = db.get(models.AssetGenerationJob, job.source_job_id)
        if (src is None or src.status == STUDIO_DELETED or src.output_kind != "image"
                or not src.result_path or not Path(src.result_path).exists()):
            raise ValueError("The original picture was deleted")
        return Path(src.result_path)
    if job.source_asset_id:
        asset = db.get(models.Asset, job.source_asset_id)
        if asset is None or not asset.file_path or not Path(asset.file_path).exists():
            raise ValueError("The original picture was deleted")
        return Path(asset.file_path)
    raise ValueError("The original picture was deleted")


def load_source(db: Session, job: models.AssetGenerationJob) -> tuple[bytes, str]:
    """(bytes, mime type) of the job's source picture."""
    raw = _source_path(db, job).read_bytes()
    _ext, mime, _w, _h = storage.image_info(raw)
    return raw, mime


# ---------- models, sizes and prices ----------


def _runware():
    from ..providers import runware

    return runware


def model_name(model: str) -> str:
    if model.startswith(reg.LOCAL_PREFIX):
        return "Basic upscale (free, no AI)"
    if model.startswith("mock/"):
        return "Placeholder (mock)"
    rw = _runware()
    if rw.is_runware(model):
        return rw.display_name(model)
    return model


def check_model(op: str, model: str) -> None:
    """422 when `model` can't do `op` (e.g. an upscaler asked to remove a
    background, or the free upscale asked to)."""
    kind = kind_for(op)
    if model.startswith(reg.LOCAL_PREFIX):
        if model not in reg.LOCAL_MODELS.get(kind, ()):
            raise OpError(422, "The basic (free) option can only upscale")
        return
    rw = _runware()
    if not rw.is_runware(model):
        return  # mock/... or an id the provider gate refuses (409)
    sch = rw.schema(model, fetch=False)
    row = None if sch else next((r for r in rw.index_rows() if r.get("air") == rw.air(model)), None)
    caps = (sch or {}).get("capabilities") or (row or {}).get("caps")
    if caps is not None and rw.kind_of(caps) != kind:
        what = "remove backgrounds" if op == "remove_background" else "upscale pictures"
        raise OpError(422, f"{model_name(model)} can't {what}; pick another model")


def factors_for(model: str) -> list[int]:
    rw = _runware()
    if rw.is_runware(model):
        return rw.upscale_factors(model)
    return list(FACTORS)


def check_factor(model: str, factor: int) -> None:
    if factor not in FACTORS:
        raise OpError(422, "Upscale by 2 or 4")
    if factor not in factors_for(model):
        raise OpError(422, f"{model_name(model)} can't upscale ×{factor}; pick ×"
                           f"{' or ×'.join(str(f) for f in factors_for(model)) or '2'} or another model")


def output_size(op: str, model: str, width: int, height: int, factor: int) -> tuple[int, int]:
    """The result's size: the picture as the model takes it (some take only
    so many pixels, so it's scaled down first), times the factor."""
    rw = _runware()
    if rw.is_runware(model) and width and height:
        width, height = rw.fit_to_limits(width, height, rw.input_limits(model))
    n = factor if op == "upscale" else 1
    return width * n, height * n


def too_large_message(width: int, height: int) -> Optional[str]:
    if max(width, height) > storage.MAX_IMAGE_SIDE:
        return (f"That would make a {width} × {height} px picture; the most is {storage.MAX_IMAGE_SIDE} px "
                "on the long side. Try ×2, or a smaller picture.")
    return None


def cost_usd(op: str, model: str, factor: int, width: int, height: int) -> float:
    """Rate-card price of one run ($0 for the free upscale and the mock)."""
    if model.startswith(reg.LOCAL_PREFIX) or model.startswith("mock/"):
        return 0.0
    rw = _runware()
    if rw.is_runware(model):
        out_w, out_h = output_size(op, model, width or 1024, height or 1024, factor)
        return rw.op_cost_usd(model, op, factor=factor if op == "upscale" else 1,
                              out_megapixels=out_w * out_h / 1e6)
    return 0.0


def source_dims(db: Session, *, source_job_id: Optional[int] = None,
                source_asset_id: Optional[int] = None) -> tuple[int, int]:
    if source_job_id is not None:
        return storage.image_size(Path(source_job(db, source_job_id).result_path))
    if source_asset_id is not None:
        asset = source_asset(db, source_asset_id)
        if asset.width and asset.height:
            return int(asset.width), int(asset.height)
        return storage.image_size(Path(asset.file_path))
    return 0, 0


def estimate(db: Session, op: str, model: Optional[str] = None, factor: int = 2, *,
             source_job_id: Optional[int] = None, source_asset_id: Optional[int] = None) -> dict:
    """What a run would cost and make, before it runs."""
    kind = kind_for(op)
    model = model or reg.active_model(kind)
    check_model(op, model)
    if op == "upscale":
        check_factor(model, factor)
    w, h = source_dims(db, source_job_id=source_job_id, source_asset_id=source_asset_id)
    out_w, out_h = output_size(op, model, w, h, factor) if w and h else (0, 0)
    usd = cost_usd(op, model, factor, w, h)
    big = too_large_message(out_w, out_h) if out_w else None
    size = f" · {out_w} × {out_h} px" if out_w else ""
    if big:
        message = big
    elif usd <= 0:
        message = f"Free{size}"
    else:
        message = f"About {money(usd)}{size}"
    return {
        "op": op, "model": model, "model_name": model_name(model), "provider": reg.provider_for(kind, model),
        "cost_usd": round(usd, 4), "is_estimate": True, "free": usd <= 0,
        "factor": factor if op == "upscale" else None,
        "output_width": out_w or None, "output_height": out_h or None,
        "too_large": bool(big), "message": message,
    }


def runs_now(kind: str, model: str) -> bool:
    """Whether a run of `model` would be accepted now (the same routing the
    provider gate checks: not switched off, set up, a known model)."""
    provider, blocked = reg.route_info(kind, model)
    return provider != "unavailable" and blocked is None


def _runware_entry(kind: str, model: str) -> Optional[dict]:
    rw = _runware()
    if not rw.is_runware(model):
        return None
    return next((e for e in rw.catalog_entries(False) if e["id"] == model and e["kind"] == kind), None)


def models_for(op: str) -> list[dict]:
    """The models offered for `op` (Runware's, the free one), each with its
    price in plain words and whether it can run now; the active one is
    marked."""
    from ..config import get_settings
    from . import model_catalog
    from .model_catalog import _local_entries

    kind = kind_for(op)
    active = reg.active_model(kind)
    mock = get_settings().mock_providers
    snap = model_catalog.get_snapshot() or {}
    # A provider switched off (or without its key) isn't offered, except in
    # mock mode, where its models run through the placeholder provider.
    entries = [e for e in snap.get("entries") or [] if e.get("kind") == kind
               and (e.get("enabled") is not False or (mock and runs_now(kind, e["id"])))]
    if not snap:
        from ..providers import runware

        entries = [e for e in runware.catalog_entries(False) if e["kind"] == kind] + \
                  [e for e in _local_entries() if e["kind"] == kind]
    out = []
    for e in entries:
        if not e.get("suggested") and e["id"] != active:
            continue
        out.append({
            "id": e["id"], "name": e.get("name") or e["id"], "description": e.get("description") or "",
            "provider": e.get("provider") or "", "price_text": e.get("price_text") or "",
            "free": e.get("provider") == "local", "usable": runs_now(kind, e["id"]),
            "factors": e.get("factors") or ([2, 4] if kind == "upscale" else []),
            "active": e["id"] == active,
        })
    if not any(m["active"] for m in out):
        known = _runware_entry(kind, active) or {}
        out.insert(0, {"id": active, "name": model_name(active), "description": known.get("description") or "",
                       "provider": reg.provider_for(kind, active), "price_text": known.get("price_text") or "",
                       "free": active.startswith(reg.LOCAL_PREFIX) or active.startswith("mock/"),
                       "usable": runs_now(kind, active),
                       "factors": factors_for(active) if kind == "upscale" else [], "active": True})
    return out


# ---------- jobs ----------


def labels(op: str, factor: int, *, source_job_id: Optional[int] = None,
           source_asset_id: Optional[int] = None) -> tuple[str, str]:
    """(prompt, label) for an operation job: a readable line and the tile tag."""
    where = f"#{source_job_id}" if source_job_id is not None else f"Cast picture #{source_asset_id}"
    if op == "remove_background":
        return f"Background removed from {where}", "No background"
    return f"Upscaled ×{factor} from {where}", f"Upscaled ×{factor}"


def new_job(db: Session, op: str, model: Optional[str], factor: int, *,
            source_job_id: Optional[int] = None, source_asset_id: Optional[int] = None,
            label: Optional[str] = None) -> models.AssetGenerationJob:
    """The checked, unsaved job for one run (the caller stores and queues
    it). OpError for anything that can't run."""
    from .provider_gate import require_model

    kind = kind_for(op)
    model = model or reg.active_model(kind)
    check_model(op, model)
    if op == "upscale":
        check_factor(model, factor)
    if source_job_id is not None:
        src = source_job(db, source_job_id)
        owner_kind, owner_id = src.owner_kind, src.owner_id
    elif source_asset_id is not None:
        owner_kind, owner_id = asset_owner(source_asset(db, source_asset_id))
    else:
        raise OpError(422, "Pick a picture")
    # The model's provider switched off in Settings: 409 before anything is queued.
    require_model(kind, model, db)
    w, h = source_dims(db, source_job_id=source_job_id, source_asset_id=source_asset_id)
    if w and h:
        big = too_large_message(*output_size(op, model, w, h, factor))
        if big:
            raise OpError(422, big)
    prompt, default_label = labels(op, factor, source_job_id=source_job_id, source_asset_id=source_asset_id)
    return models.AssetGenerationJob(
        owner_kind=owner_kind, owner_id=owner_id, output_kind="image", prompt=prompt,
        negative_prompt="", reference_asset_ids_json=[], provider_model=model,
        label=default_label if label is None else label, operation=op,
        source_job_id=source_job_id, source_asset_id=source_asset_id,
        options_json={"factor": factor} if op == "upscale" else {}, status="pending",
    )


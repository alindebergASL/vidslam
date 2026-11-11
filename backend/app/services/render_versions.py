"""One version numbering for a project's renders, used everywhere a version
is shown (render page history, Publishing / Results, the export zip).

Completed base renders are numbered 1, 2, 3… in creation order (an expired
one keeps its number, so retention never renumbers the others). A hook-lab
variant carries its base render's number and its opening label: "Version 3
· opening B". Failed, cancelled and running renders are not numbered.
"""
from __future__ import annotations

from collections import Counter
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from .. import models

NUMBERED = ("completed", "expired")


def numbers(db: Session, project_ids: Iterable[int]) -> dict[int, int]:
    """render id → version number (variants: their base render's number)."""
    ids = list(set(project_ids))
    if not ids:
        return {}
    R = models.Render
    rows = (db.query(R.id, R.project_id, R.hook_variant_id)
            .filter(R.project_id.in_(ids), R.status.in_(NUMBERED))
            .order_by(R.project_id, R.created_at, R.id).all())
    out: dict[int, int] = {}
    seen: Counter = Counter()
    variants: list[tuple[int, int]] = []
    for rid, pid, vid in rows:
        if vid:
            variants.append((rid, vid))
            continue
        seen[pid] += 1
        out[rid] = seen[pid]
    if variants:
        bases = dict(db.query(models.HookVariant.id, models.HookVariant.base_render_id)
                     .filter(models.HookVariant.id.in_({v for _, v in variants})).all())
        for rid, vid in variants:
            n = out.get(bases.get(vid) or 0)
            if n is not None:
                out[rid] = n
    return out


def _opening_labels(db: Session, renders: list[models.Render]) -> dict[int, str]:
    vids = {r.hook_variant_id for r in renders if r.hook_variant_id}
    if not vids:
        return {}
    return dict(db.query(models.HookVariant.id, models.HookVariant.label)
                .filter(models.HookVariant.id.in_(vids)).all())


def label(status: str, number: Optional[int], opening: str = "") -> str:
    if number is not None:
        return f"Version {number}" + (f" · opening {opening}" if opening else "")
    if status == "failed":
        return "Failed attempt"
    if status == "cancelled":
        return "Cancelled attempt"
    if status in NUMBERED:
        return "Version"
    return "In progress"


def describe(db: Session, renders: list[models.Render]) -> dict[int, dict]:
    """render id → {"version": n or None, "version_label": "Version 3 ·
    opening B" / "Failed attempt" / …, "opening": "B" or ""}."""
    nums = numbers(db, {r.project_id for r in renders})
    openings = _opening_labels(db, renders)
    out: dict[int, dict] = {}
    for r in renders:
        opening = openings.get(r.hook_variant_id or 0, "") if r.hook_variant_id else ""
        n = nums.get(r.id)
        out[r.id] = {"version": n, "version_label": label(r.status, n, opening), "opening": opening}
    return out

"""Give older music, sound and image spend rows their project.

Before spend was scoped inside the ambient and music services, a bed or
picture made from the scene page (or a storyboard's Generate music) was
recorded with no project id. Each of those calls also wrote a provider_logs
row with the project, just after the provider answered, so a spend row can
be matched to the log of the same kind that follows it within a few
seconds.

Only unambiguous matches are applied: every candidate log in the window must
name the same project. Logs with no project (a preset preview's picture, say)
count as candidates too, so a row near one of them is left alone rather than
charged to a scene whose log happens to follow. Dry run by default; pass --apply to write.

    python -m scripts.backfill_spend_project_ids            # show what would change
    python -m scripts.backfill_spend_project_ids --apply    # write it
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional

# Spend kind → the provider_logs endpoints that make it.
ENDPOINTS: dict[str, tuple[str, ...]] = {
    "music": ("music.generate",),
    "sound": ("sound.generate",),
    "image": ("image.generate",),
}
WINDOW = timedelta(seconds=30)


@dataclass(frozen=True)
class Row:
    id: int
    kind: str
    at: datetime


@dataclass(frozen=True)
class Log:
    project_id: Optional[int]
    endpoint: str
    at: datetime


def match(rows: Iterable[Row], logs: Iterable[Log], window: timedelta = WINDOW) -> dict[int, int]:
    """spend id → project id, for rows whose following logs (same kind,
    within `window`) all name one project."""
    logs = sorted(logs, key=lambda x: x.at)
    out: dict[int, int] = {}
    for r in rows:
        prefixes = ENDPOINTS.get(r.kind)
        if not prefixes:
            continue
        found = {lg.project_id for lg in logs
                 if lg.endpoint.startswith(prefixes) and r.at <= lg.at <= r.at + window}
        if len(found) == 1:
            pid = found.pop()
            if pid is not None:
                out[r.id] = pid
    return out


def run(apply: bool, window: timedelta = WINDOW, db=None) -> dict[int, int]:  # noqa: ANN001
    from app import models
    from app.db import SessionLocal

    own = db is None
    db = db or SessionLocal()
    try:
        spend_rows = (db.query(models.SpendEntry)
                      .filter(models.SpendEntry.project_id.is_(None),
                              models.SpendEntry.kind.in_(list(ENDPOINTS)))
                      .all())
        if not spend_rows:
            return {}
        start = min(e.created_at for e in spend_rows)
        end = max(e.created_at for e in spend_rows) + window
        logs = (db.query(models.ProviderLog)
                .filter(models.ProviderLog.status == "ok",
                        models.ProviderLog.created_at >= start,
                        models.ProviderLog.created_at <= end)
                .all())
        found = match([Row(e.id, e.kind, e.created_at) for e in spend_rows],
                      [Log(lg.project_id, lg.endpoint or "", lg.created_at) for lg in logs], window)
        by_id = {e.id: e for e in spend_rows}
        for sid, pid in sorted(found.items()):
            e = by_id[sid]
            print(f"spend {sid} ({e.kind}, {e.created_at:%Y-%m-%d %H:%M:%S}, ${e.cost_usd:.3f}) -> project {pid}")
            if apply:
                e.project_id = pid
        if apply:
            db.commit()
        print(f"{len(found)} of {len(spend_rows)} unattributed rows matched"
              + ("; written" if apply else " (dry run: pass --apply to write)"))
        return found
    finally:
        if own:
            db.close()


def main(argv: Optional[list[str]] = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--apply", action="store_true", help="write the matches (default: dry run)")
    p.add_argument("--window", type=float, default=WINDOW.total_seconds(),
                   help="seconds after a spend row to look for its provider log")
    a = p.parse_args(argv)
    run(a.apply, timedelta(seconds=a.window))


if __name__ == "__main__":
    main()

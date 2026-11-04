"""One-time copy of an existing SQLite database into Postgres.

    docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm backend \\
        python -m app.tools.sqlite_to_postgres --source sqlite:////data/app.db

The target defaults to DATABASE_URL. Steps: migrate the target to head, refuse
if it already has data (unless --force), copy every table in dependency order
(breaking the assets ↔ asset_generation_jobs cycle in two passes), then move
each id sequence past the copied rows. Files under DATA_DIR aren't touched;
the paths stored in the rows stay valid as long as DATA_DIR is the same.
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import MetaData, create_engine, func, select, text


def copy(source_url: str, target_url: str, *, force: bool = False, batch: int = 500) -> dict[str, int]:
    from alembic import command

    from .. import db as appdb
    from .. import models  # noqa: F401 — registers tables

    src = create_engine(source_url)
    appdb.reset_engine(target_url)
    tgt = appdb.engine
    if tgt.dialect.name != "postgresql":
        raise SystemExit(f"target must be Postgres, got {tgt.dialect.name}")
    command.upgrade(appdb._alembic_config(), "head")

    meta = appdb.Base.metadata
    tables = [t for t in meta.sorted_tables]
    src_meta = MetaData()
    src_meta.reflect(bind=src)
    counts: dict[str, int] = {}
    with tgt.begin() as out:
        existing = sum(out.execute(select(func.count()).select_from(t)).scalar() for t in tables)
        if existing and not force:
            raise SystemExit(f"target already has {existing} rows; pass --force to copy anyway")
        if existing and force:
            names = ", ".join(t.name for t in tables)
            out.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
        deferred: list[tuple[int, int]] = []  # (job id, result_asset_id)
        for t in tables:
            if t.name not in src_meta.tables:
                counts[t.name] = 0
                continue
            cols = [c.name for c in t.columns if c.name in src_meta.tables[t.name].columns]
            rows = []
            with src.connect() as inp:
                for r in inp.execute(select(*[src_meta.tables[t.name].c[c] for c in cols])):
                    row = dict(zip(cols, r, strict=True))
                    if t.name == "asset_generation_jobs" and row.get("result_asset_id"):
                        deferred.append((row["id"], row["result_asset_id"]))
                        row["result_asset_id"] = None  # assets don't exist yet
                    rows.append(row)
            for i in range(0, len(rows), batch):
                out.execute(t.insert(), rows[i:i + batch])
            counts[t.name] = len(rows)
        jobs = meta.tables["asset_generation_jobs"]
        for job_id, asset_id in deferred:
            out.execute(jobs.update().where(jobs.c.id == job_id).values(result_asset_id=asset_id))
        # Sequences: next id must be past the copied max.
        for t in tables:
            if "id" in t.columns and t.columns["id"].autoincrement is not False and t.columns["id"].type.python_type is int:
                out.execute(text(
                    f"SELECT setval(pg_get_serial_sequence('{t.name}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {t.name}), 0) + 1, false)"))
    return counts


def main(argv: list[str] | None = None) -> int:
    from ..config import get_settings

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", required=True, help="sqlite:////path/to/app.db")
    ap.add_argument("--target", default=None, help="postgresql+psycopg://… (default: DATABASE_URL)")
    ap.add_argument("--force", action="store_true", help="truncate a non-empty target first")
    a = ap.parse_args(argv)
    target = a.target or get_settings().database_url
    counts = copy(a.source, target, force=a.force)
    width = max(len(k) for k in counts)
    for k, v in counts.items():
        print(f"  {k:<{width}}  {v}")
    print(f"copied {sum(counts.values())} rows into {target.split('@')[-1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

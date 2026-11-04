# Postgres (production database)

The production compose overlay runs **Postgres 17**. SQLite stays the dev
default and still works for one person, but in production several processes
write at once: two web workers, the RQ worker, and the model-catalog poller.
SQLite serializes all of those writes, and Postgres doesn't.

## New deployment

```bash
# .env
POSTGRES_PASSWORD=$(openssl rand -hex 24)   # URL-safe

docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

The backend and worker wait for Postgres to be healthy. At boot the app runs
the Alembic migrations itself, under a Postgres advisory lock, so when several
processes start at once one migrates and the others wait. There's no manual
`alembic upgrade` step.

## Moving an existing SQLite deployment

```bash
# 1. Stop writers but keep the data volume.
docker compose -f docker-compose.yml -f docker-compose.prod.yml stop backend worker

# 2. Start Postgres, then copy (refuses if Postgres already has rows; --force truncates).
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d postgres
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm backend \
    python -m app.tools.sqlite_to_postgres --source sqlite:////data/app.db

# 3. Start everything.
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

The copy migrates Postgres to the latest schema, copies every table in
dependency order, and moves each id sequence past the copied rows. Files
under `DATA_DIR` (uploads, renders) aren't touched; the paths stored in the
database stay valid because `DATA_DIR` is unchanged. Keep `app.db` until
you've checked the app on Postgres.

## What changed to support it

- **The initial migration only worked on SQLite.** It created tables
  alphabetically, so foreign keys pointed at tables that didn't exist yet,
  which Postgres rejects. It now runs in dependency order, and the one
  circular reference (assets ↔ asset_generation_jobs) is added as a
  separate constraint (`use_alter`). Existing SQLite databases are
  unaffected.
- **Project delete left orphans.** Provider logs and upload records
  referencing a deleted project's renders were left behind on SQLite (which
  didn't enforce foreign keys) and blocked the delete on Postgres. Upload
  records are now deleted with the project; provider logs are kept as an
  audit trail, detached from the project.
- **Migrations run at boot** on Postgres (advisory-locked). SQLite keeps
  `create_all`. An old database without Alembic history is stamped at the
  latest revision after any missing tables are created.
- **Boot migrations keep the app's logging.** Alembic's `env.py` no longer
  disables existing loggers when the app runs it.
- **SQLite now enforces foreign keys too**, plus WAL and a busy timeout, so
  dev and tests catch integrity bugs before production does.

## Testing

```bash
# The full suite against any Postgres (schema built from the migrations once;
# tables truncated per test):
TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/avs_test pytest -q
```

CI runs this in the `backend-postgres` job, after a migration round-trip
(upgrade → downgrade → upgrade) and `alembic check` (schema drift against the
models).

## Backups

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec postgres \
    pg_dump -U avs -Fc avs > avs-$(date +%F).dump
# restore: pg_restore -U avs -d avs --clean avs-YYYY-MM-DD.dump
```

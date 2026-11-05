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

## Schema and migrations

The schema starts from one baseline revision
(`migrations/versions/0001_baseline_schema.py`). A live deployment now runs
on it, so never edit or regenerate an applied revision: add a new one for
every schema change (`make db-revision MSG=...`) and it applies
automatically at the next boot. A new NOT NULL column needs a
`server_default` so existing rows can be filled (see `0002`).

- On Postgres the app runs `alembic upgrade head` at boot, under a Postgres
  advisory lock, so when several processes start together one migrates and
  the others wait. SQLite (local dev and tests) uses `create_all`.
- The one circular reference (assets ↔ asset_generation_jobs) is created as
  a separate constraint after both tables exist. An inline `use_alter`
  foreign key is silently skipped by Alembic, which `alembic check` caught.
- SQLite enforces foreign keys too (plus WAL and a busy timeout), so dev and
  tests catch integrity bugs the same way Postgres does.

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

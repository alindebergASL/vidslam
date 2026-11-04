#!/usr/bin/env bash
# Regenerate migrations/versions/0001_baseline_schema.py from the models.
# Net-new build only: while nothing is deployed, schema changes are folded
# into the single baseline instead of stacking revisions. Needs an empty
# Postgres to diff against:  PG_URL=postgresql+psycopg://… scripts/regen_baseline.sh
set -euo pipefail
cd "$(dirname "$0")/.."
: "${PG_URL:?set PG_URL to an empty Postgres database}"
export MOCK_PROVIDERS=true MVP_PASSWORD=x SESSION_SECRET=dev-session-secret-change-me-please-very-long
rm -f migrations/versions/0001_*.py
DATABASE_URL="$PG_URL" .venv/bin/alembic revision --autogenerate --rev-id 0001 -m "baseline schema" >/dev/null 2>&1
# Alembic silently skips an inline use_alter FK inside create_table; add the
# assets <-> asset_generation_jobs cycle explicitly once both tables exist.
.venv/bin/python - <<'PY'
p = "migrations/versions/0001_baseline_schema.py"
s = open(p).read()
s = s.replace("    sa.ForeignKeyConstraint(['result_asset_id'], ['assets.id'], "
              "name='fk_asset_generation_jobs_result_asset_id', use_alter=True),\n", "")
i = s.index("op.create_table('assets',")
j = s.index("    op.create_table(", i + 10)
s = s[:j] + (
    "    # assets <-> asset_generation_jobs reference each other: add this FK once\n"
    "    # both tables exist (an inline use_alter FK is skipped by Alembic; batch\n"
    "    # mode lets SQLite add it too).\n"
    "    with op.batch_alter_table('asset_generation_jobs', schema=None) as batch_op:\n"
    "        batch_op.create_foreign_key(\n"
    "            'fk_asset_generation_jobs_result_asset_id', 'assets', ['result_asset_id'], ['id']\n"
    "        )\n\n") + s[j:]
s = s.replace('def downgrade() -> None:\n    """Downgrade schema."""\n',
              'def downgrade() -> None:\n    """Downgrade schema."""\n'
              "    with op.batch_alter_table('asset_generation_jobs', schema=None) as batch_op:\n"
              "        batch_op.drop_constraint('fk_asset_generation_jobs_result_asset_id', type_='foreignkey')\n", 1)
open(p, "w").write(s)
PY
for db in "$PG_URL" "sqlite:////tmp/regen_check.db"; do
  rm -f /tmp/regen_check.db
  for step in "upgrade head" "downgrade base" "upgrade head"; do DATABASE_URL="$db" .venv/bin/alembic $step >/dev/null; done
  DATABASE_URL="$db" .venv/bin/alembic check 2>&1 | grep -E "No new upgrade|FAILED" | tail -1
done

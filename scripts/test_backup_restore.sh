#!/usr/bin/env bash
# Round-trip test for backup.sh / restore.sh against a local Postgres:
# back up, damage the database and the media, restore, and check that
# everything is back. Also checks the failure paths.
#
#   scripts/test_backup_restore.sh            # local Postgres, role avs/avs
#   BACKUP_PG_EXEC="docker exec -i <pg-container>" scripts/test_backup_restore.sh
#
# Needs psql on PATH (for checks), rsync, and a role that can create
# databases. pg_dump/pg_restore run through BACKUP_PG_EXEC, the same way the
# real scripts reach the database inside its container.
set -Eeuo pipefail
cd "$(dirname "$0")/.."

: "${PGHOST:=localhost}" "${PGUSER:=avs}" "${PGPASSWORD:=avs}"
export PGHOST PGUSER PGPASSWORD
DB=avs_backup_test
root="$(mktemp -d)"
trap 'rm -rf "$root"; psql -q -d postgres -c "DROP DATABASE IF EXISTS $DB WITH (FORCE)" >/dev/null 2>&1 || true' EXIT

pass() { printf '  ok  %s\n' "$*"; }
boom() { printf 'FAIL  %s\n' "$*"; exit 1; }
q() { psql -tA -d "$DB" -c "$1"; }

export BACKUP_PG_EXEC="${BACKUP_PG_EXEC-env PGHOST=$PGHOST PGPASSWORD=$PGPASSWORD}"
ALEMBIC=alembic
[[ -x backend/.venv/bin/alembic ]] && ALEMBIC=.venv/bin/alembic
export BACKUP_DB_NAME=$DB BACKUP_DB_USER=$PGUSER
export BACKUP_SERVICES_CMD=":"
export BACKUP_DATA_DIR="$root/data"
export BACKUP_TARGET="$root/target"
export TMPDIR="$root"

# --- a database with the real schema and some rows, plus media ---
psql -q -d postgres -c "DROP DATABASE IF EXISTS $DB WITH (FORCE)" -c "CREATE DATABASE $DB OWNER $PGUSER"
(cd backend && DATABASE_URL="postgresql+psycopg://$PGUSER:$PGPASSWORD@$PGHOST:5432/$DB" \
  $ALEMBIC upgrade head >/dev/null)
q "INSERT INTO app_settings (key, value, updated_at) VALUES ('probe', '{\"v\": 1}', now())" >/dev/null
q "INSERT INTO app_settings (key, value, updated_at) VALUES ('marker', '{\"v\": \"before\"}', now())" >/dev/null
mkdir -p "$root/data/renders/1" "$root/data/uploads"
echo "render bytes" > "$root/data/renders/1/final.mp4"
echo "upload bytes" > "$root/data/uploads/hero.png"
before_rows=$(q "SELECT count(*) FROM app_settings")

# --- failure paths ---
if scripts/backup.sh >/dev/null 2>&1; then boom "backup succeeded without its target directory"; fi
pass "refuses a missing target (a dropped mount)"
mkdir -p "$BACKUP_TARGET"
if BACKUP_DB_NAME=no_such_db scripts/backup.sh >/dev/null 2>&1; then boom "backup succeeded with a bad database"; fi
[[ -z "$(ls "$BACKUP_TARGET/db"/avs-*.dump 2>/dev/null)" ]] || boom "a failed dump left a dump file behind"
pass "a failed dump exits non-zero and stores nothing"

# --- backup ---
scripts/backup.sh > "$root/backup.log"
latest=$(cat "$BACKUP_TARGET/db/LATEST")
[[ -f "$BACKUP_TARGET/db/$latest" ]] || boom "LATEST points at a missing dump"
(cd "$BACKUP_TARGET/db" && sha256sum -c --quiet "$latest.sha256") || boom "checksum mismatch"
[[ -f "$BACKUP_TARGET/data/renders/1/final.mp4" ]] || boom "media not mirrored"
pass "backup wrote a verified dump ($latest) and mirrored media"

# --- damage everything ---
q "DELETE FROM app_settings" >/dev/null
q "INSERT INTO app_settings (key, value, updated_at) VALUES ('junk', '{}', now())" >/dev/null
rm "$root/data/renders/1/final.mp4"
echo "stray" > "$root/data/uploads/stray.png"

# --- restore ---
if scripts/restore.sh --db latest --data < /dev/null >/dev/null 2>&1; then boom "restore ran without confirmation"; fi
pass "restore refuses to run without confirmation"
scripts/restore.sh --db latest --data --yes > "$root/restore.log"
[[ "$(q "SELECT count(*) FROM app_settings")" == "$before_rows" ]] || boom "row count not restored"
[[ "$(q "SELECT value->>'v' FROM app_settings WHERE key='marker'")" == before ]] || boom "marker row not restored"
[[ -z "$(q "SELECT 1 FROM app_settings WHERE key='junk'")" ]] || boom "post-backup row survived the restore"
[[ "$(cat "$root/data/renders/1/final.mp4")" == "render bytes" ]] || boom "deleted render not restored"
[[ ! -e "$root/data/uploads/stray.png" ]] || boom "file added after the backup survived"
ls "$BACKUP_TARGET/db"/pre-restore-*.dump >/dev/null || boom "no safety dump before restoring"
[[ "$(q "SELECT version_num FROM alembic_version")" != "" ]] || boom "alembic_version missing after restore"
pass "restore brought back the database and media, after a safety dump"

# --- retention ---
touch -d '30 days ago' "$BACKUP_TARGET/db/avs-20200101T000000Z.dump"
BACKUP_KEEP_DAYS=14 scripts/backup.sh > /dev/null
[[ ! -e "$BACKUP_TARGET/db/avs-20200101T000000Z.dump" ]] || boom "old dump not pruned"
[[ -f "$BACKUP_TARGET/db/$latest" ]] || boom "recent dump was pruned"
pass "dumps older than BACKUP_KEEP_DAYS are pruned"

echo "all backup/restore checks passed"

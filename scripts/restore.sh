#!/usr/bin/env bash
# Restore from a backup made by scripts/backup.sh.
#
#   scripts/restore.sh --list                    # what's in the backup
#   scripts/restore.sh --db latest --data        # full restore (asks to confirm)
#   scripts/restore.sh --db avs-20261105T031500Z.dump
#   scripts/restore.sh --data                    # media files only
#   ... --yes                                    # no prompt (scripted drills)
#
# What it does, in order:
#   1. Takes a safety dump of the CURRENT database into <target>/db/pre-restore-*.dump
#   2. Stops backend, worker and frontend (Postgres keeps running)
#   3. --db:   drops and recreates the database, then pg_restore's the dump
#      --data: rsyncs <target>/data/ back into ./data
#   4. Starts the services again. Migrations run at boot, so a dump from an
#      older version is upgraded to the current schema automatically.
#
# Same settings as backup.sh (BACKUP_TARGET etc., env or .env).
set -Eeuo pipefail

cd "$(dirname "$0")/.."

env_value() {
  [[ -f .env ]] || return 0
  sed -n "s/^$1=//p" .env | tail -n1 | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

TARGET="${BACKUP_TARGET:-$(env_value BACKUP_TARGET)}"
DATA_DIR="${BACKUP_DATA_DIR:-./data}"
DB_NAME="${BACKUP_DB_NAME:-avs}"
DB_USER="${BACKUP_DB_USER:-avs}"
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
PG_EXEC="${BACKUP_PG_EXEC-$COMPOSE exec -T postgres}"
SERVICES="${BACKUP_SERVICES_CMD-$COMPOSE}"  # testing: ":" skips stop/start

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

db="" data=false yes=false list=false
while (( $# )); do
  case "$1" in
    --db) db="${2:?--db needs a dump name or 'latest'}"; shift ;;
    --data) data=true ;;
    --yes) yes=true ;;
    --list) list=true ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
  shift
done

[[ -n "$TARGET" ]] || die "set BACKUP_TARGET"
is_s3=false
[[ "$TARGET" == s3://* ]] && is_s3=true
$is_s3 && { command -v aws >/dev/null || die "aws CLI required for s3:// targets"; }
$is_s3 || [[ -d "$TARGET/db" ]] || die "no backups at $TARGET (is the mount up?)"

if $list; then
  if $is_s3; then aws s3 ls "$TARGET/db/"
  else ls -lh "$TARGET/db/"; fi
  exit 0
fi
[[ -n "$db" ]] || $data || die "nothing to restore: pass --db <dump|latest> and/or --data"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

if [[ -n "$db" ]]; then
  if [[ "$db" == latest ]]; then
    if $is_s3; then db="$(aws s3 cp --only-show-errors "$TARGET/db/LATEST" -)"
    else db="$(cat "$TARGET/db/LATEST")"; fi
  fi
  db="$(basename "$db")"
  if $is_s3; then
    aws s3 cp --only-show-errors "$TARGET/db/$db" "$work/$db"
    aws s3 cp --only-show-errors "$TARGET/db/$db.sha256" "$work/$db.sha256" || true
  else
    [[ -f "$TARGET/db/$db" ]] || die "no such dump: $TARGET/db/$db"
    cp "$TARGET/db/$db" "$work/$db"
    [[ -f "$TARGET/db/$db.sha256" ]] && cp "$TARGET/db/$db.sha256" "$work/"
  fi
  if [[ -f "$work/$db.sha256" ]]; then
    (cd "$work" && sha256sum -c --quiet "$db.sha256") || die "$db failed its checksum: not restoring it"
  fi
  # shellcheck disable=SC2086
  $PG_EXEC pg_restore --list < "$work/$db" > /dev/null || die "$db is not a readable dump"
fi

echo "About to restore into this server:"
[[ -n "$db" ]] && echo "  database '$DB_NAME'  ← $db   (current data is REPLACED; a safety dump is taken first)"
$data && echo "  $DATA_DIR  ← $TARGET/data/   (files not in the backup are removed)"
if ! $yes; then
  read -r -p "Type 'restore' to continue: " answer
  [[ "$answer" == restore ]] || die "cancelled"
fi

if [[ -n "$db" ]]; then
  safety="pre-restore-$(date -u +%Y%m%dT%H%M%SZ).dump"
  log "safety dump of the current database → $safety"
  # shellcheck disable=SC2086
  if $PG_EXEC pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc --no-owner > "$work/$safety" 2>/dev/null; then
    if $is_s3; then aws s3 cp --only-show-errors "$work/$safety" "$TARGET/db/$safety"
    else cp "$work/$safety" "$TARGET/db/$safety"; fi
  else
    log "warning: could not dump the current database (it may not exist yet); continuing"
  fi
fi

log "stopping backend, worker and frontend"
$SERVICES stop backend worker frontend

restart() { log "starting services"; $SERVICES up -d backend worker frontend; }
trap 'restart; rm -rf "$work"' EXIT

if [[ -n "$db" ]]; then
  log "recreating database $DB_NAME"
  # shellcheck disable=SC2086
  $PG_EXEC psql -v ON_ERROR_STOP=1 -U "$DB_USER" -d postgres \
    -c "DROP DATABASE IF EXISTS \"$DB_NAME\" WITH (FORCE)" \
    -c "CREATE DATABASE \"$DB_NAME\" OWNER \"$DB_USER\"" > /dev/null
  log "restoring $db"
  # shellcheck disable=SC2086
  $PG_EXEC pg_restore -U "$DB_USER" -d "$DB_NAME" --no-owner --exit-on-error < "$work/$db"
  log "database restored"
fi

if $data; then
  log "restoring media into $DATA_DIR"
  mkdir -p "$DATA_DIR"
  if $is_s3; then aws s3 sync --only-show-errors --delete "$TARGET/data/" "$DATA_DIR/"
  else rsync -rt --delete "$TARGET/data/" "$DATA_DIR/"; fi
  log "media restored"
fi

log "restore complete (migrations, if any, run as the backend starts)"

#!/usr/bin/env bash
# Nightly backup: the Postgres database plus the media files under ./data.
#
#   scripts/backup.sh                      # target from BACKUP_TARGET (env or .env)
#   BACKUP_TARGET=/mnt/s3files/avs-backups scripts/backup.sh
#   BACKUP_TARGET=s3://my-bucket/avs-backups scripts/backup.sh   # needs the aws CLI
#
# Layout at the target:
#   db/avs-<UTC stamp>.dump   pg_dump custom format (restore with scripts/restore.sh)
#   db/avs-<stamp>.dump.sha256
#   db/LATEST                 name of the newest verified dump
#   data/                     mirror of ./data (uploads, Studio output, renders)
#
# The database itself stays on local disk: Postgres's data directory must not
# live on a network or object-backed file system. Only dumps go to the target.
#
# Settings (environment, or the same names in .env):
#   BACKUP_TARGET      directory (e.g. an S3 Files mount) or s3://bucket/prefix
#   BACKUP_KEEP_DAYS   dumps older than this are pruned (default 14; directory
#                      targets only — use an S3 lifecycle rule for s3://)
#   ALERT_WEBHOOK_URL  Slack/Discord-compatible webhook, posted to on failure
# Testing overrides: BACKUP_PG_EXEC (command prefix for pg_dump/pg_restore,
# default: docker compose exec into the postgres service), BACKUP_DB_NAME,
# BACKUP_DB_USER, BACKUP_DATA_DIR.
set -Eeuo pipefail

cd "$(dirname "$0")/.."

env_value() {  # read KEY from .env without sourcing it (values may hold $, quotes…)
  [[ -f .env ]] || return 0
  sed -n "s/^$1=//p" .env | tail -n1 | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

TARGET="${BACKUP_TARGET:-$(env_value BACKUP_TARGET)}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-$(env_value BACKUP_KEEP_DAYS)}"
KEEP_DAYS="${KEEP_DAYS:-14}"
ALERT_URL="${ALERT_WEBHOOK_URL:-$(env_value ALERT_WEBHOOK_URL)}"
DATA_DIR="${BACKUP_DATA_DIR:-./data}"
DB_NAME="${BACKUP_DB_NAME:-avs}"
DB_USER="${BACKUP_DB_USER:-avs}"
PG_EXEC="${BACKUP_PG_EXEC-docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T postgres}"

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }

alert() {
  [[ -n "$ALERT_URL" ]] || return 0
  local text="🔴 AvatarVideoStudio · backup failed on $(hostname): $1"
  curl -fsS -m 15 -H 'Content-Type: application/json' \
    -d "$(printf '{"text":%s,"content":%s}' "$(json_str "$text")" "$(json_str "$text")")" \
    "$ALERT_URL" >/dev/null || log "warning: could not post the failure alert"
}

json_str() { python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"; }

fail() { log "ERROR: $*"; alert "$*"; exit 1; }
trap 'fail "line $LINENO: $BASH_COMMAND"' ERR

[[ -n "$TARGET" ]] || { log "ERROR: set BACKUP_TARGET (a directory such as an S3 Files mount, or s3://bucket/prefix)"; exit 2; }

# One run at a time (a slow upload must not overlap the next night's run).
exec 9>"${TMPDIR:-/tmp}/avs-backup.lock"
flock -n 9 || { log "another backup is running; exiting"; exit 0; }

is_s3=false
[[ "$TARGET" == s3://* ]] && is_s3=true
if $is_s3; then
  command -v aws >/dev/null || fail "BACKUP_TARGET is s3:// but the aws CLI is not installed"
else
  # A mount that dropped would otherwise "succeed" by writing to the local
  # directory underneath it. Require the target to exist already.
  [[ -d "$TARGET" ]] || fail "BACKUP_TARGET $TARGET does not exist (is the S3 Files mount up? findmnt -T $TARGET)"
  mkdir -p "$TARGET/db" "$TARGET/data"
fi
command -v rsync >/dev/null || $is_s3 || fail "rsync is not installed (sudo apt-get install -y rsync)"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
name="avs-$stamp.dump"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# ---- database ----
log "dumping database $DB_NAME"
# shellcheck disable=SC2086 # PG_EXEC is a command prefix
$PG_EXEC pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc --no-owner > "$work/$name"
size=$(wc -c < "$work/$name")
(( size > 1024 )) || fail "dump is only $size bytes"
# A dump that pg_restore can't list is not a backup.
# shellcheck disable=SC2086
tables=$($PG_EXEC pg_restore --list < "$work/$name" | grep -c ' TABLE ' || true)
(( tables > 0 )) || fail "dump has no tables"
(cd "$work" && sha256sum "$name" > "$name.sha256")
log "dump ok: $size bytes, $tables tables"

if $is_s3; then
  aws s3 cp --only-show-errors "$work/$name" "$TARGET/db/$name"
  aws s3 cp --only-show-errors "$work/$name.sha256" "$TARGET/db/$name.sha256"
  echo "$name" | aws s3 cp --only-show-errors - "$TARGET/db/LATEST"
else
  # Copy under a temporary name, then rename: a half-written file never looks
  # like a finished dump.
  cp "$work/$name" "$TARGET/db/.$name.partial"
  mv "$TARGET/db/.$name.partial" "$TARGET/db/$name"
  cp "$work/$name.sha256" "$TARGET/db/$name.sha256"
  (cd "$TARGET/db" && sha256sum -c --quiet "$name.sha256") || fail "copied dump failed its checksum"
  echo "$name" > "$TARGET/db/LATEST"
fi
log "dump stored: $TARGET/db/$name"

# ---- media ----
if [[ -d "$DATA_DIR" ]]; then
  log "mirroring $DATA_DIR"
  excludes=(--exclude '.readyz-*' --exclude '*.partial' --exclude 'tmp/')
  if $is_s3; then
    aws s3 sync --only-show-errors --delete "${excludes[@]}" "$DATA_DIR/" "$TARGET/data/"
  else
    # -rt, not -a: object-backed file systems may not keep owners/permissions,
    # and restoring sets them anyway.
    rsync -rt --delete "${excludes[@]}" "$DATA_DIR/" "$TARGET/data/"
  fi
  log "media mirrored to $TARGET/data/"
else
  log "warning: $DATA_DIR not found; skipped media"
fi

# ---- retention ----
if ! $is_s3; then
  pruned=$(find "$TARGET/db" -maxdepth 1 -name 'avs-*.dump*' -mtime +"$KEEP_DAYS" -print -delete | wc -l)
  (( pruned == 0 )) || log "pruned $pruned files older than $KEEP_DAYS days"
fi

log "backup complete"

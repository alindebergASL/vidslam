#!/usr/bin/env bash
#
# Ephemeral deploy verifier: boot docker compose with mock providers, prove
# the host port mapping is intact, then exec scripts.verify_deploy inside the
# backend container to drive the full pipeline. Captures logs on failure and
# tears the stack down (with volumes) so the next run starts clean. Exits
# non-zero on any failure so it can gate a deploy pipeline.
#
# Usage:
#   scripts/verify_up.sh
#   MVP_PASSWORD=hunter2 TIMEOUT=600 scripts/verify_up.sh
#   COMPOSE_FILE=docker-compose.yml scripts/verify_up.sh

set -euo pipefail

MVP_PASSWORD="${MVP_PASSWORD:-changeme}"
TIMEOUT="${TIMEOUT:-300}"                       # max seconds to wait for /health
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Bail fast with a clear message if docker isn't usable.
if ! command -v docker >/dev/null 2>&1; then
    echo "verify-up: docker not found on PATH" >&2
    exit 2
fi
if ! docker info >/dev/null 2>&1; then
    echo "verify-up: docker daemon is not reachable (is it running?)" >&2
    exit 2
fi

# docker-compose.yml uses `env_file: .env` per service, which is a path
# relative to the compose file's directory. We write a temporary .env there,
# but refuse to clobber an existing one.
if [[ -f .env ]]; then
    echo "verify-up: refusing to overwrite an existing .env in $(pwd)" >&2
    echo "verify-up: temporarily move it and re-run." >&2
    exit 2
fi

# Random project name so this never collides with an existing `make up`.
PROJECT="avs-verify-$RANDOM$RANDOM"

cat >.env <<EOF
MOCK_PROVIDERS=true
MVP_PASSWORD=$MVP_PASSWORD
SESSION_SECRET=verify-up-session-secret-not-prod
PUBLIC_BASE_URL=http://localhost:8000
NEXT_PUBLIC_API_BASE=http://localhost:8000
DATABASE_URL=sqlite:////data/app.db
DATA_DIR=/data
REDIS_URL=redis://redis:6379/0
EOF

DC=(docker compose -f "$COMPOSE_FILE" -p "$PROJECT")

cleanup() {
    local rc=$?
    if [[ $rc -ne 0 ]]; then
        echo
        echo "===== docker compose logs (failure tail) =====" >&2
        "${DC[@]}" logs --tail=200 backend worker 2>&1 || true
    fi
    "${DC[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
    rm -f .env
    exit "$rc"
}
trap cleanup EXIT

echo "verify-up: project=$PROJECT, timeout=${TIMEOUT}s, compose=$COMPOSE_FILE"
echo "===== bringing up stack (backend + worker + redis) ====="
"${DC[@]}" up -d --build backend worker redis

echo "===== waiting for host-side /health (up to ${TIMEOUT}s) ====="
deadline=$(( $(date +%s) + TIMEOUT ))
until curl -fsS "http://localhost:8000/health" >/dev/null 2>&1; do
    if (( $(date +%s) > deadline )); then
        echo "verify-up: /health did not respond within ${TIMEOUT}s" >&2
        exit 1
    fi
    sleep 1
done
echo "verify-up: host port 8000 -> backend container OK"

echo "===== running scripts.verify_deploy inside the backend container ====="
"${DC[@]}" exec -T \
    -e BASE_URL="http://localhost:8000" \
    -e MVP_PASSWORD="$MVP_PASSWORD" \
    backend python -m scripts.verify_deploy

echo
echo "===== verify-up PASS — tearing down ====="

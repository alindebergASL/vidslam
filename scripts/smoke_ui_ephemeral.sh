#!/usr/bin/env bash
# Boot a temporary backend + frontend, seed the demo, drive a render to
# completion, and run the UI smoke test. Tears down everything on exit so it
# leaves no state and never collides with a long-running dev stack on the
# default ports.
#
# Useful for verifying a local change end-to-end without `make up` (no docker)
# and without manually juggling background processes.
#
# Usage:  ./scripts/smoke_ui_ephemeral.sh
# Env:    BE_PORT (default 8123)  FE_PORT (default 3123)
set -euo pipefail

cd "$(dirname "$0")/.."

BE_PORT="${BE_PORT:-8123}"
FE_PORT="${FE_PORT:-3123}"
WORKDIR="$(mktemp -d -t avs-smoke-XXXXXX)"
BE_LOG="$WORKDIR/backend.log"
FE_LOG="$WORKDIR/frontend.log"
COOKIES="$WORKDIR/cookies"
BE_PID=""
FE_PID=""

teardown() {
  local rc=$?
  [[ -n "$BE_PID" ]] && kill "$BE_PID" 2>/dev/null || true
  [[ -n "$FE_PID" ]] && kill "$FE_PID" 2>/dev/null || true
  # Give children a moment to exit cleanly before SIGKILL.
  sleep 1
  [[ -n "$BE_PID" ]] && kill -9 "$BE_PID" 2>/dev/null || true
  [[ -n "$FE_PID" ]] && kill -9 "$FE_PID" 2>/dev/null || true
  if [[ "$rc" -ne 0 ]]; then
    echo
    echo "--- backend log (tail) ---"
    tail -80 "$BE_LOG" 2>/dev/null || true
    echo "--- frontend log (tail) ---"
    tail -80 "$FE_LOG" 2>/dev/null || true
  fi
  rm -rf "$WORKDIR"
  exit "$rc"
}
trap teardown EXIT INT TERM

wait_for() {
  local url="$1" label="$2"
  for _ in $(seq 1 30); do
    if curl -sf "$url" > /dev/null 2>&1; then
      echo "  ${label} up"
      return
    fi
    sleep 1
  done
  echo "  ${label} never came up at ${url}"
  return 1
}

# Resolve to absolute paths once so the per-process `cd` doesn't break them.
REPO_ROOT="$(pwd)"
PYBIN="$REPO_ROOT/backend/.venv/bin/python"
[[ -x "$PYBIN" ]] || PYBIN="$(command -v python3)"
NODEMOD="$REPO_ROOT/frontend/node_modules/.bin/next"
[[ -x "$NODEMOD" ]] || { echo "frontend deps missing — run: cd frontend && npm install"; exit 1; }

export MOCK_PROVIDERS=true
export MVP_PASSWORD=smoke-pw
export SESSION_SECRET=smoke-secret-32-chars-long-enough-ok
export DATABASE_URL="sqlite:///${WORKDIR}/smoke.db"
export DATA_DIR="${WORKDIR}/data"
export PUBLIC_BASE_URL="http://localhost:${BE_PORT}"
export FRONTEND_ORIGIN="http://localhost:${FE_PORT}"
export RATE_LIMIT_ENABLED=false
mkdir -p "$DATA_DIR"

echo "→ booting backend on :${BE_PORT}"
(cd backend && "$PYBIN" -m uvicorn app.main:app --host 127.0.0.1 --port "$BE_PORT" --log-level warning) > "$BE_LOG" 2>&1 &
BE_PID=$!
wait_for "http://127.0.0.1:${BE_PORT}/healthz" backend

echo "→ booting frontend on :${FE_PORT}"
(cd frontend && NEXT_PUBLIC_API_BASE="http://localhost:${BE_PORT}" "$NODEMOD" dev -p "$FE_PORT" -H 127.0.0.1) > "$FE_LOG" 2>&1 &
FE_PID=$!
wait_for "http://127.0.0.1:${FE_PORT}" frontend

echo "→ seeding demo + driving renders (need 2 for the compare test)"
curl -sf -c "$COOKIES" -X POST "http://127.0.0.1:${BE_PORT}/api/auth/login" \
  -H "Content-Type: application/json" -d '{"password":"smoke-pw"}' > /dev/null
curl -sf -b "$COOKIES" -X POST "http://127.0.0.1:${BE_PORT}/api/system/seed" > /dev/null
curl -sf -b "$COOKIES" -X POST "http://127.0.0.1:${BE_PORT}/api/projects/1/generate-plan" > /dev/null
sleep 3

# Two renders so the compare-sync test has versions to A/B.
for n in 1 2; do
  curl -sf -b "$COOKIES" -X POST "http://127.0.0.1:${BE_PORT}/api/projects/1/generate-video" > /dev/null
  for _ in $(seq 1 60); do
    S=$(curl -sf -b "$COOKIES" "http://127.0.0.1:${BE_PORT}/api/projects/1/status" \
      | python3 -c "import sys,json; d=json.load(sys.stdin); r=d.get('latest_render'); print(d['project_status']+'/'+(r['status'] if r else 'none'))")
    case "$S" in
      completed/completed) echo "  render #${n}: $S"; break ;;
      */failed) echo "render #${n} failed"; exit 1 ;;
    esac
    sleep 2
  done
done

echo "→ running UI smoke (route walk)"
AVS_BASE_URL="http://localhost:${FE_PORT}" AVS_PASSWORD=smoke-pw node scripts/smoke_ui.mjs

echo "→ running compare-sync test"
AVS_BASE_URL="http://localhost:${FE_PORT}" \
  AVS_API_BASE="http://localhost:${BE_PORT}" \
  AVS_PASSWORD=smoke-pw \
  node frontend/tests/compare_sync.mjs

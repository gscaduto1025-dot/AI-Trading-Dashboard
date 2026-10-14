#!/usr/bin/env bash
# Full-system integration check:
#   1. starts the FastAPI backend (demo mode unless backend/.env has Alpaca keys) on a
#      throwaway SQLite database,
#   2. builds and starts the production Next.js frontend pointed at it,
#   3. runs the API smoke test (every feature area over HTTP) and the UI smoke test
#      (every tab in headless Chromium; skipped if Playwright is not installed).
# Ports: BACKEND_PORT (8765), FRONTEND_PORT (3765).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BP="${BACKEND_PORT:-8765}"
FP="${FRONTEND_PORT:-3765}"
TMP="$(mktemp -d)"
cleanup() {  # stop both servers and wait until they have exited (ports free again)
  for p in ${BPID:-} ${FPID:-}; do kill "$p" 2>/dev/null || true; done
  for p in ${BPID:-} ${FPID:-}; do wait "$p" 2>/dev/null || true; done
}
trap cleanup EXIT

port_free() {  # refuse to run against a server left over from another run
  if curl -s -o /dev/null "http://127.0.0.1:$1"; then
    echo "Port $1 is already in use; stop that process or set BACKEND_PORT/FRONTEND_PORT."; exit 1
  fi
}
port_free "$BP"; port_free "$FP"

wait_for() {  # url, seconds
  for _ in $(seq 1 "$2"); do curl -fs "$1" >/dev/null 2>&1 && return 0; sleep 1; done
  echo "Timed out waiting for $1"; return 1
}

echo "==> Starting backend on :$BP (database $TMP/integration.db)"
(cd "$ROOT/backend" && DATABASE_PATH="$TMP/integration.db" SCAN_INTERVAL_SECONDS=0 \
  exec .venv/bin/uvicorn app.main:app --port "$BP" >"$TMP/backend.log" 2>&1) &
BPID=$!
wait_for "http://127.0.0.1:$BP/api/health" 60

echo "==> Building frontend against the backend"
(cd "$ROOT/frontend" && BACKEND_URL="http://127.0.0.1:$BP" npm run build >"$TMP/build.log" 2>&1) \
  || { tail -30 "$TMP/build.log"; exit 1; }
(cd "$ROOT/frontend" && BACKEND_URL="http://127.0.0.1:$BP" exec node node_modules/next/dist/bin/next start -p "$FP" >"$TMP/frontend.log" 2>&1) &
FPID=$!
wait_for "http://127.0.0.1:$FP" 60

echo "==> API smoke test"
API=0; python3 "$ROOT/scripts/smoke_api.py" "http://127.0.0.1:$BP" || API=$?

echo "==> UI smoke test"
UI=0; (cd "$ROOT" && node scripts/smoke_ui.mjs "http://127.0.0.1:$FP" "$TMP") || UI=$?
if [ "$UI" = 2 ]; then UI=0; fi  # skipped: playwright not installed

echo "Logs and screenshot: $TMP"
if [ "$API" != 0 ] || [ "$UI" != 0 ]; then echo "INTEGRATION FAILED"; exit 1; fi
echo "INTEGRATION PASSED"

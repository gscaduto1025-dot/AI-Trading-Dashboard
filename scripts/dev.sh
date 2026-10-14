#!/usr/bin/env bash
# Start the TradeScope AI backend (port 8000) and frontend (port 3000) together.
# First run installs dependencies. Ctrl+C stops both.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [ ! -x "$ROOT/backend/.venv/bin/uvicorn" ]; then
  echo "==> Creating Python virtualenv and installing backend dependencies"
  python3 -m venv "$ROOT/backend/.venv"
  "$ROOT/backend/.venv/bin/pip" install -q -r "$ROOT/backend/requirements-dev.txt"
fi
if [ ! -d "$ROOT/frontend/node_modules" ]; then
  echo "==> Installing frontend dependencies"
  (cd "$ROOT/frontend" && npm install)
fi
if [ ! -f "$ROOT/backend/.env" ]; then
  cp "$ROOT/backend/.env.example" "$ROOT/backend/.env"
  echo "==> Created backend/.env (DEMO DATA mode until you add API keys)"
fi

cleanup() { kill 0 2>/dev/null || true; }
trap cleanup EXIT INT TERM

(cd "$ROOT/backend" && .venv/bin/uvicorn app.main:app --reload --port 8000) &
(cd "$ROOT/frontend" && npm run dev -- --port 3000) &

echo "==> Backend:  http://localhost:8000/docs"
echo "==> Dashboard: http://localhost:3000"
wait

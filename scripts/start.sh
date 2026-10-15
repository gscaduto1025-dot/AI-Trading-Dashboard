#!/usr/bin/env bash
# Start TradeScope AI for everyday use (production build), on this computer.
#   ./scripts/start.sh            dashboard on http://localhost:3000 (this computer only)
#   ./scripts/start.sh --lan      also reachable from your phone on the same Wi-Fi
# The backend always listens on 127.0.0.1 only; the browser reaches it through the
# dashboard's /api proxy. Your API keys never leave this computer, but with --lan anyone on
# your Wi-Fi can open the dashboard and use its API (there is no login), so use --lan only
# on a network you trust.
# First run installs dependencies. Ctrl+C stops both servers.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${PORT:-3000}"
API_PORT="${BACKEND_PORT:-8000}"
HOST=127.0.0.1
[ "${1:-}" = "--lan" ] && HOST=0.0.0.0

if [ ! -x "$ROOT/backend/.venv/bin/uvicorn" ]; then
  echo "==> Creating Python virtualenv and installing backend dependencies"
  python3 -m venv "$ROOT/backend/.venv"
  "$ROOT/backend/.venv/bin/pip" install -q -r "$ROOT/backend/requirements.txt"
fi
if [ ! -d "$ROOT/frontend/node_modules" ]; then
  echo "==> Installing frontend dependencies"
  (cd "$ROOT/frontend" && npm ci)
fi
if [ ! -f "$ROOT/backend/.env" ]; then
  cp "$ROOT/backend/.env.example" "$ROOT/backend/.env"
  chmod 600 "$ROOT/backend/.env"
  echo "==> Created backend/.env"
fi
if [ -z "${ALPACA_API_KEY_ID:-${APCA_API_KEY_ID:-}}" ] && \
   ! grep -Eq '^(ALPACA_API_KEY_ID|APCA_API_KEY_ID)=.+' "$ROOT/backend/.env"; then
  echo "!!  No Alpaca keys in backend/.env: the dashboard will show DEMO DATA."
  echo "!!  Add ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY there, then restart."
fi

echo "==> Building the dashboard (about a minute)"
(cd "$ROOT/frontend" && BACKEND_URL=http://127.0.0.1:$API_PORT npm run build >/dev/null)

PIDS=()
cleanup() { [ ${#PIDS[@]} -eq 0 ] || kill "${PIDS[@]}" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

(cd "$ROOT/backend" && exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$API_PORT" --workers 1) &
PIDS+=($!)
(cd "$ROOT/frontend" && BACKEND_URL=http://127.0.0.1:$API_PORT exec ./node_modules/.bin/next start --hostname "$HOST" --port "$PORT") &
PIDS+=($!)

echo "==> Dashboard: http://localhost:$PORT"
if [ "$HOST" = 0.0.0.0 ]; then
  IP="$( (ipconfig getifaddr en0 || ipconfig getifaddr en1 || hostname -I | awk '{print $1}') 2>/dev/null || true)"
  echo "==> On your phone (same Wi-Fi): http://${IP:-YOUR-COMPUTER-IP}:$PORT"
fi
echo "==> The first load after a restart takes 1-2 minutes while market data downloads."
wait

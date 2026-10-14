#!/usr/bin/env bash
# Run every check: backend tests, frontend unit tests, typecheck, lint and production build.
# (The full-system check that starts both servers is scripts/integration.sh.)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
echo "==> Backend tests"
(cd "$ROOT/backend" && .venv/bin/python -m pytest -q)
echo "==> Frontend unit tests"
(cd "$ROOT/frontend" && npm test)
echo "==> Frontend typecheck"
(cd "$ROOT/frontend" && npx tsc --noEmit)
echo "==> Frontend lint"
(cd "$ROOT/frontend" && npm run lint)
echo "==> Frontend build"
(cd "$ROOT/frontend" && npm run build)

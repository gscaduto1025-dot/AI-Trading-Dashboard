#!/usr/bin/env bash
# Run every check: backend tests, frontend typecheck, lint and production build.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
echo "==> Backend tests"
(cd "$ROOT/backend" && .venv/bin/python -m pytest -q)
echo "==> Frontend typecheck"
(cd "$ROOT/frontend" && npx tsc --noEmit)
echo "==> Frontend lint"
(cd "$ROOT/frontend" && npm run lint)
echo "==> Frontend build"
(cd "$ROOT/frontend" && npm run build)

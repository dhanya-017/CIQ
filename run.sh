#!/usr/bin/env bash
# Start CIQ Data Intelligence (backend + UI) at http://localhost:8000
# Rebuilds the warehouse first if the claims file, reference files, rules or build code changed.
# Settings come from .env (see .env.example). Usage: ./run.sh [--reload]
set -euo pipefail
cd "$(dirname "$0")"
PY=$([ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)
if ! $PY -m ciq.build.build_warehouse --if-changed; then
  if [ -f Data/warehouse/ciq.duckdb ]; then
    echo "Build stopped (see above) - starting with the existing warehouse"
  else
    echo "No warehouse to serve - fix the input (python scripts/doctor.py) and try again"; exit 1
  fi
fi
EXTRA=()
[ "${1:-}" = "--reload" ] && EXTRA=(--reload --reload-dir app --reload-dir ciq)
exec $PY -m uvicorn app.server:app --host 127.0.0.1 --port "${PORT:-8000}" ${EXTRA[@]+"${EXTRA[@]}"}

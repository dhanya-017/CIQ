#!/usr/bin/env bash
# Start CIQ Data Intelligence (backend + UI) at http://localhost:8000
# Settings come from .env (see .env.example). Usage: ./run.sh [--reload]
set -euo pipefail
cd "$(dirname "$0")"
PY=$([ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)
[ -f Data/warehouse/ciq.duckdb ] || $PY -m ciq.build.build_warehouse
EXTRA=()
[ "${1:-}" = "--reload" ] && EXTRA=(--reload --reload-dir app --reload-dir ciq)
exec $PY -m uvicorn app.server:app --host 127.0.0.1 --port "${PORT:-8000}" ${EXTRA[@]+"${EXTRA[@]}"}

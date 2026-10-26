#!/usr/bin/env bash
# Start LoTrace Data Intelligence (backend + UI) at http://localhost:8000
#
# All a new user needs:  1) put master_nsclc.csv in Data/real_data/   2) run ./run.sh
# First run: creates .venv, installs packages and creates .env (one-time, a few minutes).
# Every run: re-installs packages if requirements.txt changed, rebuilds the warehouse only if the claims file,
# reference files, rules or build code changed, then starts the app.
# Usage: ./run.sh [--reload]      (port: PORT=8001 ./run.sh)
set -euo pipefail
cd "$(dirname "$0")"
PY=.venv/bin/python

if [ ! -x "$PY" ]; then
  echo "First run: creating the Python environment (.venv)"
  python3 -m venv .venv || { echo "Python 3.10+ is required (https://www.python.org/downloads/)."; exit 1; }
fi
STAMP=.venv/requirements.installed.txt
if [ ! -f "$STAMP" ] || ! cmp -s requirements.txt "$STAMP"; then
  echo "Installing packages (requirements.txt)"
  $PY -m pip install -q --upgrade pip
  $PY -m pip install -q -r requirements.txt
  cp requirements.txt "$STAMP"
fi
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example (the defaults work; edit it to change settings)"
fi

if ! $PY -m ciq.build.build_warehouse --if-changed; then
  if [ -f Data/warehouse/ciq.duckdb ]; then
    echo "Build stopped (see above) - starting with the existing warehouse"
  else
    echo "Nothing to serve yet: put master_nsclc.csv in Data/real_data/ and run ./run.sh again."; exit 1
  fi
fi
EXTRA=()
[ "${1:-}" = "--reload" ] && EXTRA=(--reload --reload-dir app --reload-dir ciq)
echo "Starting LoTrace at http://localhost:${PORT:-8000}"
exec $PY -m uvicorn app.server:app --host 127.0.0.1 --port "${PORT:-8000}" ${EXTRA[@]+"${EXTRA[@]}"}

#!/usr/bin/env bash
# One-time developer setup (macOS / Linux). Usage: ./setup.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "1/5 Creating virtual environment (.venv)"
[ -d .venv ] || python3 -m venv .venv
PY=.venv/bin/python

echo "2/5 Installing packages"
$PY -m pip install --upgrade pip -q
$PY -m pip install -r requirements.txt -q

echo "3/5 Creating .env from .env.example (if missing)"
[ -f .env ] || cp .env.example .env

echo "4/5 Building warehouse"
if [ -f Data/master/claims_master.parquet ]; then
  $PY -m ciq.build.build_warehouse
  $PY -m pytest tests -q
else
  echo "   Data/master/claims_master.parquet not found - see Data/README.md, then rerun ./setup.sh"
fi

echo "5/5 Environment check"
$PY scripts/doctor.py

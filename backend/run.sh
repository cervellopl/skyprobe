#!/usr/bin/env bash
# Development server:  ./run.sh [port]
set -euo pipefail
cd "$(dirname "$0")"
PORT="${1:-8765}"
[ -d ../.venv ] || python3 -m venv ../.venv
../.venv/bin/pip install -q -r requirements.txt
exec ../.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --reload

#!/usr/bin/env bash
# Start Uptracker on port 8090 (override: PORT=xxxx ./run.sh).
set -e
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"
exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8090}"

#!/usr/bin/env bash
# Load .env and start the review dashboard in the foreground.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

export APP_HOST="${APP_HOST:-0.0.0.0}"
export APP_PORT="${APP_PORT:-8080}"

bash "$ROOT/deploy/prepare-data.sh"
exec "$ROOT/.venv/bin/python" dashboard/app.py

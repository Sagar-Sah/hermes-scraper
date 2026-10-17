#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install -r backend/requirements.lock
  .venv/bin/python -m pip install --no-deps -e backend
  .venv/bin/python -m playwright install chromium
fi
if [[ ! -f frontend/dist/index.html ]]; then
  (cd frontend && npm ci && npm run build)
fi
export ATLAS_DATA_DIR="${ATLAS_DATA_DIR:-$project_dir/data}"
# A desktop launch lets the user complete the source's ordinary sign-in flow.
if [[ -z "${ATLAS_BROWSER_HEADLESS+x}" ]]; then
  if [[ "$(uname -s)" == "Darwin" || -n "${DISPLAY:-}" || -n "${WAYLAND_DISPLAY:-}" ]]; then
    export ATLAS_BROWSER_HEADLESS=false
  else
    export ATLAS_BROWSER_HEADLESS=true
  fi
fi
# Uvicorn's dotenv loader preserves explicitly exported shell settings.
# In particular, browser worker settings in .env must affect local launches too.
env_file_args=()
if [[ -f "$project_dir/.env" ]]; then
  env_file_args=(--env-file "$project_dir/.env")
fi
exec .venv/bin/python -m uvicorn crawler.app:app "${env_file_args[@]}" --host 127.0.0.1 --port "${ATLAS_PORT:-18877}" --no-access-log

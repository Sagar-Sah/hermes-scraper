#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
build_tool="$project_dir/backend/crawler/build_info.py"
prepare_only=false
if [[ "${1:-}" == "--prepare-only" && $# == 1 ]]; then
  prepare_only=true
elif [[ $# != 0 ]]; then
  echo "Usage: ./scripts/start-local.sh [--prepare-only]" >&2
  exit 1
fi
# Check before changing dependencies/assets. Never replace files under a running
# worker or silently attach the user to a stale process after a Git update.
if [[ "$prepare_only" == true && -n "${HERMES_UPDATE_PREPARING:-}" ]]; then
  .venv/bin/python backend/crawler/managed_update.py verify-preparation
else
  running_status=0
  python3 "$build_tool" check-running --port "${ATLAS_PORT:-18877}" || running_status=$?
  if [[ "$running_status" == 10 ]]; then exit 0; fi
  if [[ "$running_status" != 0 ]]; then exit 1; fi
fi
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
  rm -f .local-build/python_dependencies.json
fi
if ! .venv/bin/python "$build_tool" dependency-ready python_dependencies; then
  echo "Hermes: installing changed Python dependencies…"
  .venv/bin/python -m pip install -r backend/requirements.lock
  .venv/bin/python -m pip install --no-deps -e backend
  .venv/bin/python -m playwright install chromium
  .venv/bin/python "$build_tool" stamp-dependencies python_dependencies
fi
if [[ ! -d frontend/node_modules ]] || ! .venv/bin/python "$build_tool" dependency-ready frontend_dependencies; then
  echo "Hermes: installing changed dashboard dependencies…"
  (cd frontend && npm ci)
  .venv/bin/python "$build_tool" stamp-dependencies frontend_dependencies
fi
if ! .venv/bin/python "$build_tool" frontend-ready; then
  echo "Hermes: dashboard source/assets changed; rebuilding…"
  (cd frontend && npm run build)
fi
.venv/bin/python "$build_tool" frontend-ready
.venv/bin/python "$build_tool" describe
if [[ "$prepare_only" == true ]]; then exit 0; fi
# Settings already defaults to ./data from this project directory. Do not export
# a fallback here: it would override a user's existing .env data directory.
# Desktop browser defaults are applied by the supervisor after dotenv is merged,
# so saved .env browser settings keep their precedence below explicit shell values.
# Supervisor loads .env with the same explicit-shell precedence, owns the exact
# backend child, and provides the managed in-app update/restart handshake.
exec .venv/bin/python backend/crawler/managed_update.py supervise --port "${ATLAS_PORT:-18877}"

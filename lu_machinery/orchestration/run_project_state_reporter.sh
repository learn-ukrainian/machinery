#!/usr/bin/env bash
# Loopback POST wrapper for scripts/api/project_state_local.py (#7188).
set -euo pipefail
# Read-only Git children (status refreshing the index) must never take
# index.lock: a reporter killed mid-call would leave it behind (#8874). An
# explicit operator value wins.
export GIT_OPTIONAL_LOCKS="${GIT_OPTIONAL_LOCKS:-0}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
source "$REPO_ROOT/lu_machinery/lib/project_interpreter.sh"
if ! PYTHON="$(project_interpreter_resolve "$REPO_ROOT")"; then
  echo "project interpreter missing" >&2
  exit 1
fi
exec "$PYTHON" "$REPO_ROOT/lu_machinery/api/project_state_local.py" report

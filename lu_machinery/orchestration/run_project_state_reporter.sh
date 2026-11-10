#!/usr/bin/env bash
# Loopback POST wrapper for scripts/api/project_state_local.py (#7188).
set -euo pipefail
# Read-only Git children (status refreshing the index) must never take
# index.lock: a reporter killed mid-call would leave it behind (#8874). An
# explicit operator value wins.
export GIT_OPTIONAL_LOCKS="${GIT_OPTIONAL_LOCKS:-0}"

# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/../lib/project_interpreter.sh"
REPO_ROOT="$(project_product_root_resolve)" || exit 1
if ! PYTHON="$(project_interpreter_resolve "$REPO_ROOT")"; then
  echo "project interpreter missing" >&2
  exit 1
fi
cd "$REPO_ROOT"
project_python_command "$PYTHON"
exec "${PROJECT_PYTHON_COMMAND[@]}" -m lu_machinery.api.project_state_local report

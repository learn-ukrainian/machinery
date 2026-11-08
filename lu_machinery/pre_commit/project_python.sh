#!/usr/bin/env bash
# Launch a command with the one project interpreter (R66, Refs #100).
# Callers pass the checkout as the working directory. The selector is
# lu_machinery/lib/project_interpreter.sh; this file does not choose a path.
set -euo pipefail

if [[ $# -eq 0 ]]; then
  echo "[project-python] ERROR: missing Python command arguments" >&2
  exit 2
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$script_dir/../lib/project_interpreter.sh"
if ! python="$(project_interpreter_resolve "$PWD")"; then
  exit 127
fi

exec "$python" "$@"

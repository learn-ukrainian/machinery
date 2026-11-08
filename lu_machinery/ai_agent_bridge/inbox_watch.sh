#!/usr/bin/env bash
# Inbox notifications and bounded Fleet Comms supervisory wakes.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd -- "$script_dir/../.." && pwd -P)"
# shellcheck disable=SC1091
source "$repo_root/lu_machinery/lib/project_interpreter.sh"
python="$(project_interpreter_resolve "$repo_root")"
project_python_command "$python"
exec "${PROJECT_PYTHON_COMMAND[@]}" -m lu_machinery.ai_agent_bridge._inbox_watch "$@"

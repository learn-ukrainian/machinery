#!/usr/bin/env bash
# Inbox notifications and bounded Fleet Comms supervisory wakes.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck disable=SC1091
source "$script_dir/../lib/project_interpreter.sh"
repo_root="$(project_product_root_resolve)" || exit 1
python="$(project_interpreter_resolve "$repo_root")"
project_python_command "$python"
cd "$repo_root"
exec "${PROJECT_PYTHON_COMMAND[@]}" -m lu_machinery.ai_agent_bridge._inbox_watch "$@"

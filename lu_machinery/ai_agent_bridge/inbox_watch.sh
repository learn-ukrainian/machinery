#!/usr/bin/env bash
# Inbox notifications and bounded Fleet Comms supervisory wakes.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd -- "$script_dir/../.." && pwd -P)"
source "$repo_root/lu_machinery/lib/session_supervisor.sh"
state_root="$(_canonical_state_root "$repo_root")"

exec "$state_root/.venv/bin/python" -m lu_machinery.ai_agent_bridge._inbox_watch "$@"

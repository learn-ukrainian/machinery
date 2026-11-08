#!/usr/bin/env bash
# Opt-in wrapper: HOOK_TIMING=1 logs wall-ms to batch_state/hook-timing.jsonl under the state
# root while preserving the underlying hook's stdin/stdout/stderr/rc.
#
# Usage (Claude settings command field):
#   HOOK_EVENT_NAME=PreToolUse HOOK_MATCHER=Bash \
#     <path to this file> $CLAUDE_PROJECT_DIR/.claude/hooks/enforce-venv.sh
#
# The timer runs as `-m lu_machinery.hooks.hook_timing` under the one project interpreter.
# Timing is diagnostics only: an invalid configured interpreter is not replaced, and
# without a usable interpreter, or one that cannot import the timer, the hook runs
# untimed with its own exit code.
set -u
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$script_dir/../lib/project_interpreter.sh"
if [ "${HOOK_TIMING:-}" = "1" ] || [ "${HOOK_TIMING:-}" = "true" ] || [ "${HOOK_TIMING:-}" = "always" ]; then
  project="${CLAUDE_PROJECT_DIR:-$PWD}"
  if PYTHON="$(project_interpreter_resolve "$project")" \
      && project_python_run "$PYTHON" -c 'import lu_machinery.hooks.hook_timing' >/dev/null 2>&1; then
    project_python_command "$PYTHON"
    exec "${PROJECT_PYTHON_COMMAND[@]}" -m lu_machinery.hooks.hook_timing wrap -- "$@"
  fi
  echo "run_hook_timed: hook runs untimed (project interpreter unavailable or cannot import lu_machinery)" >&2
fi
exec "$@"

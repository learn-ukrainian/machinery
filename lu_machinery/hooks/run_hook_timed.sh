#!/usr/bin/env bash
# Opt-in wrapper: HOOK_TIMING=1 logs wall-ms to batch_state/hook-timing.jsonl under the state
# root while preserving the underlying hook's stdin/stdout/stderr/rc.
#
# Usage (Claude settings command field):
#   HOOK_EVENT_NAME=PreToolUse HOOK_MATCHER=Bash \
#     <path to this file> $CLAUDE_PROJECT_DIR/.claude/hooks/enforce-venv.sh
#
# The timer runs as `-m lu_machinery.hooks.hook_timing` under the configured interpreter
# (LEARN_UKRAINIAN_PYTHON), never one found from this file's location or the cwd (-P keeps the
# cwd off sys.path). Timing is diagnostics only: without a usable interpreter, or one that
# cannot import the timer, the hook runs untimed with its own exit code.
set -u
PYTHON="${LEARN_UKRAINIAN_PYTHON:-}"
if [ "${HOOK_TIMING:-}" = "1" ] || [ "${HOOK_TIMING:-}" = "true" ] || [ "${HOOK_TIMING:-}" = "always" ]; then
  if [ -n "$PYTHON" ] && [ -x "$PYTHON" ] && "$PYTHON" -P -c 'import lu_machinery.hooks.hook_timing' 2>/dev/null; then
    exec "$PYTHON" -P -m lu_machinery.hooks.hook_timing wrap -- "$@"
  fi
  echo "run_hook_timed: hook runs untimed (LEARN_UKRAINIAN_PYTHON unset or cannot import lu_machinery)" >&2
fi
exec "$@"

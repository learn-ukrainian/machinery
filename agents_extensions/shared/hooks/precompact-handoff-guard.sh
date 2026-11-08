#!/bin/bash
# PreCompact(auto): pause only for this interactive Claude session's own proven
# operator-restart handoff. Unavailable, unsafe or timed-out evidence fails open.
case "${SESSION_HANDOFF_AGENT:-claude}" in
  claude|claude-*) ;;
  *) exit 0 ;;
esac
if [[ "${0:-}" == *"/.codex/"* ]] \
  || [ -n "${CODEX_THREAD_ID:-}${CODEX_SESSION_ID:-}${CODEX_SESSION:-}" ] \
  || [ -n "${CLAUDE_NON_INTERACTIVE:-}${LEARN_UK_PIPELINE:-}${LEARN_UKRAINIAN_PIPELINE:-}" ] \
  || [ -n "${GEMINI_SESSION:-}${GROK_AGENT:-}${LEARN_UKRAINIAN_DISPATCH_TASK_ID:-}" ]; then
  exit 0
fi

# shellcheck source=agents_extensions/shared/hooks/context-rollover-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/context-rollover-lib.sh" 2>/dev/null || exit 0
# The project is configured (CLAUDE_PROJECT_DIR from the harness, else
# LU_PRODUCT_ROOT), never derived from this file's location: an installed
# release lives in site-packages, not in the project (machinery #100, R51).
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-${LU_PRODUCT_ROOT:-}}"
case "$PROJECT_DIR" in
  /*) [ -d "$PROJECT_DIR" ] || { echo "precompact-handoff-guard.sh: project root $PROJECT_DIR is not a directory" >&2; exit 1; } ;;
  *) echo "precompact-handoff-guard.sh: no project root: set CLAUDE_PROJECT_DIR or LU_PRODUCT_ROOT to an absolute checkout path" >&2; exit 1 ;;
esac
CANONICAL_ROOT=$(context_canonical_root "$PROJECT_DIR") || exit 0
# One interpreter (R66). Fail open. In-tree, the resolver is authoritative. A
# deployed copy probes only to find the installed release, then keeps the
# resolver's answer. An invalid override does not fall back.
_hook_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 0
case "$_hook_dir" in
  */agents_extensions/shared/hooks)
    MACHINERY_DIR="${_hook_dir%/agents_extensions/shared/hooks}"
    # shellcheck source=lu_machinery/lib/project_interpreter.sh
    source "$MACHINERY_DIR/lu_machinery/lib/project_interpreter.sh" 2>/dev/null || exit 0
    BOUNDED_PYTHON="$(project_interpreter_resolve "$PROJECT_DIR" 2>/dev/null)" || exit 0
    ;;
  *)
    BOUNDED_PYTHON="$(_context_rule_probe "$CANONICAL_ROOT" 2>/dev/null)" || exit 0
    MACHINERY_DIR=$("$BOUNDED_PYTHON" -I -c 'import os, lu_machinery; print(os.path.dirname(os.path.dirname(os.path.abspath(lu_machinery.__file__))))' 2>/dev/null) || exit 0
    # shellcheck source=lu_machinery/lib/project_interpreter.sh
    source "$MACHINERY_DIR/lu_machinery/lib/project_interpreter.sh" 2>/dev/null || exit 0
    RESOLVED="$(project_interpreter_resolve "$PROJECT_DIR" 2>/dev/null)" || exit 0
    if [ "$RESOLVED" != "$BOUNDED_PYTHON" ]; then
      MACHINERY_DIR=$("$RESOLVED" -I -c 'import os, lu_machinery; print(os.path.dirname(os.path.dirname(os.path.abspath(lu_machinery.__file__))))' 2>/dev/null) || exit 0
      # shellcheck source=lu_machinery/lib/project_interpreter.sh
      source "$MACHINERY_DIR/lu_machinery/lib/project_interpreter.sh" 2>/dev/null || exit 0
    fi
    BOUNDED_PYTHON="$RESOLVED"
    ;;
esac
unset _hook_dir
BOUNDED_RUNNER_DEFAULT="$MACHINERY_DIR/lu_machinery/agent_runtime/bounded_command.py"
BOUNDED_RUNNER="${SESSION_BOUNDED_RUNNER:-$BOUNDED_RUNNER_DEFAULT}"
[ -f "$BOUNDED_RUNNER" ] || exit 0
cd "$PROJECT_DIR" 2>/dev/null || exit 0

project_python_command "$BOUNDED_PYTHON"
RESULT=$("$BOUNDED_PYTHON" -I "$BOUNDED_RUNNER" --timeout 2 -- \
  "${PROJECT_PYTHON_COMMAND[@]}" -m lu_machinery.orchestration.precompact_handoff_check \
  --state-root "$CANONICAL_ROOT" --agent "${SESSION_HANDOFF_AGENT:-claude}" 2>/dev/null) || exit 0
[ "$RESULT" = "prepared" ] || exit 0
printf '%s\n' 'Automatic compaction paused: this session has prepared its rollover handoff. Restart the session to continue.' >&2
exit 2

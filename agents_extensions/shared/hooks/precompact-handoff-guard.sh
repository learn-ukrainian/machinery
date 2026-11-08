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
BOUNDED_PYTHON="${THREAD_ROLLOVER_PYTHON:-$CANONICAL_ROOT/.venv/bin/python}"
[ -x "$BOUNDED_PYTHON" ] || exit 0
# Code root (machinery #100 R62, R69): run from a machinery tree (a checkout or the
# installed wheel), this file's own tree supplies the code; a deployed copy takes it
# from the release the project interpreter imports, never from $PROJECT_DIR. Machinery
# modules then run through the shared module bootstrap (lib/project_interpreter.sh).
_hook_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
case "$_hook_dir" in
  */agents_extensions/shared/hooks) MACHINERY_DIR="${_hook_dir%/agents_extensions/shared/hooks}" ;;
  *) MACHINERY_DIR=$("$BOUNDED_PYTHON" -I -c 'import os, lu_machinery; print(os.path.dirname(os.path.dirname(os.path.abspath(lu_machinery.__file__))))' 2>/dev/null) || MACHINERY_DIR="" ;;
esac
unset _hook_dir
# shellcheck source=lu_machinery/lib/project_interpreter.sh
source "$MACHINERY_DIR/lu_machinery/lib/project_interpreter.sh" 2>/dev/null || exit 0
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

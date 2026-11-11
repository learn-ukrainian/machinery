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

# The installed hook-root rule selects the product, never this file's location.
# shellcheck source=agents_extensions/shared/hooks/context-rollover-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/context-rollover-lib.sh" \
  || { echo "precompact-handoff-guard.sh: context-rollover-lib.sh is missing beside this hook" >&2; exit 1; }
context_machinery_bootstrap "${LU_PRODUCT_ROOT:-${CLAUDE_PROJECT_DIR:-}}" || exit 1

CANONICAL_ROOT=$(context_canonical_root "$PROJECT_DIR") || exit 0
# Code root (machinery #100 R51, R62, R69, R66): the release the one project
# interpreter imports, never this file's location. Fails closed: refuse, with the
# reason on stderr, rather than run code from anywhere else.
BOUNDED_PYTHON="$CONTEXT_PROJECT_PYTHON"
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

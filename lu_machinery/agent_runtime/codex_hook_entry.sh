#!/bin/bash
# Single, deterministic entry point for Codex lifecycle hooks.
#
# Codex launches every matching command hook for an event concurrently. Keeping
# the Bash policy checks in one runner avoids redundant interpreter startup,
# makes their order deterministic, and prevents policy hooks from contending
# with each other for Git/GitHub state.

set -u

MODE="${1:-}"
PAYLOAD=$(cat)

# SCRIPT_ROOT is the machinery tree holding this file: it supplies the code
# (the hooks and the policy module), never the project (machinery #100 R70).
SCRIPT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HOOKS_DIR="$SCRIPT_ROOT/agents_extensions/shared/hooks"
# shellcheck source=lu_machinery/lib/project_interpreter.sh
source "$SCRIPT_ROOT/lu_machinery/lib/project_interpreter.sh"

# A working directory may be a subdirectory. The resolver treats a directory
# with no .git as its own primary, so a nested virtualenv would become the
# interpreter. Walk to the nearest .git and let the resolver validate that
# checkout. A directory outside any checkout is passed through unchanged.
owning_checkout() {
  local dir="$1"
  dir="$(cd -P "$dir" 2>/dev/null && pwd)" || return 1
  while [ ! -e "$dir/.git" ]; do
    [ "$dir" = "/" ] && return 1
    dir="$(dirname "$dir")"
  done
  printf '%s\n' "$dir"
}
# The project is the payload's checkout, else the configured one (CLAUDE_PROJECT_DIR,
# else LU_PRODUCT_ROOT); never this file's own tree.
CONFIGURED_PROJECT="${CLAUDE_PROJECT_DIR:-${LU_PRODUCT_ROOT:-}}"

payload_cwd=$(printf '%s' "$PAYLOAD" | jq -r '
  .tool_input.workdir
  // .tool_input.cwd
  // .tool_input.working_directory
  // .cwd
  // empty
' 2>/dev/null)
[ -n "$payload_cwd" ] || payload_cwd="$CONFIGURED_PROJECT"

COMMON_DIR=""
if [ -n "$payload_cwd" ]; then
  COMMON_DIR=$(git -C "$payload_cwd" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) \
    || COMMON_DIR=""
fi
if [ -z "$COMMON_DIR" ] && [ -n "$CONFIGURED_PROJECT" ]; then
  COMMON_DIR=$(git -C "$CONFIGURED_PROJECT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) \
    || COMMON_DIR=""
fi

if [ -n "$COMMON_DIR" ]; then
  CANONICAL_ROOT="$(dirname "$COMMON_DIR")"
else
  CANONICAL_ROOT="$CONFIGURED_PROJECT"
fi

resolve_root="${payload_cwd:-$CONFIGURED_PROJECT}"
if [ -n "$resolve_root" ] && checkout="$(owning_checkout "$resolve_root")"; then
  resolve_root="$checkout"
fi
PYTHON_BIN=""
if [ -n "$resolve_root" ]; then
  PYTHON_BIN="$(project_interpreter_resolve "$resolve_root" 2>/dev/null)" || PYTHON_BIN=""
fi

case "$MODE" in
  pre-tool-use)
    if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
      printf 'Codex hook policy cannot run: project interpreter is missing\n' >&2
      exit 2
    fi

    printf '%s' "$PAYLOAD" \
      | project_python_run "$PYTHON_BIN" -m lu_machinery.agent_runtime.codex_hook_policy \
        --python-bin "$PYTHON_BIN" \
        --hooks-dir "$HOOKS_DIR" \
        --canonical-root "$CANONICAL_ROOT"
    policy_rc=${PIPESTATUS[1]}
    [ "$policy_rc" -eq 0 ] || exit "$policy_rc"
    ;;

  post-tool-use)
    printf '%s' "$PAYLOAD" \
      | CLAUDE_PROJECT_DIR="$CANONICAL_ROOT" bash "$HOOKS_DIR/tool-timing.sh"
    # stamp-pytest only inspects a Bash `command` (#8529); running it for
    # every other tool payload was pure interpreter-startup cost.
    TOOL_NAME=$(printf '%s' "$PAYLOAD" | jq -r '.tool_name // empty' 2>/dev/null || true)
    if [ "$TOOL_NAME" = "Bash" ]; then
      printf '%s' "$PAYLOAD" \
        | CLAUDE_PROJECT_DIR="$CANONICAL_ROOT" bash "$HOOKS_DIR/stamp-pytest.sh"
      # Primary-checkout dirt audit (#11253): exit 2 hands the attribution
      # report to the model; Codex runs PostToolUse for non-zero exits too.
      AUDIT_HOOK="$HOOKS_DIR/audit-primary-checkout-dirt.py"
      if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
        printf 'Codex primary-checkout audit cannot run: project interpreter is missing\n' >&2
        exit 2
      fi
      printf '%s' "$PAYLOAD" | "$PYTHON_BIN" -I "$AUDIT_HOOK"
      audit_rc=$?
      [ "$audit_rc" -eq 0 ] || exit "$audit_rc"
    fi
    ;;

  user-prompt-submit)
    printf '%s' "$PAYLOAD" \
      | CLAUDE_PROJECT_DIR="$CANONICAL_ROOT" \
        LEARN_UK_HOOK_RECIPIENT=codex \
        bash "$HOOKS_DIR/check-agent-inbox.sh"
    ;;

  *)
    printf 'Unknown Codex hook entry mode: %s\n' "$MODE" >&2
    exit 1
    ;;
esac

#!/bin/bash
# Hook: PreToolUse (Bash) — Enforce .venv/bin/python usage.
# Rejects bare `python3` or `python` commands with one copyable replacement.
# It never rewrites a command behind the caller's back.

# Read tool input from stdin
INPUT=$(cat)

# Extract the command from JSON. An unreadable payload or a missing jq must
# block, not pass every command through unchecked.
if ! COMMAND=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty'); then
  printf 'enforce-venv: hook payload could not be read; blocking.\n' >&2
  exit 2
fi

if [ -z "$COMMAND" ]; then
  exit 0
fi

# Check if the command starts with an unqualified interpreter name.
# Match: "python3 ...", "python ...", but NOT ".venv/bin/python ..." or "/path/to/python ..."
if echo "$COMMAND" | grep -qE '^python3?[[:space:]]'; then
  # Already using venv? Skip.
  if echo "$COMMAND" | grep -qE '^\./\.venv/|^\.venv/|^/.*\.venv/'; then
    exit 0
  fi

  # The suggestion is the interpreter project_interpreter.sh would pick
  # (LEARN_UKRAINIAN_PYTHON, else the primary checkout's .venv). The checkout
  # handed to that selector is LEARN_UK_CANONICAL_ROOT when the Codex policy
  # set it, else the tool's working directory.
  CHECKOUT="${LEARN_UK_CANONICAL_ROOT:-}"
  if [ -z "$CHECKOUT" ]; then
    CHECKOUT=$(printf '%s' "$INPUT" | jq -r '
      .tool_input.workdir
      // .tool_input.cwd
      // .tool_input.working_directory
      // .cwd
      // empty
    ')
    [ -n "$CHECKOUT" ] || CHECKOUT="${CLAUDE_PROJECT_DIR:-${LU_PRODUCT_ROOT:-$PWD}}"
  fi
  # shellcheck source=agents_extensions/shared/hooks/context-rollover-lib.sh
  source "$(dirname "${BASH_SOURCE[0]}")/context-rollover-lib.sh" \
    || { printf 'enforce-venv: context-rollover-lib.sh is missing beside this hook\n' >&2; exit 2; }
  context_machinery_bootstrap "$CHECKOUT" || exit 2
  PYTHON_BIN="$CONTEXT_PROJECT_PYTHON"

  PRIMARY_ROOT=""
  if PRIMARY_ROOT="$(project_primary_root_resolve "$CHECKOUT" 2>/dev/null)"; then
    PYTHON_VERSION_FILE="$PRIMARY_ROOT/.python-version"
  else
    PYTHON_VERSION_FILE=""
  fi
  if [ -z "${LEARN_UKRAINIAN_PYTHON:-}" ] && [ ! -f "$PYTHON_VERSION_FILE" ]; then
    printf 'Unqualified interpreter blocked: project Python pin is missing at %s\n' \
      "${PYTHON_VERSION_FILE:-<unresolved primary>}" >&2
    exit 2
  fi
  if [ -f "$PYTHON_VERSION_FILE" ]; then
    EXPECTED_PYTHON_VERSION=$(head -1 "$PYTHON_VERSION_FILE" | tr -d '[:space:]')
    ACTUAL_PYTHON_VERSION=$("$PYTHON_BIN" --version 2>&1)
    if [ -z "$EXPECTED_PYTHON_VERSION" ] \
      || [ "$ACTUAL_PYTHON_VERSION" != "Python $EXPECTED_PYTHON_VERSION" ]; then
      printf 'Unqualified interpreter blocked: expected Python %s, got %s\n' \
        "${EXPECTED_PYTHON_VERSION:-<missing pin>}" "${ACTUAL_PYTHON_VERSION:-<unavailable>}" >&2
      exit 2
    fi
  fi

  printf -v QUOTED_PYTHON '%q' "$PYTHON_BIN"
  case "$COMMAND" in
    python3*) FIXED="${QUOTED_PYTHON}${COMMAND#python3}" ;;
    python*) FIXED="${QUOTED_PYTHON}${COMMAND#python}" ;;
  esac

  printf 'Unqualified interpreter blocked. Run this command instead:\n  %s\n' "$FIXED" >&2
  exit 2
fi

# Qualified commands pass through unchanged.
exit 0

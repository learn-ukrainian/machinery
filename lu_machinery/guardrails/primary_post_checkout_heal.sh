#!/usr/bin/env bash
# Git post-checkout hook fragment: diagnose a PRIMARY worktree off main.
#
# Git has already moved HEAD when this runs. If the main worktree is detached
# or on a non-main branch, warn without moving HEAD, fetching, or pulling.
# Added worktrees are ignored (feature branches are expected there).
#
# Install via scripts/install_git_hooks.sh. Safe to re-run.

set -euo pipefail

# post-checkout args: previous HEAD, new HEAD, flag (1=branch checkout)
# We do not use them beyond knowing a checkout happened.

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$ROOT" ]; then
  exit 0
fi

# Only act in the main worktree (not .git/worktrees/*)
GIT_DIR="$(git rev-parse --git-dir)"
GIT_COMMON="$(git rev-parse --git-common-dir)"
# Normalize relative paths
GIT_DIR="$(cd "$(dirname "$GIT_DIR")" && pwd)/$(basename "$GIT_DIR")"
GIT_COMMON="$(cd "$(dirname "$GIT_COMMON")" && pwd)/$(basename "$GIT_COMMON")"
if [ "$GIT_DIR" != "$GIT_COMMON" ]; then
  exit 0
fi

if [ ! -f "$ROOT/lu_machinery/guardrails/assert_primary_on_main.py" ]; then
  exit 0
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$script_dir/../lib/project_interpreter.sh"
if ! PY="$(project_interpreter_resolve "$ROOT")"; then
  echo "WARNING: primary-on-main diagnostic skipped; project interpreter is unavailable." >&2
  exit 0
fi

# Diagnose quietly if broken; always allow the checkout to complete (Git has
# already moved it). Repair remains an explicit operator action.
DOCTOR=lu_machinery/guardrails/assert_primary_on_main.py
if ! "$PY" "$ROOT/lu_machinery/guardrails/assert_primary_on_main.py" --cwd "$ROOT" --quiet 2>/dev/null; then
  echo "WARNING: primary is off main/detached. Inspect it, then run the explicit doctor if appropriate: project interpreter $DOCTOR --heal" >&2
fi

exit 0

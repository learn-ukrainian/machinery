#!/usr/bin/env bash
# Shared path and runtime helpers for the tracked Git hook chain.

set -euo pipefail

hook_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
worktree_root="$(git rev-parse --show-toplevel)"
git_common_dir="$(git rev-parse --git-common-dir)"
if [[ "$git_common_dir" != /* ]]; then
  git_common_dir="$worktree_root/$git_common_dir"
fi
main_root="$(cd "$git_common_dir/.." && pwd -P)"

source_root="$worktree_root"
if [[ ! -f "$source_root/lu_machinery/lib/project_interpreter.sh" || ! -f "$source_root/.pre-commit-config.yaml" ]]; then
  source_root="$main_root"
fi

# The one interpreter rule (R66, machinery #100 R79): lib/project_interpreter.sh selects,
# for this worktree, LEARN_UKRAINIAN_PYTHON or the primary checkout's .venv, else refuses.
project_interpreter="$source_root/lu_machinery/lib/project_interpreter.sh"
if [[ ! -f "$project_interpreter" ]]; then
  echo "Git hook error: project interpreter selector not found at $project_interpreter" >&2
  exit 127
fi
# shellcheck source=lu_machinery/lib/project_interpreter.sh
source "$project_interpreter"

run_project_python() {
  local python
  python="$(project_interpreter_resolve "$worktree_root")" || return 127
  "$python" "$@"
}

run_pre_commit() {
  local hook_type="$1"
  shift
  run_project_python \
    -m pre_commit hook-impl \
    -c "$source_root/.pre-commit-config.yaml" \
    --hook-type "$hook_type" \
    --hook-dir "$hook_dir" \
    -- "$@"
}

# A dispatched write task learns at commit or push time that it changed a path outside its
# owned paths; settlement still refuses such a delivery (#12113). Work outside a task skips it.
check_owned_paths() {
  if [[ -z "${LEARN_UKRAINIAN_DISPATCH_TASK_ID:-}" || ! -f "$source_root/lu_machinery/dispatcher/ownership.py" ]]; then
    return 0
  fi
  (cd "$source_root" && run_project_python -m lu_machinery.dispatcher.ownership --worktree "$worktree_root" "$@")
}

require_git_lfs() {
  if ! command -v git-lfs >/dev/null 2>&1; then
    echo "Git hook error: this repository uses Git LFS, but git-lfs is not installed." >&2
    exit 2
  fi
}

#!/bin/bash
# Shared helpers for context-monitor.sh and context-rollover-guard.sh (#8511).
# Sourced, never registered as a hook; executing it directly does nothing.
# State I/O is delegated to the descriptor-relative Python helper (#9791).

# The machinery code root (#100 R62, R69, R66): run from a machinery tree (a checkout
# or the installed wheel), this file's own tree; a deployed copy asks the one project
# interpreter (LEARN_UKRAINIAN_PYTHON when that is an absolute executable file, else
# the canonical checkout's .venv, else refuse) for the release it imports. Never
# <project>/lu_machinery. An invalid override does not fall back.
_context_rule_probe() {
  local configured="${LEARN_UKRAINIAN_PYTHON:-}" root="$1"
  if [ -n "$configured" ]; then
    case "$configured" in
      /*) ;;
      *) return 1 ;;
    esac
    [ -f "$configured" ] && [ -x "$configured" ] || return 1
    printf '%s\n' "$configured"
    return 0
  fi
  # Git could not name the primary, so the probe root is this directory.
  # A linked worktree's .git is a file; its .venv is not the interpreter.
  if [ -f "${PROJECT_DIR:-}/.git" ] && [ "$root" = "${PROJECT_DIR:-}" ]; then
    return 1
  fi
  [ -n "$root" ] && [ -x "$root/.venv/bin/python" ] || return 1
  printf '%s\n' "$root/.venv/bin/python"
}
_context_code_root() {
  local dir probe
  dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || return 1
  case "$dir" in
    */agents_extensions/shared/hooks) printf '%s\n' "${dir%/agents_extensions/shared/hooks}" ;;
    *)
      probe="$(_context_rule_probe "$(context_canonical_root "$1")")" || return 1
      "$probe" -I -c 'import os, lu_machinery; print(os.path.dirname(os.path.dirname(os.path.abspath(lu_machinery.__file__))))' 2>/dev/null
      ;;
  esac
}

# Source the code root's project_interpreter.sh: the interpreter resolver and the
# shared module bootstrap. Returns 1 when the code root is unavailable.
_context_source_interpreter_lib() {
  local root resolved found dir
  dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || return 1
  root=$(_context_code_root "$1") && [ -n "$root" ] || return 1
  # shellcheck source=lu_machinery/lib/project_interpreter.sh
  source "$root/lu_machinery/lib/project_interpreter.sh" 2>/dev/null || return 1
  # In-tree, the sourced tree is the code. A deployed copy re-sources the
  # release the resolver actually selected when that differs from the probe.
  case "$dir" in
    */agents_extensions/shared/hooks) return 0 ;;
  esac
  resolved=$(project_interpreter_resolve "$1" 2>/dev/null) || return 1
  found=$("$resolved" -I -c 'import os, lu_machinery; print(os.path.dirname(os.path.dirname(os.path.abspath(lu_machinery.__file__))))' 2>/dev/null) || return 0
  if [ -n "$found" ] && [ "$found" != "$root" ] && [ -f "$found/lu_machinery/lib/project_interpreter.sh" ]; then
    # shellcheck source=lu_machinery/lib/project_interpreter.sh
    source "$found/lu_machinery/lib/project_interpreter.sh" 2>/dev/null || return 1
  fi
}

# The one project interpreter. Never fall back to PATH Python or a component override.
context_hook_state() {
  local operation="$1" project_dir="$2" session_id="$3" interpreter helper
  shift 3
  _context_source_interpreter_lib "$project_dir" || return 0
  interpreter=$(project_interpreter_resolve "$project_dir" 2>/dev/null) || return 0
  [ -x "$interpreter" ] || return 0
  helper="$(dirname "${BASH_SOURCE[0]}")/context-hook-state.py"
  # The helper is stdlib-only: -I keeps the cwd and PYTHONPATH off its import path.
  "$interpreter" -I "$helper" "$operation" "$project_dir" "$session_id" "$@" 2>/dev/null || true
}

# Latest assistant input/cache usage from a Claude Code transcript, or 0.
# Output tokens are not current context usage and are deliberately excluded.
# An optional usage object lets statuslines apply the same rule to current_usage.
context_latest_usage_tokens() {
  local transcript="$1" usage="${2:-}"
  if [ -z "$usage" ]; then
    usage=$(tail -200 "$transcript" 2>/dev/null \
      | jq -s '[.[] | select(.type == "assistant" and (.message.usage | type) == "object")] | last | .message.usage // empty' 2>/dev/null)
  fi
  if [ -z "$usage" ] || [ "$usage" = "null" ]; then
    printf '0\n'
    return 0
  fi
  # The #10471 measurement found top-level usage summed both main passes around
  # an advisor call. Only the last message iteration describes current main context.
  printf '%s' "$usage" | jq -r '
    def tokens:
      (if type == "number" then .
       elif type == "string" then
         # Absolute anchors reject even a final newline: digits, optional fraction.
         if test("\\A[0-9]+(\\.[0-9]+)?\\z") then (tonumber? // 0) else 0 end
       else 0 end)
      | if isnan or (isfinite | not) or . > 1000000000000000 then 0
        else [., 0] | max | floor end;
    (if (.iterations | type) == "array" then
       ([.iterations[] | select(type == "object" and .type == "message")] | last) // .
     else . end)
    | ((.input_tokens | tokens) + (.cache_read_input_tokens | tokens) + (.cache_creation_input_tokens | tokens))
    | floor
  ' 2>/dev/null || printf '0\n'
}

# Canonical checkout that owns runtime state (.agent/): CODEX_CANONICAL_REPO_ROOT
# when set, else the git common dir's checkout, so linked worktrees share the
# primary checkout's session records and rollover leases.
context_canonical_root() {
  local project_dir="$1" git_common_dir
  if [ -n "${CODEX_CANONICAL_REPO_ROOT:-}" ]; then
    printf '%s\n' "$CODEX_CANONICAL_REPO_ROOT"
    return 0
  fi
  git_common_dir=$(git -C "$project_dir" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)
  if [ -n "$git_common_dir" ] && [ "$(basename "$git_common_dir")" = ".git" ]; then
    dirname "$git_common_dir"
  else
    printf '%s\n' "$project_dir"
  fi
}

# Canonical session record path: an explicit existing override, else
# <canonical checkout>/.agent/sessions/<session id>.json.
context_session_record_file() {
  local project_dir="$1" session_id="$2"
  if [ -n "${LEARN_UKRAINIAN_SESSION_RECORD:-}" ] && [ -f "$LEARN_UKRAINIAN_SESSION_RECORD" ]; then
    printf '%s\n' "$LEARN_UKRAINIAN_SESSION_RECORD"
    return 0
  fi
  printf '%s\n' "$(context_canonical_root "$project_dir")/.agent/sessions/$session_id.json"
}

# Effective rollover mode for this session. operator_restart applies only when
# an operator can restart the session: a delegated worker has nobody to wait
# for, so it keeps the continuation behaviour and its native compaction.
context_effective_rollover_mode() {
  if [ "${1:-}" = "operator_restart" ] && [ -z "${LEARN_UKRAINIAN_DISPATCH_TASK_ID:-}" ]; then
    printf 'operator_restart\n'
  else
    printf 'continuation\n'
  fi
}

# The name of the guard-managed driver lane this session runs in, or nothing.
# scripts/ops/context_guard.py hands off and restarts the lanes in
# scripts/config/driver_lanes.json by tmux session name, so the operator is not
# the one who restarts such a session. Both facts must hold: the launcher's
# exported SESSION_EPIC names a lane epic, and this pane's tmux session is that
# lane's session. The registry is read with the guard's own loader, so a
# registry the guard rejects (duplicate session, malformed file) yields no lane.
# Anything missing or failing means "not guard-managed": the operator text stays.
context_driver_lane_name() {
  local project_dir="$1" interpreter session name
  local registry_module=lu_machinery.ops.driver_lanes
  [ -n "${SESSION_EPIC:-}" ] && [ -n "${TMUX:-}" ] && [ -n "${TMUX_PANE:-}" ] || return 0
  command -v tmux >/dev/null 2>&1 || return 0
  session=$(tmux display-message -p -t "$TMUX_PANE" '#{session_name}' 2>/dev/null) || return 0
  [ -n "$session" ] || return 0
  _context_source_interpreter_lib "$project_dir" || return 0
  interpreter=$(project_interpreter_resolve "$project_dir" 2>/dev/null) || return 0
  [ -x "$interpreter" ] || return 0
  # The registry module runs from the code root through the shared module bootstrap.
  name=$(project_python_run "$interpreter" -c '
import importlib
import sys
load_registry = importlib.import_module(sys.argv[1]).load_registry
lanes = [
    lane.name
    for lane in load_registry().lanes
    if lane.session == sys.argv[2] and lane.epic == sys.argv[3] and lane.kind == "claude"
]
print(lanes[0] if len(lanes) == 1 else "")
' "$registry_module" "$session" "$SESSION_EPIC" 2>/dev/null) || return 0
  printf '%s\n' "$name"
}

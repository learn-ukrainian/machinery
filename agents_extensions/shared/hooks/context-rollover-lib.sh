#!/bin/bash
# Shared helpers for the shell hooks that run machinery code (#8511, #100 R51).
# Sourced, never registered as a hook; executing it directly does nothing.
# State I/O is delegated to the descriptor-relative Python helper (#9791).

# The machinery code root (#100 R51, R62, R69, R66): the release the one project
# interpreter imports, never this file's location and never <project>/lu_machinery.
# The rule is lib/project_interpreter.sh, which lives inside the release it has to
# find, so the first probe writes the same rule out, with that file's gitfile
# checks and without asking git (a rewritten gitfile or commondir must not choose
# the interpreter that runs first): LEARN_UKRAINIAN_PYTHON when it is an absolute
# executable file; else the .venv of the operator-set CODEX_CANONICAL_REPO_ROOT
# when that directory's .git is a real directory or a gitfile, not a symlink and
# not absent; else the .venv of the primary checkout, which is the project itself
# when its .git is a real directory, or the <primary> of a linked worktree whose
# gitfile names a real <primary>/.git/worktrees/<name> that points back at it;
# else refuse. A directory with no .git entry is not a checkout. An invalid
# override does not fall back.
_context_rule_root() {
  local root="$1" git_path line gitdir worktrees common pointer pointed
  git_path="$root/.git"
  [ ! -L "$git_path" ] || return 1
  # A missing .git used to count as a primary, so <dir>/.venv/bin/python ran
  # for any directory a caller passed. Only a real .git directory is this
  # checkout; a gitfile is checked below and never by running git.
  if [ -d "$git_path" ]; then
    printf '%s\n' "$root"
    return 0
  fi
  [ -f "$git_path" ] || return 1
  IFS= read -r line < "$git_path" || true
  case "$line" in gitdir:*) ;; *) return 1 ;; esac
  gitdir="${line#gitdir:}"
  gitdir="${gitdir#"${gitdir%%[![:space:]]*}"}"
  gitdir="${gitdir%"${gitdir##*[![:space:]]}"}"
  case "$gitdir" in /*) ;; *) gitdir="$root/$gitdir" ;; esac
  # Collapse "." and ".." without following symlinks, so a symlink stays visible.
  local part normalized=""
  local -a parts kept=()
  IFS=/ read -r -a parts <<< "$gitdir"
  for part in ${parts[@]+"${parts[@]}"}; do
    case "$part" in
      ''|.) ;;
      ..) [ "${#kept[@]}" -gt 0 ] && unset "kept[$((${#kept[@]} - 1))]" ;;
      *) kept+=("$part") ;;
    esac
  done
  for part in ${kept[@]+"${kept[@]}"}; do normalized="$normalized/$part"; done
  gitdir="$normalized"
  worktrees="${gitdir%/*}"
  common="${worktrees%/*}"
  [ "${worktrees##*/}" = worktrees ] && [ "${common##*/}" = .git ] || return 1
  [ -d "$gitdir" ] && [ ! -L "$gitdir" ] && [ ! -L "$worktrees" ] || return 1
  [ ! -L "$common" ] && [ -d "$common" ] || return 1
  [ -f "$gitdir/gitdir" ] && [ ! -L "$gitdir/gitdir" ] || return 1
  IFS= read -r pointer < "$gitdir/gitdir" || true
  pointer="${pointer#"${pointer%%[![:space:]]*}"}"
  pointer="${pointer%"${pointer##*[![:space:]]}"}"
  case "$pointer" in /*) ;; ?*) pointer="$gitdir/$pointer" ;; *) return 1 ;; esac
  [ "${pointer##*/}" = .git ] || return 1
  pointed="$(cd -P "${pointer%/*}" 2>/dev/null && pwd)" || return 1
  [ "$pointed" = "$root" ] || return 1
  printf '%s\n' "${common%/*}"
}
_context_rule_probe() {
  local configured="${LEARN_UKRAINIAN_PYTHON:-}" project_dir="$1" root git_path
  if [ -n "$configured" ]; then
    case "$configured" in
      /*) ;;
      *) echo "${0##*/}: LEARN_UKRAINIAN_PYTHON must be an absolute path, got $configured" >&2; return 1 ;;
    esac
    [ -f "$configured" ] && [ -x "$configured" ] \
      || { echo "${0##*/}: LEARN_UKRAINIAN_PYTHON=$configured is not an executable file" >&2; return 1; }
    printf '%s\n' "$configured"
    return 0
  fi
  if [ -n "${CODEX_CANONICAL_REPO_ROOT:-}" ]; then
    # Operator-set, so a separate-git-dir gitfile stays valid, but a directory
    # with no .git entry is not a checkout and its interpreter must not run.
    if ! root="$(cd -P "$CODEX_CANONICAL_REPO_ROOT" 2>/dev/null && pwd)"; then
      echo "${0##*/}: project interpreter not found: untrusted git metadata at ${CODEX_CANONICAL_REPO_ROOT}/.git; set LEARN_UKRAINIAN_PYTHON" >&2
      return 1
    fi
    git_path="$root/.git"
    if [ -L "$git_path" ] || { [ ! -d "$git_path" ] && [ ! -f "$git_path" ]; }; then
      echo "${0##*/}: project interpreter not found: untrusted git metadata at $git_path; set LEARN_UKRAINIAN_PYTHON" >&2
      return 1
    fi
  elif ! root="$(cd -P "$project_dir" 2>/dev/null && pwd)" || ! root="$(_context_rule_root "$root")"; then
    echo "${0##*/}: project interpreter not found: untrusted git metadata at $project_dir/.git; set LEARN_UKRAINIAN_PYTHON" >&2
    return 1
  fi
  [ -f "$root/.venv/bin/python" ] && [ -x "$root/.venv/bin/python" ] \
    || { echo "${0##*/}: project interpreter not found: $root/.venv/bin/python; set LEARN_UKRAINIAN_PYTHON" >&2; return 1; }
  printf '%s\n' "$root/.venv/bin/python"
}
_context_import_release() {
  local found
  found="$("$1" -I -c 'import os, lu_machinery; print(os.path.dirname(os.path.dirname(os.path.abspath(lu_machinery.__file__))))' 2>/dev/null)" \
    && [ -f "$found/lu_machinery/lib/project_interpreter.sh" ] \
    && source "$found/lu_machinery/lib/project_interpreter.sh" 2>/dev/null \
    || { echo "${0##*/}: no installed lu_machinery release: the project interpreter $1 cannot import lu_machinery" >&2; return 1; }
  MACHINERY_DIR="$found"
}

# Set MACHINERY_DIR (the installed release) and CONTEXT_PROJECT_PYTHON (the one
# project interpreter) and source the release's project_interpreter.sh. The probe
# only finds the release; the release's resolver then decides, and its release is
# sourced when it differs. Fails closed: on any doubt it says why on stderr and
# returns 1, and the hook refuses instead of running code from anywhere else.
context_machinery_bootstrap() {
  local project_dir="$1" probe resolved start candidate parent
  MACHINERY_DIR="" CONTEXT_PROJECT_PYTHON=""
  # Callers pass the project directory. The probe and the release resolver
  # receive the checkout root: the directory itself when its .git entry is
  # present, otherwise the nearest ancestor that has one. A directory that is
  # not inside a checkout is refused here, before any interpreter runs. The
  # probe's canonical-root override validates that override, not this
  # directory, and project_interpreter_resolve still treats a missing .git as
  # a primary, so it must not see that directory.
  if ! start="$(cd -P "$project_dir" 2>/dev/null && pwd)"; then
    echo "${0##*/}: project interpreter not found: untrusted git metadata at ${project_dir}/.git; set LEARN_UKRAINIAN_PYTHON" >&2
    return 1
  fi
  candidate="$start"
  while [ ! -e "$candidate/.git" ] && [ ! -L "$candidate/.git" ]; do
    parent="$(dirname "$candidate")"
    [ "$parent" != "$candidate" ] || break
    candidate="$parent"
  done
  if [ ! -e "$candidate/.git" ] && [ ! -L "$candidate/.git" ]; then
    echo "${0##*/}: project interpreter not found: untrusted git metadata at ${start}/.git; set LEARN_UKRAINIAN_PYTHON" >&2
    return 1
  fi
  probe="$(_context_rule_probe "$candidate")" || return 1
  _context_import_release "$probe" || return 1
  resolved="$(project_interpreter_resolve "$candidate")" || { MACHINERY_DIR=""; return 1; }
  if [ "$resolved" != "$probe" ]; then
    _context_import_release "$resolved" || { MACHINERY_DIR=""; return 1; }
  fi
  CONTEXT_PROJECT_PYTHON="$resolved"
}

# The one project interpreter. Never fall back to PATH Python or a component override.
context_hook_state() {
  local operation="$1" project_dir="$2" session_id="$3" helper
  shift 3
  [ -n "${CONTEXT_PROJECT_PYTHON:-}" ] || context_machinery_bootstrap "$project_dir" 2>/dev/null || return 0
  # The helper is the imported release's copy, never the one beside this file. It is
  # stdlib-only: -I keeps the cwd and PYTHONPATH off its import path.
  helper="$MACHINERY_DIR/agents_extensions/shared/hooks/context-hook-state.py"
  [ -f "$helper" ] || return 0
  "$CONTEXT_PROJECT_PYTHON" -I "$helper" "$operation" "$project_dir" "$session_id" "$@" 2>/dev/null || true
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
  [ -n "${CONTEXT_PROJECT_PYTHON:-}" ] || context_machinery_bootstrap "$project_dir" 2>/dev/null || return 0
  interpreter="$CONTEXT_PROJECT_PYTHON"
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

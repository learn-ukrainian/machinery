#!/bin/bash
# Hook: Claude invokes this as PostCompact after context compaction. Codex invokes
# it from SessionStart(source=compact), because Codex PostCompact cannot emit
# model-visible additionalContext. Inject a concise context reminder so the agent
# doesn't lose track.

# Skip in non-interactive mode
if [ -n "$CLAUDE_NON_INTERACTIVE" ] || [ -n "$LEARN_UKRAINIAN_PIPELINE" ] || [ -n "$GEMINI_SESSION" ]; then
  exit 0
fi

# A dispatch.py dispatch worker inherits its dispatcher's environment, so a
# worker launched from a driver shell carries that driver's SESSION_EPIC (#10455).
# Driver-only replay (Codex hydration) belongs to the
# launcher-bound driver alone; drop the inherited epic so every branch below
# treats the worker as an ordinary non-driver session.
if [ -n "${LEARN_UKRAINIAN_DISPATCH_TASK_ID:-}" ]; then
  unset SESSION_EPIC
fi

# The project is configured (CLAUDE_PROJECT_DIR from the harness, else
# LU_PRODUCT_ROOT), never derived from this file's location: an installed
# release lives in site-packages, not in the project (machinery #100, R51).
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-${LU_PRODUCT_ROOT:-}}"
case "$PROJECT_DIR" in
  /*) [ -d "$PROJECT_DIR" ] || { echo "post-compact.sh: project root $PROJECT_DIR is not a directory" >&2; exit 1; } ;;
  *) echo "post-compact.sh: no project root: set CLAUDE_PROJECT_DIR or LU_PRODUCT_ROOT to an absolute checkout path" >&2; exit 1 ;;
esac

# Resolve the primary checkout before choosing an interpreter, exactly as
# session-setup.sh does: it owns the shared venv and the Claude memory
# directory, and a linked worktree has neither. This must not depend on
# run_bounded, whose interpreter is chosen from the result.
_HOOK_TIMEOUT_BIN="$(command -v timeout 2>/dev/null || command -v gtimeout 2>/dev/null || true)"
git_common_dir() {
  if [ -n "$_HOOK_TIMEOUT_BIN" ]; then
    "$_HOOK_TIMEOUT_BIN" -s KILL 2 git -C "$PROJECT_DIR" rev-parse --path-format=absolute \
      --git-common-dir 2>/dev/null
  else
    git -C "$PROJECT_DIR" rev-parse --path-format=absolute --git-common-dir 2>/dev/null
  fi
}
GIT_COMMON_DIR=$(git_common_dir) || GIT_COMMON_DIR=""
PRIMARY_ROOT=""
if [ -n "$GIT_COMMON_DIR" ] && [ "$(basename "$GIT_COMMON_DIR")" = ".git" ]; then
  PRIMARY_ROOT=$(dirname "$GIT_COMMON_DIR")
fi
if [ -n "${CODEX_CANONICAL_REPO_ROOT:-}" ]; then
  CANONICAL_ROOT="$CODEX_CANONICAL_REPO_ROOT"
else
  CANONICAL_ROOT="${PRIMARY_ROOT:-$PROJECT_DIR}"
fi
# One interpreter (R66). An invalid override does not fall back. A missing
# interpreter leaves the later bounded calls fail-open (return 127).
_post_probe_python() {
  local configured="${LEARN_UKRAINIAN_PYTHON:-}"
  if [ -n "$configured" ]; then
    case "$configured" in
      /*) ;;
      *) return 1 ;;
    esac
    [ -f "$configured" ] && [ -x "$configured" ] || return 1
    printf '%s\n' "$configured"
    return 0
  fi
  # Git could not name the primary, so CANONICAL_ROOT is this directory.
  # A linked worktree's .git is a file; its .venv is not the interpreter.
  if [ -f "${PROJECT_DIR:-}/.git" ] && [ "$CANONICAL_ROOT" = "${PROJECT_DIR:-}" ]; then
    return 1
  fi
  [ -x "$CANONICAL_ROOT/.venv/bin/python" ] || return 1
  printf '%s\n' "$CANONICAL_ROOT/.venv/bin/python"
}
BOUNDED_PYTHON=""
MACHINERY_DIR=""
_hook_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || _hook_dir=""
case "$_hook_dir" in
  */agents_extensions/shared/hooks)
    MACHINERY_DIR="${_hook_dir%/agents_extensions/shared/hooks}"
    # shellcheck source=lu_machinery/lib/project_interpreter.sh
    source "$MACHINERY_DIR/lu_machinery/lib/project_interpreter.sh" 2>/dev/null || MACHINERY_DIR=""
    if [ -n "$MACHINERY_DIR" ]; then
      BOUNDED_PYTHON="$(project_interpreter_resolve "$PROJECT_DIR" 2>/dev/null)" || BOUNDED_PYTHON=""
    fi
    ;;
  *)
    BOUNDED_PYTHON="$(_post_probe_python 2>/dev/null)" || BOUNDED_PYTHON=""
    if [ -n "$BOUNDED_PYTHON" ]; then
      MACHINERY_DIR=$("$BOUNDED_PYTHON" -I -c 'import os, lu_machinery; print(os.path.dirname(os.path.dirname(os.path.abspath(lu_machinery.__file__))))' 2>/dev/null) || MACHINERY_DIR=""
      # shellcheck source=lu_machinery/lib/project_interpreter.sh
      source "$MACHINERY_DIR/lu_machinery/lib/project_interpreter.sh" 2>/dev/null || MACHINERY_DIR=""
      if [ -n "$MACHINERY_DIR" ]; then
        RESOLVED="$(project_interpreter_resolve "$PROJECT_DIR" 2>/dev/null)" || RESOLVED=""
        if [ -n "$RESOLVED" ] && [ "$RESOLVED" != "$BOUNDED_PYTHON" ]; then
          MACHINERY_DIR=$("$RESOLVED" -I -c 'import os, lu_machinery; print(os.path.dirname(os.path.dirname(os.path.abspath(lu_machinery.__file__))))' 2>/dev/null) || MACHINERY_DIR=""
          # shellcheck source=lu_machinery/lib/project_interpreter.sh
          source "$MACHINERY_DIR/lu_machinery/lib/project_interpreter.sh" 2>/dev/null || MACHINERY_DIR=""
        fi
        [ -n "$RESOLVED" ] && BOUNDED_PYTHON="$RESOLVED"
      fi
    fi
    ;;
esac
unset _hook_dir
BOUNDED_RUNNER_DEFAULT="$MACHINERY_DIR/lu_machinery/agent_runtime/bounded_command.py"
BOUNDED_RUNNER="${SESSION_BOUNDED_RUNNER:-$BOUNDED_RUNNER_DEFAULT}"
run_bounded() {
  local timeout_seconds="$1"
  shift
  if [ ! -x "$BOUNDED_PYTHON" ] || [ ! -f "$BOUNDED_RUNNER" ]; then
    return 127
  fi
  "$BOUNDED_PYTHON" -I "$BOUNDED_RUNNER" --timeout "$timeout_seconds" -- "$@"
}
# run_bounded_python <timeout> <python> (-m <module> | -c <code>) [args...]: a machinery
# child through the shared module bootstrap, under the bound.
run_bounded_python() {
  local timeout_seconds="$1" python="$2"
  shift 2
  [ -n "$MACHINERY_DIR" ] && declare -F project_python_command >/dev/null || return 127
  project_python_command "$python"
  run_bounded "$timeout_seconds" "${PROJECT_PYTHON_COMMAND[@]}" "$@"
}
CONTEXT=""

emit_context() {
  local context="$1"
  if [ "${CODEX_COMPACT_SESSION_START:-}" = "1" ]; then
    printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":%s}}' \
      "$(printf '%s' "$context" | jq -Rs '.')"
  else
    printf '{"additionalContext": %s}' "$(printf '%s' "$context" | jq -Rs '.')"
  fi
}

# Grok (native TUI / build): skip the Claude curriculum in_progress scan entirely
# (hook audit 2026-08-06). Epic-bound Grok still gets a short reminder; native
# compaction remains authoritative. Do not run multi-second find/grep over curriculum/.
if [ -n "${GROK_AGENT:-}" ] || [ "${SESSION_HANDOFF_AGENT:-}" = "grok" ] \
  || [ "${SESSION_HANDOFF_AGENT:-}" = "grok-build" ]; then
  _grok_ctx="KEY REMINDERS (Grok post-compact, thin path):
  - Fleet driver: dispatch with ROUTING_CARD_V1; default code author = Claude (Sonnet 5.5; Opus 5.5 for hard or security work), reviewed by Sol 6.1; Ukrainian content authored by Sol 6.1; review seats are Gemini (AGY) and Sol, Gemini (AGY) alone for an OpenAI author, Sol alone for a Google author, and no Claude seat; Sol at high is an eligible code worker only when the routing card says why; explicit Luna/Flash bounded routes require a Sol advisory envelope.
  - Heavy atlas compute on VPS (atlas-runner), not the Mac.
  - Tool-backed claims only; breadth report before handoff.
  - Epic=${SESSION_EPIC:-none}"
  if [ -n "${SESSION_EPIC:-}" ]; then
    _grok_ctx="${_grok_ctx}
  - Continue from file handoff / fleet-comms for epic ${SESSION_EPIC}; do not re-scan curriculum state."
  fi
  emit_context "$_grok_ctx"
  exit 0
fi

# Ordinary Codex tasks use the runtime's native remote compaction and need no
# repository-authored context replay. A launcher-bound fleet driver is the
# exception: hydrate only its exact stream and point at the durable shadow
# diary instead of scanning every rollover or curriculum artifact.
POST_COMPACT_AGENT="${SESSION_HANDOFF_AGENT:-}"
if [ -z "$POST_COMPACT_AGENT" ] \
  && { [[ "${0:-}" == *"/.codex/"* ]] || [ -n "${CODEX_THREAD_ID:-}${CODEX_SESSION_ID:-}${CODEX_SESSION:-}" ]; }; then
  POST_COMPACT_AGENT="codex"
fi
case "$POST_COMPACT_AGENT" in
  codex|codex-*)
    if [ -z "${SESSION_EPIC:-}" ]; then
      # Native Codex compaction is self-contained for non-driver tasks. This
      # includes explicitly tagged Codex sessions: they must never fall through
      # into the shared Claude-oriented replay below.
      exit 0
    else
      # Reuse the canary's lightweight selector without importing lane runtimes.
      # A first Codex launch may continue
      # from the shared driver handoff; a fixed filename can select stale state.
      DIARY_REL=""
      HANDOFF_SELECT_MODULE=lu_machinery.session_canary.handoff_select
      DIARY_REL=$(run_bounded_python 2 "$BOUNDED_PYTHON" -c '
import importlib
import sys
from pathlib import Path

load_codex_candidates = importlib.import_module(sys.argv[3]).load_codex_candidates

repo = Path(sys.argv[1]).resolve()
selected = next((item.path for item in load_codex_candidates(repo, sys.argv[2]) if item.path.is_file()), None)
if selected is not None:
    print(selected.resolve().relative_to(repo))
' "$PROJECT_DIR" "$SESSION_EPIC" "$HANDOFF_SELECT_MODULE" 2>/dev/null) || DIARY_REL=""
      HYDRATION_STREAM="${SESSION_STREAM_ID:-}"
      if [ -z "$HYDRATION_STREAM" ]; then
        # Positional arguments expand in the bounded child shell.
        # shellcheck disable=SC2016
        HYDRATION_STREAM=$(run_bounded 2 bash -c '
source "$1" && launcher_selector_stream "$2"
' post-compact-stream "$MACHINERY_DIR/lu_machinery/lib/handoff_identity.sh" "$SESSION_EPIC" 2>/dev/null) \
          || HYDRATION_STREAM=""
      fi
      if [ -z "$HYDRATION_STREAM" ]; then
        HYDRATION_RC=2
        HYDRATION="unresolved-stream-selector: $SESSION_EPIC. Repair the launcher selector before continuing."
      elif [ -z "$DIARY_REL" ] || [ ! -f "$PROJECT_DIR/$DIARY_REL" ]; then
        HYDRATION_RC=2
        HYDRATION="No Codex/shared driver handoff selected by the Codex canary resolver. Repair the handoff before continuing."
      else
        HYDRATION_RC=0
        # Hydrate retries timeout-class failures up to 3 times (500 ms each plus
        # interpreter start-up), so it needs more than the 2 s used elsewhere here.
        HYDRATION=$(run_bounded_python 6 "$BOUNDED_PYTHON" \
          -m lu_machinery.session_canary.codex_lane hydrate --epic "$SESSION_EPIC" --stream "$HYDRATION_STREAM" 2>&1) \
          || HYDRATION_RC=$?
      fi
      if [ "$HYDRATION_RC" -eq 0 ]; then
        # The lane goal file is the operator-maintained state; a stream boundary
        # can lag behind it, so the goal file wins when the two disagree.
        GOAL_REL=".claude/${SESSION_EPIC}-epic/DRIVER-STATE.md"
        if [ -f "$PROJECT_DIR/$GOAL_REL" ]; then
          BOUNDARY_RULE="Lane goal file: $GOAL_REL. It outranks the capsule's next_drive_boundary: where they conflict, follow the goal file and record a corrected next_action in the stream. Native Codex still owns compaction."
        else
          BOUNDARY_RULE="Native Codex still owns compaction; continue only from the capsule's next_drive_boundary."
        fi
        CONTEXT="CODEX FLEET-DRIVER HYDRATION
$HYDRATION
Shadow diary: $DIARY_REL
$BOUNDARY_RULE"
      else
        CONTEXT="CODEX FLEET-DRIVER HYDRATION BLOCKED
${HYDRATION:-Hydration helper unavailable.}
Shadow diary: $DIARY_REL
Do not select the next queue action until the stream lease or diary is repaired."
      fi
      emit_context "$CONTEXT"
      exit 0
    fi
    ;;
esac

# 1. Find current in-progress modules
IN_PROGRESS=""
if [ -d "$PROJECT_DIR/curriculum" ]; then
  IN_PROGRESS=$(run_bounded 2 find "$PROJECT_DIR/curriculum" -name "state-v3.json" \
    -exec grep -l '"in_progress"' {} \; 2>/dev/null | head -3)
fi

if [ -n "$IN_PROGRESS" ]; then
  MODULE_LIST=$(echo "$IN_PROGRESS" | while read -r f; do
    slug=$(basename "$(dirname "$f")")
    track=$(basename "$(dirname "$(dirname "$(dirname "$f")")")")
    echo "  - $track/$slug"
  done)
  CONTEXT="$CONTEXT
IN-PROGRESS MODULES:
$MODULE_LIST"
fi

# 2. Local rollover health only. PostCompact must never prepare, resume, prove,
# confirm, clean up, query GitHub, or manufacture handoff anchors.
HANDOFF_AGENT="${SESSION_HANDOFF_AGENT:-}"
if [ -z "$HANDOFF_AGENT" ]; then
  if [[ "${0:-}" == *"/.codex/"* ]] || [ -n "${CODEX_THREAD_ID:-}${CODEX_SESSION_ID:-}" ]; then
    HANDOFF_AGENT="codex"
  else
    HANDOFF_AGENT="claude"
  fi
fi
ROLLOVER_PYTHON="$BOUNDED_PYTHON"
# THREAD_ROLLOVER_SCRIPT names a replacement script (a test seam); the default is
# this code root's thread_handoff module through the bootstrap.
if [ -n "${THREAD_ROLLOVER_SCRIPT:-}" ]; then
  ROLLOVER_HEALTH=$(run_bounded 2 "$ROLLOVER_PYTHON" -I "$THREAD_ROLLOVER_SCRIPT" \
    --repo-root "$CANONICAL_ROOT" detect --agent "$HANDOFF_AGENT" 2>&1) || true
else
  ROLLOVER_HEALTH=$(run_bounded_python 2 "$ROLLOVER_PYTHON" -m lu_machinery.orchestration.thread_handoff \
    --repo-root "$CANONICAL_ROOT" detect --agent "$HANDOFF_AGENT" 2>&1) || true
fi

# 3. Key reminders. Claude keys its per-project directory by the checkout
# path with every character outside [A-Za-z0-9-] replaced by '-'. The memory
# directory belongs to the primary checkout, resolved exactly as
# session-setup.sh resolves it (CANONICAL_ROOT, above), so a worktree session
# names the same directory.
CLAUDE_PROJECT_KEY=$(printf '%s' "$CANONICAL_ROOT" | sed 's/[^A-Za-z0-9-]/-/g')
CONTEXT="$CONTEXT
KEY REMINDERS:
  - Thread rollover health (read-only): $ROLLOVER_HEALTH
  - If a live packet is shown, read its handoff path; SessionStart provides the lifecycle commands.
  - Legacy module word targets are MINIMUMS (check config.py); core fresh-build lessons: non-negotiable-rules.md rule 4
  - Edit agents_extensions/shared/, not .claude/ directly
  - .venv/bin/python only
  - Pre-commit: ruff + /simplify + cross-family code review (non-Gemini seat)
  - Read audit/ and review/ files before fixing modules
  - MEMORY: \$HOME/.claude/projects/$CLAUDE_PROJECT_KEY/memory/MEMORY.md"
emit_context "CONTEXT RESTORED AFTER COMPACTION:$CONTEXT"
exit 0

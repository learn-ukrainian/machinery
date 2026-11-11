#!/bin/bash
# Hook: Stop — /goal driver async-dispatch awareness + thread-lease heartbeat.
#
# Reads the Stop-event JSON on stdin and asks scripts/goal_driver/stop_hook.py
# to inspect the transcript's last status line. The Python module emits
# additionalContext if (a) the last turn was GOAL_WAIT, or (b) the last turn
# was GOAL_STATUS while /api/delegate/active reports an in-flight dispatch.
#
# This hook NEVER blocks Stop. /goal's native predicate enforcement is
# unchanged; this hook only annotates state so the next turn's counters
# stay honest under async-heavy work. See issue #1933.
#
# It also best-effort refreshes this session's durable thread-lease heartbeat
# (see refresh_thread_lease_heartbeat in scripts/orchestration/
# thread_handoff.py). This is diagnostic only — claim_thread_lease never
# takes an uncheckable owner over on clock age at all, so there is no window
# left for a fresh heartbeat to protect. The refresh is a no-op unless this
# exact session already owns the lease AND its process identity can be
# reconfirmed (require_proof=True re-derives this process's harness-ancestor
# pid/start time), so it can never steal or clobber another session's lease.
# No --generation is passed: it used to be required
# (LEARN_UKRAINIAN_THREAD_LEASE_GENERATION, exported by session-setup.sh into
# $CLAUDE_ENV_FILE), but that export reaches only Bash tool calls, never this
# hook's own subprocess, so the generation gate was silently dead code in
# every real session — identity proof is strictly stronger and is now the
# sole fence. This refresh is unconditional (unthrottled) since Stop only
# fires once per turn; the throttled per-tool-call companion is
# thread-lease-heartbeat.sh (PostToolUse, issue #5759).
#
# Skip in non-interactive / pipeline contexts to avoid latency in batch jobs.

if [ -n "$CLAUDE_NON_INTERACTIVE" ] || [ -n "$LEARN_UKRAINIAN_PIPELINE" ] || [ -n "$GEMINI_SESSION" ]; then
  exit 0
fi

# The installed hook-root rule selects the product, never this file's location.
# shellcheck source=agents_extensions/shared/hooks/context-rollover-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/context-rollover-lib.sh" \
  || { echo "goal-driver-stop.sh: context-rollover-lib.sh is missing beside this hook" >&2; exit 1; }
context_machinery_bootstrap "${LU_PRODUCT_ROOT:-${CLAUDE_PROJECT_DIR:-}}" || exit 1

# Canonical checkout owns the shared state: linked worktrees share the primary's
# (F001 r5 on #5896: the PROJECT_DIR default silently skipped this hook in
# every worktree session).
CANONICAL_ROOT=$(context_canonical_root "$PROJECT_DIR")
# One interpreter (R66) and code root (machinery #100 R51, R62, R69): the release the
# one project interpreter imports, never this file's location. Fails closed: refuse,
# with the reason on stderr, rather than run code from anywhere else. Exit 1 does not block Stop.
PYTHON="$CONTEXT_PROJECT_PYTHON"

# The modules run from the code root above, never the cwd; the project root stays
# the working directory the stop hook reports against (cwd-drift family: #4912/#4899).
cd "$PROJECT_DIR" || exit 0

# Read stdin exactly once — both the heartbeat refresh below and the exec'd
# stop_hook module need the same session_id / transcript payload.
STDIN_JSON=""
if [ ! -t 0 ]; then
  STDIN_JSON=$(cat)
fi

HANDOFF_AGENT="${SESSION_HANDOFF_AGENT:-claude}"
case "$HANDOFF_AGENT" in
  claude|claude-*)
    if [ -n "$STDIN_JSON" ]; then
      SESSION_ID=$(printf '%s' "$STDIN_JSON" | jq -r '.session_id // empty' 2>/dev/null)
      if [ -n "$SESSION_ID" ]; then
        # CANONICAL_ROOT derived once at the top of this hook (F001 r5).
        # No --generation: identity proof is the sole fence (see above). If
        # this repo checkout still has an OLDER thread_handoff.py that
        # hard-requires --generation, the CLI's error is swallowed by
        # `|| true` below — a safe no-op in a mixed-deploy, exactly like the
        # old missing-env-var path.
        project_python_run "$PYTHON" -m lu_machinery.orchestration.thread_handoff \
          --repo-root "$CANONICAL_ROOT" refresh-thread-lease-heartbeat \
          --agent "$HANDOFF_AGENT" --current-thread-id "$SESSION_ID" \
          >/dev/null 2>&1 || true
        unset CANONICAL_ROOT GIT_COMMON_DIR
      fi
      unset SESSION_ID
    fi
    ;;
esac
unset HANDOFF_AGENT

project_python_command "$PYTHON"
exec "${PROJECT_PYTHON_COMMAND[@]}" -m lu_machinery.goal_driver.stop_hook <<<"$STDIN_JSON"

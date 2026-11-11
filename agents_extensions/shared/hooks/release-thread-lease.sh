#!/bin/bash
# Hook: SessionEnd — best-effort release of this session's durable thread lease.
#
# session-setup.sh claims a single-writer lease per agent slot at SessionStart
# so two driver sessions can never double-drive the same queue. Historically
# there was no release path at all: an exited or crashed session held the
# slot until its heartbeat aged past a fixed clock window, which is exactly
# the operator-restart lockout this lease exists to prevent from recurring in
# the *other* direction.
#
# This hook is the fast, cooperative release path. It is intentionally
# best-effort: SessionEnd does not fire on SIGKILL or a hard crash, so the
# owner-pid liveness check in claim_thread_lease (scripts/orchestration/
# thread_handoff.py) remains the primary mechanism. Release is a no-op unless
# this exact session id still owns the lease, so a hook that fires late (or
# fires after another session has already taken the lease over) can never
# clobber a newer owner's lease.
#
# Fenced by process identity, not generation: release_thread_lease re-derives
# this calling process's harness-ancestor pid/start time and requires it to
# match what the lease recorded (require_proof=True) before releasing
# anything — this is the fence that makes the previous paragraph true, and it
# is strictly stronger than a caller-supplied generation. This used to
# require --generation (exported by session-setup.sh into $CLAUDE_ENV_FILE as
# LEARN_UKRAINIAN_THREAD_LEASE_GENERATION), but that export reaches only Bash
# tool calls, never this hook's own subprocess, so the generation gate was
# silently dead code in every real session — every SessionEnd release
# no-oped, which is exactly how a 4h-ended session was still able to lock out
# its successor. This hook no longer requires a generation at all.
#
# Skip in non-interactive / pipeline contexts, matching session-setup.sh.

if [ -n "$CLAUDE_NON_INTERACTIVE" ] || [ -n "$LEARN_UKRAINIAN_PIPELINE" ] || [ -n "$GEMINI_SESSION" ]; then
  exit 0
fi

# The installed hook-root rule selects the product, never this file's location.
# shellcheck source=agents_extensions/shared/hooks/context-rollover-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/context-rollover-lib.sh" \
  || { echo "release-thread-lease.sh: context-rollover-lib.sh is missing beside this hook" >&2; exit 1; }
context_machinery_bootstrap "${LU_PRODUCT_ROOT:-${CLAUDE_PROJECT_DIR:-}}" || exit 1

# Canonical checkout owns the shared state: linked worktrees share the primary's
# (F001 r5 on #5896: the PROJECT_DIR default silently skipped this hook in
# every worktree session).
CANONICAL_ROOT=$(context_canonical_root "$PROJECT_DIR")
# One interpreter (R66) and code root (machinery #100 R51, R62, R69): the release the
# one project interpreter imports, never this file's location. Fails closed: refuse,
# with the reason on stderr, rather than run code from anywhere else. Exit 1 does not block SessionEnd.
PYTHON="$CONTEXT_PROJECT_PYTHON"

cd "$PROJECT_DIR" || exit 0

STDIN_JSON=""
if [ ! -t 0 ]; then
  STDIN_JSON=$(cat)
fi

if [ -z "$STDIN_JSON" ]; then
  exit 0
fi

SESSION_ID=$(printf '%s' "$STDIN_JSON" | jq -r '.session_id // empty' 2>/dev/null)
if [ -z "$SESSION_ID" ]; then
  exit 0
fi

# SessionEnd marker cleanup is fenced by the harness-owner PID.  A delayed
# predecessor with the same session id must not remove a successor marker;
# the sweeper remains responsible for dead or expired markers.
SESSION_MARKER_PID="${SESSION_PID:-$PPID}"
if [[ "$SESSION_MARKER_PID" =~ ^[1-9][0-9]*$ ]]; then
  project_python_run "$PYTHON" -m lu_machinery.orchestration.session_markers \
    remove --instance-id "$SESSION_ID" --pid "$SESSION_MARKER_PID" \
    >/dev/null 2>&1 || true
fi
unset SESSION_MARKER_PID

HANDOFF_AGENT="${SESSION_HANDOFF_AGENT:-claude}"
case "$HANDOFF_AGENT" in
  claude|claude-*) ;;
  *) exit 0 ;;
esac

# NO generation is carried here — deliberately (formal CF F001 round 3 on
# #5896): a sidecar keyed only by SESSION_ID is MUTABLE across a same-id
# resume, so a delayed SessionEnd from a dead predecessor could read the
# SUCCESSOR's generation and tombstone the successor's live lease — defeating
# the exact ABA protection the generation fence exists for. Identity proof is
# the sole fence on this path: release no-ops when the process identity cannot
# be reconfirmed, and claim_thread_lease's pid-liveness check reclaims any
# stale lease that leaves behind. If a repo checkout still has an OLDER
# thread_handoff.py that hard-requires --generation, the CLI error is
# swallowed by `|| true` — a safe no-op in a mixed deploy.
project_python_run "$PYTHON" -m lu_machinery.orchestration.thread_handoff --repo-root "$CANONICAL_ROOT" \
  release-thread-lease --agent "$HANDOFF_AGENT" --current-thread-id "$SESSION_ID" \
  >/dev/null 2>&1 || true

exit 0

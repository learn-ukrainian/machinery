#!/bin/bash
# Hook: Check for unread cross-agent messages on every prompt submit.
# Serves every agent seat (claude/codex/gemini/agy/grok/orchestrator) via
# LEARN_UK_HOOK_RECIPIENT; the Gemini CLI seat is retired, the name is not.
# Uses the live inbox CLI so fleet-comms remains the authority for messages.
#
# Fast path (#8529): a read-only, 1 s-bounded unread COUNT against the same
# broker DB the CLI reads skips the Python bridge startup entirely when the
# inbox is empty. Any probe uncertainty falls through to the live CLI so
# messages are never hidden.
#
# PIPELINE GUARD: Skips during build_module / ai_agent_bridge runs
# to prevent ping-pong between automated pipeline phases.

# Skip in pipeline/headless mode
if [ "${GEMINI_SESSION:-}" = "1" ] || [ "${LEARN_UKRAINIAN_PIPELINE:-}" = "1" ]; then
  exit 0
fi

HOOK_INPUT=$(cat)
# The project is configured (CLAUDE_PROJECT_DIR from the harness, else
# LU_PRODUCT_ROOT), never derived from this file's location: an installed
# release lives in site-packages, not in the project (machinery #100, R51).
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-${LU_PRODUCT_ROOT:-}}"
case "$PROJECT_DIR" in
  /*) [ -d "$PROJECT_DIR" ] || { echo "check-agent-inbox.sh: project root $PROJECT_DIR is not a directory" >&2; exit 1; } ;;
  *) echo "check-agent-inbox.sh: no project root: set CLAUDE_PROJECT_DIR or LU_PRODUCT_ROOT to an absolute checkout path" >&2; exit 1 ;;
esac
RECIPIENT="${LEARN_UK_HOOK_RECIPIENT:-claude}"

case "$RECIPIENT" in
  claude|codex|gemini|agy|grok|orchestrator) ;;
  *) exit 0 ;;
esac

RECIPIENT_LABEL=$(printf '%s' "$RECIPIENT" | tr '[:lower:]' '[:upper:]')

# The inbox CLI is optional in hook contexts. A missing project interpreter or
# a CLI failure must never block the provider's prompt submission. The interpreter
# is the one rule (R66): an invalid LEARN_UKRAINIAN_PYTHON does not fall back to
# this checkout's .venv, and a worktree .venv is not the interpreter.
_inbox_probe_python() {
  local configured="${LEARN_UKRAINIAN_PYTHON:-}" root=""
  if [ -n "$configured" ]; then
    case "$configured" in
      /*) ;;
      *) return 1 ;;
    esac
    [ -f "$configured" ] && [ -x "$configured" ] || return 1
    printf '%s\n' "$configured"
    return 0
  fi
  if [ -n "${CODEX_CANONICAL_REPO_ROOT:-}" ]; then
    root="$CODEX_CANONICAL_REPO_ROOT"
  else
    root=$(git -C "$PROJECT_DIR" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)
    if [ -n "$root" ] && [ "$(basename "$root")" = ".git" ]; then
      root=$(dirname "$root")
    else
      root="$PROJECT_DIR"
    fi
  fi
  [ -x "$root/.venv/bin/python" ] || return 1
  printf '%s\n' "$root/.venv/bin/python"
}
_hook_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 0
case "$_hook_dir" in
  */agents_extensions/shared/hooks)
    MACHINERY_DIR="${_hook_dir%/agents_extensions/shared/hooks}"
    # shellcheck source=lu_machinery/lib/project_interpreter.sh
    source "$MACHINERY_DIR/lu_machinery/lib/project_interpreter.sh" 2>/dev/null || exit 0
    INBOX_PYTHON="$(project_interpreter_resolve "$PROJECT_DIR" 2>/dev/null)" || exit 0
    ;;
  *)
    INBOX_PYTHON="$(_inbox_probe_python 2>/dev/null)" || exit 0
    MACHINERY_DIR=$("$INBOX_PYTHON" -I -c 'import os, lu_machinery; print(os.path.dirname(os.path.dirname(os.path.abspath(lu_machinery.__file__))))' 2>/dev/null) || exit 0
    # shellcheck source=lu_machinery/lib/project_interpreter.sh
    source "$MACHINERY_DIR/lu_machinery/lib/project_interpreter.sh" 2>/dev/null || exit 0
    RESOLVED="$(project_interpreter_resolve "$PROJECT_DIR" 2>/dev/null)" || exit 0
    if [ "$RESOLVED" != "$INBOX_PYTHON" ]; then
      MACHINERY_DIR=$("$RESOLVED" -I -c 'import os, lu_machinery; print(os.path.dirname(os.path.dirname(os.path.abspath(lu_machinery.__file__))))' 2>/dev/null) || exit 0
      # shellcheck source=lu_machinery/lib/project_interpreter.sh
      source "$MACHINERY_DIR/lu_machinery/lib/project_interpreter.sh" 2>/dev/null || exit 0
    fi
    INBOX_PYTHON="$RESOLVED"
    ;;
esac
unset _hook_dir

# Fast path: probe the same broker DB the bridge CLI reads
# (scripts/ai_agent_bridge/_config.py DB_PATH → _messaging.py check_inbox)
# with a read-only, 1 s-bounded unread count. Zero unread → exit silently
# without paying the Python bridge startup. DB missing/locked/error, schema
# drift, or an inconclusive result → fall through to the live CLI below.
DB_FILE="${AB_DB_PATH:-}"
if [ -z "$DB_FILE" ]; then
  GIT_COMMON_DIR=$(git -C "$PROJECT_DIR" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)
  if [ -n "$GIT_COMMON_DIR" ]; then
    DB_FILE="$(dirname "$GIT_COMMON_DIR")/.mcp/servers/message-broker/messages.db"
  fi
fi

if [ -n "$DB_FILE" ] && [ -f "$DB_FILE" ]; then
  # Mirror check_inbox recipient resolution for the seats this hook serves:
  # resolve_recipient_alias is identity for registered seats, and
  # seat_read_aliases dual-reads only grok ↔ grok-build. RECIPIENT is already
  # whitelisted above, so interpolating it into SQL cannot inject.
  case "$RECIPIENT" in
    grok|grok-build) TO_LLM_SQL="'grok','grok-build'" ;;
    *) TO_LLM_SQL="'$RECIPIENT'" ;;
  esac
  # "unread" matches _message_consumption_state: neither acknowledged nor
  # consumed by a live driver (NULL is falsy in Python, 0 via COALESCE here).
  UNREAD_COUNT=""
  if command -v sqlite3 >/dev/null 2>&1; then
    UNREAD_COUNT=$(sqlite3 -readonly -cmd ".timeout 1000" "$DB_FILE" \
      "SELECT COUNT(*) FROM messages WHERE to_llm IN ($TO_LLM_SQL) AND COALESCE(acknowledged, 0) = 0 AND COALESCE(consumed_by_live_driver, 0) = 0;" \
      2>/dev/null || true)
  else
    # stdlib-only fallback for hosts without the sqlite3 CLI; prints nothing
    # on any error so the live CLI below stays the authority.
    UNREAD_COUNT=$("$INBOX_PYTHON" -I - "$DB_FILE" "$RECIPIENT" <<'PYEOF'
import sqlite3
import sys
from pathlib import Path

db, recipient = sys.argv[1], sys.argv[2]
recipients = ("grok", "grok-build") if recipient in ("grok", "grok-build") else (recipient,)
try:
    conn = sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True, timeout=1.0)
    try:
        placeholders = ",".join("?" * len(recipients))
        row = conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE to_llm IN ({placeholders}) "
            "AND COALESCE(acknowledged, 0) = 0 AND COALESCE(consumed_by_live_driver, 0) = 0",
            recipients,
        ).fetchone()
        print(row[0])
    finally:
        conn.close()
except Exception:
    pass
PYEOF
    )
  fi
  case "$UNREAD_COUNT" in
    ''|*[!0-9]*) ;;  # probe inconclusive → the live CLI decides below
    0) exit 0 ;;
  esac
fi

# Ask the live CLI for the recipient's inbox. Keep its existing output as the
# source of both the count and the bounded preview; the fast path above only
# ever short-circuits a proven-empty inbox, never replaces this listing.
INBOX_OUTPUT=$(project_python_run "$INBOX_PYTHON" -m lu_machinery.ai_agent_bridge inbox --for "$RECIPIENT" 2>/dev/null) || exit 0

# The legacy human listing emits `N unread`; the live channel listing emits a
# `pending: N` line. Accept both existing CLI shapes without creating a new
# output protocol for the hook.
COUNT=$(printf '%s\n' "$INBOX_OUTPUT" | sed -nE \
  -e 's/.*:[[:space:]]*([0-9]+)[[:space:]]+unread.*/\1/p' \
  -e 's/^[[:space:]]*pending:[[:space:]]*([0-9]+).*/\1/p' | head -n 1)

# Keep the pending IDs separate from previews: session-local dedupe state may
# never contain message content. The CLI's human listing places the state in
# the second bracket; `pending` covers the live channel listing if it exposes
# delivery IDs in the same row shape.
PENDING_IDS=$(printf '%s\n' "$INBOX_OUTPUT" | sed -nE \
  's/^[[:space:]]*\[([^]]+)\][[:space:]]*\[(unread|pending)\].*/\1/p')

case "$COUNT" in
  ''|*[!0-9]*)
    COUNT=$(printf '%s\n' "$PENDING_IDS" | sed '/^$/d' | wc -l | tr -d ' ')
    ;;
esac

if [ -z "$COUNT" ] || [ "$COUNT" -eq 0 ]; then
  exit 0
fi

# A structured count without IDs still deserves a notification. Use only the
# count as the body-free fallback dedupe key; human listings use their exact
# pending ID list.
if [ -z "$PENDING_IDS" ]; then
  PENDING_IDS="count:${COUNT}"
fi

# A UserPromptSubmit hook can fire repeatedly before another worker claims or
# acknowledges its message. Suppress only an identical recipient/session ID
# list. No session identity or unavailable state is deliberately fail-open.
SESSION_ID="${LEARN_UK_HOOK_SESSION_ID:-${LEARN_UKRAINIAN_SESSION_ID:-${CODEX_THREAD_ID:-${CODEX_SESSION_ID:-}}}}"
if [ -z "$SESSION_ID" ]; then
  SESSION_ID=$(printf '%s' "$HOOK_INPUT" | jq -r '.session_id // empty' 2>/dev/null)
fi
if [ -n "$SESSION_ID" ]; then
  STATE_DIR="$PROJECT_DIR/.agent/runtime"
  STATE_KEY=""
  if command -v shasum >/dev/null 2>&1; then
    STATE_KEY=$(printf '%s\n%s' "$RECIPIENT" "$SESSION_ID" | shasum -a 256 | awk '{print $1}')
  fi
  STATE_FILE="$STATE_DIR/inbox-${STATE_KEY}.ids"
  if [ -n "$STATE_KEY" ] && mkdir -p "$STATE_DIR" 2>/dev/null; then
    STATE_TMP=$(mktemp "$STATE_DIR/.inbox-${STATE_KEY}.XXXXXX" 2>/dev/null || true)
    if [ -n "$STATE_TMP" ]; then
      printf '%s\n' "$PENDING_IDS" > "$STATE_TMP"
      if [ -f "$STATE_FILE" ] && cmp -s "$STATE_TMP" "$STATE_FILE"; then
        rm -f "$STATE_TMP"
        exit 0
      fi
      if ! mv -f "$STATE_TMP" "$STATE_FILE" 2>/dev/null; then
        rm -f "$STATE_TMP"
      fi
    fi
  fi
fi

# Surface at most five rows from the CLI's own human listing. The following
# line is the CLI-provided preview, not a second read of message content.
PREVIEWS=$(printf '%s\n' "$INBOX_OUTPUT" | awk '
  /^[[:space:]]*\[[^]]+\][[:space:]]*\[(unread|pending)\]/ {
    if (shown >= 5) exit
    print
    if (getline > 0) print
    shown++
  }
')

# The live channel listing reports one bounded preview after this marker
# instead of exposing message IDs. Preserve that existing preview shape.
if [ -z "$PREVIEWS" ]; then
  PREVIEWS=$(printf '%s\n' "$INBOX_OUTPUT" | awk '
    /^[[:space:]]*oldest preview:[[:space:]]*$/ { capture = 1; next }
    capture && NF {
      print
      shown++
      if (shown >= 5) exit
    }
  ')
fi

CONTEXT="${RECIPIENT_LABEL} INBOX: ${COUNT} unread message(s) waiting.
---"

if [ -n "$PREVIEWS" ]; then
  CONTEXT="${CONTEXT}
${PREVIEWS}"
fi

INBOX_MODULE=lu_machinery.ai_agent_bridge
CONTEXT="${CONTEXT}
---
.venv/bin/python -m ${INBOX_MODULE} inbox --for ${RECIPIENT}"

jq -n --arg msg "$CONTEXT" \
  '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":$msg}}'

exit 0

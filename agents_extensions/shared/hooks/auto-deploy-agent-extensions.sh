#!/bin/bash
# Hook: FileChanged on agents_extensions/** — auto-rsync to .claude/, .codex/, .gemini/, .agents/
#
# Closes a real workflow gap: edits in agents_extensions/ are the canonical
# source, but Claude Code (and other agents) read from .claude/ etc. Without
# this hook every rule/skill/setting edit needs a manual `npm run agents:deploy`
# before it takes effect, and forgetting it = silent staleness.
#
# Safe to fire repeatedly: scripts/deploy_prompts.sh is idempotent rsync —
# unchanged files cost ~0, no-op output is "no changes to deploy."
#
# Encoded 2026-05-12 after user asked "you have to deploy from agents_extensions/shared ????"
# while watching me edit a rule file then manually `npm run agents:deploy`.

# Skip in non-interactive mode or during pipeline builds (would be wasteful)
if [ -n "$CLAUDE_NON_INTERACTIVE" ] || [ -n "$LEARN_UKRAINIAN_PIPELINE" ] || [ -n "$GEMINI_SESSION" ]; then
  exit 0
fi

# The project is configured (CLAUDE_PROJECT_DIR from the harness, else
# LU_PRODUCT_ROOT), never derived from this file's location: an installed
# release lives in site-packages, not in the project (machinery #100, R51).
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-${LU_PRODUCT_ROOT:-}}"
case "$PROJECT_DIR" in
  /*) [ -d "$PROJECT_DIR" ] || { echo "auto-deploy-agent-extensions.sh: project root $PROJECT_DIR is not a directory" >&2; exit 1; } ;;
  *) echo "auto-deploy-agent-extensions.sh: no project root: set CLAUDE_PROJECT_DIR or LU_PRODUCT_ROOT to an absolute checkout path" >&2; exit 1 ;;
esac
CHANGED_FILE="${1:-}"

if [ -z "$CHANGED_FILE" ]; then
  exit 0
fi

# Only fire for actual agents_extensions/ subtree changes
if [[ "$CHANGED_FILE" =~ ^agents_extensions/ ]] || [[ "$CHANGED_FILE" =~ /agents_extensions/ ]]; then
  # Run deploy quietly. If it has actual changes to ship, surface them; else
  # stay silent so this doesn't spam the agent context on every edit.
  DEPLOY_OUTPUT=$(cd "$PROJECT_DIR" && npm run agents:deploy --silent 2>&1 | grep -v '^$' | tail -10)

  if echo "$DEPLOY_OUTPUT" | grep -qE '(error|fail|warn|✗|❌)' ; then
    # Surface failures only — clean deploys stay silent.
    printf '{"additionalContext": %s}' "$(printf '%s' "AUTO-DEPLOY ($CHANGED_FILE): $DEPLOY_OUTPUT" | jq -Rs '.')"
  fi
fi

exit 0

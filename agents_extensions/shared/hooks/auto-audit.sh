#!/bin/bash
# Hook: FileChanged — auto-audit when curriculum .md files change
# Runs audit_module.py on the changed file to catch issues immediately.

# Skip in non-interactive mode or during pipeline builds (would be recursive)
if [ -n "$CLAUDE_NON_INTERACTIVE" ] || [ -n "$LEARN_UKRAINIAN_PIPELINE" ] || [ -n "$GEMINI_SESSION" ]; then
  exit 0
fi

# The project is configured (CLAUDE_PROJECT_DIR from the harness, else
# LU_PRODUCT_ROOT), never derived from this file's location: an installed
# release lives in site-packages, not in the project (machinery #100, R51).
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-${LU_PRODUCT_ROOT:-}}"
case "$PROJECT_DIR" in
  /*) [ -d "$PROJECT_DIR" ] || { echo "auto-audit.sh: project root $PROJECT_DIR is not a directory" >&2; exit 1; } ;;
  *) echo "auto-audit.sh: no project root: set CLAUDE_PROJECT_DIR or LU_PRODUCT_ROOT to an absolute checkout path" >&2; exit 1 ;;
esac

# The changed file path is passed as the first argument
CHANGED_FILE="${1:-}"

if [ -z "$CHANGED_FILE" ]; then
  exit 0
fi

# Only audit actual module content files (not orchestration, not plans)
# Pattern: curriculum/l2-uk-en/{level}/{slug}.md
if [[ "$CHANGED_FILE" =~ curriculum/l2-uk-en/[^/]+/[^/]+\.md$ ]] && [[ ! "$CHANGED_FILE" =~ /orchestration/ ]] && [[ ! "$CHANGED_FILE" =~ /audit/ ]] && [[ ! "$CHANGED_FILE" =~ /review/ ]]; then
  # The one project interpreter rule (R66, machinery #100 R74), never a worktree
  # .venv. The audit is the product's script, so no machinery release is needed.
  # shellcheck source=agents_extensions/shared/hooks/context-rollover-lib.sh
  source "$(dirname "${BASH_SOURCE[0]}")/context-rollover-lib.sh" \
    || { echo "auto-audit.sh: context-rollover-lib.sh is missing beside this hook" >&2; exit 1; }
  AUDIT_PYTHON="$(_context_rule_probe "$PROJECT_DIR")" || exit 1
  AUDIT_RESULT=$("$AUDIT_PYTHON" "$PROJECT_DIR/scripts/audit_module.py" "$CHANGED_FILE" 2>&1 | tail -5)

  if [ -n "$AUDIT_RESULT" ]; then
    printf '{"additionalContext": %s}' "$(printf '%s' "AUTO-AUDIT ($CHANGED_FILE): $AUDIT_RESULT" | jq -Rs '.')"
  fi
fi

exit 0

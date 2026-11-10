#!/bin/bash
# Hook: PostToolUse / PostToolUseFailure — record proven pytest success for #M-7.

set -u

# The project is configured (CLAUDE_PROJECT_DIR from the harness, else
# LU_PRODUCT_ROOT), never derived from this file's location: an installed
# release lives in site-packages, not in the project (machinery #100, R51).
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-${LU_PRODUCT_ROOT:-}}"
case "$PROJECT_ROOT" in
  /*) [ -d "$PROJECT_ROOT" ] || { echo "stamp-pytest.sh: project root $PROJECT_ROOT is not a directory" >&2; exit 1; } ;;
  *) echo "stamp-pytest.sh: no project root: set CLAUDE_PROJECT_DIR or LU_PRODUCT_ROOT to an absolute checkout path" >&2; exit 1 ;;
esac
HELPER="$PROJECT_ROOT/.githooks/pytest_stamp.py"
[ -f "$HELPER" ] || exit 0

# The one project interpreter rule (R66, machinery #100 R74): LEARN_UKRAINIAN_PYTHON,
# else the primary checkout's .venv; never a worktree .venv and never python or
# python3 from PATH, which can belong to another checkout or lack pinned
# dependencies (#5134). The helper is the product's, so no machinery release is
# needed. Stamping is observational: no proven project interpreter means no
# marker, and the later pre-push guard remains fail-closed.
# shellcheck source=agents_extensions/shared/hooks/context-rollover-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/context-rollover-lib.sh" 2>/dev/null || exit 0
PYTHON="$(_context_rule_probe "$PROJECT_ROOT" 2>/dev/null)" || exit 0

exec "$PYTHON" "$HELPER"

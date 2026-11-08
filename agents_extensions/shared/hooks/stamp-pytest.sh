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
PYTHON="$PROJECT_ROOT/.venv/bin/python"
HELPER="$PROJECT_ROOT/.githooks/pytest_stamp.py"

if [ ! -x "$PYTHON" ]; then
  COMMON_DIR=$(git -C "$PROJECT_ROOT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || exit 0
  PRIMARY_ROOT="$(dirname "$COMMON_DIR")"
  PYTHON="$PRIMARY_ROOT/.venv/bin/python"
fi

# Do not fall back to python/python3 from PATH. Repository commands require the
# project venv, and a system interpreter can belong to another checkout or lack
# pinned dependencies (#5134). Stamping is observational: no proven project
# interpreter means no marker, and the later pre-push guard remains fail-closed.
[ -x "$PYTHON" ] || exit 0
[ -f "$HELPER" ] || exit 0

exec "$PYTHON" "$HELPER"

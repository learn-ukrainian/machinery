#!/bin/bash
# Hook: PostToolUse / PostToolUseFailure — record proven pytest success for #M-7.

set -u

# The installed hook-root rule selects the product, never this file's location.
# shellcheck source=agents_extensions/shared/hooks/context-rollover-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/context-rollover-lib.sh" \
  || { echo "stamp-pytest.sh: context-rollover-lib.sh is missing beside this hook" >&2; exit 1; }
context_machinery_bootstrap "${LU_PRODUCT_ROOT:-${CLAUDE_PROJECT_DIR:-}}" || exit 1

PROJECT_ROOT="$PROJECT_DIR"
HELPER="$PROJECT_ROOT/.githooks/pytest_stamp.py"
[ -f "$HELPER" ] || exit 0

exec "$CONTEXT_PROJECT_PYTHON" "$HELPER"

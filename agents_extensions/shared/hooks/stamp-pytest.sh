#!/bin/bash
# Hook: PostToolUse / PostToolUseFailure — record proven pytest success for #M-7.

set -u

# The installed hook-root rule selects the product, never this file's location.
# shellcheck source=agents_extensions/shared/hooks/context-rollover-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/context-rollover-lib.sh" \
  || { echo "stamp-pytest.sh: context-rollover-lib.sh is missing beside this hook" >&2; exit 1; }
context_machinery_bootstrap "${LU_PRODUCT_ROOT:-${CLAUDE_PROJECT_DIR:-}}" || exit 1

# The helper is the configured primary's (the validated shared-state anchor), never
# the product checkout's: a worktree's tracked copy is the worker's to rewrite, and
# this hook runs outside any provider sandbox. It stamps the payload's checkout.
HELPER="$LEARN_UKRAINIAN_PRIMARY_REPO_ROOT/.githooks/pytest_stamp.py"
[ -f "$HELPER" ] || exit 0

exec "$CONTEXT_PROJECT_PYTHON" "$HELPER"

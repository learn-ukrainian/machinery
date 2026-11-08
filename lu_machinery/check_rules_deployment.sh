#!/usr/bin/env bash
# Compatibility adapter for read-only release deployment verification.
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$(dirname "${BASH_SOURCE[0]}")/lib/project_interpreter.sh"
PROJECT_PYTHON="$(project_interpreter_resolve "$PROJECT_ROOT")"
exec "$PROJECT_PYTHON" -I -m lu_machinery.agent_deploy --project-root "$PROJECT_ROOT" --check "$@"

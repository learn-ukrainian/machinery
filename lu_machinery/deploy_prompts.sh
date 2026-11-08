#!/usr/bin/env bash
# Compatibility adapter. Product wrappers must live in their own checkout.
# The consumer is --project-root or else LU_PRODUCT_ROOT, the order the deployer
# applies; this file's location never selects it (an installed release lives in
# site-packages). Consumer wrappers pass their own checkout (machinery #100, R56).
set -euo pipefail
PROJECT_ROOT="${LU_PRODUCT_ROOT:-}"
previous=""
for arg in "$@"; do
  case "$arg" in
    --project-root=*) PROJECT_ROOT="${arg#--project-root=}" ;;
    *) [ "$previous" != "--project-root" ] || PROJECT_ROOT="$arg" ;;
  esac
  previous="$arg"
done
case "$PROJECT_ROOT" in
  /*) ;;
  *)
    echo "deploy_prompts.sh: no consumer is selected: pass --project-root /absolute/checkout or set LU_PRODUCT_ROOT" >&2
    exit 2
    ;;
esac
source "$(dirname "${BASH_SOURCE[0]}")/lib/project_interpreter.sh"
PROJECT_PYTHON="$(project_interpreter_resolve "$PROJECT_ROOT")"
exec "$PROJECT_PYTHON" -I -m lu_machinery.agent_deploy "$@"

#!/usr/bin/env bash
# fleet_comms_cold_start.sh — shared authority-aware fleet-comms cold-start helpers.
#
# Binding doctrine: agents_extensions/shared/rules/fleet-comms-coordination.md
# (also served at GET /api/rules?scope=task:fleet-comms or scope=full).
# Method playbook: drive-epic skill (#5632).
# Authority cutover approved by the operator in #6159 on 2026-08-01.
#
# Usage (from a start-*.sh after PROJECT_DIR is set):
#   # shellcheck source=scripts/lib/fleet_comms_cold_start.sh
#   source "$PROJECT_DIR/scripts/lib/fleet_comms_cold_start.sh"
#   export FLEET_COMMS_PLANE_MODE="$(fleet_comms_resolve_plane_mode)"
#   fleet_comms_print_banner_line
#   _clause="$(fleet_comms_cold_clause)"

# shellcheck disable=SC2034  # PROJECT_DIR is provided by the sourcing launcher

# The machinery package's name (its import name, also its directory name in
# checkout paths), from this file's unresolved location (design-v1 amendment 1).
_fleet_comms_lib_dir="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)" || _fleet_comms_lib_dir=''
FLEET_COMMS_MACHINERY_PACKAGE="${_fleet_comms_lib_dir%/*}"
FLEET_COMMS_MACHINERY_PACKAGE="${FLEET_COMMS_MACHINERY_PACKAGE##*/}"
# This tree's package runs through the shared module bootstrap (#100 R68).
# shellcheck source=scripts/lib/project_interpreter.sh
[ -z "$_fleet_comms_lib_dir" ] || source "$_fleet_comms_lib_dir/project_interpreter.sh"
unset _fleet_comms_lib_dir

# Select ACP for eligible `ab discuss` calls without starting any process.
export LU_AGENT_COMM_TRANSPORT="${LU_AGENT_COMM_TRANSPORT:-acp}"

fleet_comms_rule_relpath() {
  printf '%s' "agents_extensions/shared/rules/fleet-comms-coordination.md"
}

# Resolve the one project interpreter (R66): never a worktree .venv.
fleet_comms_resolve_python() {
  local root="${1:-${PROJECT_DIR:-${LC_ROOT:-.}}}"
  declare -F project_interpreter_resolve >/dev/null || return 1
  project_interpreter_resolve "$root" 2>/dev/null
}

# Resolve plane mode once (fail-open → off).
fleet_comms_resolve_plane_mode() {
  local mode="off"
  local root="${PROJECT_DIR:-${LC_ROOT:-.}}"
  local py=""
  if [ -f "$PROJECT_CODE_ROOT/$FLEET_COMMS_MACHINERY_PACKAGE/fleet_comms/message_plane.py" ] \
      && py="$(fleet_comms_resolve_python "$root")"; then
    mode="$(
      (
        cd "$root" || exit 0
        project_python_run "$py" -c \
          'import importlib, sys; print(importlib.import_module(sys.argv[1]).resolve_plane_mode(None))' \
          "$FLEET_COMMS_MACHINERY_PACKAGE.fleet_comms.message_plane" 2>/dev/null || true
      )
    )"
    case "${mode}" in
      off|shadow|dual_write|authority) ;;
      *) mode="off" ;;
    esac
  fi
  printf '%s' "$mode"
}

# Compact authority-aware clause for injected cold-start prompts. The optional
# argument names the seat's entry skill: drive-epic for lane drivers, cto for
# the CTO seat (#12496).
fleet_comms_cold_clause() {
  local skill="${1:-drive-epic}"
  local plane_mode="${FLEET_COMMS_PLANE_MODE:-}"
  if [ -z "$plane_mode" ]; then
    plane_mode="$(fleet_comms_resolve_plane_mode)"
  fi
  local rule
  rule="$(fleet_comms_rule_relpath)"
  printf '%s' \
    "Fleet-comms (#6159) authority cutover — obey ${rule} (also in /api/rules?scope=task:fleet-comms or scope=full). " \
    "Method playbook: load skill ${skill} (provider drivers inject the binding after lease + canary). " \
    "Use fleet-comms as communication authority when current mode=${plane_mode}; " \
    "legacy bridge/channel stores are read-only migration projections in authority mode. " \
    "Topology: \`.venv/bin/python -m ${FLEET_COMMS_MACHINERY_PACKAGE}.fleet_comms plane-status\` (+ metrics/backlog/dead-letters). " \
    "Landing: PRs may open so CI runs; enqueue only after independent exact-head cross-family APPROVE and same-head green CI Gate. " \
    "Then the worker that owns the brief (or the driver, when no brief owns the PR) enqueues through the merge queue (never --auto, never ask the operator to merge), confirms MERGED and runs closeout. " \
    "Cross-family CF: direct ask-<lane> for verdict+findings (sealed review-pr / lu-review temps RETIRED — do not use). Never self-seal. " \
    "All normal inter-agent asks and discussions of 2 to 4 enabled seats use ACP; other seat counts reject loudly. " \
    "never fall back to bridge/provider execution. ACP transports; fleet-comms owns durable state. " \
    "Closeout: use the task-prescribed interpreter with \`-m ${FLEET_COMMS_MACHINERY_PACKAGE}.orchestration.merge_closeout <N> --repo <owner/name> --repo-root <checkout> --apply\`; the cleanup proof is that command exiting 0 after the worker's task has settled (its worktree removed by the dispatcher). " \
    "Continuity: stream lease already claimed; write durable receipts to fleet-comms."
}

# One-line operator banner (stdout).
fleet_comms_print_banner_line() {
  local plane_mode="${FLEET_COMMS_PLANE_MODE:-}"
  if [ -z "$plane_mode" ]; then
    plane_mode="$(fleet_comms_resolve_plane_mode)"
  fi
  case "$plane_mode" in
    off)
      echo "  fleet-comms: plane=off · diary authoritative · CF via direct ask · rule=$(fleet_comms_rule_relpath) · skill=drive-epic"
      ;;
    shadow|dual_write)
      echo "  fleet-comms: plane=${plane_mode} (compatibility soak) · rule=$(fleet_comms_rule_relpath) · skill=drive-epic"
      ;;
    authority)
      echo "  fleet-comms: plane=authority · ACP transport · legacy read-only · rule=$(fleet_comms_rule_relpath) · skill=drive-epic"
      ;;
    *)
      echo "  fleet-comms: plane=${plane_mode} (unknown→treat off) · rule=$(fleet_comms_rule_relpath)"
      ;;
  esac
}

# Prefer the production Monitor on loopback (Mac tunnel client). If it is down,
# warn and continue on the Mac — never start a second Monitor, never lift the
# retired sqlite.
fleet_comms_warn_if_plane_unreachable() {
  local root="${PROJECT_DIR:-${LC_ROOT:-.}}"
  local py=""
  if [ "${LU_SKIP_PLANE_TUNNEL_CHECK:-}" = "1" ]; then
    return 0
  fi
  if ! py="$(fleet_comms_resolve_python "$root")"; then
    echo "⚠️  plane probe skipped: no project interpreter; continuing without plane status." >&2
    return 0
  fi
  (
    cd "$root" || exit 0
    project_python_run "$py" -m "$FLEET_COMMS_MACHINERY_PACKAGE.orchestration.plane_tunnel_gate"
  ) || true
}

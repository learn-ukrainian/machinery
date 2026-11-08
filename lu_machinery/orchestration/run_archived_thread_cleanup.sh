#!/bin/bash
# launchd binds LWCR to ProgramArguments[0]. That must stay /bin/bash
# (Apple-signed). Never put .venv/bin/python there: a venv rebuild
# replaces the binary and launchd then fails with
# "Unable to get updated LWCR ... error 0x3 - No such process" (exit 78).
set -euo pipefail

primary=""
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  if [[ "${args[$i]}" == "--repo-root" ]]; then
    primary="${args[$((i + 1))]:-}"
    break
  fi
done

if [[ -z "${primary}" ]]; then
  echo "archived-thread-cleanup: missing --repo-root (primary checkout)" >&2
  exit 78
fi

script_dir="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "${script_dir}/../lib/project_interpreter.sh"
module="${script_dir}/archived_thread_cleanup.py"
if [[ ! -f "${module}" ]]; then
  echo "archived-thread-cleanup: missing module: ${module}" >&2
  exit 78
fi
if ! python="$(project_interpreter_resolve "${primary}")"; then
  echo "archived-thread-cleanup: missing interpreter" >&2
  exit 78
fi

exec "${python}" -m lu_machinery.orchestration.archived_thread_cleanup "$@"

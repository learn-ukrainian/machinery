#!/usr/bin/env bash
# Read-only AGY PreInvocation announcement. Never persist stdin or probe output.
fleet_module=lu_machinery.fleet_comms
fallback='{"injectSteps":[{"ephemeralMessage":"PENDING ORDERS: unknown; legacy: ai_agent_bridge inbox for the registered handoff slot; authority: project interpreter -m '"$fleet_module"' deliveries --help"}]}'
# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/../lib/project_interpreter.sh" || { cat >/dev/null; printf '%s\n' "$fallback"; exit 0; }
root="$(project_product_root_resolve)" || { cat >/dev/null; printf '%s\n' '{}'; exit 0; }
py="$(project_interpreter_resolve "$root")" || { cat >/dev/null; printf '%s\n' "$fallback"; exit 0; }
# The child drains stdin; on early import/exec failure the parent drains the rest.
project_python_command "$py" || { cat >/dev/null; printf '%s\n' "$fallback"; exit 0; }
if output=$(cd "$root" && "${PROJECT_PYTHON_COMMAND[@]}" -m lu_machinery.inbox_pending --agy-hook 2>/dev/null); then
    child_ok=1
else
    child_ok=0
fi
cat >/dev/null
if [ "$child_ok" = 1 ] && validated=$(printf '%s' "$output" | "$py" -I -c '
import json, sys
value = json.load(sys.stdin)
if value != {}:
    assert set(value) == {"injectSteps"}
    steps = value["injectSteps"]
    assert isinstance(steps, list) and len(steps) == 1
    assert set(steps[0]) == {"ephemeralMessage"}
    line = steps[0]["ephemeralMessage"]
    assert isinstance(line, str) and line.startswith("PENDING ORDERS: ")
    assert len(line) <= 200 and line.isascii() and all(32 <= ord(c) < 127 for c in line)
print(json.dumps(value, separators=(",", ":")))
' 2>/dev/null); then
    printf '%s\n' "$validated"
else
    printf '%s\n' "$fallback"
fi
exit 0

#!/usr/bin/env python3
"""Translate a Grok PreToolUse event for the existing fleet guard scripts.

Grok sends camelCase tool fields and native tool names. The tracked guards
consume Claude-shaped fields. This bridge preserves the original payload and
adds the shape the guards require; an unreadable event or missing guard denies.
Reviewer sessions and write sessions both enter through this bridge.

The guard argument is exactly one tracked guard path in the release's shared
hooks directory. Python guards execute with this process's interpreter and -I;
shell guards execute directly. The configured product root plays no part.
Other paths, interpreters and extra arguments deny.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

# Claude matchers auto-expand to some Grok ids (Bash → run_terminal_command,
# Write|Edit|MultiEdit → search_replace). Write sessions also name the
# model-facing ids that expansion does not cover. Unknown ids still deny.
_TOOL_NAMES = {
    "run_terminal_command": "Bash",
    "run_terminal_cmd": "Bash",
    "Bash": "Bash",
    "search_replace": "Edit",
    "hashline_edit": "Edit",
    "write": "Write",
    "Write": "Write",
    "Edit": "Edit",
    "MultiEdit": "MultiEdit",
}


# The deployed wrapper is translated away by the adapter; it is never a guard.
_PROJECT_PYTHON_HOOK_WRAPPER = "run-project-python-hook.sh"


class GuardInvocationError(ValueError):
    """The guard argument is not exactly one tracked guard path."""


def guard_argv(command: str) -> list[str]:
    """Isolate one tracked Python guard; keep shell guards as direct invocations."""
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise GuardInvocationError("unreadable fleet guard invocation") from exc
    from lu_machinery.common.repo_root import machinery_path

    hooks_dir = machinery_path("agents_extensions/shared/hooks")

    def tracked(guard: Path) -> bool:
        return guard == hooks_dir / guard.name

    if len(argv) != 1:
        raise GuardInvocationError("one tracked fleet guard path required")
    guard = Path(argv[0])
    if not tracked(guard) or guard.name == _PROJECT_PYTHON_HOOK_WRAPPER:
        raise GuardInvocationError("only tracked fleet guards may run")
    if not guard.is_file():
        raise GuardInvocationError("fleet guard unavailable")
    return [sys.executable, "-I", str(guard)] if guard.suffix == ".py" else argv


def main() -> int:
    try:
        if len(sys.argv) != 2:
            raise ValueError("one fleet guard path required")
        argv = guard_argv(sys.argv[1])
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict) or payload.get("hook_event_name") != "PreToolUse":
            raise ValueError("invalid PreToolUse event")
        tool_name = _TOOL_NAMES.get(payload.get("toolName"))
        tool_input = payload.get("toolInput")
        if tool_name is None or not isinstance(tool_input, dict):
            raise ValueError("unknown reviewer tool payload")
        if tool_name == "Bash" and not isinstance(tool_input.get("command"), str):
            raise ValueError("shell command unavailable")
        translated = dict(payload)
        translated["tool_name"] = tool_name
        translated["tool_input"] = dict(tool_input)
        translated["tool_input"].setdefault("cwd", payload.get("cwd"))
        result = subprocess.run(argv, input=json.dumps(translated), text=True, check=False, timeout=10)
        return 0 if result.returncode == 0 else 2
    except (ImportError, OSError, ValueError, TypeError, subprocess.TimeoutExpired) as exc:
        detail = f": {exc}" if isinstance(exc, GuardInvocationError) else ""
        print(f"BLOCKED by grok reviewer hook bridge: {type(exc).__name__}{detail}.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

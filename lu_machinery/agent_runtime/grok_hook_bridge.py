#!/usr/bin/env python3
"""Translate a Grok PreToolUse event for the existing fleet guard scripts.

Grok sends camelCase tool fields and native tool names. The tracked guards
consume Claude-shaped fields. This bridge preserves the original payload and
adds the shape the guards require; an unreadable event or missing guard denies.
Reviewer sessions and write sessions both enter through this bridge.

The guard argument is either a tracked guard path, run directly, or the project
interpreter followed by one of the tracked guards that need it. A tracked guard
sits in the release's shared hooks directory or, while the Claude settings
builder still emits product paths, the configured product's. The Claude adapter emits that second form; any other path,
interpreter, guard or extra argument denies.
"""

from __future__ import annotations

import contextlib
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


# Must match ``PROJECT_PYTHON_GUARDS`` in ``adapters/claude.py``; a test pins the
# two sets together. The bridge does not import the adapter: it runs on every
# guarded tool call.
PROJECT_PYTHON_GUARDS = frozenset({"guard-pr-merge.py", "guard-admin-merge.py", "guard-branch-switch-in-main.py"})
# The deployed wrapper is translated away by the adapter; it is never a guard.
_PROJECT_PYTHON_HOOK_WRAPPER = "run-project-python-hook.sh"


class GuardInvocationError(ValueError):
    """The guard argument is neither a guard path nor a pinned invocation."""


def guard_argv(command: str) -> list[str]:
    """Return the argv for one guard path or one pinned interpreter invocation.

    The argument is split into shell words first, so quoting and escaping never
    change how it is classified; an unreadable argument denies.
    """
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise GuardInvocationError("unreadable fleet guard invocation") from exc
    try:
        from lu_machinery.common.repo_root import ProductRootError, machinery_path, product_root
    except ModuleNotFoundError:
        # File launch from a checkout: the package is not installed on this interpreter.
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from lu_machinery.common.repo_root import ProductRootError, machinery_path, product_root
    # Release guards always qualify. Product-tree guards qualify only while the Claude
    # settings builder still emits them; without a configured product, only the release does.
    hooks_dirs = [machinery_path("agents_extensions/shared/hooks")]
    with contextlib.suppress(ProductRootError):
        hooks_dirs.append(product_root() / "agents_extensions/shared/hooks")

    def tracked(guard: Path) -> bool:
        return any(guard == hooks_dir / guard.name for hooks_dir in hooks_dirs)

    if len(argv) == 1:
        guard = Path(argv[0])
        if not tracked(guard) or guard.name == _PROJECT_PYTHON_HOOK_WRAPPER:
            raise GuardInvocationError("only tracked fleet guards may run")
        if not guard.is_file():
            raise GuardInvocationError("fleet guard unavailable")
        return argv
    if len(argv) != 2:
        raise GuardInvocationError("one fleet guard path or pinned interpreter invocation required")
    python_bin, guard = (Path(arg) for arg in argv)
    if guard.name not in PROJECT_PYTHON_GUARDS or not tracked(guard):
        raise GuardInvocationError("only tracked parser guards may run under the project interpreter")
    if not guard.is_file():
        raise GuardInvocationError("fleet guard unavailable")
    if python_bin != Path(sys.executable):
        raise GuardInvocationError("fleet guard interpreter is not this process's interpreter")
    return argv


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

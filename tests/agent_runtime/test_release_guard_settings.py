"""Fleet guards must not be selected from a consumer's product tree."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

from lu_machinery.agent_runtime.adapters import claude
from lu_machinery.agent_runtime.grok_hook_bridge import GuardInvocationError, guard_argv


def test_product_tree_cannot_supply_fleet_guard_executables(tmp_path, monkeypatch):
    """A consumer without fleet sources still gets the release's mandatory guards."""
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(tmp_path))
    settings = tmp_path / "agents_extensions/shared/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [{
        "matcher": "Bash",
        "hooks": [
            {"type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/enforce-venv.sh"},
            {"type": "command", "command": "bash $CLAUDE_PROJECT_DIR/.claude/hooks/run-project-python-hook.sh guard-pr-merge.py"},
        ],
    }]}}))
    result = json.loads(claude._worker_guard_settings(publish_guard=True))
    # The package and shared resource directory are siblings in both source and wheel.
    release = Path(claude.__file__).absolute().parents[3]
    hooks = release / "agents_extensions/shared/hooks"
    assert result == {"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [
            {"type": "command", "command": shlex.quote(str(hooks / "enforce-venv.sh"))},
            {"type": "command", "command": shlex.join([sys.executable, str(hooks / "guard-pr-merge.py")])},
        ]},
        {"matcher": "Bash", "hooks": [{
            "type": "command", "command": shlex.quote(str(hooks / "guard-reviewer-publish.py")),
            "timeout": 10, "onFailure": "block",
        }]},
    ]}}
    for group in result["hooks"]["PreToolUse"]:
        for hook in group["hooks"]:
            script = Path(shlex.split(hook["command"])[-1])
            assert script.is_file()
            assert script.is_relative_to(release)
            assert not script.is_relative_to(tmp_path)
            assert guard_argv(hook["command"]) == shlex.split(hook["command"])
    # The bridge must keep refusing product scripts, extra arguments, and unpinned Python.
    product_guard = settings.parent / "hooks/enforce-venv.sh"
    product_guard.parent.mkdir()
    product_guard.write_text("#!/bin/sh\nexit 0\n")
    for command in (
        shlex.quote(str(product_guard)),
        shlex.join([str(hooks / "enforce-venv.sh"), "extra"]),
        shlex.join(["/untrusted/python", str(hooks / "guard-pr-merge.py")]),
    ):
        with pytest.raises(GuardInvocationError):
            guard_argv(command)


def test_missing_product_settings_refuses_without_filesystem_traceback(tmp_path, monkeypatch):
    """A missing consumer setting must refuse explicitly instead of FileNotFoundError."""
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(tmp_path))
    with pytest.raises(RuntimeError, match=r"^Claude worker product settings unavailable: agents_extensions/shared/settings\.json$"):
        claude._worker_guard_settings()

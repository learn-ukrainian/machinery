"""The Grok hook bridge must run fleet guards from the release, never a consumer's product tree."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest

from lu_machinery.agent_runtime import grok_hook_bridge
from lu_machinery.agent_runtime.grok_hook_bridge import GuardInvocationError, guard_argv


def test_bridge_accepts_release_guards_and_refuses_product_scripts(tmp_path, monkeypatch):
    """Split roots: release guard commands pass; product scripts, extra arguments and other interpreters deny."""
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(tmp_path))
    # The package and shared resource directory are siblings in both source and wheel.
    release = Path(grok_hook_bridge.__file__).absolute().parents[2]
    hooks = release / "agents_extensions/shared/hooks"
    # The plain, parser-guard and reviewer-publish forms the Claude settings builder emits.
    for command in (
        shlex.quote(str(hooks / "enforce-venv.sh")),
        shlex.join([sys.executable, str(hooks / "guard-pr-merge.py")]),
        shlex.quote(str(hooks / "guard-reviewer-publish.py")),
    ):
        argv = guard_argv(command)
        assert argv == shlex.split(command)
        script = Path(argv[-1])
        assert script.is_file()
        assert script.is_relative_to(release)
        assert not script.is_relative_to(tmp_path)
    # The bridge must keep refusing product scripts, extra arguments, and unpinned Python.
    product_guard = tmp_path / "agents_extensions/shared/hooks/enforce-venv.sh"
    product_guard.parent.mkdir(parents=True)
    product_guard.write_text("#!/bin/sh\nexit 0\n")
    for command in (
        shlex.quote(str(product_guard)),
        shlex.join([str(hooks / "enforce-venv.sh"), "extra"]),
        shlex.join(["/untrusted/python", str(hooks / "guard-pr-merge.py")]),
        shlex.join([sys.executable, str(hooks / "enforce-venv.sh")]),
        shlex.quote(str(hooks / "run-project-python-hook.sh")),
    ):
        with pytest.raises(GuardInvocationError):
            guard_argv(command)

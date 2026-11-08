"""The Grok hook bridge runs only the release's tracked fleet guards, never the configured product's."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest

from lu_machinery.agent_runtime import grok_hook_bridge
from lu_machinery.agent_runtime.grok_hook_bridge import GuardInvocationError, guard_argv

# The package and shared resource directory are siblings in both source and wheel.
RELEASE = Path(grok_hook_bridge.__file__).absolute().parents[2]
RELEASE_HOOKS = RELEASE / "agents_extensions/shared/hooks"


def _hooks_tree(root: Path) -> Path:
    hooks = root / "agents_extensions/shared/hooks"
    hooks.mkdir(parents=True)
    for name in ("enforce-venv.sh", "guard-pr-merge.py", "run-project-python-hook.sh"):
        (hooks / name).write_text("#!/bin/sh\nexit 0\n")
    return hooks


def test_bridge_accepts_release_guards_and_refuses_the_rest(tmp_path, monkeypatch):
    """Split roots: release guards pass; product-tree guards, extra arguments and other interpreters deny."""
    product_hooks = _hooks_tree(tmp_path / "product")
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(tmp_path / "product"))
    # The plain, parser-guard and reviewer-publish forms the release settings builder emits.
    for command in (
        shlex.quote(str(RELEASE_HOOKS / "enforce-venv.sh")),
        shlex.join([sys.executable, str(RELEASE_HOOKS / "guard-pr-merge.py")]),
        shlex.quote(str(RELEASE_HOOKS / "guard-reviewer-publish.py")),
    ):
        assert guard_argv(command) == shlex.split(command)
    for command in (
        shlex.join([str(RELEASE_HOOKS / "enforce-venv.sh"), "extra"]),
        shlex.join(["/untrusted/python", str(RELEASE_HOOKS / "guard-pr-merge.py")]),
        shlex.join([sys.executable, str(RELEASE_HOOKS / "enforce-venv.sh")]),
        shlex.quote(str(RELEASE_HOOKS / "run-project-python-hook.sh")),
    ):
        with pytest.raises(GuardInvocationError):
            guard_argv(command)
    for command in (
        shlex.quote(str(product_hooks / "enforce-venv.sh")),
        shlex.join([sys.executable, str(product_hooks / "guard-pr-merge.py")]),
    ):
        with pytest.raises(GuardInvocationError, match="only tracked"):
            guard_argv(command)


def test_bridge_without_a_product_runs_release_guards_only(tmp_path, monkeypatch):
    """No configured product: release guards still run, a product-shaped tree does not."""
    stray_hooks = _hooks_tree(tmp_path)
    monkeypatch.delenv("LU_PRODUCT_ROOT", raising=False)
    release_guard = shlex.quote(str(RELEASE_HOOKS / "enforce-venv.sh"))
    assert guard_argv(release_guard) == shlex.split(release_guard)
    with pytest.raises(GuardInvocationError, match="only tracked fleet guards may run"):
        guard_argv(shlex.quote(str(stray_hooks / "enforce-venv.sh")))

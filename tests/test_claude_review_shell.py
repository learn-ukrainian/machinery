"""Root check of the Claude review shell prefix (#98).

The prefix imports the read-only boundary only from a git checkout or from the installed
distribution whose RECORD lists it; a loose copy anywhere else is refused.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import lu_machinery

PREFIX = Path(lu_machinery.__file__).resolve().parent / "dispatcher" / "claude_review_shell"
REFUSED = "claude read-only boundary refused: claude_review_shell_root_not_checkout\n"


def _layout(root: Path, kind: str) -> Path:
    prefix = root / "lu_machinery" / "dispatcher" / "claude_review_shell"
    prefix.parent.mkdir(parents=True)
    shutil.copy2(PREFIX, prefix)
    if kind == "checkout":
        (root / ".git").mkdir()
    elif kind == "installed":
        record = root / "lu_machinery-0.1.0.dist-info" / "RECORD"
        record.parent.mkdir()
        record.write_text("lu_machinery/__init__.py,,\nlu_machinery/dispatcher/claude_review_shell,sha256=x,1\n")
    elif kind == "foreign-record":
        record = root / "lu_machinery-0.1.0.dist-info" / "RECORD"
        record.parent.mkdir()
        record.write_text("lu_machinery/__init__.py,,\n")
    return prefix


@pytest.mark.parametrize("kind,binds", [("checkout", True), ("installed", True), ("loose", False),
                                        ("foreign-record", False)])
def test_prefix_binds_only_checkout_or_recorded_install(tmp_path, kind, binds):
    """Defect: the prefix imports boundary code from a loose copy, or refuses its own installed distribution."""
    root = tmp_path / "root"
    prefix = _layout(root, kind)
    calls = tmp_path / "calls"
    interpreter = tmp_path / "python"
    interpreter.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {calls}\n')
    interpreter.chmod(0o700)
    reviewed = tmp_path / "reviewed"
    reviewed.mkdir()
    env = {"PATH": "/usr/bin:/bin", "LU_REVIEW_SHELL_PYTHON": str(interpreter),
           "LU_REVIEW_SHELL_WORKTREE": str(reviewed)}
    run = subprocess.run([str(prefix), "true"], env=env, capture_output=True, text=True, timeout=30, check=False)
    if not binds:
        assert (run.returncode, run.stderr) == (125, REFUSED)
        assert not calls.exists()
        return
    assert (run.returncode, run.stderr) == (0, "")
    args = calls.read_text().splitlines()
    assert args[0] == "-I"
    assert args[3:] == [str(root.resolve()), "lu_machinery", "true"]

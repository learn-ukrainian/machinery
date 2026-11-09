"""Root check of the Claude review shell prefix (#98).

The prefix imports the read-only boundary only from a git checkout or from the installed
distribution whose RECORD lists it; a loose copy anywhere else is refused.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import lu_machinery

PREFIX = Path(lu_machinery.__file__).resolve().parent / "dispatcher" / "claude_review_shell"
REFUSED = "claude read-only boundary refused: claude_review_shell_root_not_checkout\n"


def _layout(root: Path, kind: str) -> Path:
    prefix = root / "lu_machinery" / "dispatcher" / "claude_review_shell"
    prefix.parent.mkdir(parents=True)
    shutil.copy2(PREFIX, prefix)
    library = root / "lu_machinery" / "lib" / "project_interpreter.sh"
    library.parent.mkdir(parents=True)
    shutil.copy2(Path(lu_machinery.__file__).resolve().parent / "lib" / "project_interpreter.sh", library)
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
    env = {"PATH": "/usr/bin:/bin", "LEARN_UKRAINIAN_PYTHON": str(interpreter),
           "LU_REVIEW_SHELL_WORKTREE": str(reviewed)}
    run = subprocess.run([str(prefix), "true"], env=env, capture_output=True, text=True, timeout=30, check=False)
    if not binds:
        assert (run.returncode, run.stderr) == (125, REFUSED)
        assert not calls.exists()
        return
    assert (run.returncode, run.stderr) == (0, ""), run.stderr
    args = calls.read_text().splitlines()
    assert args[0] == "-I"
    assert "sys.path" not in calls.read_text()
    assert args[-1] == "true"


def test_review_shell_env_publishes_the_rule_interpreter(tmp_path, monkeypatch):
    """Defect: the review shell is handed a private interpreter variable instead of the one rule."""
    from lu_machinery.dispatcher.providers.claude import review_shell_env

    monkeypatch.setenv("LEARN_UKRAINIAN_PYTHON", sys.executable)
    spec = SimpleNamespace(task_id="review-shell", worktree=tmp_path, final_file=tmp_path / "out.json")
    env = review_shell_env(spec)
    assert env["LEARN_UKRAINIAN_PYTHON"] == sys.executable
    assert "LU_REVIEW_SHELL_PYTHON" not in env

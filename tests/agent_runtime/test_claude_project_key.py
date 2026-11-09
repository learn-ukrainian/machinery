"""Claude project-directory keys: token report matching and the post-compact memory hint."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lu_machinery.common.repo_root import machinery_path

ROOT = Path(__file__).resolve().parents[2]
HOOK = machinery_path("agents_extensions/shared/hooks/post-compact.sh")

def _key(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]", "-", path)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=30)


def _primary_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """A primary checkout and a linked worktree; the worktree has no venv."""
    primary = tmp_path / "learn-ukrainian"
    primary.mkdir()
    _git("init", "-q", "-b", "main", cwd=primary)
    _git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "init",
        cwd=primary,
    )
    worktree = primary / ".worktrees" / "dispatch" / "lane"
    _git("worktree", "add", "-q", "--detach", str(worktree), cwd=primary)
    return primary, worktree


def _run_hook(worktree: Path, **overrides: str) -> str:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("CODEX_", "CLAUDE_", "SESSION_", "GROK_", "GEMINI_", "THREAD_ROLLOVER_"))
        and key != "LEARN_UKRAINIAN_PIPELINE"
    }
    environment.update(CLAUDE_PROJECT_DIR=str(worktree), SESSION_HANDOFF_AGENT="claude", **overrides)
    result = subprocess.run(
        ["bash", str(HOOK)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["additionalContext"]


def _line(context: str, marker: str) -> str:
    return next(line for line in context.splitlines() if marker in line)


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required by the hook")
def test_post_compact_uses_the_primary_checkout_venv_with_production_defaults(tmp_path: Path) -> None:
    # No interpreter or rollover override: the shared venv exists only in
    # the primary checkout, exactly as for a real linked worktree.
    primary, worktree = _primary_with_worktree(tmp_path)
    venv_python = primary / ".venv/bin/python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(sys.executable)
    assert not (worktree / ".venv").exists()

    context = _run_hook(worktree)

    memory = _line(context, "MEMORY:")
    assert f"/{_key(str(primary.resolve()))}/memory/MEMORY.md" in memory
    assert _key(str(worktree)) not in memory
    # The rollover probe ran the installed ``lu_machinery.orchestration.thread_handoff``
    # (the hook's one module bootstrap, #161) through the shared interpreter against the
    # primary checkout, which holds no rollover packet.
    assert "Thread rollover health (read-only): {" in context
    health = json.loads(context.split("Thread rollover health (read-only): ", 1)[1].split("\n  - If a live packet", 1)[0])
    assert health == {"agent": "claude", "status": "none"}

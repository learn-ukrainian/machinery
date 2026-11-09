import os
import subprocess
import sys
from pathlib import Path

import pytest

import lu_machinery
from lu_machinery.common.repo_root import machinery_path
from lu_machinery.guardrails import worktree_containment as wc


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in wc._GIT_ENV_DENYLIST}

def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, env=_clean_env(),
        timeout=30,
    )

@pytest.fixture
def repo_layout(tmp_path: Path):
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(main)],
        check=True, capture_output=True, text=True, env=_clean_env(),
        timeout=30,
    )
    _git(main, "config", "user.email", "test@example.com")
    _git(main, "config", "user.name", "Test")

    # Tracked content + gitignore rules covering runtime/local state.
    (main / ".gitignore").write_text(".worktrees/\nbuild/\n*.log\nlocal_state/\n")
    (main / "tracked.txt").write_text("tracked\n")
    (main / "pkg").mkdir()
    (main / "pkg" / "module.py").write_text("x = 1\n")
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "init")

    # create some branches
    _git(main, "branch", "feature-1")
    _git(main, "branch", "feature-2")

    # add a dispatch worktree
    dispatch_wt = main / ".worktrees" / "dispatch" / "agy" / "task-1"
    _git(main, "worktree", "add", "-q", "-b", "agy/task-1", str(dispatch_wt))

    class Layout:
        def __init__(self):
            self.main = wc.canonicalize(main)
            self.dispatch_wt = wc.canonicalize(dispatch_wt)

    return Layout()

def run_git_shim(repo: Path, *args: str, agent_no_merge: bool = True, python_bin: str | None = None):
    shim_path = machinery_path("agent_runtime/shims/git")

    env = _clean_env()
    env["LU_PRODUCT_ROOT"] = str(wc.resolve_main_root(repo))
    if agent_no_merge:
        env["AGENT_NO_MERGE"] = "1"
    else:
        env.pop("AGENT_NO_MERGE", None)

    # The installed shim has no project venv beside it: name the interpreter running pytest
    # (``LEARN_UKRAINIAN_PYTHON`` is the one interpreter pin since #171).
    env["LEARN_UKRAINIAN_PYTHON"] = python_bin if python_bin is not None else sys.executable

    # To avoid the shim blocking push due to missing AGENT_REAL_GIT,
    # we can set AGENT_REAL_GIT or let it find it.

    return subprocess.run(
        [str(shim_path), *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=30,
    )

def test_git_shim_blocks_branch_switch_in_main(repo_layout):
    # Checking out a branch in main (protected) should be blocked
    proc = run_git_shim(repo_layout.main, "checkout", "feature-1")
    assert proc.returncode == 1
    assert "primary Git policy" in proc.stderr

    proc = run_git_shim(repo_layout.main, "switch", "feature-1")
    assert proc.returncode == 1

    proc = run_git_shim(repo_layout.main, "checkout", "-b", "new-branch")
    assert proc.returncode == 1

    proc = run_git_shim(repo_layout.main, "switch", "-c", "new-branch")
    assert proc.returncode == 1

def test_git_shim_allows_file_checkout(repo_layout):
    # Checking out a file in main should be allowed
    proc = run_git_shim(repo_layout.main, "checkout", "--", "tracked.txt")
    assert proc.returncode == 0

    proc = run_git_shim(repo_layout.main, "checkout", "feature-1", "--", "tracked.txt")
    assert proc.returncode == 1  # Only restoration from the primary index is a rescue form.

def test_git_shim_allows_branch_switch_in_worktree(repo_layout):
    # Checking out a branch in worktree should be allowed
    proc = run_git_shim(repo_layout.dispatch_wt, "checkout", "feature-2")
    assert proc.returncode == 0

def test_git_shim_allows_read_only(repo_layout):
    proc = run_git_shim(repo_layout.main, "status")
    assert proc.returncode == 0

    proc = run_git_shim(repo_layout.main, "log")
    assert proc.returncode == 0

def test_git_shim_enforces_without_caller_agent_flag(repo_layout):
    # Clearing a caller-controlled flag must not disable primary containment.
    proc = run_git_shim(repo_layout.main, "checkout", "feature-1", agent_no_merge=False)
    assert proc.returncode == 1


@pytest.mark.parametrize("verb,args", [
    ("checkout", ("feature-1",)),
    ("switch", ("feature-1",)),
    ("checkout", ("-b", "new-branch")),
    ("switch", ("-c", "new-branch")),
    ("checkout", ("--", "tracked.txt")),
])
def test_git_shim_fails_closed_when_guard_interpreter_absent(repo_layout, verb, args, tmp_path):
    nonexistent = str(tmp_path / "missing-python")
    proc = run_git_shim(repo_layout.main, verb, *args, python_bin=nonexistent)
    assert proc.returncode == 1
    assert "error: git shim guard interpreter unavailable" in proc.stderr
    assert "failing closed" in proc.stderr


def test_git_shim_fails_closed_on_fake_interpreter_stdout(repo_layout):
    # A dummy binary like /bin/true that exits 0 but does not output GUARD_ALLOW must fail closed
    proc = run_git_shim(repo_layout.main, "checkout", "feature-1", python_bin="/bin/true")
    assert proc.returncode == 1
    assert "failing closed" in proc.stderr


def test_git_shim_works_when_hosted_in_worktree_without_venv(repo_layout, tmp_path):
    # Simulate a linked worktree hosting the shim without its own .venv
    # It must find the main repo's .venv via git-common-dir and allow switching in dispatch worktree
    # Create mock main repo with .venv/bin/python
    mock_main = tmp_path / "mock_main"
    mock_main.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(mock_main)], check=True, env=_clean_env(), timeout=30)
    _git(mock_main, "config", "user.email", "test@example.com")
    _git(mock_main, "config", "user.name", "Test")
    (mock_main / "file.txt").write_text("ok\n")
    _git(mock_main, "add", "-A")
    _git(mock_main, "commit", "-q", "-m", "init")

    venv_python = mock_main / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    # Symlink project python
    venv_python.symlink_to(sys.executable)

    # Symlink the installed machinery package so mock_main has lu_machinery.guardrails
    (mock_main / "lu_machinery").symlink_to(Path(lu_machinery.__file__).resolve().parent)

    # Create linked worktree where the shim lives
    mock_wt = mock_main / ".worktrees" / "dispatch" / "test" / "shim_wt"
    _git(mock_main, "worktree", "add", "-q", "-b", "test/shim_wt", str(mock_wt))

    # The worktree hosts the shim (no .venv here): its ``lu_machinery`` is the installed package, so the
    # shim keeps its unresolved location under the worktree and finds its interpreter library beside it.
    (mock_wt / "lu_machinery").symlink_to(Path(lu_machinery.__file__).resolve().parent)
    wt_shim = mock_wt / "lu_machinery" / "agent_runtime" / "shims" / "git"

    # Add another dispatch worktree to switch in
    target_wt = mock_main / ".worktrees" / "dispatch" / "test" / "target_wt"
    _git(mock_main, "worktree", "add", "-q", "-b", "test/target_wt", str(target_wt))
    _git(mock_main, "branch", "branch-to-switch")

    # Run the worktree-hosted shim from target_wt
    env = _clean_env()
    env["AGENT_NO_MERGE"] = "1"
    env["LU_PRODUCT_ROOT"] = str(mock_main)
    env.pop("LEARN_UKRAINIAN_PYTHON", None)

    proc = subprocess.run(
        [str(wt_shim), "checkout", "branch-to-switch"],
        cwd=str(target_wt),
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0, f"Expected success, got stderr: {proc.stderr}"

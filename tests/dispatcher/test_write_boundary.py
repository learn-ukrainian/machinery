"""The write boundary (#423) against a real bubblewrap sandbox and a disposable primary with one linked worktree."""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from lu_machinery.dispatcher import write_boundary


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def primary(tmp_path, monkeypatch):
    repo = tmp_path / "primary"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "probe@example.invalid")
    _git(repo, "config", "user.name", "Probe")
    (repo / "a.txt").write_text("primary\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "init")
    worktree = repo / ".worktrees" / "task"
    _git(repo, "worktree", "add", "-q", str(worktree), "-b", "task")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LU_SCRATCH_ROOT", str(tmp_path / "scratch"))
    return repo, worktree, tmp_path / "receipt.json"


def _run(primary, script: str) -> tuple[int, dict]:
    _, worktree, receipt = primary
    code = write_boundary.main([
        "--task-id", "wb-test", "--worktree", str(worktree), "--scratch", str(write_boundary.scratch("wb-test")),
        "--receipt", str(receipt), "--", "/bin/sh", "-c", script,
    ])
    return code, json.loads(receipt.read_text())


def _digest(*paths: Path) -> dict:
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None for path in paths}


@pytest.mark.parametrize("failure", ["setup", "canary"])
def test_setup_or_canary_failure_refuses_and_never_starts_the_provider(primary, monkeypatch, failure):
    """Defect class: a boundary that cannot start, or whose canary finds a writable primary, still launches the
    provider (an unsandboxed fallback), so the run proceeds without containment."""
    repo, worktree, _ = primary
    if failure == "setup":
        monkeypatch.setattr(write_boundary, "unavailable", lambda: "bubblewrap (bwrap) is not installed")
    else:
        planned = write_boundary._Boundary._probes

        def leaky(self, *args):
            # The canary reports a writable primary file: the boundary does not hold.
            return [*planned(self, *args), {"op": "write_open", "path": str(repo / "a.txt"), "expect_any": ["OK"]}]

        monkeypatch.setattr(write_boundary._Boundary, "_probes", leaky)
    marker = worktree / "provider-started"
    code, receipt = _run(primary, f"touch {marker}")
    assert code == write_boundary.REFUSED
    assert receipt["status"] == "refused"
    assert ("canary failed" if failure == "canary" else "not installed") in receipt["reason"]
    assert not marker.exists()
    assert not write_boundary.scratch("wb-test").exists()


def test_primary_working_tree_writes_are_denied_while_the_worktree_commits(primary):
    """Defect class: a write worker changes or adds files in the primary working tree, through a shell redirect,
    a link from its worktree or an interpreter, while its own worktree commit must still reach the host."""
    repo, worktree, _ = primary
    before = _digest(repo / "a.txt")
    script = (
        f"echo x > {repo}/a.txt; touch {repo}/new.txt; ln -s {repo}/a.txt {worktree}/link && echo x > {worktree}/link;"
        f"python3 -c \"open('{repo}/a.txt', 'a').write('x')\";"
        f"echo change > {worktree}/a.txt && git -C {worktree} commit -qam change"
    )
    code, receipt = _run(primary, script)
    assert code == 0 and receipt["status"] == "ran" and receipt["canary"]["passed"]
    assert _digest(repo / "a.txt") == before
    assert not (repo / "new.txt").exists()
    assert receipt["import"]["status"] == "imported"
    assert _git(repo, "rev-parse", "task") == receipt["import"]["after"] != receipt["import"]["before"]
    assert _git(worktree, "status", "--porcelain", "--untracked-files=no") == ""


def test_protected_primary_git_state_cannot_be_changed(primary):
    """Defect class: a write worker changes the primary's HEAD, index, configuration, hooks or checked-out branch,
    directly or through Git porcelain, so later host-side Git commands in the primary run its code or state."""
    repo, worktree, _ = primary
    git_dir = repo / ".git"
    protected = [git_dir / "HEAD", git_dir / "index", git_dir / "config", git_dir / "refs/heads/main",
                 git_dir / "hooks/post-checkout"]
    before = _digest(*protected)
    script = (
        f"git -C {repo} config core.hooksPath /tmp/evil; git -C {repo} checkout -q -b evil;"
        f"git -C {repo} commit -q --allow-empty -m evil; echo 'ref: refs/heads/evil' > {git_dir}/HEAD;"
        f": > {git_dir}/index; printf '#!/bin/sh\\n' > {git_dir}/hooks/post-checkout;"
        f"git -C {repo} update-ref refs/heads/main HEAD; echo 0000 > {git_dir}/refs/heads/main;"
        f"echo 'gitdir: {git_dir}' > {worktree}/.git"
    )
    _, receipt = _run(primary, script)  # the provider's own exit status is not the subject; the host state is
    assert receipt["status"] == "ran" and receipt["canary"]["passed"], receipt
    assert _digest(*protected) == before
    assert _git(repo, "symbolic-ref", "HEAD") == "refs/heads/main"
    hooks_path = subprocess.run(["git", "-C", str(repo), "config", "--get", "core.hooksPath"], capture_output=True)
    assert hooks_path.returncode == 1
    assert "evil" not in _git(repo, "branch", "--list")
    assert (worktree / ".git").read_text().startswith(f"gitdir: {git_dir / 'worktrees'}")

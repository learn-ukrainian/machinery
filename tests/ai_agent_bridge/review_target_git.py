"""Git fixture helpers for the bridge's review-target tests.

Verbatim extraction (CTO-approved for PORT-1108 T4) of ``_git``, ``_init_repo`` and
``_publish_pr_branch`` from learn-ukrainian ``tests/test_review_target_resolution.py`` at
5aba2adb (package-3 codemod output: ``sanitized_git_env`` comes from ``lu_machinery``). That
test module's own port belongs to another tranche, so this tranche imports no test module it
does not own.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from lu_machinery.common.git_context import sanitized_git_env


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # sanitized_git_env() strips GIT_DIR/GIT_WORK_TREE/etc — without it, running
    # this suite from inside a `git commit` pre-commit hook leaks the OUTER
    # repo's git env into these calls and silently operates on the wrong repo.
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
        env=sanitized_git_env(),
        timeout=30,
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    # Non-protected initial branch name: this repo is an unrelated tmp fixture,
    # but the project's global git-checkout guard (scripts/agent_runtime/shims/git)
    # blocks `checkout`/`switch` off of any branch literally named main/master —
    # it doesn't check repo identity. Naming it "trunk" avoids tripping that guard.
    _git(repo, "init", "-q", "-b", "trunk")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "app.py").write_text("print('v1')\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _publish_pr_branch(repo: Path, branch: str, sha: str) -> None:
    """Serve a synthetic PR ref through real Git at the fixture's canonical URL."""
    remote = repo.parent / f"{repo.name}-canonical.git"
    origin = _git(repo, "config", "--get", "remote.origin.url").stdout.strip()
    if not remote.exists():
        # Both repositories live under the same fixture parent. Borrow its
        # objects, including any alternates, rather than copying full history.
        _git(repo, "clone", "-q", "--bare", "--shared", str(repo), str(remote))
        _git(repo, "config", f"url.{remote}.insteadOf", origin)
    _git(repo, "update-ref", f"refs/heads/{branch}", sha)
    _git(repo, "--git-dir", str(remote), "fetch", "-q", str(repo), f"refs/heads/{branch}:refs/heads/{branch}")

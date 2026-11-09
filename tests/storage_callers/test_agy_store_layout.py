"""Exercise AGY's real launch boundary with fixture stores and no provider calls."""

import json
import os
import sqlite3
import subprocess
import sys

import pytest

from lu_machinery.dispatcher import agy_sandbox


def _linked_worktree(tmp_path):
    def git(cwd, *args):
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()

    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("reviewed\n")
    (repo / "data").mkdir()
    for name in ("sources.db", "vesum.db"):
        connection = sqlite3.connect(repo / "data" / name)
        connection.execute("CREATE TABLE proof(value TEXT)")
        connection.commit()
        connection.close()
    git(repo, "add", "README.md")
    git(repo, "-c", "user.email=s@example.invalid", "-c", "user.name=S", "commit", "-q", "-m", "base")
    worktree = tmp_path / "worktree"
    git(repo, "worktree", "add", "-q", "--detach", str(worktree), "main")
    (worktree / "data").mkdir()
    for name in ("sources.db", "vesum.db"):
        assert not (worktree / "data" / name).exists()
    # Marker values, not credential shapes, so secret scanners stay quiet.
    git(repo, "config", "remote.origin.url", "/canary-remote-marker")
    git(repo, "config", "credential.helper", "canary-helper-marker")
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text("data/\n")
    return worktree, git(worktree, "rev-parse", "HEAD")


def _launch(tmp_path, worktree, task_id, command, **popen):
    argv = [
        sys.executable,
        "-I",
        "-c",
        agy_sandbox._BOOTSTRAP,
        str(agy_sandbox.machinery_path("dispatcher/agy_sandbox.py").parent.parent.parent),
        "--task-id",
        task_id,
        "--worktree",
        str(worktree),
        "--scratch",
        str(tmp_path / f"scratch-{task_id}"),
        "--receipt",
        str(tmp_path / f"{task_id}.sandbox.json"),
        "--",
        *command,
    ]
    app = tmp_path / "agy-home"
    app.mkdir(exist_ok=True)
    (app / "antigravity-oauth-token").write_text("token")
    env = {**os.environ, "AGY_APP_DATA_DIR": str(app)}
    return argv, env


@pytest.mark.parametrize("kind", ["symlink", "dangling", "file"])
def test_worktree_data_component_never_follows_branch_content(tmp_path, kind):
    worktree, _head = _linked_worktree(tmp_path)
    data = worktree / "data"
    data.rmdir()
    if kind == "file":
        data.write_text("not a directory")
    else:
        data.symlink_to(tmp_path / ("repo/data" if kind == "symlink" else "absent"))
    with pytest.raises(agy_sandbox.Refused, match="worktree_local_store"):
        agy_sandbox._trusted_database(worktree, tmp_path / "repo", "data/sources.db")


def test_disagreeing_primary_anchors_refuse_before_scratch_or_provider(tmp_path, monkeypatch):
    worktree, _head = _linked_worktree(tmp_path)
    monkeypatch.setattr(agy_sandbox, "main_checkout_root", lambda root: tmp_path)
    argv, _env = _launch(tmp_path, worktree, "anchor", ["/bin/true"])
    assert agy_sandbox.main(argv[argv.index("--task-id"):]) == 125
    receipt = json.loads((tmp_path / "anchor.sandbox.json").read_text())
    assert receipt["reason"] == "store_primary_disagreement"
    assert not (tmp_path / "scratch-anchor").exists()

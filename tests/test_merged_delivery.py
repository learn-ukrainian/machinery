"""Merge rescue of a write task whose remote branch was deleted after its pull request merged (machinery #118)."""

import json
import subprocess
from contextlib import contextmanager
from functools import partial
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lu_machinery.common import github_client
from lu_machinery.dispatcher import git, worker
from lu_machinery.orchestration import pr_ownership


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.mark.parametrize(("case", "tree"), [
    ("merged", "clean"), ("open", "clean"), ("merged-other-head", "clean"),
    ("merged", "untracked"), ("merged", "tracked"), ("merged", "staged"), ("merged", "late-commit"),
])
def test_merged_pr_proves_delivery_after_branch_and_pull_ref_are_gone(tmp_path, monkeypatch, case, tree):
    """Defect class: a squash-merged task whose branch closeout deleted, and whose refs/pull/N/head the remote
    no longer advertises, or whose tree held only untracked scratch, settled no_deliverable. An open PR, a
    merged PR whose head is not the task's head, a tracked or staged change left in the tree, or one committed
    after the exit snapshot still refuses."""
    remote, work = tmp_path / "remote.git", tmp_path / "work"
    _git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    _git(tmp_path, "clone", str(remote), str(work))
    for key, value in (("user.name", "t"), ("user.email", "t@example.invalid"), ("commit.gpgsign", "false")):
        _git(work, "config", key, value)
    _git(work, "commit", "--allow-empty", "-m", "base")
    base = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", "main")
    _git(work, "switch", "-c", "claude/rescue-probe")
    (work / "docs").mkdir()
    (work / "docs" / "owned.md").write_text("delivered\n")
    _git(work, "add", "docs/owned.md")
    _git(work, "commit", "-m", "deliver")
    head = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", "claude/rescue-probe")
    # The queue squash-merges the work as a new commit on main, then closeout deletes the task branch.
    _git(work, "switch", "main")
    _git(work, "merge", "--squash", "claude/rescue-probe")
    _git(work, "commit", "-m", "deliver (#7)")
    _git(work, "push", "origin", "main", ":claude/rescue-probe")
    # The task worktree stays on its branch, with the canonical origin recorded at dispatch.
    _git(work, "switch", "claude/rescue-probe")
    _git(work, "remote", "set-url", "origin", "https://github.com/org/repo.git")

    @contextmanager
    def private_remote(task_id, url):
        namespace = tmp_path / f"ns-{task_id}"
        namespace.mkdir()
        run = partial(git._verification_git, namespace, git_dir=namespace)
        run("init", "--bare", "--template=")
        run("config", "remote.origin.url", str(remote))
        yield run, namespace

    pr = {"state": "OPEN" if case == "open" else "MERGED", "baseRefName": "main",
          "mergedAt": "2026-11-08T12:50:00Z", "headRefOid": "b" * 40 if case == "merged-other-head" else head}

    def gh(args, **_):
        words = args[1:]
        out = (json.dumps([{"number": 7, "isCrossRepository": False, "headRefName": "claude/rescue-probe"}])
               if words[:2] == ["pr", "list"] else json.dumps(pr) if words[:2] == ["pr", "view"]
               else json.dumps("docs/owned.md"))
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr(git, "_private_remote_git", private_remote)
    monkeypatch.setattr(github_client, "run", gh)
    monkeypatch.setattr(pr_ownership, "state_path", lambda: tmp_path / "ownership.json")
    # The ownership walk fetches the deleted branch, as the recorded failures did.
    monkeypatch.setattr(git, "ownership_git", Mock(side_effect=subprocess.CalledProcessError(128, ["git", "fetch"])))
    assert not _git(remote, "for-each-ref", "refs/pull", "refs/heads/claude")
    if tree == "untracked":
        (work / ".pytest_breadcrumbs").mkdir()
        (work / ".pytest_breadcrumbs" / "breadcrumb_master.txt").write_text("scratch\n")
    elif tree in {"tracked", "staged", "late-commit"}:
        (work / "docs" / "owned.md").write_text("further work\n")
        if tree == "staged":
            _git(work, "add", "docs/owned.md")
    record = {
        "task_id": "rescue-probe", "mode": "workspace-write", "status": "done", "error": None,
        "owned_paths": ["docs/owned.md"], "worktree_remote_url": "https://github.com/org/repo.git",
        "worktree_base_ref": "origin/main", "worktree_branch": "claude/rescue-probe", "publish_branch": None,
        "started_at": "2026-11-08T12:30:00+00:00", "worktree_base_sha": base, "worktree_path": str(work),
    }
    record.update(git.snapshot_delivery(SimpleNamespace(worktree=work, mode="workspace-write"), record))
    assert (record["head_sha"], record["commits_ahead"], record["worktree_dirty_on_exit"]) == (head, 1, tree != "clean")
    if tree == "late-commit":
        # A surviving descendant commits after the exit snapshot: the gate reads the snapshot, not the tree.
        _git(work, "commit", "-am", "further work")
    worker._enforce_delivery(record)
    if case == "merged" and tree in {"clean", "untracked"}:
        assert (record["status"], record["error"], record["delivery_pr"]) == ("done", None, "org/repo#7")
    else:
        assert (record["status"], record["error"]) == (
            "no_deliverable", "owned_paths_verification_failed: git fetch failed")
        assert "delivery_pr" not in record

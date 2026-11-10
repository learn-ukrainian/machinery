"""Installed commit/push hooks reject unverifiable dispatched ownership (Refs #100)."""

from __future__ import annotations

import json
import subprocess

import pytest

from tests.githooks.test_owned_paths_hook import (
    _base_env,
    _git,
    _installed_python,
    _product_repo,
    _run,
    _task,
)


@pytest.fixture(scope="module")
def installed_python(tmp_path_factory):
    root = tmp_path_factory.mktemp("installed-ownership")
    return _installed_python(root)


@pytest.fixture
def hook_world(tmp_path, installed_python):
    env = {**_base_env(), "LEARN_UKRAINIAN_PYTHON": str(installed_python), "TMPDIR": str(tmp_path)}
    repo = _product_repo(tmp_path, env)
    env["LEARN_UKRAINIAN_PRIMARY_REPO_ROOT"] = str(repo)
    task_env = _task(repo, tmp_path / "tasks", env)
    record_path = tmp_path / "tasks" / "owned-paths-task.json"
    return repo, env, task_env, record_path


def _hook(world, stage, path="owned/change.txt", *, staged_guard=False):
    repo, env, task_env, _ = world
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("changed\n", encoding="utf-8")
    assert _git(repo, "add", path, env=env).returncode == 0
    if stage == "staged":
        if staged_guard:
            # The product fixture has no pre-commit dependency. Positive staged
            # cases exercise the exact real hook guard without its later runner.
            return _run(
                ["/usr/bin/bash", "-c", 'source "$1"; check_owned_paths staged',
                 "owned-path-stage", str(repo / ".githooks/_lib.sh")],
                cwd=repo, env=task_env,
            )
        return _run(["/usr/bin/bash", str(repo / ".git/hooks/pre-commit")], cwd=repo, env=task_env)
    # Construct the outgoing commit as fixture data, then run the installed
    # pre-push delegator with Git's update protocol and no network dependency.
    tree = _git(repo, "write-tree", env=env)
    assert tree.returncode == 0, tree.stderr
    commit = _git(repo, "commit-tree", tree.stdout.strip(), "-p", "HEAD", "-m", "ownership proof", env=env)
    assert commit.returncode == 0, commit.stderr
    head = commit.stdout.strip()
    return subprocess.run(
        ["/usr/bin/bash", str(repo / ".git/hooks/pre-push")], cwd=repo, env=task_env,
        input=f"refs/heads/topic {head} refs/heads/topic {'0' * 40}\n",
        capture_output=True, text=True, timeout=60,
    )


@pytest.mark.parametrize("stage", ["staged", "push"])
@pytest.mark.parametrize("case", [
    "missing", "damaged", "unreadable", "empty-object", "wrong-id", "wrong-mode",
    "missing-binding", "empty-binding", "relative-binding", "non-string-binding",
    "absent-binding", "file-binding", "loop-binding", "non-git-binding", "subdirectory-binding", "bad-scope", "bad-grants",
    "bad-grant-binding", "invalid-grant-path", "missing-grant-nonce",
    "invalid-read-only", "invalid-unrestricted", "invalid-different-tree", "git-failure", "empty-git-root",
])
def test_installed_hooks_refuse_unverifiable_ownership(hook_world, stage, case):
    repo, _, _, record_path = hook_world
    record = json.loads(record_path.read_text())
    if case == "empty-object":
        record = {}
    elif case == "wrong-id":
        record["task_id"] = "another-task"
    elif case == "wrong-mode":
        record["mode"] = "unknown"
    elif case == "missing-binding":
        del record["worktree_path"]
    elif case == "empty-binding":
        record["worktree_path"] = ""
    elif case == "relative-binding":
        record["worktree_path"] = "."
    elif case == "non-string-binding":
        record["worktree_path"] = []
    elif case == "absent-binding":
        record["worktree_path"] = str(repo / "absent")
    elif case == "file-binding":
        record["worktree_path"] = str(record_path)
    elif case == "loop-binding":
        loop = repo / "loop"
        loop.symlink_to(loop)
        record["worktree_path"] = str(loop)
    elif case in {"non-git-binding", "subdirectory-binding"}:
        other = repo.parent / "not-a-checkout" if case == "non-git-binding" else repo / "subdirectory"
        other.mkdir()
        record["worktree_path"] = str(other)
    elif case == "bad-scope":
        record["owned_paths"] = "owned"
    elif case == "bad-grants":
        record["owned_path_grants"] = "invalid"
    elif case == "bad-grant-binding":
        record["owned_path_grants"] = [{"task_id": "another-task"}]
    elif case in {"invalid-grant-path", "missing-grant-nonce"}:
        record["run_nonce"] = "run"
        record["owned_path_grants"] = [{
            "task_id": record["task_id"], "run_nonce": "run", "grant_id": "grant",
            "owned_paths": ["../outside" if case == "invalid-grant-path" else "extra.txt"],
        }]
        if case == "missing-grant-nonce":
            record.pop("run_nonce")
            record["owned_path_grants"][0].pop("run_nonce")
    elif case.startswith("invalid-"):
        record["owned_path_grants"] = "invalid"
        if case == "invalid-read-only":
            record["mode"] = "read-only"
        elif case == "invalid-unrestricted":
            record["owned_paths"] = []
        else:
            record["worktree_path"] = str(repo.parent)
    elif case == "git-failure" and stage == "push":
        record["worktree_base_ref"] = "missing-base"
    record_path.write_text(json.dumps(record), encoding="utf-8")
    if case == "missing":
        record_path.unlink()
    elif case == "damaged":
        record_path.write_text('{"secret": "DO-NOT-ECHO', encoding="utf-8")
    elif case == "unreadable":
        record_path.chmod(0)
    elif case == "empty-git-root" or (case == "git-failure" and stage == "staged"):
        shim = repo.parent / "git-bin"
        shim.mkdir()
        executable = shim / "git"
        failure = ('if [ "$1" = -C ]; then exit 0; fi' if case == "empty-git-root" else
                   'if [ "$1" = diff ]; then exit 23; fi')
        executable.write_text(
            '#!/bin/sh\n' + failure + '\nexec /usr/bin/git "$@"\n',
            encoding="utf-8",
        )
        executable.chmod(0o755)
        hook_world[2]["PATH"] = str(shim) + ":" + hook_world[2]["PATH"]
    try:
        result = _hook(hook_world, stage)
    finally:
        if case == "unreadable":
            record_path.chmod(0o600)
    assert result.returncode != 0, result.stdout
    assert "owned-path check could not run (" in result.stderr
    assert "); refusing" in result.stderr
    assert "DO-NOT-ECHO" not in result.stderr
    assert "owned-path check skipped" not in result.stderr


@pytest.mark.parametrize("stage", ["staged", "push"])
@pytest.mark.parametrize("case", ["allowed", "outside", "no-task", "different-tree", "read-only", "unrestricted", "legacy-unrestricted", "null-unrestricted", "granted"])
def test_installed_hooks_preserve_scope_and_only_valid_skips(hook_world, stage, case, tmp_path):
    _, _, task_env, record_path = hook_world
    record = json.loads(record_path.read_text())
    if case == "no-task":
        task_env.pop("LEARN_UKRAINIAN_DISPATCH_TASK_ID")
    elif case == "different-tree":
        other = tmp_path / "other"
        other.mkdir()
        assert _git(other, "init", "-q", "-b", "main", env=hook_world[1]).returncode == 0
        record["worktree_path"] = str(other)
        # Git's caller-local environment must not spoof the stored binding.
        task_env["GIT_DIR"] = str(hook_world[0] / ".git")
        task_env["GIT_WORK_TREE"] = str(hook_world[0])
    elif case == "read-only":
        record["mode"] = "read-only"
    elif case == "unrestricted":
        record["owned_paths"] = []
    elif case == "legacy-unrestricted":
        record.pop("owned_paths")
    elif case == "null-unrestricted":
        record["owned_paths"] = None
    elif case == "granted":
        record["run_nonce"] = "run"
        record["owned_path_grants"] = [{
            "task_id": record["task_id"], "run_nonce": "run", "grant_id": "grant",
            "owned_paths": ["outside.txt"],
        }]
    record_path.write_text(json.dumps(record), encoding="utf-8")
    result = _hook(
        hook_world, stage, "owned/change.txt" if case == "allowed" else "outside.txt",
        staged_guard=case != "outside",
    )
    assert (result.returncode == 0) == (case != "outside"), result.stderr
    if case == "outside":
        assert "outside.txt" in result.stderr and "owned-path check refused" in result.stderr


@pytest.mark.parametrize("failure", [PermissionError("DO-NOT-ECHO"), RuntimeError("DO-NOT-ECHO\nforged"), subprocess.TimeoutExpired(["git", "diff"], 30)])
def test_checker_refuses_unexpected_verification_errors_without_secrets(monkeypatch, capsys, tmp_path, failure):
    from lu_machinery.dispatcher import ownership

    monkeypatch.setenv(ownership.TASK_ID_ENV, "task")
    monkeypatch.setattr(ownership, "_task_record", lambda root: {"owned_paths": ["owned"]})

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(ownership, "_local_git", fail)
    assert ownership.main(["--worktree", str(tmp_path), "staged"]) == 1
    assert capsys.readouterr().err == (
        f"owned-path check could not run ({type(failure).__name__}: "
        "task record or ownership verification failed); refusing\n"
    )

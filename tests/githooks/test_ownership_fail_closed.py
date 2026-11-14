"""Installed commit/push hooks reject unverifiable dispatched ownership (Refs #100)."""

from __future__ import annotations

import errno
import json
import subprocess
from pathlib import Path

import pytest

from tests.githooks.test_owned_paths_hook import (
    _base_env,
    _git,
    _installed_python,
    _product_repo,
    _run,
    _task,
)
from tests.helpers.wheel import release_wheel


@pytest.fixture(scope="module")
def installed_python(tmp_path_factory):
    root = tmp_path_factory.mktemp("installed-ownership")
    wheel = release_wheel(Path(__file__).resolve().parents[2], root)
    return _installed_python(root, wheel)


@pytest.fixture
def hook_world(tmp_path, installed_python):
    env = {**_base_env(), "LEARN_UKRAINIAN_PYTHON": str(installed_python), "TMPDIR": str(tmp_path)}
    repo = _product_repo(tmp_path, env, installed_python)
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


def test_ownership_view_keeps_only_the_fields_the_check_reads():
    """Defect class: the projected view copies the brief, a grant reason or another task's record."""
    from lu_machinery.dispatcher import ownership

    view = ownership.ownership_view({
        "task_id": "wb-test",
        "mode": "workspace-write",
        "owned_paths": ["owned"],
        "run_nonce": "run",
        "worktree_path": "/work",
        "prompt_file": "SECRET-DO-NOT-LEAK",
        "owned_path_grants": [{
            "task_id": "wb-test",
            "run_nonce": "run",
            "grant_id": "grant",
            "owned_paths": ["extra.txt"],
            "reason": "operator words",
            "approval_ref": "message-1",
        }],
    })
    assert view == {
        "task_id": "wb-test",
        "mode": "workspace-write",
        "owned_paths": ["owned"],
        "run_nonce": "run",
        "worktree_path": "/work",
        "owned_path_grants": [{
            "task_id": "wb-test",
            "run_nonce": "run",
            "grant_id": "grant",
            "owned_paths": ["extra.txt"],
        }],
    }
    assert ownership._BOUNDARY_BROKER == "/run/lu-write-boundary/broker.sock"
    from lu_machinery.dispatcher import write_boundary

    assert ownership._BOUNDARY_BROKER == write_boundary._BROKER_INSIDE
    assert ownership._BOUNDARY_VIEW == Path(write_boundary._BROKER_INSIDE).with_name("ownership.json")


def test_a_caller_chosen_broker_path_is_not_the_ownership_view(monkeypatch, tmp_path):
    """Defect class: the hook follows LU_WRITE_BOUNDARY_BROKER to a view the worker can write, and
    an empty owned_paths list then skips the check."""
    from lu_machinery.dispatcher import ownership

    forged = tmp_path / "ownership.json"
    forged.write_text(json.dumps({
        "task_id": "wb-test", "mode": "workspace-write", "owned_paths": [], "worktree_path": str(tmp_path),
    }), encoding="utf-8")
    monkeypatch.setenv(ownership.TASK_ID_ENV, "wb-test")
    monkeypatch.setenv("LU_WRITE_BOUNDARY_BROKER", str(tmp_path / "broker.sock"))
    with pytest.raises(ValueError, match="invalid task record identity"):
        ownership._load_record("wb-test")


def test_missing_boundary_view_fails_closed_without_reading_the_store(monkeypatch, tmp_path):
    """Defect class: inside the boundary a missing projection falls through to store.read and treats
    a hidden record as 'no rule', so the commit is allowed."""
    from lu_machinery.dispatcher import ownership

    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "wb-test.json").write_text(json.dumps({
        "task_id": "wb-test", "mode": "workspace-write", "owned_paths": [], "worktree_path": str(tmp_path),
    }), encoding="utf-8")
    monkeypatch.setenv("LU_TASKS_DIR", str(tasks))
    monkeypatch.setenv(ownership.TASK_ID_ENV, "wb-test")
    monkeypatch.setenv("LU_WRITE_BOUNDARY_BROKER", ownership._BOUNDARY_BROKER)
    monkeypatch.setattr(ownership, "_BOUNDARY_VIEW", tmp_path / "absent.json")
    with pytest.raises(ValueError, match="task not found"):
        ownership._load_record("wb-test")


def test_unsetting_the_broker_variable_still_reads_the_mounted_view(monkeypatch, tmp_path):
    """Defect class: the worker clears LU_WRITE_BOUNDARY_BROKER and points LU_TASKS_DIR at a record
    whose empty owned_paths skips the check. The mounted boundary directory still selects the view."""
    from lu_machinery.dispatcher import ownership

    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "wb-test.json").write_text(json.dumps({
        "task_id": "wb-test", "mode": "workspace-write", "owned_paths": [], "worktree_path": str(tmp_path),
    }), encoding="utf-8")
    mounted = tmp_path / "mounted"
    mounted.mkdir()
    monkeypatch.setenv("LU_TASKS_DIR", str(tasks))
    monkeypatch.delenv("LU_WRITE_BOUNDARY_BROKER", raising=False)
    monkeypatch.setattr(ownership, "_BOUNDARY_VIEW", mounted / "ownership.json")
    with pytest.raises(ValueError, match="task not found"):
        ownership._load_record("wb-test")


def test_a_boundary_that_vanishes_during_refresh_does_not_fail_the_record_write(tmp_path, monkeypatch):
    """Defect class: sandbox teardown removes the boundary directory after the record is replaced,
    and the refresh then fails a write that already committed, including when the directory goes
    between the refresh's check and the writer's own (the writer then reported ``ValueError``)."""
    from lu_machinery.dispatcher import ownership

    monkeypatch.setenv("LU_SCRATCH_ROOT", str(tmp_path / "scratch"))
    directory = tmp_path / "scratch" / "write-boundary-wb-test" / "boundary"
    directory.mkdir(parents=True)
    real_write = ownership.write_ownership_view

    def vanished(path, record):
        raise FileNotFoundError(errno.ENOENT, "No such file", str(path))

    monkeypatch.setattr(ownership, "write_ownership_view", vanished)
    ownership.refresh_running_boundary_view({"task_id": "wb-test", "owned_paths": ["a.txt"]})

    # The real writer, with the directory removed after the refresh's own check (review F2 of #536).
    def torn_down(path, record):
        path.parent.rmdir()
        real_write(path, record)

    monkeypatch.setattr(ownership, "write_ownership_view", torn_down)
    ownership.refresh_running_boundary_view({"task_id": "wb-test", "owned_paths": ["a.txt"]})
    assert not directory.exists()
    with pytest.raises(FileNotFoundError):
        real_write(directory / "ownership.json", {"task_id": "wb-test"})
    directory.parent.mkdir(exist_ok=True)
    directory.symlink_to(tmp_path)
    with pytest.raises(ValueError, match="not a regular file"):
        real_write(directory / "ownership.json", {"task_id": "wb-test"})
    directory.unlink()
    directory.mkdir()

    def denied(path, record):
        raise PermissionError(errno.EACCES, "denied", str(path))

    monkeypatch.setattr(ownership, "write_ownership_view", denied)
    with pytest.raises(PermissionError):
        ownership.refresh_running_boundary_view({"task_id": "wb-test", "owned_paths": ["a.txt"]})


def test_a_record_write_refreshes_a_running_boundary_view_and_creates_nothing_else(tmp_path, monkeypatch):
    """Defect class: a grant saved in the task record never reaches the commit hook, or the refresh
    writes a second record the ledger scan will ingest."""
    from lu_machinery.dispatcher import ownership, store

    tasks = tmp_path / "tasks"
    tasks.mkdir()
    monkeypatch.setenv("LU_TASKS_DIR", str(tasks))
    monkeypatch.setenv("LU_SCRATCH_ROOT", str(tmp_path / "scratch"))
    ownership.refresh_running_boundary_view({"task_id": "wb-test", "owned_paths": ["a.txt"]})
    assert not (tmp_path / "scratch").exists()
    assert list(tasks.iterdir()) == []

    record = {
        "task_id": "wb-test", "mode": "workspace-write", "owned_paths": ["a.txt"], "run_nonce": "run",
        "prompt_file": "SECRET-DO-NOT-LEAK",
    }
    (tasks / "wb-test.json").write_text(json.dumps(record), encoding="utf-8")
    directory = tmp_path / "scratch" / "write-boundary-wb-test" / "boundary"
    directory.mkdir(parents=True)
    ownership.write_ownership_view(directory / "ownership.json", record)
    record["owned_path_grants"] = [{
        "task_id": "wb-test", "run_nonce": "run", "grant_id": "grant", "owned_paths": ["outside.txt"],
        "reason": "operator words",
    }]
    store.replace(record)
    view = json.loads((directory / "ownership.json").read_text(encoding="utf-8"))
    assert view["owned_path_grants"] == [{
        "grant_id": "grant", "owned_paths": ["outside.txt"], "run_nonce": "run", "task_id": "wb-test",
    }]
    text = (directory / "ownership.json").read_text(encoding="utf-8")
    assert "SECRET-DO-NOT-LEAK" not in text and "operator words" not in text
    # The task lock is a stable sibling, not a second record the ledger scan ingests.
    assert sorted(path.name for path in tasks.iterdir()) == ["wb-test.json", "wb-test.json.lock"]


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

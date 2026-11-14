"""A dispatched Codex read-only task gets a per-task writable scratch; the checkout stays read-only."""

import os
import shutil
import subprocess
import sys
from pathlib import Path
import pytest
from lu_machinery.agent_runtime.read_only_tmp import create_dispatch_lease, discard_dispatch_lease, dispatch_lease
from lu_machinery.dispatcher.environment import child_env
from lu_machinery.dispatcher.providers import load
from lu_machinery.dispatcher.types import TaskSpec
from lu_machinery.dispatcher import write_boundary
from tests.helpers.product_tree import configured_roots  # noqa: F401





TASK_ID = "review-demo-r1"


@pytest.fixture
def worktree(tmp_path, monkeypatch):
    monkeypatch.setenv("LU_RUNTIME_TMP_BASE_ROOT", str(tmp_path / "runtime"))
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    return checkout


def spec_for(worktree, mode="read-only"):
    prompt = worktree.parent / "prompt.txt"
    prompt.write_text("Review the change")
    return TaskSpec(TASK_ID, "codex", "explicit-model", None, mode, worktree, prompt, worktree.parent / "final.txt")


def test_scratch_is_present_private_and_outside_the_checkout(worktree):
    lease = create_dispatch_lease(TASK_ID, worktree)
    assert lease.is_dir()
    assert lease.stat().st_mode & 0o777 == 0o700
    assert not lease.is_relative_to(worktree.resolve()) and not worktree.resolve().is_relative_to(lease)
    assert lease == dispatch_lease(TASK_ID).resolve()
    assert child_env(spec_for(worktree))["TMPDIR"] == str(lease)
    with pytest.raises(FileExistsError):
        create_dispatch_lease(TASK_ID, worktree)  # one task's scratch is never shared or reused
    assert discard_dispatch_lease(TASK_ID) is None
    assert not lease.exists()
    # A symlinked namespace would place the writable scratch wherever it points.
    namespace = lease.parent
    shutil.rmtree(namespace)
    elsewhere = namespace.parent / "elsewhere"
    elsewhere.mkdir()
    namespace.symlink_to(elsewhere)
    with pytest.raises(ValueError, match="symlink"):
        create_dispatch_lease(TASK_ID, worktree)
    assert not any(elsewhere.iterdir())
    # Cleanup never deletes through it either: a same-named directory at the target survives.
    survivor = elsewhere / lease.name
    survivor.mkdir()
    (survivor / "keep.txt").write_text("kept")
    assert "symlink" in discard_dispatch_lease(TASK_ID)
    assert (survivor / "keep.txt").read_text() == "kept"


def test_discard_removes_the_lease_where_it_was_created_after_the_scratch_root_moves(worktree, tmp_path, monkeypatch):
    """Defect class: settlement resolved the lease root again, so a changed scratch root made it look in another
    directory, report nothing and leak the lease (#100 R80)."""
    created = dispatch_lease(TASK_ID)
    create_dispatch_lease(TASK_ID, worktree)
    monkeypatch.setenv("LU_RUNTIME_TMP_BASE_ROOT", str(tmp_path / "elsewhere"))
    assert discard_dispatch_lease(TASK_ID, str(created)) is None
    assert not created.exists()
    # A recorded path that is not this task's lease is refused, never deleted.
    other = created.parent / "codex-read-only-other"
    other.mkdir(parents=True)
    assert "not this task's lease" in discard_dispatch_lease(TASK_ID, str(other))
    assert other.is_dir()


def test_checkout_stays_read_only_and_only_the_scratch_is_writable(worktree, monkeypatch):
    spec = spec_for(worktree)
    lease = dispatch_lease(TASK_ID)
    invocation = load("codex").build(spec, child_env(spec))
    # The common boundary's syscall tests prove mount enforcement. This dispatch contract binds
    # Codex to that read-only mode and its own scratch rather than starting an incompatible inner sandbox.
    monkeypatch.setattr(write_boundary.shutil, "which", lambda *args, **kwargs: str(Path(sys.executable).resolve()))
    argv = write_boundary.launcher(spec, invocation, worktree.parent / "task.json")
    assert argv[argv.index("--mode") + 1] == "read-only"
    assert argv[argv.index("--runtime") + 1] == str(lease)
    assert "--dangerously-bypass-approvals-and-sandbox" in invocation.argv
    assert invocation.env["TMPDIR"] == str(lease)
    # Other modes and agents keep their own temp handling.
    write = spec_for(worktree, "workspace-write")
    assert child_env(write).get("TMPDIR") == os.environ.get("TMPDIR")
    assert "permissions=" not in " ".join(load("codex").build(write, child_env(write)).argv)


def test_socket_created_by_a_plugin_lands_under_the_scratch(worktree):
    spec = spec_for(worktree)
    lease = create_dispatch_lease(TASK_ID, worktree)
    # The test base can exceed the sockaddr_un limit, so bind by a relative name from inside the directory.
    child = (
        "import os, socket, tempfile\n"
        "directory = tempfile.mkdtemp(prefix='plugin-')\n"
        "os.chdir(directory)\n"
        "socket.socket(socket.AF_UNIX).bind('plugin.sock')\n"
        "print(os.path.realpath('plugin.sock'))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", child],
        env={**child_env(spec), "PATH": os.defpath},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).is_relative_to(lease)
    assert not any(worktree.iterdir())


pytestmark = pytest.mark.usefixtures("configured_roots")

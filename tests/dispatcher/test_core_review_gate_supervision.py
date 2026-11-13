"""Review gate, correction turns and supervisor lifecycle: cancel, stop, deadline, crash."""

import concurrent.futures
import contextlib
import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
import yaml
from lu_machinery.dispatcher import environment, store, worker
from lu_machinery.dispatcher.types import TaskSpec
from tests.dispatcher.conftest import SOURCE, await_file, run_git
from tests.helpers.product_tree import configured_roots  # noqa: F401





_SCHEMA_REJECTED = "structured_output_invalid: structured output is not valid JSON"


@pytest.mark.parametrize("text,model,required,error", [
    ("\nReview details\nVERDICT: APPROVE\n  \t", "file", True, _SCHEMA_REJECTED),
    ("VERDICT: REQUEST_CHANGES", "echo", True, _SCHEMA_REJECTED),
    ("VERDICT: APPROVE\nVERDICT: APPROVE", "echo", True, _SCHEMA_REJECTED),
    ("Review has no decision", "echo", True, _SCHEMA_REJECTED),
    ("VERDICT: APPROVE\nVERDICT: REQUEST_CHANGES", "echo", True, _SCHEMA_REJECTED),
    ("> VERDICT: APPROVE\nI might report VERDICT: APPROVE later", "echo", True, _SCHEMA_REJECTED),
    ("VERDICT: APPROVE", "fail", True, "provider exited unsuccessfully"),
    ("Review has no decision", "fail", True, "provider exited unsuccessfully"),
    ("Review has no decision", "echo", False, None),
])
def test_review_gate_prevents_false_completion_and_preserves_results(sandbox, text, model, required, error):
    """Defect classes: false completion, failure promotion, result corruption and opt-in leakage."""
    # Use the production shared parser and its dependencies in the disposable CLI repo.
    (sandbox.repo / "lu_machinery/fleet/__init__.py").touch()
    (sandbox.repo / "lu_machinery/__init__.py").write_text(f"__path__.append({str(SOURCE / 'lu_machinery')!r})\n")
    dispatched = sandbox.cli(
        "dispatch", "--agent", "sol", "--model", model, "--prompt", text,
        *(["--require-review-verdict", *sandbox.review_target(model)] if required else []),
    )
    task = dispatched.stdout.splitlines()
    record = sandbox.settle(task, expected=1 if error else 0)
    assert record["status"] == ("failed" if error else "done")
    assert record["error"] == error
    assert record["returncode"] == (17 if model == "fail" else 0)
    assert Path(record["result_file"]).read_bytes() == text.encode()
    assert record["result_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert json.loads(sandbox.cli("status", task[0]).stdout)["status"] == record["status"]
    assert json.loads(sandbox.cli("list", "--status", record["status"]).stdout)[0]["task_id"] == task[0]


@pytest.mark.parametrize(
    "second", ["kept", "changed", "invalid", "ambiguous", "quoted", "meaning", "unconfident"]
)
def test_schemaless_reviewer_missing_verdict_gets_one_correction_turn(sandbox, second):
    """Case 3: a reply from a reviewer that cannot bind a schema, with no recognised VERDICT line, gets one
    format correction. The dormant Muse adapter supplies this protocol through a local stub;
    its model is admitted only in this disposable sandbox, never the production catalog.

    The second reply is accepted only when it is valid findings JSON and keeps the
    first reply's decision. A changed or still-invalid second reply fails closed,
    and there is no third launch.
    """
    catalog_path = sandbox.repo / "lu_machinery/config/model_catalog.yaml"
    catalog = yaml.safe_load(catalog_path.read_text())
    catalog["models"]["muse-wonder-full-1.0"].update(lifecycle="active", transports=["native_muse"])
    catalog_path.write_text(yaml.safe_dump(catalog, sort_keys=False))
    findings = (
        '{"schema_version":"code-review-findings.v1",'
        '"overall":{"correctness":"correct","explanation":"No defects.","confidence":0.9},'
        '"findings":[]}'
    )
    other = findings.replace('"correctness":"correct"', '"correctness":"incorrect"', 1)
    conflict = findings.replace(
        '"explanation":"No defects."',
        '"explanation":"VERDICT: REQUEST_CHANGES. Cannot approve this change."',
    )
    reply = findings
    if second == "ambiguous":
        first = "VERDICT: APPROVE\n" + findings + "\n" + other
    elif second == "quoted":
        first = "Example only, not my review:\n> " + findings
    elif second == "meaning":
        first = "The change is sound.\n" + conflict
    elif second == "unconfident":
        first = (
            "The change is sound.\n"
            '{"schema_version":"code-review-findings.v1",'
            '"overall":{"correctness":"correct",'
            '"explanation":"VERDICT: REQUEST_CHANGES. Cannot approve this change."},'
            '"findings":[]}'
        )
    elif second == "changed":
        first = "The change is sound.\n" + findings
        reply = other
    elif second == "invalid":
        first = "The change is sound.\n" + findings
        reply = "Still commentary without a verdict."
    else:
        first = "The change is sound.\n" + findings
    binary_dir = sandbox.repo.parent / "bin"
    binary_dir.mkdir()
    version = "0.0.0-R1"
    (binary_dir / ".muse-version").write_text(version)
    executable = binary_dir / "muse"
    executable.write_text('#!/bin/sh\nexec "$(dirname "$0")/muse-bin-0.0.0-R1" "$@"\n')
    native = binary_dir / f"muse-bin-{version}"
    first_event = json.dumps({"payload_type": "run.terminal.completed", "payload": {"text": first}})
    correction_event = json.dumps({"payload_type": "run.terminal.completed", "payload": {"text": reply}})
    # The real sandbox permits no host counter writes. The dispatcher already
    # captures stderr across launches, so use it to observe the native calls.
    native.write_text(r'''#!/bin/sh
set -eu
while [ "$#" -gt 0 ]; do
    if [ "$1" = "--prompt-file" ]; then
        shift
        prompt=$(cat "$1")
        break
    fi
    shift
done
printf 'TEST_PROMPT_BEGIN\n%s\nTEST_PROMPT_END\n' "$prompt" >&2
case "$prompt" in
''' + f"*'Keep the same verdict'*) printf '%s\\n' {shlex.quote(correction_event)} ;;\n"
        + f"*) printf '%s\\n' {shlex.quote(first_event)} ;;\nesac\n")
    executable.chmod(0o755)
    native.chmod(0o755)
    home = sandbox.repo.parent / "synthetic-home"
    home.mkdir()
    sandbox.env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"))
    schema_dir = sandbox.repo / "schemas"
    schema_dir.mkdir(exist_ok=True)
    shutil.copy(SOURCE / "lu_machinery/schemas/code-review-findings.v1.schema.json", schema_dir)
    sandbox.env["PATH"] = str(binary_dir) + os.pathsep + os.defpath
    head = run_git(
        sandbox.repo, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m",
        "authored change\n\nX-Agent: claude/claude-opus-5-5",
    )
    run_git(sandbox.repo, "branch", "review-source", head)
    run_git(sandbox.repo, "push", "origin", "review-source")
    dispatched = sandbox.cli(
        "dispatch", "--agent", "muse", "--model", "muse-wonder-full-1.0",
        "--prompt", "Review the change", "--mode", "read-only",
        "--branch", "review-source", "--require-review-verdict", "--review-profile", "code",
        "--review-author-model", "claude-opus-5-5",
    )
    record = sandbox.settle(dispatched.stdout.splitlines(), expected=0 if second == "kept" else 1)
    launches = 1 if second in {"ambiguous", "quoted"} else 2
    trace = (sandbox.tasks / "logs" / f"{record['task_id']}.stderr.log").read_text()
    frames = trace.split("TEST_PROMPT_BEGIN\n")[1:]
    assert len(frames) == launches
    assert all("\nTEST_PROMPT_END\n" in frame for frame in frames)
    prompts = [frame.split("\nTEST_PROMPT_END\n", 1)[0] for frame in frames]
    if launches == 2:
        correction = prompts[1]
        assert correction.startswith('<rules-core seat="core"')
        assert "</rules-core>" in correction
        assert "Keep the same verdict" in correction
        if second == "unconfident":
            assert "REQUEST_CHANGES" in correction
        else:
            assert findings in correction or conflict in correction
    if second == "kept":
        assert record["status"] == "done"
        assert json.loads(record["result_file"] and Path(record["result_file"]).read_text())["overall"]["correctness"] == "correct"
    elif second in {"changed", "meaning", "unconfident"}:
        assert record["status"] == "failed"
        assert record["error"] == "review_ambiguous_verdict"
    elif second == "ambiguous":
        assert record["status"] == "failed"
        assert record["error"] == "review_gate_error"
    else:
        assert record["status"] == "failed"
        assert record["error"] == "review_missing_verdict_line"


def test_review_gate_exception_cannot_leave_supervisor_running(sandbox):
    """Defect class: scanner exceptions lose terminal failure or expose exception contents."""
    review = sandbox.repo / "lu_machinery/review"
    review.mkdir(exist_ok=True)
    (review / "__init__.py").touch()
    # Dispatch admission reads branch authorship through the recorder, which imports the rest of the parser.
    parser = review / "verdict_parser.py"
    parser.write_text(
        parser.read_text() + "\n\ndef recognized_verdicts(text):\n    raise RuntimeError('sensitive scanner diagnostic')\n"
    )
    receipt = (
        '{"schema_version":"code-review-findings.v1","overall":{"correctness":"correct",'
        '"explanation":"No defects.","confidence":0.9},"findings":[]}'
    )
    record = sandbox.settle(
        sandbox.cli(
            "dispatch", "--agent", "sol", "--model", "echo", "--prompt", receipt,
            "--require-review-verdict", *sandbox.review_target("echo"),
        ).stdout.splitlines(),
        expected=1,
    )
    assert record["status"] == "failed"
    assert record["error"] == "review_gate_error"
    assert record["returncode"] == 0
    assert record["finished_at"] is not None
    assert json.loads(Path(record["result_file"]).read_text()) == json.loads(receipt)
    assert "sensitive scanner diagnostic" not in json.dumps(record)


def test_nonce_refuses_status_wait_and_cancel(sandbox):
    task = sandbox.dispatch("blocked")
    await_file(sandbox.tasks / f"{task[0]}.started")
    for command in ("status", "wait", "cancel"):
        result = sandbox.cli(command, task[0], "--run-nonce", "stale", expected=2)
        assert result.stderr == "dispatcher: run nonce mismatch\n"
    assert json.loads(sandbox.cli("status", task[0], "--run-nonce", task[1]).stdout)["status"] == "running"
    sandbox.cli("cancel", task[0], "--run-nonce", task[1])


def test_cancel_kills_child_process_group(sandbox):
    task = sandbox.dispatch("tree")
    child = int(await_file(sandbox.tasks / f"{task[0]}.started"))
    grandchild = int(await_file(sandbox.tasks / f"{task[0]}.grandchild"))
    record = json.loads(sandbox.cli("cancel", task[0], "--run-nonce", task[1]).stdout)
    assert record["status"] == "cancelled"
    assert worker._identity(child) is None
    assert worker._identity(grandchild) is None


def test_settling_a_task_terminates_a_descendant_in_another_process_group(sandbox):
    """Defect (#12520): a descendant outside the provider's process group (GNU ``timeout`` puts its child
    in one) or reparented away from it (a double fork) outlived a cancelled task on a Popen-fallback launch."""
    task = sandbox.dispatch("escape")
    own_session = int(await_file(sandbox.tasks / f"{task[0]}.grandchild"))
    daemon = int(await_file(sandbox.tasks / f"{task[0]}.daemon"))
    assert os.getpgid(own_session) != os.getpgid(int(await_file(sandbox.tasks / f"{task[0]}.started")))
    assert worker._identity(own_session) is not None and worker._identity(daemon) is not None
    record = json.loads(sandbox.cli("cancel", task[0], "--run-nonce", task[1]).stdout)
    assert record["status"] == "cancelled"
    assert worker._identity(own_session) is None
    assert worker._identity(daemon) is None


def test_fallback_survivor_recorded_by_a_failed_stop_keeps_the_tree_until_it_exits(sandbox):
    """Defect (#12520): a descendant that left the provider's group and survived the stop was forgotten, so a
    later retry saw the group gone and released the tree under it."""
    ended = subprocess.Popen(["true"])
    ended.wait(timeout=30)
    survivor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)
    try:
        record = {"launch_mode": "popen-fallback", "provider_pgid": ended.pid,
                  "fallback_survivors": {str(survivor.pid): worker._identity(survivor.pid)}}
        assert worker._provider_alive(record) is True
        survivor.kill()
        survivor.wait(timeout=30)
        assert worker._provider_alive(record) is False
    finally:
        survivor.kill()
        survivor.wait(timeout=30)


def test_failed_fallback_stop_retains_the_worktree_without_settling_it(monkeypatch):
    """Defect (#12520): a scan that could not be completed was read as no descendant, and the tree went."""
    from lu_machinery.orchestration import worker_leftovers

    monkeypatch.setattr(worker, "_end_own_scope", lambda _record: worker_leftovers.StopResult(ok=False, error="x"))
    monkeypatch.setattr(worker.git, "settle_worktree", Mock(side_effect=AssertionError("settled under a live scan")))
    record = {"task_id": "t", "launch_mode": "popen-fallback", "provider_pgid": None}
    worker._reap_settled(record)
    assert (record["worktree_disposition"], record["worktree_disposition_reason"]) == (
        "retained", "provider_liveness_unknown")


def _settle_with_worktree(monkeypatch, tmp_path, state):
    """Settle task ``scratch-task`` against a temporary scratch base, its worktree disposition being ``state``."""
    base = tmp_path / "var-tmp"
    base.mkdir()
    monkeypatch.setattr(environment, "TASK_SCRATCH_BASE", base)
    monkeypatch.setattr(worker, "_end_own_scope", lambda _record: None)
    monkeypatch.setattr(worker, "_provider_alive", lambda _record: False)
    monkeypatch.setattr(worker.git, "settle_worktree", lambda *_args, **_kwargs: {
        "worktree_disposition": state, "worktree_disposition_reason": "clean" if state == "removed" else "dirty"})
    record = {"task_id": "scratch-task"}
    return base, record


def test_settlement_removes_the_task_scratch_including_a_read_only_directory(monkeypatch, tmp_path):
    """Defect class: scratch leak. A settled task whose worktree went left /var/tmp/<task-id> behind, and a
    0500 directory a test left inside it refused deletion."""
    base, record = _settle_with_worktree(monkeypatch, tmp_path, "removed")
    locked = base / "scratch-task" / "fixture" / "locked"
    locked.mkdir(parents=True)
    (locked / "data").write_bytes(b"x" * 10)
    locked.chmod(0o500)
    base.joinpath("scratch-task").chmod(0o500)
    worker._reap_settled(record)
    assert not (base / "scratch-task").exists()
    assert (record["task_scratch_disposition"], record["task_scratch_bytes"]) == ("removed", 10)


def test_settlement_refuses_a_symlinked_task_scratch_and_leaves_its_target(monkeypatch, tmp_path):
    """Defect class: symlink-following deletion. A symlink at /var/tmp/<task-id> led settlement to delete
    the directory it points to."""
    base, record = _settle_with_worktree(monkeypatch, tmp_path, "removed")
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "keep").write_text("kept")
    (base / "scratch-task").symlink_to(target, target_is_directory=True)
    worker._reap_settled(record)
    assert (base / "scratch-task").is_symlink() and (target / "keep").read_text() == "kept"
    assert record["task_scratch_disposition"] == "refused"
    assert record["worktree_disposition"] == "removed"  # the refusal never fails settlement


def test_retained_worktree_keeps_the_task_scratch(monkeypatch, tmp_path):
    """Defect class: evidence loss. Scratch went while the worktree it serves was kept for inspection."""
    base, record = _settle_with_worktree(monkeypatch, tmp_path, "retained")
    (base / "scratch-task").mkdir()
    (base / "scratch-task" / "log").write_text("evidence")
    worker._reap_settled(record)
    assert (base / "scratch-task" / "log").read_text() == "evidence"
    assert record["task_scratch_disposition"] == "retained"
    assert "worktree retained" in record["task_scratch_disposition_reason"]


def test_settlement_removes_only_recreatable_caches_from_the_task_output_directories(sandbox):
    """Defect (C-4946(a)): settled tasks kept venv, node_modules and __pycache__ copies in their .artifacts and
    .evidence directories for good. Settlement and the scheduled sweep remove those caches from a finished
    task only, keep every other file, and remove a cache-named link without following it."""
    task = sandbox.dispatch("blocked")
    await_file(sandbox.tasks / f"{task[0]}.started")
    artifacts, evidence = sandbox.tasks / f"{task[0]}.artifacts", sandbox.tasks / f"{task[0]}.evidence"
    caches = [artifacts / "venv", artifacts / "node_modules", artifacts / "probe/__pycache__"]
    for cache in caches:
        (cache / "sub").mkdir(parents=True)
        (cache / "sub/file").write_text("re-creatable")
    kept = {artifacts / "run.log": "log", artifacts / "probe/result.json": "{}", evidence / "receipt.json": "{}"}
    evidence.mkdir()
    for path, text in kept.items():
        path.write_text(text)
    elsewhere = sandbox.tasks.parent / "linked-cache"
    elsewhere.mkdir()
    (elsewhere / "outside.txt").write_text("not the task's")
    (evidence / ".cache").symlink_to(elsewhere, target_is_directory=True)

    sandbox.sweep()
    assert all((cache / "sub/file").exists() for cache in caches)
    assert (evidence / ".cache").is_symlink()

    (sandbox.tasks / f"{task[0]}.release").touch()
    record = sandbox.settle(task)
    assert record["status"] == "done" and "task_cache_residue" not in record
    assert not any(os.path.lexists(cache) for cache in [*caches, evidence / ".cache"])
    assert {path: path.read_text() for path in kept} == kept
    assert (elsewhere / "outside.txt").read_text() == "not the task's"

    # A cache that appears once the task has settled is removed by the scheduled sweep.
    (artifacts / ".venv/lib").mkdir(parents=True)
    sandbox.sweep()
    assert not (artifacts / ".venv").exists()
    assert {path: path.read_text() for path in kept} == kept


def _owner_rwx(path: Path) -> None:
    """Give owner rwx so a failed case cannot leave a mode-0555 tree for tmp cleanup."""
    if path.is_symlink() or not path.exists():
        return
    path.chmod(0o755)
    if path.is_dir():
        for child in path.iterdir():
            _owner_rwx(child)


def test_settlement_removes_a_read_only_cache_subtree(sandbox):
    """Defect class: read-only cache residue. A cache directory whose children are mode 0555
    (the Go module-cache layout) made removal raise PermissionError, so the residue was
    retried every hour and never cleared. Settlement and the hourly sweep remove that tree
    from a finished task, leave an active task's tree in place, and do not change a sibling
    directory's mode or a file outside the task outputs."""
    task = sandbox.dispatch("blocked")
    await_file(sandbox.tasks / f"{task[0]}.started")
    artifacts, evidence = sandbox.tasks / f"{task[0]}.artifacts", sandbox.tasks / f"{task[0]}.evidence"
    evidence.mkdir()
    (evidence / "receipt.json").write_text("{}")
    trees = []
    for root_name, cache_name in ((artifacts, ".cache"), (evidence, "pip-cache")):
        sub = root_name / cache_name / "ro" / "sub"
        sub.mkdir(parents=True)
        (sub / "mod").write_text("module")
        (root_name / cache_name / "ro").chmod(0o555)
        sub.chmod(0o555)
        trees.append(root_name / cache_name)
    (artifacts / "run.log").write_text("log")
    sibling = artifacts / "notes"
    sibling.mkdir()
    (sibling / "keep.txt").write_text("keep")
    outside = sandbox.tasks.parent / "outside-keep.txt"
    outside.write_text("outside")
    try:
        sibling.chmod(0o555)
        sandbox.sweep()
        assert all((tree / "ro/sub/mod").read_text() == "module" for tree in trees)
        assert sibling.stat().st_mode & 0o777 == 0o555

        (sandbox.tasks / f"{task[0]}.release").touch()
        record = sandbox.settle(task)
        assert record["status"] == "done"
        assert [tree.exists() for tree in trees] == [False, False], record.get("task_cache_residue")
        assert "task_cache_residue" not in record
        for root_name, cache_name in ((artifacts, ".cache"), (evidence, "pip-cache")):
            sub = root_name / cache_name / "ro" / "sub"
            sub.mkdir(parents=True)
            (sub / "mod").write_text("module")
            (root_name / cache_name / "ro").chmod(0o555)
            sub.chmod(0o555)
        sandbox.sweep()
        disk = json.loads((sandbox.tasks / f"{task[0]}.json").read_text())
        assert [tree.exists() for tree in trees] == [False, False], disk.get("task_cache_residue")
        assert "task_cache_residue" not in disk
        assert (artifacts / "run.log").read_text() == "log"
        assert (evidence / "receipt.json").read_text() == "{}"
        assert (sibling / "keep.txt").read_text() == "keep"
        assert sibling.stat().st_mode & 0o777 == 0o555
        assert outside.read_text() == "outside"
    finally:
        _owner_rwx(sibling)
        for tree in trees:
            _owner_rwx(tree)


def test_settlement_leaves_an_empty_cache_inside_a_read_only_non_cache_parent(sandbox, monkeypatch):
    """Defect class: empty cache directory and hourly residue under a read-only non-cache parent.

    A cache directory inside an owner-read-only parent that is not itself a cache cannot be
    unlinked without chmod on that parent. Settlement removes the cache contents, leaves the
    empty directory, does not change the parent's mode, and does not record
    ``task_cache_residue``. The hourly sweep does not record one either.
    """
    task = sandbox.dispatch("blocked")
    await_file(sandbox.tasks / f"{task[0]}.started")
    artifacts = sandbox.tasks / f"{task[0]}.artifacts"
    evidence = sandbox.tasks / f"{task[0]}.evidence"
    evidence.mkdir()
    (evidence / "receipt.json").write_text("{}")
    parent = artifacts / "notes"
    payload = parent / ".cache" / "ro" / "sub"
    payload.mkdir(parents=True)
    (payload / "mod").write_text("module")
    (artifacts / "run.log").write_text("log")
    (parent / ".cache" / "ro").chmod(0o555)
    payload.chmod(0o555)
    cache = parent / ".cache"
    try:
        parent.chmod(0o555)
        # RV298-F1: a listed mount below the cache must retain its payload and parent mode.
        with monkeypatch.context() as mounted:
            mounted.setattr(worker.task_scratch, "mount_points", lambda: frozenset({str(payload)}))
            fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with pytest.raises(worker.task_scratch.ContainmentError, match="mount point"):
                    worker._remove_cache_entry(fd, ".cache")
            finally:
                os.close(fd)
        assert (payload / "mod").read_text() == "module"
        assert parent.stat().st_mode & 0o777 == 0o555
        sandbox.sweep()
        assert (payload / "mod").read_text() == "module"
        assert parent.stat().st_mode & 0o777 == 0o555

        (sandbox.tasks / f"{task[0]}.release").touch()
        record = sandbox.settle(task)
        assert record["status"] == "done"
        assert cache.is_dir() and not any(cache.iterdir()), record.get("task_cache_residue")
        assert "task_cache_residue" not in record
        assert parent.stat().st_mode & 0o777 == 0o555
        assert (artifacts / "run.log").read_text() == "log"
        assert (evidence / "receipt.json").read_text() == "{}"

        sandbox.sweep()
        disk = json.loads((sandbox.tasks / f"{task[0]}.json").read_text())
        assert cache.is_dir() and not any(cache.iterdir()), disk.get("task_cache_residue")
        assert "task_cache_residue" not in disk
        assert parent.stat().st_mode & 0o777 == 0o555
    finally:
        _owner_rwx(parent)


def test_settlement_removes_a_dangling_or_file_cache_link(sandbox):
    """Defect class: cache-named link kept. fwalk puts a dangling link and a link to a file in
    ``files``, which cache removal never inspected, so both stayed. Settlement and the hourly
    sweep unlink those names and leave the file target, a non-cache symlink, and an active
    task's links in place."""
    task = sandbox.dispatch("blocked")
    await_file(sandbox.tasks / f"{task[0]}.started")
    artifacts, evidence = sandbox.tasks / f"{task[0]}.artifacts", sandbox.tasks / f"{task[0]}.evidence"
    artifacts.mkdir()
    evidence.mkdir()
    (evidence / "receipt.json").write_text("{}")
    (artifacts / "run.log").write_text("log")
    target = sandbox.tasks.parent / "cache-file-target"
    target.write_text("target-bytes")
    note_target = sandbox.tasks.parent / "note-target"
    note_target.write_text("note")
    dangling = artifacts / "node_modules"
    file_link = evidence / "venv"
    other = artifacts / "notes"
    dangling.symlink_to("/nonexistent")
    file_link.symlink_to(target)
    other.symlink_to(note_target)
    links = (dangling, file_link)

    sandbox.sweep()
    assert all(link.is_symlink() for link in links)
    assert target.read_text() == "target-bytes"

    (sandbox.tasks / f"{task[0]}.release").touch()
    record = sandbox.settle(task)
    assert record["status"] == "done"
    assert [os.path.lexists(link) for link in links] == [False, False], record.get("task_cache_residue")
    assert "task_cache_residue" not in record
    dangling.symlink_to("/nonexistent")
    file_link.symlink_to(target)
    sandbox.sweep()
    disk = json.loads((sandbox.tasks / f"{task[0]}.json").read_text())
    assert [os.path.lexists(link) for link in links] == [False, False], disk.get("task_cache_residue")
    assert "task_cache_residue" not in disk
    assert target.read_text() == "target-bytes"
    assert note_target.read_text() == "note"
    assert other.is_symlink() and other.read_text() == "note"
    assert (artifacts / "run.log").read_text() == "log"
    assert (evidence / "receipt.json").read_text() == "{}"


def test_stop_never_signals_a_marked_non_descendant_that_appears_mid_stop():
    """Defect (#12520, review F1): the sweep takes the task's marker for ownership, so an unrelated client that
    inherits it, and appears after an earlier ownership scan, must still be left alone."""
    from lu_machinery.orchestration import worker_leftovers
    from tests.orchestration.worker_leftovers_fakes import FAKE_PID_BASE, TASK_ID, FakeProc, FakeProcs, scope_of, stop_kwargs

    class LateClient(FakeProcs):
        def pids(self):
            listed = super().pids()
            self.procs.setdefault(FAKE_PID_BASE + 2, FakeProc(task=TASK_ID))  # appears between two listings
            return listed

    table = LateClient()
    mine, client = FAKE_PID_BASE + 1, FAKE_PID_BASE + 2
    table.procs[mine] = FakeProc(task=TASK_ID)
    result = worker_leftovers.stop_leftovers(scope_of(), reader=table, owned=lambda pid: pid == mine,
                                             **stop_kwargs(table))
    assert result.ok and table.signalled() == {mine} and client in table.procs
    # The supervisor's own tree is judged live: a child is its descendant, its parent is not.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        assert worker._is_own_descendant(child.pid) and not worker._is_own_descendant(os.getppid())
    finally:
        child.kill()
        child.wait(timeout=30)


def test_unresolved_ancestry_is_unknown_never_a_non_descendant(monkeypatch):
    """Defect (#12520, review F5): an ancestor that exits mid-walk, or an unreadable ancestry, hid a live
    descendant from the sweep as if it were a stranger."""
    from lu_machinery.orchestration import worker_leftovers

    me = os.getpid()
    parents = {5_000_001: 5_000_002, 5_000_002: me}  # a sleeper whose parent is a provider the supervisor adopts
    calls = []

    class Process:
        def __init__(self, pid):
            self.pid = pid

        def ppid(self):
            calls.append(self.pid)
            if self.pid == 5_000_002 and calls.count(5_000_002) == 1:
                raise worker.psutil.NoSuchProcess(self.pid)  # the provider exits mid-walk; the sleeper reparents
            if self.pid == 5_000_001 and 5_000_002 in calls:
                return me
            if self.pid == 9_000_001:
                raise worker.psutil.AccessDenied(self.pid)
            return parents.get(self.pid, 1)

    monkeypatch.setattr(worker.psutil, "Process", Process)
    assert worker._is_own_descendant(5_000_001) is True
    assert worker._is_own_descendant(5_000_777) is False  # a proven stranger: its parent is init
    with pytest.raises(worker_leftovers.ScanUnknown):
        worker._is_own_descendant(9_000_001)
    # A descendant deeper than the walk's limit is unknown too: the limit proves nothing about it.
    parents.update({6_000_000 + hop: 6_000_001 + hop for hop in range(worker.ANCESTRY_DEPTH + 1)})
    parents[6_000_000 + worker.ANCESTRY_DEPTH + 1] = me
    with pytest.raises(worker_leftovers.ScanUnknown):
        worker._is_own_descendant(6_000_000)


@pytest.mark.parametrize("libc", [SimpleNamespace(prctl=lambda *_args: -1), SimpleNamespace()],
                         ids=["refused", "unavailable"])
def test_fallback_run_fails_before_any_provider_when_orphan_adoption_fails(sandbox, monkeypatch, libc):
    """Defect (#12520, review F4): without PR_SET_CHILD_SUBREAPER a double-forked descendant escapes the sweep
    unseen, so the run must fail before it starts a provider."""
    monkeypatch.setenv("LU_TASKS_DIR", str(sandbox.tasks))
    monkeypatch.setattr(worker.ctypes, "CDLL", lambda *_args, **_kwargs: libc)
    monkeypatch.setattr(worker.providers, "load", Mock(side_effect=AssertionError("a provider was started")))
    monkeypatch.setattr(worker, "_reap_settled", lambda *_args, **_kwargs: None)
    spec = TaskSpec("no-adoption", "codex", "echo", None, "read-only", sandbox.repo,
                    sandbox.repo / "no-adoption.prompt", sandbox.tasks / "no-adoption.result")
    spec.prompt_file.write_bytes(b"literal final response")
    record = store.new_record(spec, {"key": "public", "github": str(sandbox.remote)}, "main",
                              run_git(sandbox.repo, "rev-parse", "HEAD"), {"research_owned_path": []})
    record.update(prompt_file=str(sandbox.tasks / "no-adoption.prompt"), launch_mode="popen-fallback")
    path = store.create(record, b"literal final response")
    (sandbox.tasks / "logs").mkdir()
    worker.supervise(path, {}, 10)
    settled = json.loads(path.read_text())
    assert settled["status"] == "failed" and settled["error"].startswith("fallback_adoption_unavailable")


def test_deadline_settles_as_timeout(sandbox):
    task = sandbox.dispatch("blocked", "--hard-timeout", "0.2")
    record = sandbox.settle(task, expected=1)
    assert record["status"] == "timeout"
    assert record["returncode"] == -9


def test_immediate_cancel_and_duplicate_id_do_not_leave_a_second_run(sandbox):
    task = sandbox.dispatch("blocked", "--task-id", "one-run")
    cancelled = json.loads(sandbox.cli("cancel", task[0], "--run-nonce", task[1]).stdout)
    assert cancelled["status"] == "cancelled"
    branches = run_git(sandbox.repo, "branch", "--list")
    result = sandbox.cli(
        "dispatch", "--agent", "sol", "--model", "echo", "--prompt", "x", "--task-id", "one-run", expected=2
    )
    assert result.stderr == "dispatcher: task id already exists; choose a new task id\n"
    assert run_git(sandbox.repo, "branch", "--list") == branches
    assert json.loads((sandbox.tasks / "one-run.json").read_text())["run_nonce"] == task[1]


def test_supervisor_crash_is_reconciled_without_claiming_success(sandbox):
    task = sandbox.dispatch("blocked")
    child = int(await_file(sandbox.tasks / f"{task[0]}.started"))
    record = json.loads(sandbox.cli("status", task[0]).stdout)
    os.kill(record["pid"], signal.SIGKILL)
    # The provider remains separately identifiable for test cleanup after a supervisor SIGKILL.
    os.killpg(child, signal.SIGKILL)
    record = sandbox.settle(task, expected=1)
    assert record["status"] == "crashed"
    assert record["finished_at"]


def _killed_review_supervisor(sandbox, tmp_path, model):
    """Dispatch a held Codex read-only review on ``model``, SIGKILL its supervisor once gone; the provider pid."""
    from lu_machinery.common import test_slots

    for name in (test_slots.HOLDER_ENV, test_slots.SESSION_ENV, "CI"):
        sandbox.env.pop(name, None)
    sandbox.env.update({test_slots.ENABLE_ENV: "on", test_slots.DIR_ENV: str(tmp_path / "pool"),
                        test_slots.COUNT_ENV: "4", test_slots.WAIT_ENV: "5"})
    task = sandbox.dispatch(model, "--require-review-verdict", *sandbox.review_target(model))
    child = int(await_file(sandbox.tasks / f"{task[0]}.started"))
    if model == "governed":
        assert await_file(sandbox.tasks / f"{task[0]}.joined") == "joined"
    # The provider's start marker precedes the supervisor's record update.
    record = json.loads(await_file(
        sandbox.tasks / f"{task[0]}.json", ready=lambda content: "review_test_slots" in json.loads(content)
    ))
    assert record["review_test_slots"]["outcome"] == "held"
    os.kill(record["pid"], signal.SIGKILL)
    deadline = time.monotonic() + 10
    while worker._identity(record["pid"]) == record["process_start"]:
        assert time.monotonic() < deadline, "supervisor survived SIGKILL"
        time.sleep(0.02)
    assert worker._identity(child) is not None, "the provider must outlive its supervisor here"
    return task, child


def _pool_report(sandbox):
    from lu_machinery.common import test_slots

    report = test_slots.status(sandbox.env)
    assert report["error"] is None, report
    return report


def _provider_environment(pid, sandbox):
    """The provider's own environment, on the test's pool (the provider environment carries no override)."""
    from lu_machinery.common import test_slots

    entries = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    environment = dict(entry.decode().split("=", 1) for entry in entries if b"=" in entry)
    return {**environment, test_slots.DIR_ENV: sandbox.env[test_slots.DIR_ENV]}


def test_a_slot_holding_preload_starts_no_thread_pool_that_keeps_the_review_hold(tmp_path):
    """Defect: the build-engine preload starts an OpenBLAS pool that keeps review flocks past SIGKILL.

    ``scripts.build.fresh.manifest`` reaches numpy. Its threads share the supervisor's file table, so once the
    leader is a zombie ``_identity`` reports it gone while the flocks are still held. The preload a slot-holding
    supervisor runs is ``load_post_start_code(False)``: a bound review attempt is the only run that passes True,
    and that run does not take review slots.
    """
    from lu_machinery.common import test_slots

    pool = tmp_path / "pool"
    ready = tmp_path / "ready"
    code = """
import json, os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
os.environ["LU_TEST_SLOTS"] = "on"
os.environ["LU_TEST_SLOTS_DIR"] = sys.argv[2]
os.environ["LU_TEST_SLOTS_COUNT"] = "4"
os.environ["LU_TEST_SLOTS_WAIT_S"] = "5"
os.environ.pop("CI", None)
os.environ.pop("LU_TEST_SLOTS_HOLDER", None)
os.environ.pop("LU_TEST_SLOTS_SESSION", None)
from lu_machinery.dispatcher.worker import load_post_start_code
load_post_start_code(False)
threads = len(list(Path("/proc/self/task").iterdir()))
from lu_machinery.common import test_slots
test_slots.acquire(2, kind="review", task_id="preload-threads")
Path(sys.argv[3]).write_text(json.dumps({"threads": threads}))
time.sleep(30)
"""
    err = tmp_path / "child.err"
    with err.open("w") as stream:
        proc = subprocess.Popen(
            [sys.executable, "-c", code, str(SOURCE), str(pool), str(ready)],
            stdout=subprocess.DEVNULL, stderr=stream,
        )
    try:
        deadline = time.monotonic() + 30
        while not ready.exists():
            if proc.poll() is not None:
                raise AssertionError(err.read_text() or f"preload child exited {proc.returncode}")
            assert time.monotonic() < deadline, "the preload child did not take its slots"
            time.sleep(0.02)
        assert json.loads(ready.read_text())["threads"] == 1
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
        status = test_slots.status({
            test_slots.ENABLE_ENV: "on", test_slots.DIR_ENV: str(pool),
            test_slots.COUNT_ENV: "4", test_slots.WAIT_ENV: "5",
        })
        assert status["error"] is None, status
        assert status["slots_in_use"] == 0
        assert status["slots_draining"] == 0
    finally:
        if proc.poll() is None:
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)


def test_a_killed_supervisors_review_hold_ends_with_it_and_its_provider_holds_no_pool_descriptor(sandbox, tmp_path):
    """Defect class: a provider that outlives its supervisor keeps the review hold, so slots strand without bound."""
    from lu_machinery.common import test_slots

    task, child = _killed_review_supervisor(sandbox, tmp_path, "blocked")
    try:
        pool = os.path.realpath(tmp_path / "pool")
        links = [os.readlink(entry) for entry in Path(f"/proc/{child}/fd").iterdir()]
        assert not [link for link in links if link == pool or link.startswith(pool + os.sep)]
        report = _pool_report(sandbox)
        assert report["slots_in_use"] + report["slots_draining"] == 0
        environment = _provider_environment(child, sandbox)
        assert environment[test_slots.HOLDER_ENV]
        assert test_slots.inherited_hold(environment) is None
        assert worker._identity(child) is not None
        assert sandbox.settle(task, expected=1)["status"] == "crashed"
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child, signal.SIGKILL)


def test_a_governed_turn_in_flight_keeps_its_slots_past_a_killed_supervisor_and_nothing_new_joins(sandbox, tmp_path):
    """Defect class: a killed supervisor frees slots a running test still uses, or lets a new run join its hold."""
    from lu_machinery.common import test_slots

    task, child = _killed_review_supervisor(sandbox, tmp_path, "governed")
    try:
        governed = int(await_file(sandbox.tasks / f"{task[0]}.grandchild"))
        report = _pool_report(sandbox)
        assert (report["slots_in_use"], report["slots_draining"]) == (0, worker.admission.REVIEW_TEST_SLOTS)
        assert test_slots.join_inherited(_provider_environment(child, sandbox), wait_s=0.3) is None
        (sandbox.tasks / f"{task[0]}.child-release").touch()
        deadline = time.monotonic() + 10
        while worker._identity(governed) is not None:
            assert time.monotonic() < deadline, "the governed run did not end once released"
            time.sleep(0.02)
        report = _pool_report(sandbox)
        assert report["slots_in_use"] + report["slots_draining"] == 0
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child, signal.SIGKILL)


def test_concurrent_dispatches_use_distinct_records(sandbox):
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        tasks = list(pool.map(lambda _: sandbox.dispatch(), range(2)))
    assert len({task[0] for task in tasks}) == 2
    assert len({task[1] for task in tasks}) == 2
    assert all(sandbox.settle(task)["status"] == "done" for task in tasks)


pytestmark = pytest.mark.usefixtures("configured_roots")

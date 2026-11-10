"""Behavioral retention proofs for inactive pytest scratch on disposable roots (#10831, #11297)."""

from __future__ import annotations

import fcntl
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lu_machinery.common import pytest_scratch, task_scratch
from lu_machinery.hygiene import pytest_retention, tmp_sweep

REPO = Path(__file__).resolve().parents[2]
HOUR = 3600.0


@pytest.fixture
def area(tmp_path, monkeypatch):
    root = tmp_path / "managed"
    root.mkdir(mode=0o700)
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    temp = tmp_path / "system-temp"
    temp.mkdir()
    proc = tmp_path / "proc"
    proc.mkdir()
    monkeypatch.setenv("LU_SCRATCH_ROOT", str(root))
    monkeypatch.setattr(pytest_retention, "scratch_scan_roots", lambda: [root])
    # An empty process table: a lease owner is judged against the real one only where a test says so.
    monkeypatch.setattr(pytest_retention, "PROC_ROOT", proc)
    monkeypatch.setattr(tmp_sweep, "SYSTEM_TEMP_AREAS", (temp,))
    monkeypatch.setattr(tmp_sweep, "process_snapshot", lambda: ([], True))
    monkeypatch.setattr(tmp_sweep, "registered_worktrees", lambda _: set())
    return root, dict(temp_root=temp, task_root=tasks, state_dir=tmp_path / "state", repo_root=REPO)



def namespace(root: Path, name: str = pytest_scratch.RUNS_DIRNAME) -> Path:
    path = root / pytest_scratch.NAMESPACE / name
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    (root / pytest_scratch.NAMESPACE).chmod(0o700)
    path.chmod(0o700)
    return path



def unowned(root: Path, name: str, *, ns: str = pytest_scratch.RUNS_DIRNAME) -> Path:
    """A run folder with no ownership record or lock at all."""
    path = namespace(root, ns) / name
    (path / "scratch" / "base").mkdir(parents=True)
    (path / "scratch" / "base" / "payload").write_bytes(b"x" * 8192)
    return path



def malformed(root: Path, name: str, *, ns: str = pytest_scratch.RUNS_DIRNAME) -> Path:
    path = unowned(root, name, ns=ns)
    (path / "lease.json").write_text("{not json")
    (path / "lease.lock").touch(mode=0o600)
    return path



def completed(root: Path, count: int = 2, *, ns: str = task_scratch.NAMESPACE_DIRNAME) -> list[Path]:
    # Real ownership metadata and flocks, released by a different process that exits.
    namespace(root, ns)
    result = subprocess.run(
        [sys.executable, "-c", """
import json, sys
from pathlib import Path
from lu_machinery.common.task_scratch import allocate
paths = []
for index in range(int(sys.argv[2])):
    run = allocate('pytest', root=Path(sys.argv[1]) / 'pytest-runs', namespace_dirname=sys.argv[3])
    (run.payload_dir / 'payload').write_bytes(b'x' * 4096)
    paths.append(str(run.path))
    run.preserve()
print(json.dumps(paths))
""", str(root), str(count), ns],
        cwd=REPO, capture_output=True, text=True, check=True, timeout=30,
    )
    return [Path(path) for path in json.loads(result.stdout)]



def later(monkeypatch, seconds: float, *, base: float | None = None) -> float:
    now = (tmp_sweep.time.time() if base is None else base) + seconds
    monkeypatch.setattr(tmp_sweep.time, "time", lambda: now)
    return now



def reaped_pid() -> tuple[int, int]:
    """A real pid and start time that are provably dead once the child exits (#11409)."""
    from lu_machinery.common.acp_runtime_lock import process_start_time

    # The child stays alive until communicate() closes its stdin, so its start time is always readable.
    child = subprocess.Popen(
        [sys.executable, "-c", "import os, sys; print(os.getpid(), flush=True); sys.stdin.read()"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        pid = int(child.stdout.readline())
        start = process_start_time(pid)
        assert start is not None
    finally:
        child.communicate(timeout=10)
    assert child.returncode == 0
    return pid, start



def live_pid() -> tuple[subprocess.Popen, int, int]:
    """A real live child with its recorded start time; the caller stops it."""
    from lu_machinery.common.acp_runtime_lock import process_start_time

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    start = process_start_time(child.pid)
    assert start is not None
    return child, child.pid, start



def write_lease(
    run: Path, *, pid: int, start_time: int | None, boot_id: str | None, state: str = "preserved",
) -> None:
    """A real lease file in the run's own format, plus the lock the owner would have created."""
    info = run.stat()
    payload = {
        "schema_version": task_scratch.LEASE_SCHEMA_VERSION,
        "kind": task_scratch.NAMESPACE_DIRNAME,
        "task_id": "pytest",
        "invocation_id": run.name,
        "uid": os.geteuid(),
        "dir": {"dev": info.st_dev, "ino": info.st_ino},
        "owner": {"pid": pid, "start_time": start_time, "boot_id": boot_id},
        "child": None,
        "state": state,
        "created_at": 0.0,
    }
    (run / "lease.json").write_text(json.dumps(payload))
    (run / "lease.lock").touch(mode=0o600)



def leased(
    root: Path, name: str, *, pid: int, start_time: int | None, boot_id: str | None,
    ns: str = pytest_scratch.RUNS_DIRNAME, size: int = 8192,
) -> Path:
    run = unowned(root, name, ns=ns)
    (run / "scratch" / "base" / "payload").write_bytes(b"x" * size)
    write_lease(run, pid=pid, start_time=start_time, boot_id=boot_id)
    return run



def current_boot() -> str:
    boot = task_scratch.current_boot_id()
    assert boot is not None
    return boot



def tree_state(*roots: Path) -> list[tuple]:
    """Every node's identity and timestamps except atime: a dry run must leave all of it alone."""
    state = []
    for root in roots:
        for path in sorted([root, *root.rglob("*")]):
            info = path.lstat()
            state.append((str(path), info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
    return state



def allocated(path: Path) -> int:
    seen, total = set(), 0
    for node in [path, *path.rglob("*")]:
        info = node.lstat()
        if (info.st_dev, info.st_ino) not in seen:
            seen.add((info.st_dev, info.st_ino))
            total += info.st_blocks * 512
    return total



def row_for(report: dict, path: Path) -> dict:
    return next(row for row in report["rows"] if row["name"] == path.name)



def test_old_unowned_malformed_and_completed_runs_dry_run_then_delete(area, monkeypatch):
    root, args = area
    runs = [
        unowned(root, "unowned"), malformed(root, "malformed"), *completed(root, 1, ns=pytest_scratch.RUNS_DIRNAME),
        unowned(root, "legacy-unowned", ns=task_scratch.NAMESPACE_DIRNAME), *completed(root, 2),
    ]
    later(monkeypatch, 2 * HOUR + 1)
    before = tree_state(root, args["temp_root"])
    sizes = {path.name: allocated(path) for path in runs}

    dry = tmp_sweep.sweep(**args)["pytest_retention"]
    assert tree_state(root, args["temp_root"]) == before  # zero effects
    assert not args["state_dir"].exists()
    assert {row["name"]: row["decision"] for row in dry["rows"]} == dict.fromkeys(sizes, "would_delete")
    assert {row["reason"] for row in dry["rows"]} == {"inactive_pytest_scratch"}
    assert dry["would_delete_bytes"] == sum(sizes.values()) > 0
    assert dry["deleted_bytes"] == 0 and dry["errors"] == 0

    report = tmp_sweep.sweep(**args, apply=True)
    retention = report["pytest_retention"]
    assert report["digest"] == tmp_sweep.plan_digest(report)
    assert {row["name"]: row["decision"] for row in retention["rows"]} == dict.fromkeys(sizes, "deleted")
    assert retention["deleted_entries"] == len(runs)
    assert retention["deleted_bytes"] == sum(sizes.values()) == dry["would_delete_bytes"]
    assert retention["errors"] == 0 and retention["remaining_runs"] == 0
    assert not any(path.exists() for path in runs)
    # No quarantine, no custody: nothing recoverable is left behind or recorded.
    assert not [path for path in root.rglob("*") if path.name.startswith(tmp_sweep.QUARANTINE_PREFIX)]
    assert tmp_sweep.Ledger(args["state_dir"]).entries()[0] == {}



def test_young_runs_are_retained_until_strictly_past_two_hours(area, monkeypatch):
    root, args = area
    run = unowned(root, "boundary")
    _bytes, _newest, changed, problem = tmp_sweep.tree_facts(run)
    assert problem is None
    assert row_for(tmp_sweep.sweep(**args, apply=True)["pytest_retention"], run)["reason"] == "too_young"
    # Exactly two hours after the newest change, in float arithmetic, is still young.
    boundary = changed + 2 * HOUR
    while boundary - changed > 2 * HOUR:
        boundary = math.nextafter(boundary, 0)
    while boundary - changed < 2 * HOUR:
        boundary = math.nextafter(boundary, math.inf)
    later(monkeypatch, 0, base=boundary)
    report = tmp_sweep.sweep(**args, apply=True)["pytest_retention"]
    assert row_for(report, run)["reason"] == "too_young" and run.is_dir()
    later(monkeypatch, 0, base=math.nextafter(boundary, math.inf))
    assert row_for(tmp_sweep.sweep(**args)["pytest_retention"], run)["decision"] == "would_delete"
    assert row_for(tmp_sweep.sweep(**args, apply=True)["pytest_retention"], run)["decision"] == "deleted"



def test_recently_extracted_old_mtime_files_keep_the_folder_young(area, monkeypatch):
    root, args = area
    run = unowned(root, "extracted")
    for path in [run, *run.rglob("*")]:
        os.utime(path, (1, 1), follow_symlinks=False)
    assert max(path.lstat().st_mtime for path in [run, *run.rglob("*")]) == 1
    later(monkeypatch, HOUR)
    row = row_for(tmp_sweep.sweep(**args, apply=True)["pytest_retention"], run)
    assert (row["decision"], row["reason"]) == ("preserve", "too_young")
    assert (run / "scratch" / "base" / "payload").is_file()



def test_symlinks_never_lead_outside_the_namespace(area, monkeypatch, tmp_path):
    root, args = area
    outside = tmp_path / "outside"
    (outside / "keep").mkdir(parents=True)
    (outside / "keep" / "data").write_text("outside data")
    ns = namespace(root)
    (ns / "linked-run").symlink_to(outside, target_is_directory=True)
    (ns / "stray-file").write_text("not a folder")
    run = unowned(root, "inner-link")
    (run / "scratch" / "escape").symlink_to(outside / "keep", target_is_directory=True)
    later(monkeypatch, 3 * HOUR)
    report = tmp_sweep.sweep(**args, apply=True)["pytest_retention"]
    reasons = {row["name"]: (row["decision"], row["reason"]) for row in report["rows"]}
    assert reasons["linked-run"] == ("preserve", "symlink_or_not_directory")
    assert reasons["stray-file"] == ("preserve", "symlink_or_not_directory")
    assert reasons["inner-link"] == ("deleted", "inactive_pytest_scratch")
    assert (outside / "keep" / "data").read_text() == "outside data"
    assert (ns / "linked-run").is_symlink() and (ns / "stray-file").is_file()



def test_symlinked_or_shared_namespace_is_untrusted(area, monkeypatch, tmp_path):
    root, args = area
    outside = tmp_path / "elsewhere"
    run = unowned(root, "real")
    (root / pytest_scratch.NAMESPACE / pytest_scratch.RUNS_DIRNAME).rename(outside)
    (root / pytest_scratch.NAMESPACE / pytest_scratch.RUNS_DIRNAME).symlink_to(outside, target_is_directory=True)
    shared = unowned(root, "shared", ns=task_scratch.NAMESPACE_DIRNAME)
    shared.parent.chmod(0o755)
    later(monkeypatch, 3 * HOUR)
    report = tmp_sweep.sweep(**args, apply=True)["pytest_retention"]
    assert report["by_reason"] == {"namespace_untrusted": 2} and report["errors"] == 2
    assert (outside / run.name / "scratch" / "base" / "payload").is_file()
    assert (shared / "scratch" / "base" / "payload").is_file()



@pytest.mark.parametrize("worktrees,reason", [("registered", "registered_worktree"), (None, "git_registration_unknown")])
def test_worktree_registration_still_preserves(area, monkeypatch, worktrees, reason):
    root, args = area
    run = unowned(root, "worktree")
    registered = {run / "scratch"} if worktrees else None
    monkeypatch.setattr(tmp_sweep, "registered_worktrees", lambda _: registered)
    later(monkeypatch, 3 * HOUR)
    assert row_for(tmp_sweep.sweep(**args, apply=True)["pytest_retention"], run)["reason"] == reason



def use_real_proc(monkeypatch) -> None:
    """Judge lease liveness against the real process table, never a fabricated one."""
    monkeypatch.setattr(pytest_retention, "PROC_ROOT", Path("/proc"))



@pytest.mark.parametrize("ns", [pytest_scratch.RUNS_DIRNAME, task_scratch.NAMESPACE_DIRNAME])
@pytest.mark.parametrize("fixture_git", [False, True])
def test_reaped_lease_owner_is_reclaimed_without_all_process_visibility(area, monkeypatch, ns, fixture_git):
    """Defect class #11409: an unreadable unrelated process blocked every reclaim.

    The lease names a real child that has exited; the run lock is free and the
    tree is old, so the run must be deleted against the real process table,
    whatever else runs on the host. Fixture Git metadata inside does not keep it.
    """
    root, args = area
    pid, start = reaped_pid()
    run = leased(root, "dead-owner", pid=pid, start_time=start, boot_id=current_boot(), ns=ns)
    if fixture_git:
        subprocess.run(["git", "init", str(run / "scratch" / "fixture")], check=True, capture_output=True, timeout=10)
    expected_bytes = allocated(run)
    later(monkeypatch, 3 * HOUR)
    use_real_proc(monkeypatch)
    before = tree_state(root, args["temp_root"])
    dry = tmp_sweep.sweep(**args)["pytest_retention"]
    assert tree_state(root, args["temp_root"]) == before  # a dry run changes nothing
    assert (row_for(dry, run)["decision"], row_for(dry, run)["reason"]) == (
        "would_delete", "inactive_pytest_scratch",
    )
    assert dry["would_delete_bytes"] == expected_bytes > 0
    # No snapshot of other processes is taken; the lease proof admits the deletion.
    report = tmp_sweep.sweep(**args, apply=True)["pytest_retention"]
    row = row_for(report, run)
    assert row["decision"] == "deleted" and not run.exists()
    assert report["reclaimed_entries"] == 1
    assert report["reclaimed_bytes"] == report["deleted_bytes"] == expected_bytes > 0



def test_live_lease_owner_preserves_the_run(area, monkeypatch):
    """Defect class: a lease owner that is alive on the same boot was reclaimed."""
    root, args = area
    child, pid, start = live_pid()
    try:
        run = leased(root, "live-owner", pid=pid, start_time=start, boot_id=current_boot())
        later(monkeypatch, 3 * HOUR)
        use_real_proc(monkeypatch)
        row = row_for(tmp_sweep.sweep(**args, apply=True)["pytest_retention"], run)
        assert (row["decision"], row["reason"]) == ("preserve", "live_owner")
        assert (run / "scratch" / "base" / "payload").read_bytes() == b"x" * 8192
    finally:
        child.terminate()
        child.wait(timeout=10)



def test_held_lease_lock_preserves_a_dead_owner_run(area, monkeypatch):
    """Defect class: a competing writer won a race while its lock was held."""
    root, args = area
    pid, start = reaped_pid()
    run = leased(root, "lock-held", pid=pid, start_time=start, boot_id=current_boot())
    later(monkeypatch, 3 * HOUR)
    with (run / "lease.lock").open("r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        row = row_for(tmp_sweep.sweep(**args, apply=True)["pytest_retention"], run)
    assert (row["decision"], row["reason"]) == ("preserve", "in_use")
    assert run.is_dir()



def test_unreadable_lease_is_kept(area, monkeypatch):
    """Defect class: metadata that could not be read was treated as no owner at all."""
    if os.geteuid() == 0:
        pytest.skip("root can read a mode-000 lease")
    root, args = area
    pid, start = reaped_pid()
    run = leased(root, "unreadable-lease", pid=pid, start_time=start, boot_id=current_boot())
    (run / "lease.json").chmod(0o000)
    later(monkeypatch, 3 * HOUR)
    row = row_for(tmp_sweep.sweep(**args, apply=True)["pytest_retention"], run)
    assert (row["decision"], row["reason"]) == ("preserve", "lease_unreadable")
    assert run.is_dir()



def test_reused_pid_with_a_different_start_time_is_reclaimed(area, monkeypatch):
    """Defect class: a recycled numeric pid kept an abandoned run alive forever."""
    root, args = area
    child, pid, start = live_pid()
    try:
        run = leased(root, "reused-pid", pid=pid, start_time=start + 1, boot_id=current_boot())
        later(monkeypatch, 3 * HOUR)
        use_real_proc(monkeypatch)
        row = row_for(tmp_sweep.sweep(**args)["pytest_retention"], run)
        assert (row["decision"], row["reason"]) == ("would_delete", "inactive_pytest_scratch")
        report = tmp_sweep.sweep(**args, apply=True)["pytest_retention"]
        assert row_for(report, run)["decision"] == "deleted" and not run.exists()
    finally:
        child.terminate()
        child.wait(timeout=10)





def test_a_future_dated_fixture_file_does_not_keep_a_dead_owner_run_forever(area, monkeypatch):
    """Defect class: a test that dates a file into the future made its abandoned run too_young on every sweep."""
    root, args = area
    pid, start = reaped_pid()
    run = leased(root, "future-mtime", pid=pid, start_time=start, boot_id=current_boot())
    future = run / "scratch" / "base" / "payload"
    os.utime(future, (9_000_000_000, 9_000_000_000))
    later(monkeypatch, 3 * HOUR)
    use_real_proc(monkeypatch)
    report = tmp_sweep.sweep(**args, apply=True)["pytest_retention"]
    assert row_for(report, run)["decision"] == "deleted" and not run.exists()

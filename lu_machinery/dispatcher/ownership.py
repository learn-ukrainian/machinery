"""The owned-path rule for a dispatched write task's delivery, and its commit and push check.

Settlement (``worker._enforce_delivery``) is the authority: it refuses a delivery whose commits
touch a path outside the task's owned paths. The commit and push hooks run the same rule early,
so the worker learns of an outside path while it can still drop it (#12113). The hooks never
replace settlement. Coordinator grants add attributable scope for an honest but mistaken
worker; they do not authenticate a principal. Exact-head independent review, CI and the
merge queue remain the landing backstop.

Inside the write boundary the task store is hidden, so the hook does not call ``store.read``.
The launcher projects this task's ownership fields to ``/run/lu-write-boundary/ownership.json``
(read-only) and refreshes that file from the live record when the record is written, which is
how a grant issued during the run becomes visible. Any other broker path, a missing view, or a
view for another task fails closed. Outside the boundary the hook still reads the task store.

Usage from the tracked hooks, with the dispatched task's id in ``LEARN_UKRAINIAN_DISPATCH_TASK_ID``::

    python -m scripts.dispatcher.ownership --worktree ROOT staged
    python -m scripts.dispatcher.ownership --worktree ROOT push < git-pre-push-updates
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

TASK_ID_ENV = "LEARN_UKRAINIAN_DISPATCH_TASK_ID"
_ZERO = frozenset("0")
# The write boundary's broker socket and the ownership view beside it. The checker accepts the view
# only from this fixed path: a worker-chosen ``LU_WRITE_BOUNDARY_BROKER`` must not point the check
# at a file the worker can write. Keep these equal to ``write_boundary._BROKER_INSIDE``.
_BOUNDARY_BROKER = "/run/lu-write-boundary/broker.sock"
_BOUNDARY_VIEW = Path("/run/lu-write-boundary/ownership.json")
# What the commit-time check reads. The rest of the task record (the brief, grants' reasons, other
# tasks) stays on the host. A task-store ``*.json`` would be ingested as a record by the ledger scan.
_VIEW_FIELDS = (
    "task_id", "mode", "owned_paths", "run_nonce", "worktree_path", "worktree_base_ref",
    "worktree_base_sha", "worktree_start_sha",
)
_GRANT_FIELDS = ("task_id", "run_nonce", "grant_id", "owned_paths")
_VIEW_LIMIT = 1 << 20


def ownership_view(record: dict) -> dict:
    """The fields the commit-time check needs, and nothing else from the task record."""
    view = {key: record[key] for key in _VIEW_FIELDS if key in record}
    if "owned_path_grants" not in record:
        return view
    grants = record["owned_path_grants"]
    if not isinstance(grants, list):
        view["owned_path_grants"] = grants  # The check fails closed on a grant list that is not a list.
        return view
    view["owned_path_grants"] = [
        grant if not isinstance(grant, dict) else {key: grant[key] for key in _GRANT_FIELDS if key in grant}
        for grant in grants
    ]
    return view


def write_ownership_view(path: Path, record: dict) -> None:
    """Replace ``path`` with this record's ownership view. The parent directory must already exist."""
    directory = path.parent
    if directory.is_symlink() or not directory.is_dir() or path.is_symlink():
        raise ValueError("ownership view path is not a regular file in a directory")
    payload = (json.dumps(ownership_view(record), sort_keys=True) + "\n").encode()
    temporary = directory / f".ownership.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    try:
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def project_launch_view(task_id: str, directory: Path) -> None:
    """Project the live record into the boundary directory before the sandbox starts.

    No record yet means nothing is projected: a later commit with a task id fails closed.
    The task lock is held across the read and the write so a grant cannot be overwritten by a
    stale copy.
    """
    from lu_machinery.dispatcher import store

    with store.task_state_lock(store.record_path(task_id)):
        try:
            record = store.read(task_id)
        except (OSError, ValueError):
            return
        if isinstance(record, dict):
            write_ownership_view(directory / "ownership.json", record)


def refresh_running_boundary_view(record: dict) -> None:
    """Update the view of a boundary that is already running. No directory means no running boundary."""
    task_id = record.get("task_id")
    if not isinstance(task_id, str):
        return
    from lu_machinery.dispatcher.store import record_path

    try:
        record_path(task_id)  # Refuse an id that is not safe as a directory name.
    except ValueError:
        return
    override = os.environ.get("LU_SCRATCH_ROOT", "").strip()
    from lu_machinery.common.scratch import DEFAULT_SCRATCH_ROOT

    roots = [Path(override)] if override else [DEFAULT_SCRATCH_ROOT, Path(tempfile.gettempdir()) / "lu-scratch"]
    for root in roots:
        directory = root / f"write-boundary-{task_id}" / "boundary"
        view = directory / "ownership.json"
        if directory.is_symlink() or not directory.is_dir() or view.is_symlink():
            continue
        write_ownership_view(view, record)


def _read_boundary_view(task_id: str) -> dict:
    """The projected view. Missing, oversized or for another task fails closed."""
    try:
        descriptor = os.open(_BOUNDARY_VIEW, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ValueError("task not found") from exc
    try:
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(_VIEW_LIMIT + 1)
    except OSError as exc:
        raise ValueError("task not found") from exc
    if len(raw) > _VIEW_LIMIT:
        raise ValueError("invalid task record identity")
    try:
        record = json.loads(raw)
    except ValueError as exc:
        raise ValueError("task not found") from exc
    if not isinstance(record, dict) or record.get("task_id") != task_id:
        raise ValueError("invalid task record identity")
    return record


def _load_record(task_id: str) -> dict:
    """The ownership record: the projected view inside the write boundary, the task store outside it."""
    broker = os.environ.get("LU_WRITE_BOUNDARY_BROKER")
    if broker:
        # A different socket path is not this boundary. Do not follow it and do not fall through to
        # the store: unknown ownership fails closed.
        if broker != _BOUNDARY_BROKER:
            raise ValueError("invalid task record identity")
        return _read_boundary_view(task_id)
    from lu_machinery.dispatcher import store

    record = store.read(task_id)
    if not isinstance(record, dict):
        raise ValueError("invalid task record identity")
    return record


def effective_scope(record: dict) -> list[str]:
    """Original scope plus grants bound to this exact task run, using the shared matcher."""
    owned = list(record.get("owned_paths") or [])
    grants = record.get("owned_path_grants", [])
    if not isinstance(grants, list):
        raise ValueError("invalid owned-path grants")
    for grant in grants:
        if (not isinstance(grant, dict) or grant.get("task_id") != record.get("task_id")
                or grant.get("run_nonce") != record.get("run_nonce") or not grant.get("grant_id")
                or not isinstance(grant.get("owned_paths"), list) or not grant["owned_paths"]
                or any(not isinstance(path, str) or not path for path in grant["owned_paths"])):
            raise ValueError("invalid owned-path grant binding")
        owned.extend(grant["owned_paths"])
    # An empty original scope has always meant unrestricted delivery; an addition cannot narrow it.
    return list(dict.fromkeys(owned)) if record.get("owned_paths") else []


def grants_used(record: dict, paths: Iterable[str]) -> list[dict]:
    """Each delivered path owned only through a grant, with its approval evidence."""
    from lu_machinery.guardrails.delegate_ownership import path_is_owned

    effective_scope(record)  # Refuse malformed grants even when no path needs one.
    if not record.get("owned_paths"):
        return []
    return [dict(path=path, grant_id=grant["grant_id"])
            for path in sorted(set(paths)) if path and not path_is_owned(path, record.get("owned_paths") or [])
            for grant in record.get("owned_path_grants", []) if path_is_owned(path, grant["owned_paths"])]


def outside_owned(paths: Iterable[str], owned: Sequence[str]) -> list[str]:
    """The changed paths that no owned path covers, sorted and without repeats."""
    from lu_machinery.guardrails.delegate_ownership import path_is_owned

    return sorted(path for path in set(paths) if path and not path_is_owned(path, owned))


def delivered_paths(run: Callable[..., str], head_sha: str, base_sha: str, start_sha: str, *,
                    commits_expected: bool = False) -> list[str]:
    """Every path the task's own commits touch, as settlement reads them.

    The range is ``head`` minus everything reachable from the base or the task's start. Remerge
    diffs isolate edits made during a merge from clean upstream changes; older Git and octopus
    merges cannot prove the absence of resolution edits, so they are refused.
    """
    merge_base = run("merge-base", head_sha, base_sha)
    revision_range = (head_sha, "--not", merge_base, start_sha)
    if commits_expected and not run("rev-list", "--max-count=1", *revision_range):
        raise ValueError("empty ownership range with recorded commits ahead")
    version = tuple(int(part) for part in run("version").split()[2].split(".")[:2])
    remerge = version >= (2, 36)
    paths = run(
        "log", "--format=", "--name-only", "-z", "-r", "--no-renames", "--root",
        "--remerge-diff" if remerge else "--cc", *revision_range, strip=False,
    ).split("\0")
    if run("rev-list", "--min-parents=3" if remerge else "--min-parents=2", *revision_range):
        raise ValueError("merge resolution paths cannot be verified")
    return paths


def failure_detail(exc: Exception) -> str:
    """What stopped an ownership check: the failing Git step, or the check's own refusal."""
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"git {exc.cmd[1]} timed out"
    if isinstance(exc, subprocess.CalledProcessError):
        return f"git {exc.cmd[1]} failed"
    if isinstance(exc, ValueError):
        return str(exc)
    if isinstance(exc, KeyError):
        return f"record field {exc} missing"
    return type(exc).__name__


def _local_git(root: Path) -> Callable[..., str]:
    def run(*args: str, strip: bool = True) -> str:
        output = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                                check=True, timeout=30).stdout
        return output.strip() if strip else output

    return run


def _task_record(root: Path) -> dict | None:
    """The dispatched write task this worktree belongs to, or None when no owned-path rule applies."""
    task_id = os.environ.get(TASK_ID_ENV, "")
    if not task_id:
        return None
    record = _load_record(task_id)
    if record.get("task_id") != task_id:
        raise ValueError("invalid task record identity")
    if record.get("mode") not in {"read-only", "workspace-write"}:
        raise ValueError("invalid task mode")
    owned = record.get("owned_paths")
    if owned is not None and (not isinstance(owned, list) or
                              any(not isinstance(path, str) or not path for path in owned)):
        raise ValueError("invalid owned paths")
    effective_scope(record)  # Validate grants before any retained skip.
    from lu_machinery.guardrails.delegate_ownership import owned_path_matcher

    grants = record.get("owned_path_grants", [])
    for grant in grants:
        if (not isinstance(record.get("run_nonce"), str) or not record["run_nonce"] or
                not isinstance(grant["grant_id"], str)):
            raise ValueError("invalid owned-path grant identity")
    for path in [*(owned or []), *(path for grant in grants for path in grant["owned_paths"])]:
        if owned_path_matcher(path) is None:
            raise ValueError("invalid owned path")
    worktree = record.get("worktree_path")
    if not isinstance(worktree, str) or not worktree.strip() or not Path(worktree).is_absolute():
        raise ValueError("invalid worktree binding")
    bound = Path(worktree).resolve(strict=True)
    if not bound.is_dir():
        raise ValueError("worktree binding is not a directory")
    # Verify the stored binding independently of the committing repository's
    # inherited Git environment, including GIT_DIR and GIT_WORK_TREE.
    bound_root = subprocess.run(
        ["git", "-C", str(bound), "rev-parse", "--show-toplevel"],
        env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
        capture_output=True, text=True, check=True, timeout=30,
    ).stdout.rstrip("\n")
    if not Path(bound_root).is_absolute() or Path(bound_root).resolve(strict=True) != bound:
        raise ValueError("worktree binding is not a Git worktree root")
    # The task id is inherited by every process the worker starts; only its own worktree is bound.
    if bound != root.resolve(strict=True):
        return None
    if record.get("mode") == "read-only" or not record.get("owned_paths"):
        return None
    return record


def _staged_paths(run: Callable[..., str]) -> list[str]:
    # A merge commit's staged diff holds the clean upstream changes too; the push check reads the
    # merge's own resolution edits with a remerge diff instead.
    if Path(run("rev-parse", "--path-format=absolute", "--git-path", "MERGE_HEAD")).exists():
        return []
    return run("diff", "--cached", "--name-only", "-z", "--no-renames", strip=False).split("\0")


def _pushed_paths(run: Callable[..., str], record: dict, updates: str) -> list[str]:
    base_sha = run("rev-parse", "--verify", f"{record['worktree_base_ref']}^{{commit}}")
    start_sha = record.get("worktree_start_sha", record["worktree_base_sha"])
    paths: list[str] = []
    for line in updates.splitlines():
        fields = line.split()
        if len(fields) != 4 or not fields[2].startswith("refs/heads/") or set(fields[1]) <= _ZERO:
            continue
        paths += delivered_paths(run, fields[1], base_sha, start_sha)
    return paths


def _refusal(stage: str, record: dict, outside: list[str]) -> str:
    from lu_machinery.guardrails.delegate_ownership import sanitize_for_display
    from lu_machinery.secret_redactor import redact_text

    def shown(value: str) -> str:
        return sanitize_for_display(redact_text(value))

    action = "commit" if stage == "staged" else "push"
    drop = ("unstage and restore those paths, then commit again" if stage == "staged" else
            "rewrite the branch so no commit touches those paths; a revert commit still counts")
    lines = [
        f"owned-path check refused this {action} for task {shown(record['task_id'])}.",
        "Its commits change paths outside the task's owned paths:",
        *(f"  - {shown(path)}" for path in outside[:50]),
        *([f"  ... and {len(outside) - 50} more"] if len(outside) > 50 else []),
        "Effective owned paths: " + ", ".join(shown(path) for path in effective_scope(record)),
        "Settlement refuses any delivery that touches these paths. Resolve it one of two ways:",
        f"  1. Drop the change: {drop}.",
        "  2. Request an exact-path grant from the coordinator through Fleet Comms. Include the task id, "
        "run nonce, paths, reason and approval reference; wait for its durable grant before retrying.",
        "Never widen the owned paths yourself or bypass this hook.",
    ]
    if stage == "push":
        lines.append("If a listed path came from a base merge, run `git fetch origin` and push again.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--worktree", type=Path, required=True, help="the worktree being committed or pushed")
    parser.add_argument("stage", choices=("staged", "push"), help="what to check: the index, or pushed commits")
    args = parser.parse_args(argv)
    if not os.environ.get(TASK_ID_ENV):
        return 0
    try:
        record = _task_record(args.worktree)
        if record is None:
            return 0
        run = _local_git(args.worktree)
        paths = (_staged_paths(run) if args.stage == "staged" else
                 _pushed_paths(run, record, sys.stdin.read()))
        outside = outside_owned(paths, effective_scope(record))
        if outside:
            print(_refusal(args.stage, record, outside), file=sys.stderr)
    except Exception as exc:
        # Exception messages may contain record contents, paths or Git output. Keep this
        # boundary diagnostic fixed and single-line, including for unexpected failures.
        print(f"owned-path check could not run ({type(exc).__name__}: "
              "task record or ownership verification failed); refusing",
              file=sys.stderr)
        return 1
    return int(bool(outside))


if __name__ == "__main__":
    raise SystemExit(main())

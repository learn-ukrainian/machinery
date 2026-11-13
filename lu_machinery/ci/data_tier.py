"""Run the audited data-gated tests against the host's own data, read-only and in place.

Use the nightly systemd timer after installation, or run a no-report subset to
check the host. The tests run in a fresh code-only checkout of ``origin/main``;
the logical stores and the host data they read stay where they are and are
mounted read-only into the test process (bubblewrap), never snapshotted or
copied. The audited selection and known failures come from that checkout. This
module never edits the primary checkout or its data.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import cast, get_args

import pytest

from lu_machinery.ci._location import module_name
from lu_machinery.common import github_client, test_slots
from lu_machinery.common.repo_root import (
    ProductRootError,
    main_checkout_root,
    product_root,
    project_interpreter,
    state_root,
)
from lu_machinery.orchestration import pool_headroom, worktree_claims
from lu_machinery.orchestration.dispatch_isolation import _parse_bytes, build_scope_argv
from lu_machinery.storage.topology import STORE_FILES, StoreBinding, StoreId, resolve_store, unavailable_store_path

# The product checkout under test owns its audited selection and known failures.
SELECTION = PurePosixPath("scripts/ci/data_tier_selection.json")
BASELINE = PurePosixPath("scripts/ci/data_tier_known_failures.json")
ISSUE_TITLE = "[infra][tests] Nightly data-tier failures"
MIN_AVAILABLE_BYTES = 6 * 1024**3
# Free memory the nightly pytest child needs in lu-dispatch.slice and in the
# shared lu.slice pool, both measured without reclaimable file cache (#9975).
DISPATCH_HEADROOM_BYTES = 4 * 1024**3
RUN_BUDGET_SECONDS = 7 * 3600
PYTEST_WORKERS = 2
ISSUE_BODY_LIMIT = 60000
# Stores whose absence fails the run; every logical store in STORE_FILES is bound when present.
REQUIRED_STORES = ("sources", "vesum")
ATLAS = PurePosixPath("data/atlas.db")
# Gitignored host data the audited tests read at repository-relative paths. Each is mounted
# read-only from the primary checkout where the tested checkout lacks it; nothing is copied.
# Open-model data is not test data and is never exposed (DATATIER-INPLACE-1113).
EXPOSED_DATA = (
    "data/ua-gec",
    "data/textbook_chunks",
    "data/literary_texts",
    "data/external_articles",
    "data/corpus_audit",
    "data/lexicon",
    "site/public/lexicon",
    "site/src/data/lexicon-manifest.json",
)
ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9])/(?!/)[^\s\]\[),:;]+")
NETWORK_ADDRESS = re.compile(r"https?://[^\s\]\[),:;]+|(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")


class DataTierError(RuntimeError):
    """Nightly data-tier run cannot produce trustworthy results."""


def safe_text(value: str) -> str:
    """Keep private host paths out of persisted receipts and GitHub bodies."""
    value = value.replace(socket.gethostname(), "<host>")
    value = NETWORK_ADDRESS.sub("<network-address>", value)
    return ABSOLUTE_PATH.sub("<host-path>", value)


def command(argv: list[str], *, cwd: Path, timeout: int = 120, text: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=text, timeout=timeout, check=False)
    if result.returncode:
        stderr = result.stderr if text else result.stderr.decode(errors="replace")
        raise DataTierError(f"{argv[0]} failed ({result.returncode}): {safe_text(stderr.strip())}")
    return result


def run_process_group(
    argv: list[str],
    *,
    timeout: int,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    stdout: object | None = None,
) -> int:
    """Wait in the foreground and stop descendants if a whole-run bound expires."""
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=stdout,
        stderr=subprocess.STDOUT if stdout else None,
        start_new_session=True,
    )
    try:
        return process.wait(timeout=timeout)
    except BaseException as error:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
        if isinstance(error, subprocess.TimeoutExpired):
            raise DataTierError(f"command timed out after {timeout} seconds") from error
        raise


def primary_checkout() -> Path:
    """Git primary of the selected product, from Git metadata, never this file's location."""
    try:
        primary = main_checkout_root(product_root())
    except ProductRootError as exc:
        raise DataTierError(str(exc)) from exc
    if not (primary / ".git").is_dir():
        raise DataTierError("Git common directory does not identify a primary checkout")
    return primary


def _product_json(checkout: Path, relative: PurePosixPath) -> tuple[dict, str]:
    """Read one product-owned runner input from the tested checkout; never a packaged fallback."""
    try:
        raw = (checkout / relative).read_bytes()
    except FileNotFoundError as error:
        raise DataTierError(f"tested checkout has no {relative}") from error
    try:
        value = json.loads(raw)
    except ValueError as error:
        raise DataTierError(f"{relative} in the tested checkout is not valid JSON") from error
    if not isinstance(value, dict):
        raise DataTierError(f"{relative} in the tested checkout is not a JSON object")
    return value, hashlib.sha256(raw).hexdigest()


def load_selection(checkout: Path, expected_sha256: str | None = None) -> tuple[dict, str]:
    """The audited selection from the tested checkout and its digest.

    The pytest child passes the digest the parent validated, so both use the same bytes.
    """
    selection, digest = _product_json(checkout, SELECTION)
    if expected_sha256 is not None and digest != expected_sha256:
        raise DataTierError(f"{SELECTION} changed after the runner validated it")
    try:
        ids, files = selection["nodeids"], selection["files"]
        compatible = (
            selection["schema"] == 1
            and isinstance(ids, list)
            and isinstance(files, list)
            and len(ids) == selection["class_c_merge_group"]
            and len(ids) == len(set(ids))
        )
    except (KeyError, TypeError) as error:
        raise DataTierError(f"{SELECTION} lacks a schema-1 field: {error}") from error
    if not compatible:
        raise DataTierError("data-tier selection count or uniqueness differs from the audited class C")
    if not all(isinstance(path, str) and path.startswith("tests/") and path.endswith(".py") for path in files):
        raise DataTierError("data-tier selection contains an invalid test file")
    return selection, digest


def load_known_failures(checkout: Path) -> dict[str, int]:
    baseline, _digest = _product_json(checkout, BASELINE)
    known = baseline.get("known_failures")
    if not isinstance(known, dict) or not all(
        isinstance(key, str) and isinstance(value, int) for key, value in known.items()
    ):
        raise DataTierError(f"{BASELINE} has no known_failures map of node id to issue number")
    return known


def available_memory() -> int:
    for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    raise DataTierError("MemAvailable is unavailable")


def require_memory() -> None:
    available = available_memory()
    if available < MIN_AVAILABLE_BYTES:
        raise DataTierError(f"MemAvailable {available // 1024**2} MiB is below the 6 GiB floor")
    slice_state = command(
        [
            "systemctl",
            "--user",
            "show",
            "-p",
            "LoadState,ActiveState,MemoryCurrent,MemoryMax,ControlGroup",
            "lu-dispatch.slice",
        ],
        cwd=state_root(),
    ).stdout
    properties = dict(line.split("=", 1) for line in slice_state.splitlines() if "=" in line)
    if properties.get("LoadState") != "loaded":
        raise DataTierError("lu-dispatch.slice is unavailable")
    if properties.get("ActiveState") != "inactive":
        require_dispatch_slice_headroom(properties)
    require_pool_headroom()


def require_dispatch_slice_headroom(properties: dict[str, str]) -> None:
    """lu-dispatch.slice headroom without reclaimable file cache (#9975).

    Without a readable ``memory.stat`` the raw ``MemoryCurrent`` is used, which
    can only overstate use.
    """
    current = _parse_bytes(properties.get("MemoryCurrent"))
    maximum = _parse_bytes(properties.get("MemoryMax"))
    if current is None or (maximum is None and properties.get("MemoryMax") != "infinity"):
        raise DataTierError("lu-dispatch.slice memory headroom is unknown")
    directory = pool_headroom.cgroup_dir(properties.get("ControlGroup"))
    cache = pool_headroom.file_cache_bytes(directory) if directory is not None else None
    if cache is None:
        print("data-tier: lu-dispatch.slice file cache unknown; using raw MemoryCurrent", file=sys.stderr)
    else:
        current = max(current - cache, 0)
    if maximum is not None and maximum - current < DISPATCH_HEADROOM_BYTES:
        raise DataTierError("lu-dispatch.slice has less than 4 GiB headroom")


def require_pool_headroom() -> None:
    """Refuse when the shared lu.slice pool cannot hold the run (#9975); skip without cgroup files."""
    pool = pool_headroom.check_pool(DISPATCH_HEADROOM_BYTES)
    if pool.skipped is not None:
        print(f"data-tier: {safe_text(pool.clause())}", file=sys.stderr)
        return
    if pool.memory is not None and pool.memory.limit is None:
        print(
            "data-tier: lu.slice has no memory.high or memory.max limit; pool headroom not enforced",
            file=sys.stderr,
        )
        return
    if not pool.fits:
        raise DataTierError(pool.failure())


def prune_stale_worktrees(primary: Path, removed: list[worktree_claims.WorktreeRemoval] | None = None) -> None:
    """Reap abandoned nightly checkouts only when no nightly scope is active.

    Each removal is appended to ``removed`` as it happens, so a caller keeps the earlier receipts when a
    later checkout is refused and this raises.
    """
    parent = primary / ".worktrees" / "data-tier"
    if not parent.is_dir():
        return
    scopes = command(
        ["systemctl", "--user", "list-units", "--type=scope", "--state=active", "--no-legend", "lu-data-tier-*"],
        cwd=state_root(),
    ).stdout
    if scopes.strip():
        raise DataTierError("a previous data-tier scope is still active; cleanup withheld")
    for checkout in parent.glob("run-*"):
        if checkout.is_symlink() or not checkout.is_dir() or not re.fullmatch(r"run-[0-9a-f]{32}", checkout.name):
            continue
        # Leave a concurrent or recently interrupted runner's checkout alone.
        if time.time() - checkout.stat().st_mtime < 9 * 3600:
            continue
        removal = remove_test_worktree(primary, checkout)
        if removed is not None:
            removed.append(removal)


def remove_test_worktree(primary: Path, checkout: Path) -> worktree_claims.WorktreeRemoval:
    removal = worktree_claims.remove_unclaimed_worktree(
        checkout,
        repo_root=primary,
        reason="data-tier checkout cleanup",
        owner_task_id=None,
        force=True,
    )
    if removal.action != "removed":
        detail = f"{removal.reason}: {removal.error}" if removal.error else removal.reason
        raise DataTierError(f"checkout cleanup {removal.action}: {detail}")
    return removal


def discard_run_output(checkout: Path) -> dict[str, int]:
    """Delete the ignored and untracked files this run put in its own checkout (#12528).

    The checkout is created fresh from ``origin/main`` by :func:`make_test_worktree` and verified clean, so
    every ignored or untracked file in it was written by this run: the empty mount points of the read-only
    host data and anything the tests wrote. It runs after the test namespace has exited, so the mount points
    are empty here and the primary's data is never in reach. That is the run's declared disposable output.
    Left in place, the guarded remover would see an unrecorded checkout it cannot attribute and copy the
    bytes into ``batch_state/preserved/`` every night. The test report, log and summary the
    run needs for diagnosis live in ``batch_state/data-tier/``, outside the checkout, and are kept.
    Returns the file count and byte total discarded, for the run summary.
    """
    # ``git clean -n`` quotes and escapes non-ASCII names and has no ``-z`` form, so list the same set
    # (everything untracked or ignored) with ``ls-files -z``, whose NUL-delimited names are never quoted.
    # A nested repository is listed as one ``name/`` entry, which the directory walk below counts.
    # Read it as bytes: text mode would reject non-UTF-8 names and turn CR into LF. ``fsdecode`` round-trips them.
    listing = command(["git", "ls-files", "-z", "--others"], cwd=checkout, timeout=300, text=False).stdout
    if isinstance(listing, str):  # callers that substitute ``command`` with text output
        listing = os.fsencode(listing)
    names = [os.fsdecode(name) for name in listing.split(b"\0") if name]
    total = 0
    count = 0
    for name in names:
        target = checkout / name
        if target.is_symlink() or not target.is_dir():
            count += 1
            with contextlib.suppress(OSError):
                total += target.lstat().st_size
            continue
        for member in target.rglob("*"):
            if member.is_file() or member.is_symlink():
                count += 1
                with contextlib.suppress(OSError):
                    total += member.lstat().st_size
    command(["git", "clean", "-ffdx"], cwd=checkout, timeout=900)
    return {"files": count, "bytes": total}


def stop_scope(unit: str) -> None:
    result = subprocess.run(
        ["systemctl", "--user", "show", "-p", "LoadState", unit],
        cwd=state_root(),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if "LoadState=not-found" in result.stdout:
        return
    if result.returncode:
        raise DataTierError(f"scope state unavailable: {safe_text(result.stderr)}")
    command(["systemctl", "--user", "stop", unit], cwd=state_root(), timeout=60)


def make_test_worktree(primary: Path) -> Path:
    # Register the linked checkout from the primary work tree. Passing
    # `--git-dir` while the cwd is `.worktrees/data-tier` makes Git treat that
    # directory as the toplevel, so the reference-transaction delegator looks
    # for `.githooks` there and aborts the ref update before checkout.
    # Later freshness commands run in the new checkout, not in primary.
    parent = primary / ".worktrees" / "data-tier"
    parent.mkdir(parents=True, exist_ok=True)
    path = parent / f"run-{uuid.uuid4().hex}"
    command(
        ["git", "worktree", "add", "--detach", str(path), "origin/main"],
        cwd=primary,
        timeout=300,
    )
    try:
        command(["git", "fetch", "--no-tags", "origin", "main"], cwd=path, timeout=300)
        command(["git", "reset", "--hard", "origin/main"], cwd=path, timeout=300)
        command(["git", "sparse-checkout", "disable"], cwd=path, timeout=300)
        if command(["git", "status", "--porcelain"], cwd=path).stdout.strip():
            raise DataTierError("new data-tier checkout is not clean")
    except BaseException:
        remove_test_worktree(primary, path)
        raise
    return path


def _file_generation(path: Path) -> dict[str, object]:
    """Identify a store file without its location: its name, size and modification time."""
    info = path.stat()
    return {
        "file": path.name,
        "size": info.st_size,
        "modified": dt.datetime.fromtimestamp(info.st_mtime, dt.UTC).isoformat(timespec="seconds"),
    }


def bind_stores(primary: Path) -> tuple[dict[str, str], list[Path], dict[str, object], list[str]]:
    """Bind each logical store in place through the storage contract.

    Returns the store environment, the store directories to mount read-only, the data
    generation used and the missing required stores. Each override names the resolved file,
    so the whole run reads one Sources generation even if the selector switches meanwhile;
    a missing store is bound to the refused placeholder, never to another location.
    """
    environment: dict[str, str] = {}
    directories: list[Path] = []
    generation: dict[str, object] = {}
    missing: list[str] = []
    for store in get_args(StoreId):
        filename, key = STORE_FILES[store]
        binding = resolve_store(store, primary)
        if isinstance(binding, StoreBinding):
            environment[key] = str(binding.path)
            directories.append(binding.path.parent)
            generation[store] = _file_generation(binding.path)
        else:
            environment[key] = str(unavailable_store_path(store))
            generation[store] = f"missing: {binding.reason}"
            if store in REQUIRED_STORES:
                missing.append(filename)
    atlas = primary / ATLAS
    if atlas.is_file() and not atlas.is_symlink():
        generation["atlas"] = _file_generation(atlas)
    else:
        generation["atlas"] = "missing"
        missing.append(ATLAS.name)
    return environment, directories, generation, missing


def exposed_data(primary: Path, checkout: Path) -> list[tuple[Path, Path]]:
    """Primary data to mount read-only into the checkout, as (source, target) pairs.

    A path the tested checkout already has (tracked at its commit) is left alone; a
    directory both have is descended so only what the checkout lacks is mounted.
    """
    mounts: list[tuple[Path, Path]] = []

    def expose(relative: PurePosixPath) -> None:
        source, target = primary / relative, checkout / relative
        if source.is_symlink() or not source.exists():
            return
        if not target.exists() and not target.is_symlink():
            mounts.append((source, target))
        elif source.is_dir() and target.is_dir() and not target.is_symlink():
            for child in sorted(source.iterdir()):
                expose(relative / child.name)

    for relative in EXPOSED_DATA:
        expose(PurePosixPath(relative))
    return mounts


def link_atlas(primary: Path, checkout: Path) -> None:
    """Expose Atlas at its legacy repository-relative path as a link to the host file.

    SQLite follows the link and names the WAL and shared-memory sidecars after the host
    file, so readers use the live sidecars in the read-only host directory as the host's
    own writers create and remove them. Separately mounted sidecars would pin old inodes.
    """
    source, target = primary / ATLAS, checkout / ATLAS
    if source.is_file() and not source.is_symlink() and not target.exists() and not target.is_symlink():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(source)


# Host paths that hold same-user control endpoints: the user and system D-Bus, the systemd
# managers' private sockets and other runtime sockets under /run, and terminal multiplexer
# and agent sockets under /tmp. A read-only mount does not stop a connect() to a socket, so
# each is replaced by an empty private tmpfs in the namespace, which is also its only /tmp.
HIDDEN_ROOTS = (Path("/run"), Path("/tmp"))
CHILD_TEMP = Path("/tmp")
CHILD_SCRATCH_ROOT = Path("/tmp/lu")


def read_only_namespace(read_only: list[Path], mounts: list[tuple[Path, Path]], writable: list[Path]) -> list[str]:
    """bubblewrap argv prefix: the whole host read-only, writable only where the run writes.

    The kernel enforces this for the pytest child and every process it starts; an environment
    override alone would leave the stores writable. Writes are allowed only in ``writable``
    (the checkout and the child's output directory), ``/dev`` and the private tmpfs over each
    of ``HIDDEN_ROOTS``, so nothing the parent or another host process reads later, such as
    Git metadata or user configuration, can be planted. ``read_only`` paths are mounted again
    read-only above that, including the checkout's ``.git`` link. The namespace has no network
    (no loopback service or abstract socket of the host), and bubblewrap sets
    no-new-privileges, so setuid helpers cannot write for it either.
    """
    binary = shutil.which("bwrap")
    if binary is None:
        raise DataTierError("bubblewrap (bwrap) is required to mount host data read-only")
    argv = [binary, "--ro-bind", "/", "/", "--dev-bind", "/dev", "/dev", "--unshare-net"]
    for hidden in HIDDEN_ROOTS:
        argv.extend(["--tmpfs", str(hidden)])
    for path in writable:
        argv.extend(["--bind", str(path), str(path)])
    roots: list[Path] = []
    for path in sorted(set(read_only), key=lambda item: len(item.parts)):
        if not any(path.is_relative_to(root) for root in roots):
            roots.append(path)
            argv.extend(["--ro-bind", str(path), str(path)])
    for source, target in mounts:
        argv.extend(["--ro-bind", str(source), str(target)])
    return [*argv, "--die-with-parent", "--"]


def require_read_only(paths: list[str]) -> None:
    """Refuse to run tests unless the host data is read-only and host control endpoints are out of reach."""
    if not paths:
        raise DataTierError("no read-only host data paths were declared for the test process")
    writable = [path for path in paths if not os.statvfs(path).f_flag & os.ST_RDONLY]
    if writable:
        raise DataTierError(f"{len(writable)} host data path(s) are writable inside the test namespace")
    if any(any(root.iterdir()) for root in HIDDEN_ROOTS):
        raise DataTierError("host runtime or temporary directories are visible inside the test namespace")
    if [name for _index, name in socket.if_nameindex()] != ["lo"]:
        raise DataTierError("the test namespace shares a network with the host")


def bulk_status(checkout: Path, project_python: Path) -> tuple[str | None, str]:
    # The project interpreter, -I and the fixed module are an argv list; no shell runs.
    result = subprocess.run(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        [str(project_python), "-I", "-m", "lu_machinery.storage", "status", "--json"],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if result.returncode:
        return None, "storage status unavailable"
    try:
        bulk = json.loads(result.stdout)["bulk_root"]
    except (ValueError, KeyError, TypeError):
        return None, "storage status invalid"
    if not bulk.get("available") or not bulk.get("path"):
        return None, str(bulk.get("reason") or "bulk root unavailable")
    return str(bulk["path"]), str(bulk.get("reason") or "available")


def _key(nodeid: str) -> str:
    return "sha256:" + hashlib.sha256(nodeid.encode()).hexdigest()


class SelectionPlugin:
    def __init__(self, selection: dict, only: str | None, bulk_reason: str | None, collected: Path) -> None:
        self.selection = selection
        self.only = only
        self.bulk_reason = bulk_reason
        self.collected = collected
        self.selected_nodeids: list[str] = []
        self.bulk_skipped: list[str] = []
        self.collection_skips: list[tuple[str, str]] = []
        self.collection_errors: list[tuple[str, str]] = []

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.failed:
            self.collection_errors.append((report.nodeid, safe_text(str(report.longrepr))))
        if report.skipped and report.nodeid in self.selection["nodeids"]:
            detail = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
            self.collection_skips.append((report.nodeid, safe_text(str(detail))))

    def pytest_collection_modifyitems(self, items: list[pytest.Item], config: pytest.Config) -> None:
        wanted = set(self.selection["nodeids"])
        bulk = set(self.selection.get("bulk_nodeids", []))
        selected, deselected = [], []
        for item in items:
            nodeid = item.nodeid
            key = _key(nodeid)
            file = nodeid.split("::", 1)[0]
            keep = (nodeid in wanted or key in wanted or file in wanted) and (self.only is None or file == self.only)
            if any(item.get_closest_marker(marker) for marker in ("live_network", "live_github", "postgres")):
                keep = False
            if keep:
                if self.bulk_reason and (nodeid in bulk or key in bulk):
                    self.bulk_skipped.append(nodeid)
                    deselected.append(item)
                else:
                    selected.append(item)
            else:
                deselected.append(item)
        items[:] = selected
        config.hook.pytest_deselected(items=deselected)
        self.selected_nodeids = [item.nodeid for item in selected]
        self.collected.write_text(json.dumps([safe_text(item.nodeid) for item in selected]), encoding="utf-8")


def pytest_child(args: argparse.Namespace) -> int:
    require_read_only(args.read_only)
    selection, _digest = load_selection(Path.cwd(), args.selection_sha256)
    plugin = SelectionPlugin(selection, args.only, args.bulk_reason, Path(args.collected))
    files = [args.only] if args.only else selection["files"]
    collection_status = pytest.main(
        ["--collect-only", "--continue-on-collection-errors", "-q", *files], plugins=[plugin]
    )
    if collection_status not in (0, 1, 5) or (collection_status == 1 and not plugin.collection_errors):
        return int(collection_status)
    if (
        not plugin.selected_nodeids
        and not plugin.bulk_skipped
        and not plugin.collection_skips
        and not plugin.collection_errors
    ):
        raise DataTierError("the audited selection collected no tests")
    if plugin.selected_nodeids:
        status = run_process_group(
            [
                sys.executable,
                "-m",
                "pytest",
                "-n",
                str(PYTEST_WORKERS),
                "--require-data",
                "--timeout=180",
                "--timeout-method=thread",
                "-q",
                "-r",
                "s",
                f"--junitxml={args.junit}",
                *plugin.selected_nodeids,
            ],
            timeout=21600,
        )
    else:
        ET.ElementTree(ET.Element("testsuite")).write(args.junit, encoding="utf-8", xml_declaration=True)
        status = 0
    if plugin.bulk_skipped or plugin.collection_skips or plugin.collection_errors:
        tree = ET.parse(args.junit)
        suite = next(tree.iter("testsuite"))
        for nodeid in plugin.bulk_skipped:
            case = ET.SubElement(suite, "testcase", classname="data_tier", name=safe_text(nodeid))
            ET.SubElement(case, "skipped", message=f"bulk root unavailable: {safe_text(args.bulk_reason)}")
        for nodeid, reason in plugin.collection_skips:
            case = ET.SubElement(suite, "testcase", classname="data_tier", name=safe_text(nodeid))
            ET.SubElement(case, "skipped", message=reason)
        for nodeid, reason in plugin.collection_errors:
            case = ET.SubElement(suite, "testcase", classname="data_tier", name=safe_text(nodeid))
            ET.SubElement(case, "error", message=reason)
        skipped = len(plugin.bulk_skipped) + len(plugin.collection_skips)
        added = skipped + len(plugin.collection_errors)
        for element in {tree.getroot(), suite}:
            if element.tag in {"testsuite", "testsuites"}:
                element.set("tests", str(int(element.get("tests", "0")) + added))
                element.set("skipped", str(int(element.get("skipped", "0")) + skipped))
                element.set("errors", str(int(element.get("errors", "0")) + len(plugin.collection_errors)))
        tree.write(args.junit, encoding="utf-8", xml_declaration=True)
    return status or (1 if plugin.collection_errors else 0)


def read_child_output(path: Path) -> bytes | None:
    """Read a file the sandboxed child wrote, never through a link it may have planted there."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise DataTierError(f"child output {path.name} is not a regular file") from error
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise DataTierError(f"child output {path.name} is not a regular file")
        return stream.read()


def sanitize_junit(data: bytes, target: Path) -> None:
    """Write the child's JUnit, without host paths, to a new file the child could not reach."""
    tree = ET.ElementTree(ET.fromstring(data))
    for element in tree.iter():
        element.attrib.update({key: safe_text(value) for key, value in element.attrib.items()})
        if element.text:
            element.text = safe_text(element.text)
        if element.tail:
            element.tail = safe_text(element.tail)
    with target.open("xb") as stream:
        tree.write(stream, encoding="utf-8", xml_declaration=True)


def junit_summary(path: Path) -> dict:
    counts = Counter()
    reasons = Counter()
    failures = []
    for case in ET.parse(path).iter("testcase"):
        counts["ran"] += 1
        classname = case.attrib.get("classname", "")
        parts = classname.split(".")
        stem = next((index for index, part in enumerate(parts) if part.startswith("test_")), None)
        if stem is None:
            nodeid = case.attrib.get("name", "")
        else:
            file = "/".join(parts[: stem + 1]) + ".py"
            nodeid = "::".join([file, *parts[stem + 1 :], case.attrib.get("name", "")])
        if case.find("failure") is not None or case.find("error") is not None:
            counts["failed"] += 1
            failures.append(safe_text(nodeid))
        elif (skipped := case.find("skipped")) is not None:
            counts["skipped"] += 1
            reasons[safe_text(skipped.attrib.get("message", "unspecified").splitlines()[0])] += 1
        else:
            counts["passed"] += 1
    return {
        "ran": counts["ran"],
        "passed": counts["passed"],
        "failed": counts["failed"],
        "skipped": counts["skipped"],
        "skip_reasons": dict(reasons.most_common()),
        "failing_tests": sorted(set(failures)),
    }


def known_issue(nodeid: str, baseline: dict[str, int]) -> int | None:
    """Return the known issue for a failing node id, exact id first, then its whole test function.

    A baseline key without a ``[...]`` parametrization suffix covers every parametrization of that test.
    """
    if nodeid in baseline:
        return baseline[nodeid]
    if nodeid.endswith("]") and "[" in nodeid:
        return baseline.get(nodeid.split("[", 1)[0])
    return None


def issue_body(summary: dict, baseline: dict[str, int]) -> str:
    lines = [
        "Nightly data-tier run failed.",
        "",
        f"Run: `{safe_text(summary['run_key'])}` at `{safe_text(summary['main_sha'])}`.",
        f"Ran {summary['ran']}; passed {summary['passed']}; failed {summary['failed']}; skipped {summary['skipped']}.",
    ]
    lines.extend(["", f"Runner errors ({len(summary.get('runner_errors', []))}):"])
    lines.extend(f"- {safe_text(reason)}" for reason in summary.get("runner_errors", []))
    lines.extend(["", f"Missing databases ({len(summary.get('missing_databases', []))}):"])
    lines.extend(f"- {safe_text(name)}" for name in summary.get("missing_databases", []))
    lines.extend(["", f"Failing tests ({len(summary['failing_tests'])} total):"])
    for nodeid in summary["failing_tests"]:
        issue = known_issue(nodeid, baseline)
        lines.append(f"- `{safe_text(nodeid)}`" + (f" — known issue #{issue}" if issue else " — new"))
    lines.extend(["", "Skipped by reason:"])
    lines.extend(f"- {count}: {safe_text(reason)}" for reason, count in summary["skip_reasons"].items())
    footer = "\nFull details: local run JUnit, log and JSON summary (named by run key).\n"
    body = ""
    for index, line in enumerate(lines):
        clipped = line[:1024] + (" … [detail truncated]" if len(line) > 1024 else "")
        if len(body) + len(clipped) + len(footer) + 100 > ISSUE_BODY_LIMIT:
            body += f"\n[Truncated: {len(lines) - index} remaining report lines; see local receipts.]\n"
            break
        body += clipped + "\n"
    return body + footer


def run_failed(summary: dict) -> bool:
    return bool(
        summary["failed"]
        or summary.get("pytest_exit")
        or summary.get("runner_errors")
        or summary.get("missing_databases")
    )


def _gh(args: list[str], *, input_text: str | None = None) -> str:
    result = github_client.run(["gh", *args], input=input_text, capture_output=True, text=True, check=False, timeout=90)
    observation = getattr(result, "github_result", None)
    if observation is not None and (observation.stale or observation.error == "github_rate_limited"):
        raise github_client.GitHubRateLimited(observation.reset_at)
    if result.returncode:
        raise DataTierError(f"gh failed ({result.returncode}): {safe_text(result.stderr.strip())}")
    return result.stdout


def report(summary: dict, baseline: dict[str, int]) -> str:
    issues = json.loads(
        _gh(
            [
                "issue",
                "list",
                "--state",
                "all",
                "--search",
                "Nightly data-tier failures in:title",
                "--limit",
                "100",
                "--json",
                "number,title,state",
            ]
        )
    )
    matching = [issue for issue in issues if issue["title"] == ISSUE_TITLE]
    if len(matching) > 1:
        raise DataTierError("multiple data-tier failure issues have the stable title")
    issue = matching[0] if matching else None
    if run_failed(summary):
        body = issue_body(summary, baseline)
        if issue:
            if issue["state"] != "OPEN":
                _gh(["issue", "reopen", str(issue["number"])])
            _gh(["issue", "edit", str(issue["number"]), "--body-file", "-"], input_text=body)
            return f"updated issue #{issue['number']}"
        created = _gh(
            ["issue", "create", "--title", ISSUE_TITLE, "--label", "infra", "--body-file", "-"], input_text=body
        )
        return f"created {created.strip()}"
    if issue and issue["state"] == "OPEN":
        comments = json.loads(_gh(["issue", "view", str(issue["number"]), "--json", "comments"]))["comments"]
        marker = f"Data-tier clean run `{summary['run_key']}`"
        if not any(marker in comment["body"] for comment in comments):
            _gh(
                [
                    "issue",
                    "comment",
                    str(issue["number"]),
                    "--body",
                    marker + f": {summary['passed']} passed, {summary['skipped']} skipped.",
                ]
            )
        _gh(["issue", "close", str(issue["number"])])
        return f"closed issue #{issue['number']} after clean run"
    return "no issue change"


def run(args: argparse.Namespace) -> int:
    started = dt.datetime.now(dt.UTC)
    run_key = started.strftime("%Y%m%dT%H%M%S") + f"-{uuid.uuid4().hex[:8]}Z"
    deadline = time.monotonic() + RUN_BUDGET_SECONDS
    summary = {
        "run_key": run_key,
        "main_sha": "unavailable",
        "ran": 0,
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "collected": 0,
        "pytest_exit": None,
        "failing_tests": [],
        "skip_reasons": {},
        "runner_errors": [],
        "missing_databases": [],
        "data_generation": {},
    }
    baseline: dict[str, int] = {}
    primary = checkout = output_dir = child_dir = log = scope_unit = None
    slots: test_slots.Hold | None = None
    scope_may_exist = False
    try:
        if args.only and not args.no_report:
            raise DataTierError("a subset requires --no-report")
        primary = primary_checkout()
        output_dir = state_root() / "batch_state" / "data-tier"
        output_dir.mkdir(parents=True, exist_ok=True)
        junit = output_dir / f"{run_key}.junit.xml"
        collected = output_dir / f"{run_key}.collected.json"
        log = output_dir / f"{run_key}.log"
        # The only host directory the child may write besides its checkout; the parent adopts
        # its files without following links and removes it.
        child_dir = output_dir / f"{run_key}.child"
        child_dir.mkdir(mode=0o700)
        require_memory()
        try:
            project_python = project_interpreter(primary)
        except FileNotFoundError as error:
            raise DataTierError("shared project interpreter unavailable") from error
        if not project_python.is_file():
            raise DataTierError("shared project interpreter unavailable")
        prune_stale_worktrees(primary)
        checkout = make_test_worktree(primary)
        summary["main_sha"] = command(["git", "rev-parse", "HEAD"], cwd=checkout).stdout.strip()
        baseline = load_known_failures(checkout)
        selection, selection_sha256 = load_selection(checkout)
        summary["selection_sha256"] = selection_sha256
        if args.only and args.only not in selection["files"]:
            raise DataTierError("--only must name a file in the audited selection")
        store_environment, store_directories, generation, missing = bind_stores(primary)
        summary["data_generation"], summary["missing_databases"] = generation, missing
        mounts = exposed_data(primary, checkout)
        link_atlas(primary, checkout)
        bulk_root, bulk_reason = bulk_status(checkout, project_python)
        summary["bulk_status"] = "available" if bulk_root else f"unavailable: {safe_text(bulk_reason)}"
        require_memory()
        # -I: the checkout cwd is the pytest root, not this module's import path. The unit does not set PYTHONUNBUFFERED.
        child = [
            str(project_python),
            "-I",
            "-m",
            "lu_machinery.ci.data_tier",
            "pytest-child",
            "--junit",
            str(child_dir / "junit.xml"),
            "--collected",
            str(child_dir / "collected.json"),
        ]
        if args.only:
            child.extend(["--only", args.only])
        if not bulk_root:
            child.extend(["--bulk-reason", bulk_reason])
        # The stores, the primary data directory, the bulk root and every mounted data path are
        # read-only for the child and everything it starts; its outputs must lie outside them.
        read_only = [*store_directories, primary / "data", *(source for source, _target in mounts)]
        if bulk_root:
            read_only.append(Path(bulk_root))
        if any(path.is_relative_to(root) for path in (output_dir, checkout) for root in [*read_only, *HIDDEN_ROOTS]):
            raise DataTierError("run outputs must lie outside the read-only host data and the hidden roots")
        # The checkout's Git link stays read-only: the parent runs Git in the checkout afterwards.
        read_only.append(checkout / ".git")
        child.extend(["--selection-sha256", selection_sha256])
        verified = [Path("/"), primary, output_dir, *read_only, *(target for _source, target in mounts)]
        for path in verified:
            child.extend(["--read-only", str(path)])
        namespace = read_only_namespace(read_only, mounts, [checkout, child_dir])
        # The namespace shows the slot pool's root-owned ancestors under an unmapped owner, so the child
        # cannot take test slots itself: this process holds its workers' slots for the run instead.
        if test_slots.enabled():
            slots = test_slots.acquire(PYTEST_WORKERS, kind="pytest", task_id=f"data-tier-{run_key}")
        remaining = int(deadline - time.monotonic())
        if remaining <= 0:
            raise DataTierError("whole-run budget exhausted before pytest")
        scope_unit = f"lu-data-tier-{run_key}.scope"
        scope = build_scope_argv(["nice", "-n", "10", *namespace, *child], unit=scope_unit)
        environment = os.environ.copy()
        environment[test_slots.ENABLE_ENV] = "off"
        environment["TMPDIR"] = str(CHILD_TEMP)
        environment["LU_SCRATCH_ROOT"] = str(CHILD_SCRATCH_ROOT)
        # Every logical store is named explicitly, a missing one by its refused placeholder.
        environment.update(store_environment)
        if bulk_root:
            environment["LU_BULK_ROOT"] = bulk_root
        else:
            environment.pop("LU_BULK_ROOT", None)
        with log.open("w", encoding="utf-8") as stream:
            scope_may_exist = True
            pytest_exit = run_process_group(scope, cwd=checkout, env=environment, stdout=stream, timeout=remaining)
        summary["pytest_exit"] = pytest_exit
        junit_data = read_child_output(child_dir / "junit.xml")
        if junit_data is None:
            raise DataTierError(f"pytest produced no JUnit (exit {pytest_exit}); inspect local run log")
        sanitize_junit(junit_data, junit)
        summary.update(junit_summary(junit))
        collected_data = read_child_output(child_dir / "collected.json")
        if collected_data is not None:
            with collected.open("xb") as receipt:
                receipt.write(collected_data)
        summary.update(
            {
                "run_key": run_key,
                "pytest_exit": pytest_exit,
                "selection_cases": selection["class_c_merge_group"],
                "collected": len(json.loads(collected_data)) if collected_data is not None else 0,
            }
        )
        if pytest_exit and not summary["failed"]:
            summary["runner_errors"].append(f"pytest exited {pytest_exit} without testcase failures")
    except Exception as error:
        summary["runner_errors"].append(safe_text(str(error)))
    finally:
        # The scope is outside the service cgroup: stop it before removing its
        # checkout, including when SIGTERM interrupts the waiting parent.
        scope_stopped = True
        if scope_unit and scope_may_exist:
            try:
                stop_scope(scope_unit)
            except Exception as error:
                scope_stopped = False
                summary["runner_errors"].append(f"scope cleanup failed: {safe_text(str(error))}")
        if slots is not None and scope_stopped:
            slots.release()
        if checkout and scope_stopped:
            try:
                summary["discarded_output"] = discard_run_output(checkout)
            except Exception as error:
                # The guarded remover below then keeps whatever is left in a verified copy.
                summary["runner_errors"].append(f"output discard failed: {safe_text(str(error))}")
            try:
                remove_test_worktree(primary, checkout)
            except Exception as error:
                summary["runner_errors"].append(f"checkout cleanup failed: {safe_text(str(error))}")
        if child_dir and child_dir.exists() and scope_stopped:
            try:
                shutil.rmtree(child_dir)
            except Exception as error:
                cast(list[str], summary["runner_errors"]).append(
                    f"child output cleanup failed: {safe_text(str(error))}"
                )
        if log and log.exists():
            try:
                log.write_text(safe_text(log.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")
            except Exception as error:
                summary["runner_errors"].append(f"log sanitization failed: {safe_text(str(error))}")

    def persist() -> None:
        if output_dir:
            try:
                (output_dir / f"{run_key}.summary.json").write_text(
                    json.dumps(summary, indent=2, sort_keys=True) + "\n"
                )
            except Exception as error:
                summary["runner_errors"].append(f"summary write failed: {safe_text(str(error))}")

    persist()
    if not args.no_report:
        try:
            print(report(summary, baseline))
        except github_client.GitHubRateLimited:
            # The timer records the reset. Persist the test result first,
            # and never repeat a partially successful publishing sequence.
            persist()
            raise
        except Exception as error:
            summary["runner_errors"].append(f"GitHub reporting failed: {safe_text(str(error))}")
            persist()
    print(json.dumps(summary, sort_keys=True))
    return 1 if run_failed(summary) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the audited class-C data-tier tests from a fresh origin/main worktree against the host's\n"
            "stores and data, mounted read-only in place (bubblewrap); nothing is snapshotted or copied.\n"
            "The selection and known failures are read from that checkout's scripts/ci/.\n"
            "Use for the nightly host run; use --only with --no-report for a bounded manual check."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            f"  <project-python> -m {module_name(__spec__, __file__)} run\n"
            f"  <project-python> -m {module_name(__spec__, __file__)} run --only tests/test_citation_resolution_invariant.py --no-report\n"
            "Outputs: JUnit, run log, collection receipt and JSON summary under batch_state/data-tier/.\n"
            "Exit codes: 0 = tests passed; 1 = tests failed or run incomplete; 2 = invalid usage.\n"
            "Related: issues #9229, #9218 and #8403."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="Run the audited selection and optionally report to GitHub.")
    run_parser.add_argument(
        "--only", help="Run one selected test file for a manual smoke check (default: full selection)."
    )
    run_parser.add_argument(
        "--no-report", action="store_true", help="Do not create or update a GitHub issue (default: report)."
    )
    child = sub.add_parser("pytest-child", help="Internal subprocess executed inside the dispatch slice.")
    child.add_argument("--junit", required=True, help="JUnit output file path.")
    child.add_argument("--collected", required=True, help="Selected collection receipt path.")
    child.add_argument("--only", help="Selected file path for a manual subset.")
    child.add_argument("--bulk-reason", help="Host bulk-root unavailability reason.")
    child.add_argument(
        "--selection-sha256", required=True, help="Digest of the selection the parent validated in this checkout."
    )
    child.add_argument(
        "--read-only",
        action="append",
        default=[],
        help="Host data path that must be read-only here (repeatable; default: none).",
    )
    return parser


@github_client.timer
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    def interrupted(signum: int, _frame: object) -> None:
        raise DataTierError(f"run interrupted by signal {signum}")

    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        return pytest_child(args) if args.command == "pytest-child" else run(args)
    except (DataTierError, OSError, subprocess.TimeoutExpired) as error:
        print(f"data-tier: {safe_text(str(error))}", file=sys.stderr)
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())

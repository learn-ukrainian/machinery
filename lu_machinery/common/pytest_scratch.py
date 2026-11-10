"""Give ordinary pytest runs invocation ownership for the scheduled sweep (#10831).

When the session ends, whatever its outcome, the run's scratch is removed.
Set ``LU_KEEP_PYTEST_SCRATCH=1`` to keep it for inspection; a kept run, or one
whose process was killed, is reclaimed by ``lu_machinery.hygiene.pytest_retention``
once its owner is dead, its lock is free and it has been idle for two hours.
The owner holds the run's lease lock shared for its lifetime, and so does
every xdist worker or nested session that inherits the run's payload as
``TMPDIR``, so the run outlives a killed owner while any of them still runs.

A reviewer sandbox mounts the shared scratch root read-only (#11317). The root
is then still verified, and refused if a symlink, foreign-owned or writable by
group or others, but the run itself is allocated in a fresh owner-only
directory under the sandbox's writable temp: ``TMPDIR``, else shared memory.
``TMPDIR`` is used only if no other user can replace any directory on its path
(each owned by this user or root, and writable by others only when sticky);
shared memory only if others may not rename entries in it.

Once that directory is verified and held open, every entry of the run (its
directory, ``runs``, the run, the lease files and ``scratch``) is created
relative to a held descriptor, never by path, so a replaced directory cannot
redirect a write. The run is named by path only when complete; if that path
does not reach the held run, the candidate is removed through the descriptors
and the next one is tried.

``TMPDIR`` is carried byte for byte from the environment to the allocation:
nothing strips, expands, normalizes or resolves it, because each of those can
name a directory other than the one supplied. A value that cannot be carried
that way (empty, relative, holding a NUL or an unencodable character, or too
long for the kernel to resolve whole) is skipped for the next candidate.

An explicit ``--basetemp`` places the session's fixtures, nothing else, and
never inside a directory the nightly data backup reads (#12153): a pytest base
temp holds absolute ``*current`` symlinks, which that backup refuses, so one
run there fails every later backup. Such a base temp is refused before anything
is created, and the message names the managed scratch to use instead.

The run is the session's only temp root. Every session that is not an xdist
worker gets one, with or without an explicit base temp. An explicit
``--basetemp`` or ``cache_dir`` must resolve, as pytest resolves it, inside the
scratch root or inside the managed run this process inherited (a nested
session); anywhere else is refused before anything is created. Without one,
pytest's cache goes into the run beside its payload, so neither a checkout nor
``batch_state`` gets one. For the session, ``TMPDIR`` is the run's payload,
Claude Code started directly by a test gets a configuration directory inside
the run (``CLAUDE_CONFIG_DIR``), never the operator's, and Python writes no
bytecode (``PYTHONDONTWRITEBYTECODE``); test breadcrumbs go into the run unless
the caller names their directory (``PYTEST_BREADCRUMB_DIR``). All of these
reach xdist workers and child processes.

A terminated session still removes its run: ``SIGTERM`` and ``SIGHUP``, where
not ignored, are handled for the outermost session in the process from before
its run exists until the run is removed. While tests run they become
``KeyboardInterrupt``, so pytest unwinds and runs its cleanups (exit status 2);
earlier they are raised at session start, and later only recorded, since the
removal is already under way (see ``_Termination``). The first signal disarms
both, so a second one ends the process at once.
Only ``SIGKILL`` or a crash leaves the run, for the scheduled sweep.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import secrets
import signal
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Generator
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from lu_machinery.common.acp_runtime_lock import process_start_time
from lu_machinery.common.scratch import ensure_scratch_root, resolve_scratch_root
from lu_machinery.common.task_scratch import (
    LEASE_FILENAME,
    LEASE_SCHEMA_VERSION,
    LOCK_FILENAME,
    PAYLOAD_DIRNAME,
    TaskScratch,
    TaskScratchError,
    _rmdir_held,
    _verify_private_dir,
    _write_lease_at,
    allocate,
    current_boot_id,
)
from lu_machinery.common.task_scratch import NAMESPACE_DIRNAME as LEASE_KIND

NAMESPACE = "pytest-runs"
RUNS_DIRNAME = "runs"
PRIVATE_MODE = 0o700
FALLBACK_PREFIX = "lu-pytest-"
# Set to 1 to keep the run's scratch after the session for inspection.
KEEP_ENV_VAR = "LU_KEEP_PYTEST_SCRATCH"
# Beside the payload in the run, so neither tempfile users nor fixtures see them.
CACHE_DIRNAME = "pytest-cache"
CLAUDE_CONFIG_DIRNAME = "claude-config"
CLAUDE_CONFIG_ENV_VAR = "CLAUDE_CONFIG_DIR"
NO_BYTECODE_ENV_VAR = "PYTHONDONTWRITEBYTECODE"
# The suite conftest's per-worker test breadcrumbs; an unset value means the working directory.
BREADCRUMB_ENV_VAR = "PYTEST_BREADCRUMB_DIR"
BREADCRUMB_DIRNAME = "breadcrumbs"
PYTEST_DEFAULT_CACHE_DIR = ".pytest_cache"
# Termination signals that unwind the owning session so its cleanups remove the run.
TERMINATION_SIGNALS = (signal.SIGTERM, signal.SIGHUP)
# Top-level directories of a checkout that scripts/config/backup-allowlist.json
# keeps; scripts/backup-data.sh reads everything beneath them.
BACKED_UP_ROOTS = (".agent", ".claude", "batch_state", "data")
SHARED_MEMORY_TEMP = Path("/dev/shm")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# A read-only mount or a denied write. Any other failure stays loud.
_UNWRITABLE_ERRNOS = frozenset({errno.EROFS, errno.EACCES})
_LOCK_JOIN_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
# The inherited TMPDIR names no managed run: no lock file there, or not a plain one.
_NOT_A_RUN_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP})
# Shared holds on the runs this process works inside, by lock identity, kept until it exits.
_JOINED: dict[tuple[int, int], int] = {}


def _is_unwritable(exc: OSError) -> bool:
    return exc.errno in _UNWRITABLE_ERRNOS


def _not_private(root: Path) -> TaskScratchError:
    return TaskScratchError(f"pytest scratch root is group- or other-writable and cannot be made private: {root}")


def _private_shared_root() -> Path | None:
    """Return the verified shared pytest root, or None when it is safe but this sandbox cannot write it."""
    root = ensure_scratch_root() / NAMESPACE
    try:
        os.mkdir(root, PRIVATE_MODE)
    except FileExistsError:
        pass
    except OSError as exc:
        if not _is_unwritable(exc) or os.path.lexists(root):
            raise
        return None  # No root exists to verify, and none can be made here.
    checked = _verify_private_dir(root, what="pytest scratch root")
    try:
        fd = os.open(root, _DIR_FLAGS)
    except OSError as exc:
        # A read-only mount still opens for reading: this is the root's own mode, never a fallback.
        if stat.S_IMODE(checked.st_mode) & 0o022:
            raise _not_private(root) from exc
        raise
    try:
        # Recheck what the descriptor holds: the path may have been replaced since the check.
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) != (checked.st_dev, checked.st_ino):
            raise TaskScratchError(f"pytest scratch root changed during verification: {root}")
        if st.st_uid != os.geteuid():
            raise TaskScratchError(f"pytest scratch root is owned by uid {st.st_uid}, not {os.geteuid()}: {root}")
        mode = stat.S_IMODE(st.st_mode)
        if mode != PRIVATE_MODE:
            # The pre-push gate also verifies ancestors of its test fixtures. Do not
            # let a collaborative umask create a group-writable pytest parent.
            try:
                os.fchmod(fd, PRIVATE_MODE)
            except OSError as exc:
                if not _is_unwritable(exc):
                    raise
                if mode & 0o022:
                    raise _not_private(root) from exc
                return None
    finally:
        os.close(fd)
    return root


def _backed_up_root_holding(basetemp: str) -> Path | None:
    """The backed-up directory of a checkout that holds ``basetemp``, else None.

    Every ancestor with a ``.git`` directory counts as a checkout (a worktree's
    ``.git`` is a file), so a repository extraction kept inside ``.agent`` does
    not hide the primary checkout above it. The path is read as pytest reads it:
    ``..`` collapses first, as ``os.path.abspath`` does, and only then do symlinks
    resolve, so ``<link>/../bt`` names a sibling of the link, not of its target.
    """
    path = Path(os.path.realpath(os.path.abspath(basetemp)))
    for ancestor in path.parents:
        if not (ancestor / ".git").is_dir():
            continue
        top = path.relative_to(ancestor).parts[0]
        if top in BACKED_UP_ROOTS:
            return ancestor / top
    return None


def _others_can_rename_in(mode: int) -> bool:
    """Group or others may write the directory without the sticky bit, so may swap our entry in it."""
    return bool(mode & 0o022) and not mode & stat.S_ISVTX


def _replaceable_by_others(st: os.stat_result) -> bool:
    """Another user could rename or replace entries of this directory: it is theirs, or theirs to write."""
    return st.st_uid not in (0, os.geteuid()) or _others_can_rename_in(st.st_mode)


def _open_unreplaceable(path: str) -> int | None:
    """Open ``path`` from ``/`` one component at a time; None if another user could replace any of it.

    Owner-only mode on ``path`` does not protect its name in its parent, so every
    directory from ``/`` down must be unreplaceable. ``O_NOFOLLOW`` refuses a symlink.
    The components are walked as supplied: normalizing first would drop a symlink
    that a following ``..`` steps out of, so ``..`` is refused instead. The whole
    value must then resolve, as the kernel reads it, to the directory walked.
    """
    parts = [part for part in path.split("/") if part]
    if ".." in parts:
        return None
    fd = os.open("/", _DIR_FLAGS)
    try:
        for part in parts:
            if _replaceable_by_others(os.fstat(fd)):
                os.close(fd)
                return None
            child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        walked = os.fstat(fd)
        # Allocation names the directory by this very string; a value too long to resolve whole raises here.
        named = os.stat(path, follow_symlinks=False)
        if _replaceable_by_others(walked) or (named.st_dev, named.st_ino) != (walked.st_dev, walked.st_ino):
            os.close(fd)
            return None
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_shared_memory(path: str) -> int | None:
    """Open the provisioned shared-memory temp; None if others may rename entries in it."""
    fd = os.open(path, _DIR_FLAGS)
    if _others_can_rename_in(os.fstat(fd).st_mode):
        os.close(fd)
        return None
    return fd


def _verbatim_absolute(value: str) -> str | None:
    """``value`` unchanged if the OS can take it as one absolute path exactly as written, else None."""
    if not value.startswith("/") or "\0" in value:
        return None
    try:
        os.fsencode(value)
    except UnicodeEncodeError:
        return None
    return value


def _fallback_bases() -> list[tuple[str, Callable[[], int | None]]]:
    """Each candidate temp with how to open it: TMPDIR only if its whole path, as supplied, is unreplaceable."""
    bases: list[tuple[str, Callable[[], int | None]]] = []
    tmpdir = _verbatim_absolute(os.environ.get("TMPDIR", ""))
    if tmpdir is not None:
        bases.append((tmpdir, partial(_open_unreplaceable, tmpdir)))
    shared_memory = os.fspath(SHARED_MEMORY_TEMP)
    bases.append((shared_memory, partial(_open_shared_memory, shared_memory)))
    return bases


class _Unusable(Exception):
    """This candidate cannot hold the run; the reason is reported if no candidate can."""


def _reason(exc: OSError) -> str:
    return errno.errorcode.get(exc.errno or 0, "error")


def _make_unique_dir(parent_fd: int, prefix: str) -> str:
    """Create a fresh 0700 directory with a random name below ``parent_fd``; return its name."""
    for _ in range(100):
        name = prefix + secrets.token_hex(4)
        try:
            os.mkdir(name, PRIVATE_MODE, dir_fd=parent_fd)
        except FileExistsError:
            continue
        return name
    raise FileExistsError(errno.EEXIST, "no unused private temp name")


def _make_private_dir(base_fd: int) -> str:
    return _make_unique_dir(base_fd, FALLBACK_PREFIX)


def _open_made(parent_fd: int, name: str, made: list[tuple[int, str, int]]) -> int:
    """Hold the directory just made as ``name`` below ``parent_fd``; refuse it unless it is ours and owner-only."""
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise _Unusable("changed") from exc
    st = os.fstat(fd)
    if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != PRIVATE_MODE:
        os.close(fd)
        raise _Unusable("changed")
    made.append((parent_fd, name, fd))
    return fd


def _names_held(path: str, fd: int) -> bool:
    """``path`` reaches, as the kernel resolves it now, the very directory ``fd`` holds."""
    try:
        named = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    held = os.fstat(fd)
    return (named.st_dev, named.st_ino) == (held.st_dev, held.st_ino)


def _discard(made: list[tuple[int, str, int]], run_fd: int | None, lock_fd: int | None) -> None:
    """Remove what this candidate made, through the held descriptors only, and release its lease."""
    if run_fd is not None:
        for entry in (LEASE_FILENAME, f".{LEASE_FILENAME}.tmp", LOCK_FILENAME):
            with contextlib.suppress(OSError):
                os.unlink(entry, dir_fd=run_fd)
    if lock_fd is not None:
        os.close(lock_fd)
    for parent_fd, name, fd in reversed(made):
        # Removed only while ``name`` still is the held directory; a replacement is left alone.
        with contextlib.suppress(OSError, TaskScratchError):
            _rmdir_held(parent_fd, name, held_fd=fd, held_st=os.fstat(fd))
        os.close(fd)


def _allocate_in_created(base: str, base_fd: int, name: str) -> TaskScratch:
    """Allocate the run inside ``base/name``, creating every entry relative to a held descriptor.

    Nothing is created by path, so replacing any directory on the way cannot move
    a write out of the directories held. Only once the run is complete is it
    named by path, for pytest; that path must reach the held run and payload, or
    the candidate is removed through the descriptors and refused.
    """
    made: list[tuple[int, str, int]] = []
    run_fd: int | None = None
    lock_fd: int | None = None
    try:
        private_fd = _open_made(base_fd, name, made)
        os.mkdir(RUNS_DIRNAME, PRIVATE_MODE, dir_fd=private_fd)
        runs_fd = _open_made(private_fd, RUNS_DIRNAME, made)
        run_name = _make_unique_dir(runs_fd, "pytest.")
        run_fd = _open_made(runs_fd, run_name, made)
        lock_fd = os.open(
            LOCK_FILENAME, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=run_fd
        )
        fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        held = os.fstat(run_fd)
        # The lease allocate() writes, so lease readers treat this run like any other.
        lease: dict[str, Any] = {
            "schema_version": LEASE_SCHEMA_VERSION,
            "kind": LEASE_KIND,
            "task_id": "pytest",
            "invocation_id": run_name,
            "uid": os.geteuid(),
            "dir": {"dev": held.st_dev, "ino": held.st_ino},
            "owner": {"pid": os.getpid(), "start_time": process_start_time(os.getpid()), "boot_id": current_boot_id()},
            "child": None,
            "state": "registered",
            "created_at": time.time(),
        }
        _write_lease_at(run_fd, lease)
        os.mkdir(PAYLOAD_DIRNAME, PRIVATE_MODE, dir_fd=run_fd)
        payload_fd = _open_made(run_fd, PAYLOAD_DIRNAME, made)
        # Appended to the supplied value as it is: the one path pytest and every tempfile user get.
        run = os.path.join(base, name, RUNS_DIRNAME, run_name)
        payload = os.path.join(run, PAYLOAD_DIRNAME)
        if not (_names_held(run, run_fd) and _names_held(payload, payload_fd)):
            raise _Unusable("changed")
    except BaseException as exc:
        _discard(made, run_fd, lock_fd)
        if isinstance(exc, OSError):
            raise _Unusable(_reason(exc)) from exc
        raise
    for _parent_fd, _name, fd in made:
        if fd != run_fd:
            os.close(fd)
    return TaskScratch(
        task_id="pytest",
        invocation_id=run_name,
        namespace=Path(os.path.dirname(run)),
        path=Path(run),
        payload_dir=Path(payload),
        lease=lease,
        _dir_fd=run_fd,
        _lock_fd=lock_fd,
    )


def _allocate_private_fallback() -> TaskScratch:
    """Allocate in a fresh owner-only directory below the first writable temp no other user can tamper with."""
    failures = []
    for base, open_base in _fallback_bases():
        try:
            base_fd = open_base()
        except OSError as exc:
            failures.append(_reason(exc))
            continue
        if base_fd is None:
            failures.append("replaceable")
            continue
        try:
            # A new 0700 directory with a random name: exclusive even in a shared sticky parent.
            name = _make_private_dir(base_fd)
            return _allocate_in_created(base, base_fd, name)
        except OSError as exc:
            failures.append(_reason(exc))
        except _Unusable as exc:
            failures.append(str(exc))
        finally:
            os.close(base_fd)
    raise TaskScratchError(
        "pytest scratch root is not writable and no private writable temp could be created "
        f"(TMPDIR, then shared memory): {', '.join(failures)}"
    )


def _release(scratch: TaskScratch) -> None:
    """Remove the run whatever the session's outcome, unless the caller asked to keep it.

    Failure evidence is pytest's report and junit, not the run's checkouts and
    virtual environments. A removal the remover refuses leaves the run for the
    scheduled sweep and never fails the session; one line on stderr names the
    run and the reason, so a kept run is never silent.
    """
    try:
        if os.environ.get(KEEP_ENV_VAR) != "1":
            try:
                scratch.remove()
            except (OSError, TaskScratchError) as exc:
                reason = _reason(exc) if isinstance(exc, OSError) else str(exc)
                print(f"pytest scratch: run {scratch.path} kept for the scheduled sweep: {reason}", file=sys.stderr)
    finally:
        scratch.preserve()


def _join_inherited_run() -> str | None:
    """Share the lifetime lock of the run whose payload this process inherited as ``TMPDIR``.

    xdist workers and nested sessions work inside their parent session's run.
    The sweep needs the run's lock exclusively, so holding it shared keeps the
    run for as long as any of them lives, even after its owner is gone. The
    descriptor is close-on-exec and is released when this process exits. A
    ``TMPDIR`` whose parent holds no regular lock file names no managed run and
    joins nothing. Any other failure to join stops the session before it uses
    that scratch: the run could be deleted under it. Returns the run directory
    held, or None when ``TMPDIR`` names no managed run.
    """
    inherited = os.environ.get("TMPDIR", "")
    if os.path.basename(inherited) != PAYLOAD_DIRNAME:
        return None
    lock = os.path.join(os.path.dirname(inherited), LOCK_FILENAME)
    try:
        fd = os.open(lock, _LOCK_JOIN_FLAGS)
    except OSError as exc:
        if exc.errno in _NOT_A_RUN_ERRNOS:
            return None
        raise pytest.UsageError(f"cannot hold the scratch run of TMPDIR={inherited}: {_reason(exc)}") from exc
    try:
        held = os.fstat(fd)
        if not stat.S_ISREG(held.st_mode):
            os.close(fd)
            return None
        if (held.st_dev, held.st_ino) in _JOINED:
            # Already held by this process (an in-process nested session).
            os.close(fd)
            return os.path.dirname(inherited)
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        raise pytest.UsageError(f"cannot hold the scratch run of TMPDIR={inherited}: {_reason(exc)}") from exc
    _JOINED[(held.st_dev, held.st_ino)] = fd
    return os.path.dirname(inherited)


def _inside(real: str, roots: list[str]) -> bool:
    """The resolved path ``real`` is one of ``roots`` or lies below one."""
    return any(real == root or real.startswith(root.rstrip("/") + "/") for root in roots)


def _basetemp_destination(value: str) -> str:
    """Where pytest puts ``--basetemp``: it collapses ``..`` first (``os.path.abspath``), then the links resolve."""
    return os.path.realpath(os.path.abspath(value))


def _allowed_roots(inherited_run: str | None) -> list[str]:
    """Where a caller may place its own base temp or cache: the scratch root, or the run it works inside."""
    roots = [os.path.realpath(resolve_scratch_root())]
    if inherited_run is not None:
        roots.append(os.path.realpath(inherited_run))
    return roots


def _outside_root(what: str, value: str, roots: list[str]) -> pytest.UsageError:
    return pytest.UsageError(
        f"pytest {what} {value} is outside the managed pytest scratch ({roots[0]}). Omit it: every session gets a run "
        "there that holds its base temp and cache and is removed when the session ends."
    )


def _explicit_cache_dir(config: pytest.Config) -> str | None:
    """Where a caller-configured cache directory lands, or None for pytest's default or no cache plugin.

    Resolved as the cache plugin resolves it (``_pytest.pathlib.resolve_from_str``):
    ``~`` and then environment variables expand, a relative value joins the root
    directory, and ``..`` is never collapsed, so each one steps out of whatever
    the component before it resolves to. ``os.path.realpath`` walks it the same way.
    """
    if not config.pluginmanager.has_plugin("cacheprovider"):
        return None
    value = str(config.getini("cache_dir"))
    if value == PYTEST_DEFAULT_CACHE_DIR:
        return None
    expanded = os.path.expandvars(os.path.expanduser(value))
    return os.path.realpath(os.path.join(config.rootpath, expanded))


def _set_cache_dir(config: pytest.Config, path: Path) -> None:
    """Point pytest's cache at ``path`` before the cache plugin reads it (this hook runs first)."""
    if config.pluginmanager.has_plugin("cacheprovider"):
        config.getini("cache_dir")  # fills pytest's per-config ini cache, which the cache plugin then reads
        config._inicache["cache_dir"] = str(path)


class _Termination:
    """A termination signal, as the session that owns the run sees it.

    Installed before the run is allocated, on each termination signal whose
    disposition is the default; an ignored one stays ignored. The first signal
    disarms them all, so a repeated one kills at once. What it does depends on
    the session's phase:

    - arming, from installation until ``pytest_sessionstart``: only recorded,
      then raised at session start, inside pytest's interrupt handling, once
      every plugin has configured and the run's removal is registered;
    - running: ``KeyboardInterrupt`` where the process is, which stops a blocked
      test. If a nested in-process session catches it instead, this session
      raises it again after the test that ran the nested one;
    - finishing, from the end of the run loop (or a failed collection) through
      session finish, unconfigure and every cleanup, until the run is removed:
      only recorded, because the removal is already under way. The session
      then ends with its own status.
    """

    def __init__(self) -> None:
        self.received: int | None = None
        self.phase = "arming"
        self.installed: list[int] = []

    def handle(self, signum: int, _frame: object) -> None:
        for each in self.installed:
            signal.signal(each, signal.SIG_DFL)
        self.received = signum
        if self.phase == "running":
            raise self.interrupt()

    def interrupt(self) -> KeyboardInterrupt:
        name = signal.Signals(self.received).name if self.received is not None else "termination"
        return KeyboardInterrupt(f"pytest scratch: {name} received; removing the run")

    def finish(self) -> None:
        self.phase = "finishing"

    def disarm(self) -> None:
        """Give back the default disposition of every signal this owner still handles."""
        for signum in self.installed:
            if signal.getsignal(signum) == self.handle:
                signal.signal(signum, signal.SIG_DFL)

    def raise_if_received(self) -> None:
        if self.received is not None and self.phase == "running":
            raise self.interrupt()

    @pytest.hookimpl(tryfirst=True)
    def pytest_sessionstart(self) -> None:
        if self.phase == "arming":
            self.phase = "running"
        self.raise_if_received()

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_logfinish(self) -> None:
        self.raise_if_received()

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_collection(self) -> Generator[None, None, None]:
        try:
            return (yield)
        except BaseException:
            self.finish()  # the session ends here; no run loop follows
            raise

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtestloop(self) -> Generator[None, None, None]:
        try:
            return (yield)
        finally:
            # Before any session-finish hook, including wrappers a later conftest registers ahead of this one.
            self.finish()

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_sessionfinish(self) -> Generator[None, None, None]:
        self.finish()
        return (yield)

    @pytest.hookimpl(tryfirst=True)
    def pytest_unconfigure(self) -> None:
        self.finish()


def _owning_termination() -> bool:
    """A session in this process already handles termination: this one is nested inside it."""
    return any(isinstance(getattr(signal.getsignal(signum), "__self__", None), _Termination)
               for signum in TERMINATION_SIGNALS)


def _arm_termination() -> _Termination | None:
    """Route default-disposition termination signals to a new owner; None when nested or none can be routed."""
    if threading.current_thread() is not threading.main_thread() or _owning_termination():
        return None
    termination = _Termination()
    for signum in TERMINATION_SIGNALS:
        if signal.getsignal(signum) is signal.SIG_DFL:
            signal.signal(signum, termination.handle)
            termination.installed.append(signum)
    return termination if termination.installed else None


def _set_session_env(config: pytest.Config, values: dict[str, str]) -> None:
    """Set environment values for the session (workers and children inherit them); restore them at the end."""
    previous = {name: os.environ.get(name) for name in values}
    os.environ.update(values)

    def restore() -> None:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    config.add_cleanup(restore)


def _allocate_run() -> TaskScratch:
    root = _private_shared_root()
    if root is not None:
        try:
            return allocate("pytest", root=root, namespace_dirname=RUNS_DIRNAME)
        except OSError as exc:
            if not _is_unwritable(exc):
                raise
    return _allocate_private_fallback()


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Give the session its run and settings; an explicit base temp inside the root keeps only its fixture directory."""
    inherited_run = _join_inherited_run()
    if hasattr(config, "workerinput"):
        return
    roots = _allowed_roots(inherited_run)
    explicit_cache = _explicit_cache_dir(config)
    if explicit_cache is not None and not _inside(explicit_cache, roots):
        raise _outside_root("cache directory", explicit_cache, roots)
    if config.option.basetemp is not None:
        held_by = _backed_up_root_holding(str(config.option.basetemp))
        if held_by is not None:
            raise pytest.UsageError(
                f"--basetemp {config.option.basetemp} is inside {held_by}, which the data backup reads "
                "and refuses for the absolute symlinks pytest leaves there. Omit --basetemp, or put it under "
                "$TMPDIR, the managed scratch."
            )
        if not _inside(_basetemp_destination(str(config.option.basetemp)), roots):
            raise _outside_root("base temp", str(config.option.basetemp), roots)
    # Armed before the run exists, so no signal can land between its allocation and its removal's registration.
    termination = _arm_termination()
    try:
        scratch = _allocate_run()
    except BaseException:
        if termination is not None:
            termination.disarm()
        raise
    if termination is not None:
        config.pluginmanager.register(termination, "lu-pytest-scratch-termination")
        # Registered before the removal, so it runs after it: the run stays protected until it is gone.
        config.add_cleanup(termination.disarm)
    if config.option.basetemp is None:
        # Pytest recreates --basetemp. Keep ownership metadata outside that payload.
        config.option.basetemp = str(scratch.payload_dir / "base")
    # Registered before every other cleanup but the disarm, so it runs after the TMPDIR restore and the rest.
    config.add_cleanup(partial(_release, scratch))
    if explicit_cache is None:
        _set_cache_dir(config, scratch.path / CACHE_DIRNAME)
    previous_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    config.add_cleanup(partial(setattr, sys, "dont_write_bytecode", previous_bytecode))
    session_env = {NO_BYTECODE_ENV_VAR: "1", CLAUDE_CONFIG_ENV_VAR: str(scratch.path / CLAUDE_CONFIG_DIRNAME)}
    if BREADCRUMB_ENV_VAR not in os.environ:
        # CI names its own breadcrumb directory to upload; otherwise they would accumulate in the working directory.
        session_env[BREADCRUMB_ENV_VAR] = str(scratch.path / BREADCRUMB_DIRNAME)
    _set_session_env(config, session_env)
    # Fixtures and ordinary tempfile users must share a containment root.
    # Workers inherit TMPDIR; this process may already have cached tempdir.
    previous_env = os.environ.get("TMPDIR")
    previous_tempdir = tempfile.tempdir
    os.environ["TMPDIR"] = str(scratch.payload_dir)
    tempfile.tempdir = str(scratch.payload_dir)

    def restore_tempdir() -> None:
        tempfile.tempdir = previous_tempdir
        if previous_env is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = previous_env

    config.add_cleanup(restore_tempdir)
    if termination is not None:
        # Registered last, so it runs first: every cleanup after it, the run's removal included, is finishing.
        config.add_cleanup(termination.finish)

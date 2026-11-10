"""Shared filesystem SQLite reader boundary (#9609, #9662).

Kept in scripts/lib so tooling and corpus readers share one small dependency,
without importing a curriculum, crawler, or application package.
"""

from __future__ import annotations

import errno
import fcntl
import functools
import importlib
import inspect
import json
import os
import re
import sqlite3
import stat
import threading
from collections.abc import Callable, Iterable
from contextlib import suppress
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol, Self
from urllib.parse import parse_qs, unquote, urlsplit

try:
    from ..common.repo_root import machinery_path, machinery_root
except ImportError:  # loaded by path: the install root, not the package directory, is the import anchor
    import sys

    _root = str(Path(__file__).resolve().parents[2])
    if _root not in sys.path:
        sys.path.insert(0, _root)
    from lu_machinery.common.repo_root import machinery_path, machinery_root  # type: ignore[no-redef]


class SQLiteConnection(Protocol):
    """Connection type for annotations, without exporting a constructor."""

    row_factory: Any
    isolation_level: str | None
    in_transaction: bool

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor: ...
    def executemany(self, sql: str, parameters: Iterable[Any], /) -> sqlite3.Cursor: ...
    def executescript(self, sql: str, /) -> sqlite3.Cursor: ...
    def cursor(self) -> sqlite3.Cursor: ...
    def close(self) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def set_authorizer(self, callback: Callable[..., int] | None) -> None: ...
    def set_progress_handler(self, callback: Callable[[], int] | None, n: int) -> None: ...
    def __enter__(self) -> Self: ...
    def __exit__(self, *args: Any) -> None: ...


def is_sqlite_connection(value: object) -> bool:
    """Keep SQLite-vs-Postgres runtime checks without exporting a constructor."""
    return isinstance(value, sqlite3.Connection)


def _serialized(method: Any) -> Any:
    """Keep SQLite execution and its Python result/error handling under one lock."""

    @functools.wraps(method)
    def call(self: Any, *args: Any, **kwargs: Any) -> Any:
        connection = self.connection if isinstance(self, sqlite3.Cursor) else self
        with connection._operation_lock:
            try:
                return method(self, *args, **kwargs)
            finally:
                while connection._pending_cursor_closes:
                    cursor = connection._pending_cursor_closes.pop()
                    # Connection.close() may already have closed the reader.
                    with suppress(sqlite3.ProgrammingError):
                        sqlite3.Cursor.close(cursor)

    return call


class SerializedCursor(sqlite3.Cursor):
    """Cursors on one shared reader use their connection's operation lock."""

    execute = _serialized(sqlite3.Cursor.execute)
    executemany = _serialized(sqlite3.Cursor.executemany)
    executescript = _serialized(sqlite3.Cursor.executescript)
    fetchone = _serialized(sqlite3.Cursor.fetchone)
    fetchmany = _serialized(sqlite3.Cursor.fetchmany)
    fetchall = _serialized(sqlite3.Cursor.fetchall)
    __next__ = _serialized(sqlite3.Cursor.__next__)
    close = _serialized(sqlite3.Cursor.close)

    def __del__(self) -> None:
        try:
            connection = self.connection
            lock = connection._operation_lock
            # RLock permits same-thread GC. Never wait across connections:
            # two threads may each hold one lock while finalizing the other's
            # cursor. Retain a contended cursor until a locked call drains it;
            # letting native deallocation run here would reset SQLite unlocked.
            if not lock.acquire(blocking=False):
                connection._pending_cursor_closes.append(self)
                return
            try:
                self.close()
            finally:
                lock.release()
        except Exception:
            # Failed construction, an already closed connection, or teardown
            # may leave no usable connection or module globals for close().
            pass


class SerializedConnection(sqlite3.Connection):
    """Serialize shared read operations without holding the cache/reopen lock.

    SQLite's mutex alone does not protect CPython's subsequent error handling.
    One RLock also covers fetches, iteration and close, including held readers
    after a cache replacement. No Python SQL callbacks are installed.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._operation_lock = threading.RLock()
        self._pending_cursor_closes: list[SerializedCursor] = []
        super().__init__(*args, **kwargs)

    @_serialized
    def cursor(self, factory: Any = SerializedCursor) -> Any:
        return super().cursor(factory)

    # SQLite's convenience methods create plain cursors internally.
    def execute(self, sql: str, parameters: Any = (), /) -> Any:
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql: str, parameters: Any, /) -> Any:
        return self.cursor().executemany(sql, parameters)

    def executescript(self, script: str, /) -> Any:
        return self.cursor().executescript(script)

    close = _serialized(sqlite3.Connection.close)
    commit = _serialized(sqlite3.Connection.commit)
    rollback = _serialized(sqlite3.Connection.rollback)
    __enter__ = _serialized(sqlite3.Connection.__enter__)
    __exit__ = _serialized(sqlite3.Connection.__exit__)


def open_readonly(
    path: str | Path,
    *,
    timeout: float = 5.0,
    check_same_thread: bool = True,
    isolation_level: str | None = "",
    immutable: bool = False,
    factory: type[sqlite3.Connection] | None = None,
    query_only: bool = True,
) -> sqlite3.Connection:
    """Open exactly path, read-only; refuse extensions and other databases.

    ``query_only`` stays on unless the caller must fill a connection-local temp
    table. ``mode=ro`` still refuses writes to the file itself.
    Optional settings preserve reader concurrency, transaction, and immutable
    side-artifact contracts. The caller owns closing the returned connection.
    URI strings are not filesystem paths and must be decoded by their owner.

    SQLite's native attachment limit avoids Python callbacks on connections
    shared across threads. ATTACH raises OperationalError ("too many attached
    databases"); DETACH uses SQLite's own errors for main, temp, or absent names.
    No additional database can be attached, so none can be detached. VACUUM INTO
    is also refused by the attachment limit. Callers must preserve the limit
    and avoid installing Python SQL callbacks on shared connections.
    Shared connections disable the statement cache; CPython 3.12+ corrupts it
    when threads share one connection. ``factory`` is a ``sqlite3.Connection``
    subclass for readers that observe errors (the Sources MCP server, #11115).
    Shared readers default to ``SerializedConnection``; custom shared-reader
    factories inherit it and use ``SerializedCursor`` to retain serialization.
    """
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    if immutable:
        uri += "&immutable=1"
    settings: dict[str, Any] = {}
    if timeout != 5.0:
        settings["timeout"] = timeout
    if not check_same_thread:
        settings["check_same_thread"] = False
        # CPython 3.12+ keeps one statement cache per connection, and that cache
        # is not safe when threads share the connection: fetches come back wrong
        # and sqlite3 raises InterfaceError or IndexError (CPython #118172,
        # #11208). The operation lock also protects Python error handling
        # after SQLite releases its mutex.
        # Disabling the statement cache is the documented upstream mitigation.
        # Retain this defense even though the default shared reader now also
        # serializes native calls and their Python error handling (#11330).
        settings["cached_statements"] = 0
    if isolation_level != "":
        settings["isolation_level"] = isolation_level
    if factory is None and not check_same_thread:
        factory = SerializedConnection
    if factory is not None:
        settings["factory"] = factory
    conn = sqlite3.connect(uri, uri=True, **settings)
    try:
        if hasattr(conn, "enable_load_extension"):
            conn.enable_load_extension(False)
        if query_only:
            conn.execute("PRAGMA query_only=ON")
        # Refuse ATTACH with SQLite's own limit, never a Python callback: a
        # callback runs while SQLite holds the connection mutex with the GIL
        # released, so a connection shared across threads deadlocks against a
        # thread that holds the GIL and waits for that mutex (#10674). With no
        # attached database, DETACH has nothing to remove.
        conn.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
    except BaseException:
        conn.close()
        raise
    return conn


_SQLITE_MODULE_ROOTS = frozenset({"sqlite3", "_sqlite3"})


def import_named_module(name: str) -> ModuleType:
    """Import ``name`` unless it is a SQLite implementation module.

    A non-constant ``importlib.import_module`` is a structural finding: the
    scanner cannot prove the target is not sqlite3. Adapter and startup
    loaders call this boundary, which refuses those module names first.
    """
    root = name.split(".", 1)[0].split(":", 1)[0]
    if root in _SQLITE_MODULE_ROOTS or name in _SQLITE_MODULE_ROOTS:
        raise ImportError(f"SQLite modules are not loaded through this boundary: {name}")
    return importlib.import_module(name)


_CORPUS_DATABASE_NAMES = frozenset({"sources.db", "vesum.db"})
# Published Sources generations (scripts/storage/generations.py) live under
# this directory with names that are not sources.db.
_GENERATIONS_DIRNAME = "sources-generations"


def _checkout_relative(relative: str) -> str:
    """The checkout-relative name of machinery file ``relative``, in ``_script_path``'s form."""
    return machinery_path(relative).relative_to(machinery_root().parent).as_posix()


_GUARD_PATH = _checkout_relative("lib/readonly_sqlite.py")

# Product corpus writers (W1, machinery #100). The product declares its own
# writers in a file it owns, in the bound product checkout
# (``repo_root.product_root``); machinery's declaration lists only machinery's
# writers. The format is the writer allowlist's: a JSON list of objects whose
# ``path`` is checkout-relative and whose ``kind`` is ``writer`` for a writer.
PRODUCT_WRITER_DECLARATION = "scripts/config/sqlite_reference_allowlist.json"
_MAX_DECLARATION_BYTES = 1024 * 1024
_DECLARED_PATH = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*")


class CorpusDatabaseWriteRefused(PermissionError):
    """A generic factory cannot open sources.db or vesum.db for writing."""


class ProductWriterDeclarationRefused(CorpusDatabaseWriteRefused):
    """No product writer can be authorized, because the product root or its declaration is unusable.

    ``reason`` is a path-free code: ``product_root_unavailable``, or
    ``product_declaration_`` followed by ``missing``, ``unreadable``,
    ``not_regular_file``, ``too_large`` or ``malformed``.
    """

    def __init__(self, database_name: str, reason: str) -> None:
        self.reason = reason
        super().__init__(f"refusing writable open of corpus database {database_name}: {reason}")


def _filesystem_path(database: str | os.PathLike[str] | bytes) -> str:
    """The filesystem path a connect target names.

    A filesystem path keeps ``?`` and ``#`` as literal characters. Only a
    ``file:`` URI separates those characters into the query and the fragment.
    """
    raw = os.fsdecode(os.fspath(database))
    return unquote(urlsplit(raw).path) if raw.startswith("file:") else raw


def corpus_database_name(database: str | os.PathLike[str] | bytes) -> str | None:
    """Return ``sources.db`` or ``vesum.db`` when ``database`` names one.

    The file the path resolves to decides first, so a link or a symlinked
    parent directory cannot rename a corpus database. A file in a
    ``sources-generations`` directory is a Sources generation and returns
    ``sources.db``. The literal name still counts when the path cannot be
    resolved.
    """
    path = Path(_filesystem_path(database))
    with suppress(OSError, RuntimeError, ValueError):
        resolved = path.resolve()
        if resolved.parent.name == _GENERATIONS_DIRNAME:
            return "sources.db"
        if resolved.name in _CORPUS_DATABASE_NAMES:
            return resolved.name
    if path.parent.name == _GENERATIONS_DIRNAME:
        return "sources.db"
    if path.name in _CORPUS_DATABASE_NAMES:
        return path.name
    return None


def _uri_requests_read_only(database: str | os.PathLike[str] | bytes) -> bool:
    raw = os.fsdecode(os.fspath(database))
    if not raw.startswith("file:"):
        return False
    return parse_qs(urlsplit(raw).query).get("mode") == ["ro"]


@functools.lru_cache(maxsize=1)
def _declared_writer_paths() -> frozenset[str]:
    """Machinery's own writer paths from its allowlist file, without importing the linter."""
    candidate = machinery_path("hygiene/sqlite_reference_allowlist.json")
    if not candidate.is_file():
        return frozenset()
    entries = json.loads(candidate.read_text(encoding="utf-8"))
    return frozenset(entry["path"] for entry in entries if entry.get("kind") == "writer")


class _DeclarationUnusable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _open_no_follow(name: str, flags: int, dir_fd: int | None) -> int:
    """Open ``name`` (relative to ``dir_fd`` when given) without following a symlink, or raise a declaration code."""
    try:
        return os.open(name, flags | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=dir_fd)
    except FileNotFoundError:
        raise _DeclarationUnusable("product_declaration_missing") from None
    except OSError as exc:
        # ELOOP: a symlink; ENOTDIR: a non-directory where a directory belongs.
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise _DeclarationUnusable("product_declaration_not_regular_file") from None
        raise _DeclarationUnusable("product_declaration_unreadable") from None


def _read_product_declaration(product: str) -> bytes:
    """The bytes of :data:`PRODUCT_WRITER_DECLARATION` under real path ``product``.

    Every component below ``product`` is opened relative to its pinned parent
    descriptor with ``O_NOFOLLOW``, so a directory or file swapped for a symlink
    while this runs is refused instead of followed (#243 F1). Directories are
    pinned search-only (``O_PATH`` where the platform has it), so a directory
    that permits traversal but not listing still works (#243 F2); a symlink
    there fails with ``ENOTDIR``.
    """
    *parents, name = PRODUCT_WRITER_DECLARATION.split("/")
    search_only = getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY
    directory = _open_no_follow(product, search_only, None)
    try:
        for part in parents:
            child = _open_no_follow(part, search_only, directory)
            os.close(directory)
            directory = child
        # O_NONBLOCK: a FIFO opens at once instead of blocking; the fstat refuses it.
        descriptor = _open_no_follow(name, os.O_RDONLY | os.O_NONBLOCK, directory)
    finally:
        os.close(directory)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise _DeclarationUnusable("product_declaration_not_regular_file")
        if opened.st_size > _MAX_DECLARATION_BYTES:
            raise _DeclarationUnusable("product_declaration_too_large")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            data = handle.read(_MAX_DECLARATION_BYTES + 1)
    except OSError:
        raise _DeclarationUnusable("product_declaration_unreadable") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(data) > _MAX_DECLARATION_BYTES:
        raise _DeclarationUnusable("product_declaration_too_large")
    return data


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError("duplicate key")
    return dict(pairs)


def _product_writer_paths(product: str) -> frozenset[str]:
    """Writer paths the product checkout at real path ``product`` declares.

    Raises :class:`_DeclarationUnusable` when the declaration is missing,
    unreadable, not a regular file reached without a symlink, too large or
    malformed (not strict UTF-8 JSON, a duplicate key, a row without a string
    ``path`` and ``kind``, a non-canonical or repeated ``path``).
    """
    data = _read_product_declaration(product)

    def no_constant(literal: str) -> Any:
        raise ValueError(f"non-finite number {literal}")

    try:
        rows = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_keys, parse_constant=no_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _DeclarationUnusable("product_declaration_malformed") from None
    if not isinstance(rows, list):
        raise _DeclarationUnusable("product_declaration_malformed")
    seen: set[str] = set()
    writers: set[str] = set()
    for row in rows:
        path = row.get("path") if isinstance(row, dict) else None
        kind = row.get("kind") if isinstance(row, dict) else None
        if (
            not isinstance(path, str)
            or not isinstance(kind, str)
            or not _DECLARED_PATH.fullmatch(path)
            or any(part in (".", "..") for part in path.split("/"))
            or path in seen
        ):
            raise _DeclarationUnusable("product_declaration_malformed")
        seen.add(path)
        if kind == "writer":
            writers.add(path)
    return frozenset(writers)


def _within(root: str, real: str) -> bool:
    return real == root or real.startswith(root.rstrip(os.sep) + os.sep)


_OPEN_HELPERS = frozenset({"_open_conn", "_open_writer_conn"})
# The generic factory is itself an allowlisted connect. Its own frame must not
# authorize the corpus write it is being asked to make.
_SEAM_FUNCTIONS = frozenset({(_checkout_relative("api/resilience.py"), "connect_sqlite")})


def _stack_refusal(factory: str | None) -> str | None:
    """``None`` when an ingest function or a declared writer file is on the stack, else a path-free reason.

    Each frame is judged by its real path (symlinks resolved). A frame inside
    the machinery package counts only when machinery's declaration lists it. Any
    other frame counts only when it is inside the bound product checkout
    (``repo_root.product_root``, resolved) and the product's declaration
    (:data:`PRODUCT_WRITER_DECLARATION`) lists it. An unbound
    product or an unusable declaration authorizes no product frame; its reason
    (``product_...``) is returned when nothing else authorized the write.

    The open helpers alone do not authorize a write. A non-writer relay between
    the helper and ``main`` does not hide ``main``. Generic factories such as
    ``connect_sqlite`` pass no factory file and rely on the declared writer
    paths. A factory file is matched relative to its own checkout and must be
    declared there too.
    """
    from lu_machinery.common.repo_root import ProductRootError, product_root

    machinery_writers = _declared_writer_paths()
    package = os.path.realpath(machinery_root())
    package_parent = os.path.dirname(package)
    product: str | None = None
    product_writers: frozenset[str] | None = None
    product_error: str | None = None
    product_looked_up = False
    factory_functions: list[str] = []
    writer_on_stack = False
    for frame in inspect.stack():
        try:
            real = os.path.realpath(frame.filename)
        except (OSError, ValueError):
            continue
        if _within(package, real):
            rel = os.path.relpath(real, package_parent).replace(os.sep, "/")
            if rel == _GUARD_PATH or (rel, frame.function) in _SEAM_FUNCTIONS or rel not in machinery_writers:
                continue
        else:
            if not product_looked_up:
                product_looked_up = True
                try:
                    product = os.path.realpath(product_root())
                except (ProductRootError, OSError, ValueError):
                    product_error = "product_root_unavailable"
            if product is None or not _within(product, real):
                continue
            if product_writers is None and product_error is None:
                try:
                    product_writers = _product_writer_paths(product)
                except _DeclarationUnusable as exc:
                    product_error = exc.reason
            if product_writers is None:
                continue
            rel = os.path.relpath(real, product).replace(os.sep, "/")
            if rel not in product_writers:
                continue
        if factory is not None and rel == factory:
            factory_functions.append(frame.function)
            continue
        if frame.function in _OPEN_HELPERS:
            # The helper's file is a declared writer. The helper itself must
            # not authorize a nested writable open.
            continue
        writer_on_stack = True
    if writer_on_stack or any(name not in _OPEN_HELPERS for name in factory_functions):
        return None
    return product_error or "no_declared_writer"


# --- Sources store writer lock bookkeeping -------------------------------------
# scripts/storage/generations.py takes the store-wide writer lock and registers
# the selector here, so this guard can require the lock without importing it.
STORE_WRITER_LOCK_NAME = ".sources-writer.lock"
# Only the canonical selector carries the lock; another link in the same
# directory never authorizes a write.
STORE_SELECTOR_NAME = "sources.db"
# A process started by ``python -m scripts.storage write`` inherits the
# parent's locked descriptor and the selector it locked.
ENV_STORE_WRITER_FD = "LU_SOURCES_WRITER_FD"
ENV_STORE_WRITER_SELECTOR = "LU_SOURCES_WRITER_SELECTOR"
_store_writers: dict[str, Path] = {}
_store_writers_lock = threading.Lock()


def register_store_writer(selector: Path) -> None:
    """Record that this process holds the writer lock of ``selector``'s store."""
    with _store_writers_lock:
        _store_writers[os.path.realpath(selector.parent)] = selector


def unregister_store_writer(selector: Path) -> None:
    with _store_writers_lock:
        _store_writers.pop(os.path.realpath(selector.parent), None)


def inherited_store_writer(lock_dir: Path) -> tuple[int, Path] | None:
    """The descriptor and selector a ``scripts.storage write`` parent passed down for this store."""
    raw_fd = os.environ.get(ENV_STORE_WRITER_FD)
    raw_selector = os.environ.get(ENV_STORE_WRITER_SELECTOR)
    if raw_fd is None or raw_selector is None:
        return None
    selector = Path(raw_selector)
    if (
        not selector.is_absolute()
        or selector.name != STORE_SELECTOR_NAME
        or os.path.realpath(selector.parent) != os.path.realpath(lock_dir)
    ):
        return None
    try:
        fd = int(raw_fd)
        held = os.fstat(fd)
        expected = (lock_dir / STORE_WRITER_LOCK_NAME).lstat()
    except (ValueError, OSError):
        return None
    if (held.st_dev, held.st_ino) != (expected.st_dev, expected.st_ino):
        return None
    try:
        # The parent's open file description already holds LOCK_EX; taking it
        # again through the same description succeeds, any other holder fails.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return fd, selector


def writable_store_refusal(database: str | os.PathLike[str]) -> str | None:
    """``None`` when ``database`` may be opened writable here, else a path-free reason.

    Paths that are not the Sources store are always allowed by this check. The
    store needs this process to hold its writer lock (or to have inherited it),
    and the path must resolve to the generation the selector selects now.
    """
    from lu_machinery.storage.topology import inspect_selector, store_kind

    ref = store_kind(database)
    if ref.kind == "other":
        return None
    if ref.lock_dir is None:
        return "store_unavailable"
    with _store_writers_lock:
        selector = _store_writers.get(os.path.realpath(ref.lock_dir))
    if selector is None:
        inherited = inherited_store_writer(ref.lock_dir)
        if inherited is None:
            return "writer_lock_not_held"
        selector = inherited[1]
    state = inspect_selector(selector)
    if state.generation is None:
        return state.reason
    try:
        target = Path(os.fspath(database)).resolve()
    except (OSError, RuntimeError, ValueError):
        return "store_unavailable"
    if target != state.generation.resolve():
        return "not_selected_generation"
    return None


def refuse_writable_corpus_database(
    database: str | os.PathLike[str] | bytes,
    *,
    uri: bool = False,
    factory: str | None = None,
    allow_declared_writers: bool = True,
) -> None:
    """Refuse a writable open of sources.db or vesum.db outside ingest writers.

    ``uri`` with ``mode=ro`` is a read-only open and is allowed. ``factory`` is
    the checkout-relative file of a writer seam whose own ingest functions may
    write. Generic factories leave it unset and still allow a declared writer
    file on the stack. Machinery declares its own writers; the product declares
    its writers in :data:`PRODUCT_WRITER_DECLARATION` of the bound product
    checkout (see :func:`_stack_refusal`). An unbound product or an unusable
    product declaration raises :class:`ProductWriterDeclarationRefused` unless a
    machinery writer authorized the open. ``allow_declared_writers=False``
    refuses every caller; the control-plane store uses that. The live Sources
    store (its selector, a generation, or the pre-bootstrap file) also needs the
    store writer lock, and only the generation the selector selects may be opened.
    """
    name = corpus_database_name(database)
    if name is None:
        return
    if uri and _uri_requests_read_only(database):
        return
    if allow_declared_writers:
        refusal = _stack_refusal(factory)
        if refusal is None:
            if name != "sources.db":
                return
            reason = writable_store_refusal(_filesystem_path(database))
            if reason is None:
                return
            raise CorpusDatabaseWriteRefused(f"refusing writable open of the Sources store: {reason}")
        if refusal.startswith("product_"):
            raise ProductWriterDeclarationRefused(name, refusal)
    raise CorpusDatabaseWriteRefused(f"refusing writable open of corpus database {name}")

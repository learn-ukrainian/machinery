"""Phase 0b control-plane storage seam tests (sqlite default; pg fail-closed)."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import time
from pathlib import Path

import pytest
from lu_machinery.control_plane.storage import (
    ControlPlanePgConnectError,
    ControlPlanePgDsnMissingError,
    ControlPlaneStoreUnavailableError,
    StoreId,
    connect,
    resolve_authority,
    sqlite_path,
)
from lu_machinery.guardrails.delegate_ownership import OwnershipLedger
from lu_machinery.hygiene import lint_control_plane_sqlite
from lu_machinery.lib.readonly_sqlite import CorpusDatabaseWriteRefused
from lu_machinery.session_streams.db import SessionStreamDatabase
from tests.helpers.product_tree import configured_roots  # noqa: F401

pytestmark = pytest.mark.usefixtures("configured_roots")

# Unreachable local port — fails closed quickly without DNS (no example.invalid).
_UNREACHABLE_DSN = "postgresql://cp_ci:cp_ci@127.0.0.1:1/postgres"


@pytest.mark.parametrize(
    "store_id",
    [
        StoreId.WRITE_OWNERSHIP,
        StoreId.FLEET_COMMS,
        StoreId.SESSION_STREAMS,
    ],
)
def test_default_authority_is_sqlite(
    store_id: StoreId,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LEARN_UKRAINIAN_CP_AUTHORITY", raising=False)
    monkeypatch.delenv(f"LEARN_UKRAINIAN_CP_AUTHORITY_{store_id.name}", raising=False)
    assert resolve_authority(store_id).value == "sqlite"


def test_sqlite_path_resolution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("LEARN_UKRAINIAN_OWNERSHIP_LEDGER", raising=False)
    fleet_path = sqlite_path(StoreId.FLEET_COMMS)
    assert fleet_path.name == "comms.sqlite3"
    assert fleet_path.parent.name == "v1"
    assert fleet_path.parent.parent.name == "fleet-comms"

    streams_path = sqlite_path(StoreId.SESSION_STREAMS)
    assert streams_path.name == "session-streams.sqlite3"
    assert streams_path.parent.name == "v1"
    assert streams_path.parent.parent.name == "session-streams"

    ownership_path = sqlite_path(StoreId.WRITE_OWNERSHIP)
    assert ownership_path.name == "write-ownership.sqlite3"
    assert ownership_path.parent.name == "tasks"

    custom_override = tmp_path / "custom-ledger.sqlite3"
    monkeypatch.setenv("LEARN_UKRAINIAN_OWNERSHIP_LEDGER", str(custom_override))
    assert sqlite_path(StoreId.WRITE_OWNERSHIP) == custom_override.resolve()


def test_connect_refuses_corpus_database_names(tmp_path: Path) -> None:
    for name in ("sources.db", "vesum.db"):
        path = tmp_path / name
        path.write_bytes(b"")
        with pytest.raises(CorpusDatabaseWriteRefused, match=name):
            connect(StoreId.FLEET_COMMS, path=path)
        with pytest.raises(CorpusDatabaseWriteRefused, match=name):
            connect(StoreId.FLEET_COMMS, path=path, read_only=True)
    special = tmp_path / "review?case" / "vesum.db"
    special.parent.mkdir()
    special.write_bytes(b"")
    with pytest.raises(CorpusDatabaseWriteRefused, match=r"vesum\.db"):
        connect(StoreId.FLEET_COMMS, path=special)


def test_connect_refuses_corpus_names_even_for_a_declared_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lu_machinery.lib import readonly_sqlite

    # Every stack counts as a declared writer here; the control-plane seam must
    # still refuse, because it never consults the stack.
    monkeypatch.setattr(readonly_sqlite, "_stack_refusal", lambda factory: None)
    path = tmp_path / "sources.db"
    path.write_bytes(b"")
    with pytest.raises(CorpusDatabaseWriteRefused, match=r"sources\.db"):
        connect(StoreId.FLEET_COMMS, path=path)


def _product_writer_tree(root: Path, *, writer: Path | None = None) -> Path:
    """A product checkout at ``root`` declaring ``scripts/ingest/writer.py``; returns that path.

    The writer module opens its target through the release guard. ``writer``,
    when given, is where the module's real file lives; the declared path is then
    a symlink to it.
    """
    from lu_machinery.lib import readonly_sqlite

    declared = root / "scripts" / "ingest" / "writer.py"
    declared.parent.mkdir(parents=True)
    source = (
        "from lu_machinery.lib.readonly_sqlite import refuse_writable_corpus_database\n\n\n"
        "def ingest(path):\n"
        "    refuse_writable_corpus_database(path)\n"
    )
    if writer is None:
        declared.write_text(source, encoding="utf-8")
    else:
        writer.parent.mkdir(parents=True, exist_ok=True)
        writer.write_text(source, encoding="utf-8")
        declared.symlink_to(writer)
    declaration = root / readonly_sqlite.PRODUCT_WRITER_DECLARATION
    declaration.parent.mkdir(parents=True)
    rows = [{"path": "scripts/ingest/writer.py", "reference_count": 1, "kind": "writer", "reason": "ingest"}]
    declaration.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    return declared


def _load_writer(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_product_declared_writer_under_bound_product_root_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect class: release guard ignores product-declared writers (W1).

    On the installed release only machinery's own allowlist was consulted, so
    every product ingest writer was refused a writable corpus open.
    """
    product = tmp_path / "product"
    declared = _product_writer_tree(product)
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(product))
    database = tmp_path / "data" / "vesum.db"
    database.parent.mkdir()
    database.write_bytes(b"")

    _load_writer(declared, "w1_product_writer_allowed").ingest(str(database))


def test_product_writer_reached_through_symlink_from_outside_product_root_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect class: authorizing a frame by its logical path instead of its real path.

    The declared product path is a symlink to code that lives outside the bound
    product root; that code must not inherit the declared writer's authority.
    """
    product = tmp_path / "product"
    declared = _product_writer_tree(product, writer=tmp_path / "outside" / "scripts" / "ingest" / "writer.py")
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(product))
    database = tmp_path / "data" / "vesum.db"
    database.parent.mkdir()
    database.write_bytes(b"")

    writer = _load_writer(declared, "w1_product_writer_symlinked")
    with pytest.raises(CorpusDatabaseWriteRefused, match=r"vesum\.db"):
        writer.ingest(str(database))


@pytest.mark.parametrize(
    ("damage", "reason"),
    [
        ("missing", "product_declaration_missing"),
        ("malformed", "product_declaration_malformed"),
    ],
)
def test_unusable_product_declaration_refuses_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str, reason: str
) -> None:
    """Defect class: fail-open fallback when the product writer declaration is unusable.

    A missing or malformed declaration must refuse with a typed reason, never
    fall back to allowing the declared writer.
    """
    from lu_machinery.lib import readonly_sqlite

    product = tmp_path / "product"
    declared = _product_writer_tree(product)
    declaration = product / readonly_sqlite.PRODUCT_WRITER_DECLARATION
    if damage == "missing":
        declaration.unlink()
    else:
        # Truncated mid-row: the row that declares the writer is unreadable.
        declaration.write_text('[{"path": "scripts/ingest/writer.py", "kind": "writer"', encoding="utf-8")
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(product))
    database = tmp_path / "data" / "vesum.db"
    database.parent.mkdir()
    database.write_bytes(b"")

    writer = _load_writer(declared, f"w1_product_writer_{damage}")
    with pytest.raises(readonly_sqlite.ProductWriterDeclarationRefused, match=r"vesum\.db") as refused:
        writer.ingest(str(database))
    assert refused.value.reason == reason


def test_ownership_ledger_uses_seam_and_begin_immediate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger_path = tmp_path / "write-ownership.sqlite3"
    monkeypatch.setenv("LEARN_UKRAINIAN_OWNERSHIP_LEDGER", str(ledger_path))
    ledger = OwnershipLedger(task_state_dir=tmp_path / "tasks")
    with ledger._connect() as conn:
        assert isinstance(conn, sqlite3.Connection)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT 1").fetchone()
        assert row is not None
        conn.execute("COMMIT")


@pytest.mark.parametrize(
    ("store_id", "env_suffix"),
    [
        (StoreId.WRITE_OWNERSHIP, "WRITE_OWNERSHIP"),
        (StoreId.FLEET_COMMS, "FLEET_COMMS"),
        (StoreId.SESSION_STREAMS, "SESSION_STREAMS"),
    ],
)
def test_pg_without_dsn_fails_closed_no_sqlite_created(
    store_id: StoreId,
    env_suffix: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(f"LEARN_UKRAINIAN_CP_AUTHORITY_{env_suffix}", "pg")
    monkeypatch.delenv("LEARN_UKRAINIAN_CP_PG_DSN", raising=False)
    target = tmp_path / f"would-not-create-{store_id.value}.sqlite3"
    with pytest.raises(ControlPlanePgDsnMissingError, match=store_id.value):
        connect(store_id, path=target)
    assert not target.exists()


@pytest.mark.parametrize(
    ("store_id", "env_suffix"),
    [
        (StoreId.WRITE_OWNERSHIP, "WRITE_OWNERSHIP"),
        (StoreId.FLEET_COMMS, "FLEET_COMMS"),
        (StoreId.SESSION_STREAMS, "SESSION_STREAMS"),
    ],
)
def test_pg_unreachable_dsn_fails_closed_quickly_no_sqlite(
    store_id: StoreId,
    env_suffix: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(f"LEARN_UKRAINIAN_CP_AUTHORITY_{env_suffix}", "pg")
    monkeypatch.setenv("LEARN_UKRAINIAN_CP_PG_DSN", _UNREACHABLE_DSN)
    target = tmp_path / f"still-not-created-{store_id.value}.sqlite3"
    started = time.monotonic()
    with pytest.raises(ControlPlanePgConnectError, match=store_id.value) as raised:
        connect(store_id, path=target)
    elapsed = time.monotonic() - started
    assert not target.exists()
    assert elapsed < 8.0, f"pg connect must fail closed quickly; took {elapsed:.1f}s"
    message = str(raised.value)
    assert "127.0.0.1" not in message
    assert "cp_ci" not in message
    assert "postgresql://" not in message


def test_session_streams_db_pg_refused_by_interlock_no_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#605: session_streams is sqlite-only; the #7482 interlock refuses pg
    at the seam (before any Postgres connect is even attempted)."""
    from lu_machinery.control_plane.storage import ControlPlaneUnsupportedComponentError

    monkeypatch.setenv("LEARN_UKRAINIAN_CP_AUTHORITY_SESSION_STREAMS", "pg")
    monkeypatch.setenv("LEARN_UKRAINIAN_CP_PG_DSN", _UNREACHABLE_DSN)
    db_target = tmp_path / "session-streams.sqlite3"
    db = SessionStreamDatabase(path=db_target)
    started = time.monotonic()
    with pytest.raises(ControlPlaneUnsupportedComponentError, match="session_streams") as raised:
        db.connect()
    elapsed = time.monotonic() - started
    assert not db_target.exists()
    assert elapsed < 8.0
    message = str(raised.value)
    assert "127.0.0.1" not in message
    assert "postgresql://" not in message


def test_task_index_has_no_sqlite_path() -> None:
    with pytest.raises(ControlPlaneStoreUnavailableError, match="task_index"):
        sqlite_path(StoreId.TASK_INDEX)


def test_control_plane_lint_allowlists_remaining_direct_opens() -> None:
    violations = lint_control_plane_sqlite.find_unallowlisted_connects()
    assert violations == [], f"unallowlisted control-plane sqlite opens: {violations}"


@pytest.mark.parametrize(
    ("target_store", "db_rel_path"),
    [
        ("write_ownership", "batch_state/tasks/write-ownership.sqlite3"),
        ("fleet_comms", "batch_state/fleet-comms/v1/comms.sqlite3"),
        ("session_streams", ".agent/session-streams/v1/session-streams.sqlite3"),
    ],
)
def test_control_plane_lint_detects_new_direct_open(
    target_store: str,
    db_rel_path: str,
    tmp_path: Path,
) -> None:
    scripts = tmp_path / "scripts" / "evil"
    scripts.mkdir(parents=True)
    (scripts / "rogue.py").write_text(
        "\n".join(
            [
                "import sqlite3",
                f"def open_{target_store}():",
                f'    return sqlite3.connect("{db_rel_path}")',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    violations = lint_control_plane_sqlite.find_unallowlisted_connects(repo_root=tmp_path)
    assert len(violations) == 1
    assert Path(db_rel_path).name in violations[0][1]


def test_control_plane_changed_files_match_counted_repo_results(tmp_path, capsys, monkeypatch):
    lint = lint_control_plane_sqlite
    path = tmp_path / "scripts/rogue.py"
    path.parent.mkdir()
    line = 'connection = sqlite3.connect("write-ownership.sqlite3")'
    path.write_text("import sqlite3\n" + line + "\n")
    clean = tmp_path / "scripts/clean.py"
    clean.write_text("VALUE = 1\n")
    assert lint.main(["--repo", str(tmp_path), str(clean)]) == 0
    assert capsys.readouterr().out == "OK: no unallowlisted control-plane sqlite3.connect opens\n"
    assert lint.main(["--repo", str(tmp_path), str(path)]) == 1
    selected = capsys.readouterr()
    assert lint.main(["--repo", str(tmp_path)]) == 1
    assert capsys.readouterr() == selected
    monkeypatch.setattr(lint, "_ALLOWLIST", (("scripts/rogue.py", line, 1),))
    assert lint.main(["--repo", str(tmp_path), str(path), str(path)]) == 0
    capsys.readouterr()
    path.write_text("import sqlite3\n" + line + "\n" + line + "\n")
    assert lint.main(["--repo", str(tmp_path), str(path)]) == 1
    selected = capsys.readouterr()
    assert "scripts/rogue.py:3:" in selected.err
    assert "allowlist exceeded" in selected.err
    assert lint.main(["--repo", str(tmp_path)]) == 1
    assert capsys.readouterr() == selected
    path.write_text(line.replace("connection =", "new_connection =") + "\n")
    assert lint.main(["--repo", str(tmp_path), str(path)]) == 1

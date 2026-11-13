"""Scanner corrections must not hide real store paths or aliased writer calls."""

import json

import pytest

from lu_machinery.hygiene import lint_source_db_writable_connects as lint


def test_diagnostic_and_logical_label_are_not_store_constructions(tmp_path):
    source = '''
from pathlib import Path
ROOT = Path(__file__).parent
def check(ref, table):
    return f"{ref['rowId']} is not in sources.db", f"sqlite://data/vesum.db#{table}"
'''
    assert lint.classify_store_source(source, str(tmp_path / "example.py")) == []


def test_real_store_paths_and_sqlite_uris_are_still_caught(tmp_path):
    source = '''
from pathlib import Path
ROOT = Path(__file__).parent
database = ROOT / "data" / "vesum.db"
source = f"file:{ROOT}/data/sources.db?mode=rw"
'''
    assert len(lint.classify_store_source(source, str(tmp_path / "example.py"))) == 2


def test_aliased_writer_resolution_is_collection_time_access():
    source = '''
from lu_machinery.storage.topology import resolve_store_for_write as resolve
alias = resolve
binding = alias("sources", intent="update")
'''
    findings = lint.classify_store_source(source, "tests/example.py")
    assert [(f.kind, f.scope) for f in findings] == [("test_import_access", "<module>")]


def test_write_resolver_scope_never_exempts_raw_constructor():
    source = '''
import sqlite3
def resolve_store_for_write():
    return sqlite3.connect("sources.db")
'''
    assert len(lint.classify_source(source, "scripts/storage/topology.py")) == 1


def test_parser_help_does_not_taint_namespace_but_store_defaults_are_found(tmp_path):
    source = '''
import argparse
from pathlib import Path
ROOT = Path(__file__).parent
parser = argparse.ArgumentParser(description="Ingest notes into data/sources.db")
args = parser.parse_args()
text = ROOT / args.book
sample = len(args.items) // 2
'''
    assert lint.classify_store_source(source, str(tmp_path / "source.py")) == []
    source += "parser.add_argument('--db', default=ROOT / 'data' / 'sources.db')\n"
    assert [f.kind for f in lint.classify_store_source(source, str(tmp_path / "source.py"))] == ["store_path"]


def test_manifest_pair_tracks_scan_root_without_product_authority_leak(tmp_path, monkeypatch):
    """Defect class: a scan reading another checkout's manifest pair (machinery #338).

    Machinery's packaged rows must neither be stale in a product scan nor
    authorize a product writer. A package manifest planted in the product does
    not replace the product's declaration, which is read as the runtime guard
    reads it: a missing, incomplete or guard-refused declaration fails the scan.
    """
    packaged = [row["path"] for row in json.loads(lint.REFERENCE_MANIFEST.read_text())]
    packaged += [row["path"] for row in json.loads(lint.BASELINE.read_text())["entries"]]
    assert packaged and not [path for path in packaged if path.startswith("scripts/")]

    product = tmp_path / "product"
    (product / "scripts").mkdir(parents=True)
    connect = "import sqlite3\nc = sqlite3.connect(PATH)\n"
    (product / "scripts" / "writer.py").write_text(connect)
    (product / "scripts" / "rogue.py").write_text(connect)
    declaration, baseline = lint.manifest_pair(product)
    assert (declaration, baseline) == (product / "scripts/config/sqlite_reference_allowlist.json",
                                       product / lint.PRODUCT_BASELINE)
    declaration.parent.mkdir(parents=True)
    baseline.parent.mkdir(parents=True)
    baseline.write_text(json.dumps({"entries": []}))

    def row(path, **changes):
        return {"path": path, "reference_count": 1, "kind": "writer", "target_db": "fixture",
                "reason": "fixture", "calls": ["sqlite3.connect(PATH)"], **changes}

    # A planted package pair that authorizes the rogue writer is not the product's authority.
    shadow = product / "lu_machinery" / "hygiene"
    shadow.mkdir(parents=True)
    (shadow / lint.REFERENCE_MANIFEST.name).write_text(json.dumps([row("scripts/writer.py"), row("scripts/rogue.py")]))
    (shadow / lint.BASELINE.name).write_text(json.dumps({"entries": []}))
    assert lint.manifest_pair(product) == (declaration, baseline)
    # The running package placed inside the product, even one without a scripts/
    # tree, does not make it machinery's checkout; machinery's project file does.
    real_pair = (lint.REFERENCE_MANIFEST, lint.BASELINE)
    monkeypatch.setattr(lint, "REFERENCE_MANIFEST", shadow / lint.REFERENCE_MANIFEST.name)
    monkeypatch.setattr(lint, "BASELINE", shadow / lint.BASELINE.name)
    assert lint.manifest_pair(product) == (declaration, baseline)
    markerless = tmp_path / "markerless"
    (markerless / "lu_machinery" / "hygiene").mkdir(parents=True)
    monkeypatch.setattr(lint, "REFERENCE_MANIFEST", markerless / "lu_machinery/hygiene" / real_pair[0].name)
    monkeypatch.setattr(lint, "BASELINE", markerless / "lu_machinery/hygiene" / real_pair[1].name)
    assert lint.manifest_pair(markerless)[0] == markerless / "scripts/config/sqlite_reference_allowlist.json"
    machinery = tmp_path / "machinery"
    (machinery / "lu_machinery" / "hygiene").mkdir(parents=True)
    (machinery / "pyproject.toml").write_text('[project]\nname = "lu-machinery"\n')
    monkeypatch.setattr(lint, "REFERENCE_MANIFEST", machinery / "lu_machinery/hygiene" / real_pair[0].name)
    monkeypatch.setattr(lint, "BASELINE", machinery / "lu_machinery/hygiene" / real_pair[1].name)
    assert lint.manifest_pair(machinery) == (lint.REFERENCE_MANIFEST, lint.BASELINE)
    # Another checkout owns its pair even when the executing package lives elsewhere.
    monkeypatch.setattr(lint, "REFERENCE_MANIFEST", real_pair[0])
    monkeypatch.setattr(lint, "BASELINE", real_pair[1])
    machinery_pair = (machinery / "lu_machinery/hygiene" / real_pair[0].name,
                      machinery / "lu_machinery/hygiene" / real_pair[1].name)
    assert lint.manifest_pair(machinery) == machinery_pair
    machinery_pair[0].write_text(json.dumps([row("lu_machinery/writer.py")]))
    machinery_pair[1].write_text(json.dumps({"entries": []}))
    (machinery / "lu_machinery/writer.py").write_text(connect)
    assert [entry.path for entry in lint.load_scan_allowlist(machinery)] == ["lu_machinery/writer.py"]
    assert lint.find_violations(machinery) == ([], [])
    declaration.write_text(json.dumps([row("scripts/writer.py")]))
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(product))

    # Exercise the hosted-package branch throughout the scan, not just pair selection.
    monkeypatch.setattr(lint, "REFERENCE_MANIFEST", shadow / real_pair[0].name)
    monkeypatch.setattr(lint, "BASELINE", shadow / real_pair[1].name)
    assert lint._hosts_running_package(product)
    violations, unreadable = lint.find_violations()
    assert unreadable == []
    assert violations == ["scripts/rogue.py:2: constructor reference: c = sqlite3.connect(PATH)"]
    monkeypatch.setattr(lint, "REFERENCE_MANIFEST", real_pair[0])
    monkeypatch.setattr(lint, "BASELINE", real_pair[1])

    for unauthorized in (
        row("lu_machinery/rogue.py"),
        row("lu_machinery/storage/topology.py", kind="store_resolver", reference_count=0,
            target_db=None, calls=[], functions=["resolve_store"]),
        row("lu_machinery/ops/product_entry.py", kind="configured_entry_point", target_db=None),
    ):
        declaration.write_text(json.dumps([unauthorized]))
        with pytest.raises(ValueError, match="cannot authorize machinery package files"):
            lint.find_violations()
    declaration.write_text(json.dumps([row("scripts/writer.py")]))
    reader = product / lint.READER_BOUNDARY
    reader.parent.mkdir(parents=True)
    reader.write_text(connect)
    violations, unreadable = lint.find_violations()
    assert unreadable == []
    assert violations == [
        "lu_machinery/lib/readonly_sqlite.py:2: constructor reference: c = sqlite3.connect(PATH)",
        "scripts/rogue.py:2: constructor reference: c = sqlite3.connect(PATH)",
    ]

    for refused in (
        json.dumps([{"path": "scripts/rogue.py", "kind": "writer"}]),
        json.dumps([row("scripts/rogue.py")]).replace('"fixture"', "NaN", 1),
        json.dumps([row("scripts/rogue.py")]).replace('"kind": "writer"', '"kind": "writer", "kind": "writer"'),
        json.dumps([row("scripts/./rogue.py")]),
    ):
        declaration.write_text(refused)
        with pytest.raises(ValueError, match="invalid SQLite allowlist entry|product_declaration_malformed"):
            lint.find_violations()
    declaration.unlink()
    with pytest.raises(ValueError, match="product_declaration_missing"):
        lint.find_violations()


def test_connection_subclass_call_is_a_constructor_reference():
    """Defect class: calling a sqlite3.Connection subclass opens a writable database the lint misses."""
    source = """
import sqlite3
class Mine(sqlite3.Connection):
    pass
def open(path):
    alias = Mine
    return alias(path)
"""
    assert [finding.snippet for finding in lint.classify_source(source, "lu_machinery/mine.py")] == [
        "class Mine(sqlite3.Connection):",
        "return alias(path)",
    ]


def test_imported_connection_subclass_calls_are_reported_except_resolver_scope(tmp_path):
    """Defect class: machinery #11's three subclass-call forms (and an import alias) produce no finding.

    A class object passed as a connection factory is not a call. A call inside a
    declared resolver function is not a reference; the same call beside it is.
    Subclasses defined in lu_machinery/ and in scripts/ both count.
    """
    files = {
        "lu_machinery/lib/readonly_sqlite.py": "import sqlite3\nclass SerializedConnection(sqlite3.Connection):\n    pass\n",
        "lu_machinery/api/resilience.py": (
            "import sqlite3\n"
            "class TimedSQLiteConnection(sqlite3.Connection):\n"
            "    pass\n"
            "def connect_sqlite(database, *args, **kwargs):\n"
            "    kwargs.setdefault('factory', TimedSQLiteConnection)\n"
            "    return sqlite3.connect(database, *args, **kwargs)\n"
        ),
        "scripts/extra.py": "import sqlite3\nclass ScriptConnection(sqlite3.Connection):\n    pass\n",
        "lu_machinery/child.py": (
            "from lu_machinery.lib.readonly_sqlite import SerializedConnection\n"
            "class Child(SerializedConnection):\n"
            "    pass\n"
            "def open(path):\n"
            "    return Child(path)\n"
        ),
        "lu_machinery/caller.py": (
            "from scripts.lib.readonly_sqlite import SerializedConnection\n"
            "from scripts.lib.readonly_sqlite import SerializedConnection as Alias\n"
            "from lu_machinery.api.resilience import TimedSQLiteConnection\n"
            "from scripts.extra import ScriptConnection\n"
            "def open_dynamic(path, name):\n"
            "    return import_named_module(name).SerializedConnection(path)\n"
            "def open_static(path):\n"
            "    return SerializedConnection(path)\n"
            "def open_alias(path):\n"
            "    return Alias(path)\n"
            "def open_timed(path):\n"
            "    return TimedSQLiteConnection(path)\n"
            "def open_script(path):\n"
            "    return ScriptConnection(path)\n"
            "def open_other(path):\n"
            "    return NotAConnection(path)\n"
        ),
        "lu_machinery/storage/topology.py": (
            "from lu_machinery.lib.readonly_sqlite import SerializedConnection\n"
            "def resolve_store_for_write(resolved):\n"
            "    return SerializedConnection(resolved)\n"
            "def not_a_resolver(other):\n"
            "    return SerializedConnection(other)\n"
        ),
    }
    for rel, source in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    resolver = lint.AllowedReference(
        path="lu_machinery/storage/topology.py", reference_count=0, kind="store_resolver",
        target_db=None, reason="fixture", functions=(
            "resolve_store", "resolve_active_sources_db", "resolve_store_for_write", "_prepare_store_write_target",
        ),
    )
    violations, unreadable = lint.find_violations(tmp_path, (resolver,))
    assert unreadable == []
    reported = set(violations)
    for snippet in (
        "return import_named_module(name).SerializedConnection(path)",
        "return SerializedConnection(path)",
        "return Alias(path)",
        "return TimedSQLiteConnection(path)",
        "return ScriptConnection(path)",
        "return Child(path)",
        "return SerializedConnection(other)",
    ):
        assert any(item.endswith(f": {snippet}") for item in reported), snippet
    absent = (
        "return SerializedConnection(resolved)",
        "kwargs.setdefault('factory', TimedSQLiteConnection)",
        "return NotAConnection(path)",
    )
    assert [item for item in reported if any(snippet in item for snippet in absent)] == []


def test_subclass_factory_argument_does_not_grow_pinned_reference_counts():
    """Defect class: handing a Connection subclass to sqlite3.connect as factory grows the writer's pin."""
    package = lint.REFERENCE_MANIFEST.parent.parent
    resilience = (package / "api" / "resilience.py").read_text()
    reader = (package / "lib" / "readonly_sqlite.py").read_text()
    assert len(lint.classify_source(resilience, "lu_machinery/api/resilience.py")) == 3
    assert len(lint.classify_source(reader, lint.READER_BOUNDARY)) == 11
    assert not any("setdefault" in finding.snippet for finding in lint.classify_source(resilience, "lu_machinery/api/resilience.py"))

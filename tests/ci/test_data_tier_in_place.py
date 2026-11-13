"""The nightly data tier reads the host's own data in place, read-only, and the tested checkout's selection."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lu_machinery.ci import data_tier


def test_selection_and_known_failures_come_from_the_tested_checkout_without_fallback(tmp_path):
    """Defect class: the runner tests a packaged, stale selection instead of the checkout's, or falls back silently."""
    with pytest.raises(data_tier.DataTierError, match=r"tested checkout has no scripts/ci/data_tier_selection\.json"):
        data_tier.load_selection(tmp_path)
    with pytest.raises(
        data_tier.DataTierError, match=r"tested checkout has no scripts/ci/data_tier_known_failures\.json"
    ):
        data_tier.load_known_failures(tmp_path)

    ci = tmp_path / "scripts" / "ci"
    ci.mkdir(parents=True)
    nodeid = "tests/test_example.py::test_reads_store"
    (ci / "data_tier_selection.json").write_text(
        json.dumps({"schema": 1, "class_c_merge_group": 1, "nodeids": [nodeid], "files": ["tests/test_example.py"]}),
        encoding="utf-8",
    )
    (ci / "data_tier_known_failures.json").write_text(json.dumps({"known_failures": {nodeid: 7}}), encoding="utf-8")

    selection, digest = data_tier.load_selection(tmp_path)
    assert selection["nodeids"] == [nodeid]
    assert data_tier.load_selection(tmp_path, digest)[0] == selection
    assert data_tier.load_known_failures(tmp_path) == {nodeid: 7}
    with pytest.raises(data_tier.DataTierError, match="changed after the runner validated it"):
        data_tier.load_selection(tmp_path, "0" * 64)
    (ci / "data_tier_selection.json").write_text(json.dumps({"schema": 2}), encoding="utf-8")
    with pytest.raises(data_tier.DataTierError, match="schema-1 field"):
        data_tier.load_selection(tmp_path)


WRITER = """
import json, os, sqlite3, sys
repo, store, exposed, existing, output, atlas, *read_only = sys.argv[1:]
sys.path.insert(0, repo)
from lu_machinery.ci import data_tier

results = {}
for name, path, mode in (("store", store, "x"), ("exposed", exposed, "x"), ("existing", existing, "a"), ("output", output, "x")):
    try:
        with open(path, mode) as stream:
            stream.write("written")
        results[name] = "written"
    except OSError as error:
        results[name] = error.strerror
results["read"] = open(existing).read()
data_tier.require_read_only(read_only)
results["hidden_roots"] = {root: os.listdir(root) for root in ("/run", "/tmp")}
connection = sqlite3.connect(f"file:{atlas}?mode=ro", uri=True)
results["atlas_rows"] = connection.execute("select count(*) from lemma").fetchone()[0]
try:
    connection.execute("insert into lemma values ('written')")
    results["atlas_write"] = "written"
except sqlite3.OperationalError as error:
    results["atlas_write"] = str(error)
print(json.dumps(results))
"""


def test_store_and_exposed_data_writes_are_denied_without_copies(tmp_path):
    """Defect class: tests can write the host stores or data, directly or through a host service, or the runner copies data."""
    primary, checkout, outputs = tmp_path / "primary", tmp_path / "checkout", tmp_path / "outputs"
    (primary / "data" / "ua-gec").mkdir(parents=True)
    (primary / "data" / "vesum.db").write_text("store", encoding="utf-8")
    (primary / "data" / "ua-gec" / "corpus.txt").write_text("host data", encoding="utf-8")
    (checkout / "data").mkdir(parents=True)
    (checkout / "data" / "tracked.txt").write_text("tracked", encoding="utf-8")
    outputs.mkdir()
    # A live WAL Atlas whose writer holds it open, so its sidecars exist beside the host file.
    writer = sqlite3.connect(primary / "data" / "atlas.db")
    writer.execute("pragma journal_mode=wal")
    writer.execute("create table lemma (word text)")
    writer.execute("insert into lemma values ('слово')")
    writer.commit()

    mounts = data_tier.exposed_data(primary, checkout)
    assert mounts == [(primary / "data" / "ua-gec", checkout / "data" / "ua-gec")]
    data_tier.link_atlas(primary, checkout)
    assert (checkout / "data" / "atlas.db").resolve() == (primary / "data" / "atlas.db").resolve()
    with pytest.raises(data_tier.DataTierError, match="writable inside the test namespace"):
        data_tier.require_read_only([str(outputs)])

    argv = data_tier.read_only_namespace([primary / "data"], mounts)
    try:
        result = subprocess.run(
            [
                *argv,
                sys.executable,
                "-I",
                "-c",
                WRITER,
                str(Path(data_tier.__file__).parents[2]),
                str(primary / "data" / "new.db"),
                str(checkout / "data" / "ua-gec" / "new.txt"),
                str(checkout / "data" / "ua-gec" / "corpus.txt"),
                str(outputs / "junit.xml"),
                str(checkout / "data" / "atlas.db"),
                str(primary / "data"),
                str(checkout / "data" / "ua-gec"),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    finally:
        writer.close()
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "store": "Read-only file system",
        "exposed": "Read-only file system",
        "existing": "Read-only file system",
        "output": "written",
        "read": "host data",
        "hidden_roots": {"/run": [], "/tmp": []},
        "atlas_rows": 1,
        "atlas_write": "attempt to write a readonly database",
    }
    assert (primary / "data" / "ua-gec" / "corpus.txt").read_text(encoding="utf-8") == "host data"
    assert not (primary / "data" / "new.db").exists()
    # Only an empty mount point and the Atlas link are left in the checkout: nothing was copied.
    assert list((checkout / "data" / "ua-gec").iterdir()) == []
    assert sorted(path.name for path in (checkout / "data").iterdir()) == ["atlas.db", "tracked.txt", "ua-gec"]

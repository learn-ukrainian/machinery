"""The nightly data tier reads the host's own data in place, read-only, and the tested checkout's selection."""

from __future__ import annotations

import json
import subprocess
import sys

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
import json, os, sys
store, exposed, existing, output = sys.argv[1:5]
results = {}
for name, path, mode in (("store", store, "x"), ("exposed", exposed, "x"), ("existing", existing, "a"), ("output", output, "x")):
    try:
        with open(path, mode) as stream:
            stream.write("written")
        results[name] = "written"
    except OSError as error:
        results[name] = error.strerror
results["read"] = open(existing).read()
results["read_only_flag"] = bool(os.statvfs(exposed.rsplit("/", 1)[0]).f_flag & os.ST_RDONLY)
print(json.dumps(results))
"""


def test_store_and_exposed_data_writes_are_denied_without_copies(tmp_path):
    """Defect class: tests can write the host stores or data, or the runner copies data into the checkout."""
    primary, checkout, outputs = tmp_path / "primary", tmp_path / "checkout", tmp_path / "outputs"
    (primary / "data" / "ua-gec").mkdir(parents=True)
    (primary / "data" / "vesum.db").write_text("store", encoding="utf-8")
    (primary / "data" / "ua-gec" / "corpus.txt").write_text("host data", encoding="utf-8")
    (checkout / "data").mkdir(parents=True)
    (checkout / "data" / "tracked.txt").write_text("tracked", encoding="utf-8")
    outputs.mkdir()

    mounts = data_tier.exposed_data(primary, checkout)
    assert mounts == [(primary / "data" / "ua-gec", checkout / "data" / "ua-gec")]
    with pytest.raises(data_tier.DataTierError, match="writable inside the test namespace"):
        data_tier.require_read_only([str(outputs)])

    argv = data_tier.read_only_namespace([primary / "data"], mounts)
    result = subprocess.run(
        [
            *argv,
            sys.executable,
            "-I",
            "-c",
            WRITER,
            str(primary / "data" / "new.db"),
            str(checkout / "data" / "ua-gec" / "new.txt"),
            str(checkout / "data" / "ua-gec" / "corpus.txt"),
            str(outputs / "junit.xml"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "store": "Read-only file system",
        "exposed": "Read-only file system",
        "existing": "Read-only file system",
        "output": "written",
        "read": "host data",
        "read_only_flag": True,
    }
    assert sorted(path.name for path in (primary / "data").rglob("*")) == ["corpus.txt", "ua-gec", "vesum.db"]
    assert (primary / "data" / "ua-gec" / "corpus.txt").read_text(encoding="utf-8") == "host data"
    # Only an empty mount point is left in the checkout: the data was never copied.
    assert list((checkout / "data" / "ua-gec").iterdir()) == []

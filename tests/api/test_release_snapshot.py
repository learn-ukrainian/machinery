"""A release snapshot of a product checkout serves the product from the snapshot and machinery from the install.

The intent of learn-ukrainian's
``tests/api/test_release_snapshot.py::test_real_release_serves_live_data_routers_with_logical_paths``,
ported with a fixture product repository instead of the real checkout: build the snapshot the
launchd supervisor builds when API live mode is off, start the Monitor API the way the supervisor
starts it, and read live data through the snapshot's links by its logical path.
"""

from __future__ import annotations

import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import lu_machinery
from lu_machinery.api import launchd_supervisor
from lu_machinery.api.product_contribution import PRODUCT_DATA, PRODUCT_DATA_SCHEMA
from lu_machinery.common.git_context import sanitized_git_env
from lu_machinery.common.release_layout import LIVE_DATA_PATHS, is_release_root

_ENTRY_MODULE = "scripts.release_fixture_product.entry"
_ROUTE = "/api/release-fixture-product/served"
# The API redacts host paths outside the product from responses, so the entry reports origins in a file.
_ORIGINS_VARIABLE = "RELEASE_FIXTURE_ORIGINS_FILE"
_ENTRY_SOURCE = f'''"""Fixture product entry: records where the served modules were loaded from, and adds one route."""

import json
import os
import sys

from fastapi import APIRouter

from lu_machinery.api.product_contribution import ProductContribution, RouterMount

router = APIRouter()


@router.get("{_ROUTE}")
def served():
    return {{"product": __name__}}


def build_api(context):
    names = (__name__, "lu_machinery", "lu_machinery.api.main")
    with open(os.environ["{_ORIGINS_VARIABLE}"], "w", encoding="utf-8") as handle:
        json.dump({{name: sys.modules[name].__file__ for name in names}}, handle)
    return ProductContribution(routers=(RouterMount(router),))
'''


def _git(repo_root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        check=True,
        env=sanitized_git_env(),
        text=True,
        timeout=30,
    ).stdout.strip()


def _product_checkout(tmp_path: Path) -> tuple[Path, str]:
    """A product commit with ``scripts/`` and ``schemas/`` and no machinery package, plus its live data."""
    repo_root = tmp_path / "live"
    data = repo_root / PRODUCT_DATA
    data.parent.mkdir(parents=True)
    data.write_text(
        json.dumps({"schema": PRODUCT_DATA_SCHEMA, "schema_version": 1, "product_api": _ENTRY_MODULE}),
        encoding="utf-8",
    )
    package = repo_root.joinpath(*_ENTRY_MODULE.split(".")[:-1])
    package.mkdir(parents=True)
    for directory in (repo_root / "scripts", package):
        (directory / "__init__.py").write_text("", encoding="utf-8")
    (package / "entry.py").write_text(_ENTRY_SOURCE, encoding="utf-8")
    (repo_root / "schemas").mkdir()
    (repo_root / "schemas" / "example.json").write_text("{}\n", encoding="utf-8")
    for relative_name in LIVE_DATA_PATHS:
        (repo_root / relative_name).mkdir(parents=True, exist_ok=True)

    _git(tmp_path, "init", "-q", repo_root.name)
    _git(repo_root, "config", "user.email", "test@example.invalid")
    _git(repo_root, "config", "user.name", "Release Snapshot Test")
    _git(repo_root, "add", "scripts", "schemas")
    _git(repo_root, "commit", "-q", "-m", "product release")
    assert _git(repo_root, "ls-tree", "-d", "--name-only", "HEAD") == "schemas\nscripts"
    return repo_root, _git(repo_root, "rev-parse", "HEAD")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.read()


@pytest.mark.timeout(120)
def test_product_release_serves_product_from_snapshot_and_machinery_from_install(tmp_path: Path, monkeypatch) -> None:
    """Defect class: the snapshot requires or archives the machinery package, so a product commit cannot be served.

    Installed 0.1.4 refused every product commit ("has no lu_machinery/ directory"), so the
    supervisor's snapshot mode could not start the product's Monitor API.
    """
    repo_root, sha = _product_checkout(tmp_path)
    port = _free_port()
    origins_path = tmp_path / "origins.json"
    # The supervisor's child inherits its shared-state primary; here that is the live product checkout.
    monkeypatch.setenv("LEARN_UKRAINIAN_PRIMARY_REPO_ROOT", str(repo_root))
    monkeypatch.setenv(_ORIGINS_VARIABLE, str(origins_path))

    command, launch_dir, environment, release_line = launchd_supervisor._prepare_api_command(
        repo_root, live_mode=False, port=port
    )

    release_dir = repo_root.resolve() / ".runtime" / "api" / "releases" / sha
    assert launch_dir == release_dir
    assert is_release_root(release_dir)
    assert release_line.startswith(f"release: {sha} reused: False")
    assert sorted(path.name for path in release_dir.iterdir() if not path.is_symlink()) == [
        ".release-manifest.json",
        "schemas",
        "scripts",
        "tests",  # holds only the live-data link tests/fixtures
    ]
    # Written after publication: the API must read it from the live checkout through the snapshot's link.
    doc_path = "docs/research/release-live-data.md"
    (repo_root / "docs" / "research").mkdir()
    (repo_root / doc_path).write_text("# Live release data\n", encoding="utf-8")

    log_path = tmp_path / "release-api.log"
    with log_path.open("wb") as log_file:
        process = subprocess.Popen(command, cwd=launch_dir, env=environment, stdout=log_file, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 90
            while True:
                if process.poll() is not None:
                    pytest.fail(f"release API exited during startup:\n{log_path.read_text(encoding='utf-8')}")
                try:
                    served = json.loads(_get(f"http://127.0.0.1:{port}{_ROUTE}"))
                    break
                except (urllib.error.URLError, ConnectionError, TimeoutError):
                    if time.monotonic() >= deadline:
                        pytest.fail(f"release API did not become ready:\n{log_path.read_text(encoding='utf-8')}")
                    time.sleep(0.1)
            docs_text = _get(f"http://127.0.0.1:{port}/files/{doc_path}").decode("utf-8")
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)

    assert served == {"product": _ENTRY_MODULE}
    origins = json.loads(origins_path.read_text(encoding="utf-8"))
    print(json.dumps(origins, indent=2, sort_keys=True))
    installed = Path(lu_machinery.__file__).resolve().parent
    product_origin = Path(origins[_ENTRY_MODULE]).resolve()
    assert product_origin == release_dir / "scripts" / "release_fixture_product" / "entry.py"
    for name in ("lu_machinery", "lu_machinery.api.main"):
        origin = Path(origins[name]).resolve()
        assert origin.is_relative_to(installed), (name, origin)
        assert not origin.is_relative_to(release_dir), (name, origin)
    assert docs_text == "# Live release data\n"

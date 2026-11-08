"""Isolate the process-global status cache between API tests.

Pytest shortens very long test names onto one temporary directory. A snapshot
keyed only by that directory would otherwise be served to the next case.

Per-account Claude rows (#10630) ask the provider who owns a directory's
credentials. No API test reaches the provider: the question is refused unless a
test answers it with a fixture, and a background revalidation a test started is
waited for while that test's patches still hold.

The probe refuses a sandbox inside Git, and the pre-push gate runs pytest with
its base temp inside the Git common directory, so every fixture path is truly
inside Git there (#10646). The sandbox's Git check judges only the directories
a test builds below ``tmp_path``.

The installed package derives no product or shared-state root (``lu_machinery.common.repo_root``),
and several modules here build the Monitor app at import (``from lu_machinery.api.main import
app``). In the product suite the root conftest named its own checkout; here this conftest names
two Git repositories for the session instead, unless the environment already configures them.
The product root holds only the product data file the Monitor app reads
(``lu_machinery.api.product_contribution.PRODUCT_DATA``) and an entry whose ``build_api`` adds
nothing, so the app is the machinery app alone. The entry's ``scripts`` modules are dropped from
``sys.modules`` after collection and after each test, so another test's product root never meets
a cached ``scripts`` package from this one. The roots are removed at the end of the session.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT_VARIABLES = {"LU_PRODUCT_ROOT": "product", "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT": "state"}
_ENTRY_MODULE = "scripts.machinery_test_product.entry"
_ENTRY_SOURCE = '''"""Test product entry: the product adds nothing to the Monitor app."""

from lu_machinery.api.product_contribution import ProductContribution


def build_api(context):
    return ProductContribution()
'''


def _write_product(root: Path) -> None:
    from lu_machinery.api.product_contribution import PRODUCT_DATA, PRODUCT_DATA_SCHEMA

    data = root / PRODUCT_DATA
    data.parent.mkdir(parents=True)
    data.write_text(
        json.dumps({"schema": PRODUCT_DATA_SCHEMA, "schema_version": 1, "product_api": _ENTRY_MODULE}),
        encoding="utf-8",
    )
    package = root.joinpath(*_ENTRY_MODULE.split(".")[:-1])
    package.mkdir(parents=True, exist_ok=True)
    for directory in (root / "scripts", package):
        (directory / "__init__.py").write_text("", encoding="utf-8")
    (package / "entry.py").write_text(_ENTRY_SOURCE, encoding="utf-8")


def _configure_session_roots() -> Path | None:
    missing = [name for name in _ROOT_VARIABLES if not os.environ.get(name)]
    if not missing:
        return None
    base = Path(tempfile.mkdtemp(prefix="lu-api-roots-"))
    for name in missing:
        root = base / _ROOT_VARIABLES[name]
        root.mkdir()
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        if name == "LU_PRODUCT_ROOT":
            _write_product(root)
        os.environ[name] = str(root)
    return base


_SESSION_ROOTS = _configure_session_roots()


def _forget_entry_modules() -> None:
    if _SESSION_ROOTS is None:
        return
    for name, module in tuple(sys.modules.items()):
        origin = getattr(module, "__file__", None) or ""
        if (name == "scripts" or name.startswith("scripts.")) and origin.startswith(str(_SESSION_ROOTS)):
            del sys.modules[name]


def pytest_collection_finish(session: pytest.Session) -> None:
    _forget_entry_modules()


@pytest.fixture(autouse=True)
def _forget_test_product_modules():
    yield
    _forget_entry_modules()


def pytest_unconfigure(config: pytest.Config) -> None:
    if _SESSION_ROOTS is not None:
        shutil.rmtree(_SESSION_ROOTS, ignore_errors=True)


@pytest.fixture(autouse=True)
def _judge_git_below_tmp_path(tmp_path, monkeypatch):
    from lu_machinery.api import claude_account_usage

    boundary = tmp_path.resolve()

    def inside_git(path: Path) -> bool:
        return any(
            claude_account_usage._git_tree(directory)
            for directory in (path, *path.parents)
            if directory.is_relative_to(boundary)
        )

    monkeypatch.setattr(claude_account_usage, "_inside_git", inside_git)


def _no_provider(directory: int, account: str) -> None:
    from lu_machinery.api.claude_account_usage import ClaudeAccountRefused

    raise ClaudeAccountRefused("claude_account_identity_unavailable")


@pytest.fixture(autouse=True)
def _isolate_status_ttl_cache(monkeypatch):
    from lu_machinery.api import claude_account_usage
    from lu_machinery.api.state_helpers import cache_invalidate

    cache_invalidate()
    claude_account_usage._reset_for_tests()
    monkeypatch.setattr(
        claude_account_usage, "_private_usage", lambda: SimpleNamespace(prove_claude_owner=_no_provider)
    )
    yield
    cache_invalidate()
    claude_account_usage._reset_for_tests()

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
app``). In the product suite the root conftest named its own checkout. Here this conftest names
two session Git repositories instead, for this directory only: around the collection of each
module below it, and for each test below it through ``monkeypatch``, whatever the inherited
environment names. The environment is restored after each collection and each test, and when
the run leaves this directory the API config's cached roots and the cached production context
are dropped too, so tests in other directories still see no root and keep their no-root
refusals.

The product root holds only the product data file the Monitor app reads
(``lu_machinery.api.product_contribution.PRODUCT_DATA``) and an entry whose ``build_api`` adds
nothing, so the app is the machinery app alone. The entry's ``scripts`` modules are dropped from
``sys.modules`` after each collection and each test, so another test's product root never meets
a cached ``scripts`` package from this one. Before each test the session state root is emptied
(its ``.git`` stays), so state one test writes there, such as persisted account samples under
``batch_state/routing_budget``, never reaches the next test. The roots are removed at the end of
the session.
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


def _session_roots() -> tuple[Path, dict[str, str]]:
    base = Path(tempfile.mkdtemp(prefix="lu-api-roots-"))
    values = {}
    for name in _ROOT_VARIABLES:
        root = base / _ROOT_VARIABLES[name]
        root.mkdir()
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        if name == "LU_PRODUCT_ROOT":
            _write_product(root)
        values[name] = str(root)
    return base, values


_SESSION_ROOTS, _ROOT_VALUES = _session_roots()
# The API config caches these on first use (lu_machinery.api.config.__getattr__).
_CONFIG_ROOT_CACHES = (
    "PROJECT_ROOT", "PRODUCT_ROOT", "LIVE_REPO_ROOT", "CURRICULUM_ROOT", "DASHBOARDS_DIR", "BATCH_STATE_DIR",
    "MESSAGE_DB",
)


def _forget_entry_modules() -> None:
    for name, module in tuple(sys.modules.items()):
        origin = getattr(module, "__file__", None) or ""
        if (name == "scripts" or name.startswith("scripts.")) and origin.startswith(str(_SESSION_ROOTS)):
            del sys.modules[name]


def _forget_session_roots() -> None:
    """Drop what was cached from the session roots: the product entry modules, config roots, context."""
    _forget_entry_modules()
    config = sys.modules.get("lu_machinery.api.config")
    if config is not None:
        for name in _CONFIG_ROOT_CACHES:
            vars(config).pop(name, None)
    context = sys.modules.get("lu_machinery.api.monitor_context")
    if context is not None:
        context._cached_production_context.cache_clear()


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(collector: pytest.Collector):
    """Collect each module below this directory with the session roots configured, then restore."""
    previous = {name: os.environ.get(name) for name in _ROOT_VALUES}
    os.environ.update(_ROOT_VALUES)
    try:
        return (yield)
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        _forget_session_roots()


@pytest.fixture(autouse=True)
def _session_roots_for_this_test(monkeypatch):
    for name, value in _ROOT_VALUES.items():
        monkeypatch.setenv(name, value)
    state = _SESSION_ROOTS / _ROOT_VARIABLES["LEARN_UKRAINIAN_PRIMARY_REPO_ROOT"]
    for child in state.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    yield
    _forget_entry_modules()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None):
    """After the last test of this directory, once its fixtures are undone, drop the cached roots."""
    try:
        return (yield)
    finally:
        here = Path(__file__).parent
        if nextitem is None or here not in Path(nextitem.path).parents:
            _forget_session_roots()


def pytest_unconfigure(config: pytest.Config) -> None:
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

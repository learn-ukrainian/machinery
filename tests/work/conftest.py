"""Opt-in roots for Work API tests that build the Monitor app.

The installed package builds that app only when a product entry is configured.
The shared product tree does not carry ``scripts/curriculum/monitor_paths.v1.json``,
so these tests build a minimal entry in tmp and a git primary, then drop the
cached app and config roots so later tests still see no root.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ENTRY_MODULE = "scripts.machinery_test_product.entry"
_ENTRY_SOURCE = '''"""Test product entry: the product adds nothing to the Monitor app."""

from lu_machinery.api.product_contribution import ProductContribution


def build_api(context):
    return ProductContribution()
'''
_CONFIG_ROOT_CACHES = (
    "PROJECT_ROOT",
    "PRODUCT_ROOT",
    "LIVE_REPO_ROOT",
    "CURRICULUM_ROOT",
    "DASHBOARDS_DIR",
    "BATCH_STATE_DIR",
    "MESSAGE_DB",
)


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


def _drop_scripts() -> None:
    """Drop any cached product ``scripts`` package.

    The pytest job runs every tranche in one process. An earlier product test
    leaves ``scripts`` imported from its own temporary checkout, and the next
    product load then refuses that file. Each opt-in starts clean.
    """
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            del sys.modules[name]


def _forget(product: Path) -> None:
    _drop_scripts()
    del product
    config = sys.modules.get("lu_machinery.api.config")
    if config is not None:
        for name in _CONFIG_ROOT_CACHES:
            vars(config).pop(name, None)
    context = sys.modules.get("lu_machinery.api.monitor_context")
    if context is not None and hasattr(context, "_cached_production_context"):
        context._cached_production_context.cache_clear()
    main = sys.modules.get("lu_machinery.api.main")
    if main is not None:
        vars(main).pop("app", None)


@pytest.fixture(autouse=True)
def release_monitor_app(monkeypatch):
    """Undo ``create_app``'s process-wide providers and root caches after each test.

    Building an app installs providers and caches the roots. The pytest job runs
    every directory in one process, so the next directory must see the providers
    and roots it would have seen without this test (T4, #185).
    """
    from lu_machinery.api import product_contribution

    for name in ("_atlas", "_fleet_batches", "_product"):
        monkeypatch.setattr(product_contribution, name, getattr(product_contribution, name))
    yield
    _forget(Path("."))


@pytest.fixture(scope="module")
def monitor_roots(tmp_path_factory: pytest.TempPathFactory):
    """One minimal product entry and git primary for the module, then cleared."""
    base = tmp_path_factory.mktemp("monitor-roots")
    product = base / "product"
    _write_product(product)
    primary = base / "primary"
    primary.mkdir()
    git = ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid"]
    for args in (
        ["init", "-q", "-b", "main"],
        ["commit", "-q", "--allow-empty", "-m", "init"],
    ):
        subprocess.run([*git, *args], cwd=primary, check=True, capture_output=True, timeout=30)
    names = ("LU_PRODUCT_ROOT", "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT")
    previous = {name: os.environ.get(name) for name in names}
    os.environ["LU_PRODUCT_ROOT"] = str(product)
    os.environ["LEARN_UKRAINIAN_PRIMARY_REPO_ROOT"] = str(primary)
    _forget(product)
    yield
    for name, value in previous.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    _forget(product)

"""Artifact paths come from the configured product root, never from the install location.

Defect class: the default artifact store derived from ``paths.__file__`` points into
site-packages when installed; it must follow ``LU_PRODUCT_ROOT`` and refuse when unset.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lu_machinery.common.repo_root import PRODUCT_ROOT_ENV, ProductRootError
from lu_machinery.storage import artifacts, paths


def _git_product(tmp_path: Path) -> Path:
    product = tmp_path / "product"
    product.mkdir()
    subprocess.run(["git", "init", "-q", str(product)], check=True)
    return product


def test_store_root_follows_configured_product_root(tmp_path, monkeypatch):
    product = _git_product(tmp_path)
    monkeypatch.setenv(PRODUCT_ROOT_ENV, str(product))
    monkeypatch.delenv("LU_ARTIFACT_STORE", raising=False)
    before = sorted(tmp_path.rglob("*"))
    store = paths.artifact_store_root()
    assert store == product.resolve() / "data/.artifact-store"
    assert not Path(paths.__file__).resolve().parent.parent.parent in store.parents
    assert sorted(tmp_path.rglob("*")) == before


def test_unset_product_root_refuses(monkeypatch):
    monkeypatch.delenv(PRODUCT_ROOT_ENV, raising=False)
    with pytest.raises(ProductRootError):
        paths.artifact_store_root()
    with pytest.raises(ProductRootError):
        artifacts.write_artifact(Path("x"), "g", "p", lambda p: None)


def test_no_install_location_constants():
    assert not hasattr(paths, "ROOT")
    assert not hasattr(paths, "DATA_ROOT")
    assert not hasattr(artifacts, "ROOT")

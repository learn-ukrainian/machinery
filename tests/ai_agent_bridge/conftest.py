"""Opt-in fixtures for the bridge tests.

Roots come from the shared ``tests.helpers.product_tree.configured_roots`` fixture (CTO
ruling 4, PORT-1108); this module selects no product or primary root.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _wake_files_in_tmp(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep channel wake files out of the shared product tree.

    A post touches each recipient's wake file under ``AB_WAKE_DIR``, by default
    ``<bridge repository>/.agent/wake``; the bridge repository is the configured product,
    which ``configured_roots`` shares across the session and only reads.
    """
    monkeypatch.setenv("AB_WAKE_DIR", str(tmp_path_factory.mktemp("wake")))


@pytest.fixture
def acp_runs_in_product(configured_roots: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the root conftest's ACP test hook at the configured product.

    The bridge runs ACP calls in ``_config.REPO_ROOT``, the configured product. The root
    conftest names the checkout that runs the tests as ``LU_TEST_ACP_PRIMARY_ROOT``, which
    in learn-ukrainian was also the bridge's repository; the shared product tree is not a Git
    checkout, so the real helper would refuse it before the stubbed ACP call.
    """
    monkeypatch.setenv("LU_TEST_ACP_PRIMARY_ROOT", str(configured_roots.product))

"""Shared isolation for hygiene tests."""

from __future__ import annotations

import subprocess

import pytest

from lu_machinery.common.repo_root import machinery_root


@pytest.fixture(scope="session")
def _installed_state_anchor(tmp_path_factory):
    """A primary checkout the installed package will accept as its state anchor.

    The wheel derives no checkout. ``git init`` yields a real ``.git`` directory,
    which is the only layout ``LEARN_UKRAINIAN_PRIMARY_REPO_ROOT`` allows.
    """
    anchor = tmp_path_factory.mktemp("state-anchor")
    subprocess.run(["git", "init", "-q", str(anchor)], check=True, capture_output=True)
    return anchor


@pytest.fixture(autouse=True)
def installed_package_roots(_installed_state_anchor, monkeypatch):
    """Name the state anchor and the installed package for code that no longer derives them."""
    monkeypatch.setenv("LEARN_UKRAINIAN_PRIMARY_REPO_ROOT", str(_installed_state_anchor))
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(machinery_root()))


@pytest.fixture(autouse=True)
def isolated_state_home(tmp_path_factory, monkeypatch):
    """Point XDG_STATE_HOME at a private directory so no test touches the user's real temp-sweep ledger (#9887)."""
    state = tmp_path_factory.mktemp("xdg-state")
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    return state

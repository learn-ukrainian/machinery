"""Shared isolation for hygiene tests.

Configured roots come from ``tests.helpers.product_tree.configured_roots``.
Each test module opts in. This file does not set a product or primary root.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_state_home(tmp_path_factory, monkeypatch):
    """Point XDG_STATE_HOME at a private directory so no test touches the user's real temp-sweep ledger (#9887)."""
    state = tmp_path_factory.mktemp("xdg-state")
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    return state

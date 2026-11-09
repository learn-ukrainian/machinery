"""Cold import of the issue-stream audit must see redirected refresh sidecars (#10519)."""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

from tests.helpers.restore_import_state import restore_import_state
from tests.helpers.product_tree import configured_roots as configured_roots

pytestmark = pytest.mark.usefixtures("configured_roots")

_AUDIT = "lu_machinery.orchestration.issue_stream_audit"


def test_cold_import_redirects_refresh_sidecars(_write_ownership_base: Path, configured_roots) -> None:
    """A first import inside the test body must not bind the live primary sidecars.

    The autouse fixture used to return while this module was absent, so the
    import below received the primary checkout's state and lock paths.
    """
    with restore_import_state(_AUDIT):
        sys.modules.pop(_AUDIT, None)
        parent = sys.modules.get("lu_machinery.orchestration")
        if isinstance(parent, types.ModuleType):
            parent.__dict__.pop("issue_stream_audit", None)
        assert _AUDIT not in sys.modules

        audit = importlib.import_module(_AUDIT)

        state, lock = audit.refresh_sidecar_paths()
        live_state, live_lock = audit.refresh_sidecar_paths(configured_roots.primary)
        assert state != live_state
        assert lock != live_lock
        assert state.parent.parent == _write_ownership_base
        assert lock.parent == state.parent
        assert state.parent.name.startswith("issue-audit-refresh-")
        assert state.name == "issue_stream_audit_refresh.json"
        assert lock.name == "issue_stream_audit_refresh.lock"

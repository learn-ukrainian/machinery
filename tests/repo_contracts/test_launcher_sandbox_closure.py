"""Installed launchers need the contracts shim without copied checkout code.

Machinery PR #170 removes the checkout package before pytest. The frozen LU
regression protected source-copy closure under PYTHONPYCACHEPREFIX; that source
copy is no longer part of the wheel contract. Keep the shim import executable
and reject checkout machinery source in the sandbox closure.
"""

from __future__ import annotations

import sys
from pathlib import Path

from lu_machinery.fleet_comms import contracts
from tests.launcher_sandbox import slot_registry_sources


def test_slot_registry_closure_includes_fleet_comms_contracts_shim() -> None:
    sources = slot_registry_sources()
    copied_code = [
        path for path in sources
        if path.parts[0] in {"scripts", "lu_machinery", "agents_extensions"}
        and path.suffix in {".py", ".sh"}
    ]
    assert copied_code == []
    assert Path(contracts.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
    assert contracts.CompletionState.COMPLETE.value == "complete"

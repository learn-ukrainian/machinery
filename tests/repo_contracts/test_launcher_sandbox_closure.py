"""Launcher sandboxes copy the installed release, including prefix-bytecode shims.

Defect class: omitting a compatibility shim from the measured installed closure
breaks sandbox imports. T8 (PR #199) copies installed machinery rather than
checkout sources. Preserve the original prefix-bytecode regression under that
contract and verify the copied shim executes independently of the checkout.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from lu_machinery.common.repo_root import machinery_root
from lu_machinery.fleet_comms import contracts
from tests.launcher_sandbox import copy_slot_registry, launcher_environment, slot_registry_sources


def test_slot_registry_closure_includes_fleet_comms_contracts_shim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prefix = tmp_path / "bytecode"
    monkeypatch.setattr(
        "tests.launcher_sandbox.launcher_environment",
        lambda: launcher_environment(PYTHONPYCACHEPREFIX=str(prefix)),
    )
    slot_registry_sources.cache_clear()
    sources = set(slot_registry_sources())
    release = machinery_root().resolve()
    assert release.is_relative_to(Path(sys.prefix).resolve())
    shim = Path("lu_machinery/fleet_comms/contracts.py")
    assert shim in sources
    for relative in sources:
        source = (release.parent / relative).resolve()
        assert source.is_relative_to(release.parent)
        assert source.is_file()
    assert Path(contracts.__file__).resolve().is_relative_to(release)
    sandbox = tmp_path / "sandbox"
    copy_slot_registry(sandbox)
    assert (sandbox / shim).read_bytes() == (release.parent / shim).read_bytes()
    probe = subprocess.run(
        [
            sys.executable, "-c",
            "import json; from lu_machinery.fleet_comms import contracts; "
            "print(json.dumps([contracts.__file__, contracts.__cached__, "
            "contracts.CompletionState.COMPLETE.value]))",
        ],
        cwd=sandbox,
        env=launcher_environment(PYTHONPYCACHEPREFIX=str(prefix)),
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    imported, cached, state = json.loads(probe.stdout)
    assert Path(imported).resolve() == (sandbox / shim).resolve()
    assert Path(cached).is_relative_to(prefix)
    assert Path(cached).is_file()
    assert state == "complete"

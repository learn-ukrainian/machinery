"""Configured roots for the bridge tests.

The installed package derives no root from its own location: the bridge reads the product
checkout through ``product_root()`` (``LU_PRODUCT_ROOT``) and its broker database, pid
directory, watcher locks and reply sidecars under ``state_root()``
(``LEARN_UKRAINIAN_PRIMARY_REPO_ROOT``), each at the call (C-4843 step 5, #99). Every test
here gets a fresh, empty product checkout and a fresh primary checkout for state, outside
the test's own ``tmp_path``, so no test reads or writes a real checkout or shares state with
another test. The root conftest's ACP test hook (``LU_TEST_ACP_PRIMARY_ROOT``) names the
checkout the bridge works in, which is the configured product here, as it was in the product
repository. A test that needs particular paths uses the ``bridge_roots`` fixture, or sets
the bridge's own overrides (``AB_DB_PATH``, ``AB_PID_DIR``, ``AB_REPO_ROOT``).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import NamedTuple

import pytest

_CLEARED = (
    "AB_DB_PATH",
    "AB_PID_DIR",
    "AB_REPO_ROOT",
    "LEARN_UKRAINIAN_STATE_ROOT",
    "FLEET_COMMS_ROOT",
)


class BridgeRoots(NamedTuple):
    product: Path
    state: Path


@pytest.fixture(autouse=True)
def bridge_roots(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> BridgeRoots:
    base = tmp_path_factory.mktemp("bridge-roots")
    product = base / "product"
    state = base / "state"
    for checkout in (product, state):
        subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(product))
    monkeypatch.setenv("LU_TEST_ACP_PRIMARY_ROOT", str(product))
    monkeypatch.setenv("LEARN_UKRAINIAN_PRIMARY_REPO_ROOT", str(state))
    for name in _CLEARED:
        monkeypatch.delenv(name, raising=False)
    return BridgeRoots(product, state)


_PRODUCT_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "product"


@pytest.fixture
def product_files(bridge_roots: BridgeRoots) -> Path:
    """The product files the bridge reads, in this test's configured product checkout.

    The product supplies them; the installed package has none (C-4746). The dispatch
    admission policy and the AGY hook overlay are the product's own files at
    learn-ukrainian 5aba2adb, since their content is validated. The worker hook list names
    one guard the release ships, and the review protocol is a stand-in: no test here reads
    their content.
    """
    product = bridge_roots.product
    shutil.copytree(_PRODUCT_FIXTURE, product, dirs_exist_ok=True)
    settings = product / "agents_extensions/shared/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/enforce-venv.sh"},
        ]}]}}),
        encoding="utf-8",
    )
    protocol = product / "docs/review-protocol.md"
    protocol.parent.mkdir(parents=True)
    protocol.write_text("# Review protocol (test stand-in)\n", encoding="utf-8")
    return product

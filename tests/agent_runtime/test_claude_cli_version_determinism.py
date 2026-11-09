"""Claude invocation tests never depend on the installed CLI version (#9903).

A fake ``claude`` that reports an old version sits first on ``PATH``. Tests
that build an invocation must still pass through the shared probe stub in
``tests/conftest.py``. The gate's own tests, marked ``real_claude_cli_gate``,
keep the real probe and still fail closed.
"""

import importlib
import os

import pytest

OLD_VERSION = "2.1.50"
ALIASES = ("lu_machinery.agent_runtime.adapters.claude",)


@pytest.fixture
def old_claude_on_path(tmp_path, monkeypatch):
    """Put a fake ``claude`` reporting an old version first on ``PATH``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(f'#!/bin/sh\necho "{OLD_VERSION} (Claude Code)"\n', encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return fake


@pytest.mark.real_claude_cli_gate
@pytest.mark.parametrize("alias", ALIASES)
def test_real_gate_still_rejects_old_cli_on_every_alias(alias, old_claude_on_path):
    module = importlib.import_module(alias)
    with pytest.raises(RuntimeError, match=r"Claude CLI < 2\.1\.116"):
        module._ensure_supported_claude_cli_version((str(old_claude_on_path),))

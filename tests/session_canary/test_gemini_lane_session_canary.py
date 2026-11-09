"""Regression coverage for the Gemini session-canary command parser."""

from __future__ import annotations

import subprocess

import pytest
from lu_machinery.session_canary import gemini_lane


@pytest.mark.parametrize("subcommand", ["bootstrap", "protocol", "status"])
def test_subcommands_accept_missing_stream_argument(
    subcommand: str,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Commands without ``--stream`` must not access a missing Namespace attribute."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True, timeout=30)
    monkeypatch.setenv("LEARN_UKRAINIAN_PRIMARY_REPO_ROOT", str(tmp_path))

    assert gemini_lane.main([subcommand, "--epic", "harness"]) == 0

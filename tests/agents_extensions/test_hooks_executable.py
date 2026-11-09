import subprocess
from pathlib import Path

import pytest

from lu_machinery.common.repo_root import machinery_path

pytestmark = pytest.mark.repo_wide

REPO_ROOT = Path(__file__).resolve().parents[2]

def test_hooks_are_executable_in_git():
    """Verify that all hooks in agents_extensions/shared/hooks/ have the 100755 executable permission in git."""
    hooks_dir = machinery_path("agents_extensions/shared/hooks")
    assert hooks_dir.is_dir(), f"Hooks directory not found at {hooks_dir}"

    # Run git ls-files -s to inspect the staging area/index file modes
    cmd = ["git", "ls-files", "-s", "agents_extensions/shared/hooks/"]
    res = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=True, timeout=30)

    lines = res.stdout.strip().split("\n")
    assert lines and lines[0], "No hook files tracked under agents_extensions/shared/hooks/"

    non_executable = []
    for line in lines:
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) >= 4:
            mode = parts[0]
            path = parts[3]
            # Every executable hook retains its Git entrypoint mode.
            if mode != "100755":
                non_executable.append((path, mode))

    assert not non_executable, f"Found non-executable hook files in git tree (expected 100755): {non_executable}"

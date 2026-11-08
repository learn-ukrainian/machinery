"""Use the CI head-built wheel, or build the checkout for a local test run."""
from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path


def release_wheel(repo: Path, scratch: Path) -> Path:
    configured = os.environ.get("LU_TEST_WHEEL")
    if configured:
        wheel = Path(configured)
        if not wheel.is_absolute() or not wheel.is_file():
            raise ValueError("LU_TEST_WHEEL must name an existing absolute wheel path")
        return wheel
    builder = scratch / "builder"
    subprocess.run([sys.executable, "-I", "-m", "venv", str(builder)], check=True, capture_output=True)
    executable = builder / "bin/python"
    requirements = tomllib.loads((repo / "pyproject.toml").read_text())["build-system"]["requires"]
    for args in (
        ["install", "--only-binary=:all:", *requirements],
        ["wheel", "--no-deps", "--no-build-isolation", "--wheel-dir", str(scratch / "wheels"), str(repo)],
    ):
        result = subprocess.run(
            [str(executable), "-I", "-m", "pip", "--disable-pip-version-check", *args],
            cwd=scratch, capture_output=True, text=True, timeout=180,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    wheels = list((scratch / "wheels").glob("lu_machinery-*.whl"))
    assert len(wheels) == 1, wheels
    return wheels[0]

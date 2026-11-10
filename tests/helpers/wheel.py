"""Use the CI head-built wheel, or build the checkout for a local test run."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import sysconfig
import tomllib
import zipfile
from pathlib import Path


def release_wheel(repo: Path, scratch: Path) -> Path:
    configured = os.environ.get("LU_TEST_WHEEL")
    if configured:
        wheel = Path(configured)
        if not wheel.is_absolute() or not wheel.is_file():
            raise ValueError("LU_TEST_WHEEL must name an existing absolute wheel path")
        return wheel
    source = scratch / "wheel-source"
    source.mkdir()
    project = tomllib.loads((repo / "pyproject.toml").read_text())
    # The release ships [tool.setuptools.data-files] too, and setuptools refuses a missing one.
    data_files = project.get("tool", {}).get("setuptools", {}).get("data-files", {})
    for name in ("pyproject.toml", "MANIFEST.in", *(file for files in data_files.values() for file in files)):
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repo / name, source / name)
    for name in ("lu_machinery", "agents_extensions"):
        shutil.copytree(repo / name, source / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    builder = scratch / "builder"
    subprocess.run([sys.executable, "-I", "-m", "venv", str(builder)], check=True, capture_output=True)
    executable = builder / "bin/python"
    requirements = project["build-system"]["requires"]
    for args in (
        ["install", "--only-binary=:all:", *requirements],
        ["wheel", "--no-deps", "--no-build-isolation", "--wheel-dir", str(scratch / "wheels"), str(source)],
    ):
        result = subprocess.run(
            [str(executable), "-I", "-m", "pip", "--disable-pip-version-check", *args],
            cwd=scratch, capture_output=True, text=True, timeout=180,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    wheels = list((scratch / "wheels").glob("lu_machinery-*.whl"))
    assert len(wheels) == 1, wheels
    return wheels[0]


def install_release(wheel: Path, venv: Path, *, dependencies: bool = False) -> Path:
    """Install ``wheel`` alone into a fresh virtual environment at ``venv``; return its interpreter.

    The environment has no pip and no other copy of the package: ``lu_machinery`` imports only
    from the installed wheel. With ``dependencies``, one ``.pth`` file adds a directory of links to
    this interpreter's third-party distributions, so the installed code can import them offline;
    every top-level name the wheel ships, every other machinery distribution and every ``.pth``
    file stay out of it, so no second copy of the package becomes importable.
    """
    subprocess.run([sys.executable, "-I", "-m", "venv", "--without-pip", str(venv)], check=True, capture_output=True)
    python = venv / "bin" / "python"
    installed = subprocess.run(
        [sys.executable, "-I", "-m", "pip", "--disable-pip-version-check", "--python", str(python),
         "install", "--no-deps", "--no-index", "--no-cache-dir", str(wheel)],
        capture_output=True, text=True, timeout=180,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    if dependencies:
        purelib = subprocess.run(
            [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        with zipfile.ZipFile(wheel) as archive:
            shipped = {name.split("/", 1)[0] for name in archive.namelist()}
        links = venv / "test-dependencies"
        links.mkdir()
        for site in dict.fromkeys(Path(sysconfig.get_path(key)) for key in ("purelib", "platlib")):
            for entry in sorted(site.iterdir()):
                name = entry.name
                if (
                    name in shipped
                    or name.startswith(("lu_machinery", "__editable__"))
                    or name.endswith(".pth")
                    or name == "__pycache__"
                    or (links / name).exists()
                ):
                    continue
                (links / name).symlink_to(entry)
        (Path(purelib) / "test-dependencies.pth").write_text(f"{links}\n", encoding="utf-8")
    return python

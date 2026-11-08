"""Launcher preparation must use the running release and reject writable paths."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

from lu_machinery.agent_runtime.adapters import acpx
from lu_machinery.common.repo_root import machinery_root
from tests.helpers.wheel import release_wheel


@pytest.fixture
def private_tree():
    # /tmp is intentionally refused by the production ancestor checks. Keep the
    # proof under the caller's private home, without shortening those checks.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="r25-test-", dir=Path.home()) as directory:
        yield Path(directory)


@pytest.fixture
def source_tree(private_tree):
    source = private_tree / "source"
    source.mkdir()
    checkout = Path(__file__).resolve().parents[2]
    for name in ("pyproject.toml", "MANIFEST.in"):
        shutil.copy2(checkout / name, source / name)
    for name in ("lu_machinery", "agents_extensions"):
        shutil.copytree(machinery_root() if name == "lu_machinery" else checkout / name, source / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # Model a checkout created under umask 022, without changing the shared tree.
    for path in source.rglob("*"):
        if not path.is_symlink():
            path.chmod(path.stat().st_mode & ~0o022)
    launcher = source / "lu_machinery/agent_runtime/claude_acp_exec.sh"
    launcher.write_bytes(launcher.read_bytes() + b"\n# Source-release fixture, without a stored content pin.\n")
    return source


def test_checkout_preparation_captures_running_package(private_tree, source_tree):
    """A checkout must capture its own current launcher bytes without digest pins."""
    subprocess.run(["git", "init", str(source_tree)], check=True, capture_output=True)
    code = "import sys; sys.path.insert(0, " + repr(str(source_tree)) + ");" + _PREPARE
    result = subprocess.run([sys.executable, "-I", "-c", code], cwd=private_tree,
                            check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "launcher preparation passed"


_PREPARE = '''
from pathlib import Path
from lu_machinery.agent_runtime.adapters import acpx
from lu_machinery.common.repo_root import machinery_root
# Preparation only: production cleanup deliberately refuses when /proc is
# unreadable. The owning TemporaryDirectory removes these fixture copies.
acpx._CLAUDE_ACP_LAUNCHER_COPY_BASE = Path.cwd() / "copies"
launcher = Path(acpx._require_claude_acp_launcher(adapter_label="test"))
assert launcher.read_bytes() == (machinery_root() / "agent_runtime/claude_acp_exec.sh").read_bytes()
assert launcher.stat().st_mode & 0o777 == 0o500
print("launcher preparation passed")
'''


def test_installed_wheel_preparation_without_checkout(private_tree, source_tree):
    """The wheel must prepare its launcher without git metadata or source imports."""
    source = source_tree
    wheel = release_wheel(Path(__file__).resolve().parents[2], private_tree)
    venv = private_tree / "venv"
    # Reuse test dependencies, but install machinery itself as a wheel. -I and
    # a cwd outside the checkout prevent source-tree imports in the child.
    subprocess.run([sys.executable, "-m", "venv", "--system-site-packages", str(venv)], check=True)
    interpreter = venv / "bin/python"
    site = venv / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    (site / "test-dependencies.pth").write_text(sysconfig.get_path("purelib") + "\n")
    subprocess.run([str(interpreter), "-m", "pip", "install", "--no-deps", "--force-reinstall",
                    str(wheel)], check=True, capture_output=True, text=True, umask=0o022)
    shutil.rmtree(source)
    result = subprocess.run([str(interpreter), "-I", "-c", '''
import sys
from pathlib import Path
from lu_machinery.agent_runtime.adapters import acpx
from lu_machinery.common.repo_root import machinery_root
assert machinery_root().is_relative_to(Path(sys.prefix))
assert not any((p / ".git").exists() for p in machinery_root().parents)
acpx._CLAUDE_ACP_LAUNCHER_COPY_BASE = Path.cwd() / "copies"
launcher = Path(acpx._require_claude_acp_launcher(adapter_label="wheel-test"))
assert launcher.read_bytes() == (machinery_root() / "agent_runtime/claude_acp_exec.sh").read_bytes()
print("installed-wheel launcher preparation passed")
'''], cwd=private_tree, env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
        check=False, capture_output=True, text=True, umask=0o022)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "installed-wheel launcher preparation passed"


@pytest.mark.parametrize("mode", [0o770, 0o707])
def test_writable_ancestor_above_package_refuses(private_tree, monkeypatch, mode):
    """A writable parent above the resource root must not authorize replacement."""
    parent = private_tree / "unsafe"
    package = parent / "package"
    closure = package / "agent_runtime/claude_acp_exec.sh"
    closure.parent.mkdir(parents=True)
    closure.write_text("#!/bin/sh\nexit 0\n")
    closure.chmod(0o700)
    closure.parent.chmod(0o755)
    package.chmod(0o755)
    parent.chmod(mode)
    # A group shared with another account is unsafe, unlike a user-private group.
    monkeypatch.setattr(acpx, "_claude_code_group_write_permitted", lambda gid: False)
    monkeypatch.setattr(acpx, "machinery_root", lambda: package)
    write_kind = "group" if mode == 0o770 else "world"
    # The refusal must identify the intended parent above the package, not a
    # lower fixture directory or an unrelated private-copy path.
    message = rf"launcher closure file .*: its ancestor directory 3 levels up is {write_kind}-writable"
    with pytest.raises(acpx.ClaudeCodeExecutableRefusal, match=message) as refusal:
        acpx._require_claude_acp_launcher(adapter_label="test")
    assert refusal.value.reason == "untrusted_path"


def test_symlink_closure_refuses(private_tree, monkeypatch):
    """A launcher symlink must be rejected before any target bytes are captured."""
    package = private_tree / "package"
    closure = package / "agent_runtime/claude_acp_exec.sh"
    closure.parent.mkdir(parents=True)
    target = private_tree / "target.sh"
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o700)
    closure.symlink_to(target)
    monkeypatch.setattr(acpx, "machinery_root", lambda: package)
    with pytest.raises(acpx.ClaudeCodeExecutableRefusal, match="symlink") as refusal:
        acpx._require_claude_acp_launcher(adapter_label="test")
    assert refusal.value.reason == "untrusted_path"

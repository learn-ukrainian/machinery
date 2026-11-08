"""One interpreter for hooks, launchers and the shell resolver, and package-only guards.

Defect class: a component override, a worktree .venv, or sys.executable used to
select the interpreter, and a deployed guard used to import a module planted in
the consumer's parent directory.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2]
HOOKS = SOURCE / "agents_extensions" / "shared" / "hooks"
LIB = SOURCE / "lu_machinery" / "lib"
SESSION_STDIN = json.dumps({"session_id": "interpreter-rule", "source": "startup", "model": "claude-opus-5-5"})

GUARDS = {
    "heal-core-bare.py": ("git status", 0),
    "guard-branch-switch-in-main.py": ("git checkout topic", 2),
    "guard-primary-checkout-write.py": ("git reset --hard", 2),
    "guard-pr-merge.py": ("gh pr merge 1", 2),
    "guard-public-github-text.py": ("gh pr view 1", 0),
}


def _executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _wrapper(path: Path, log: Path) -> Path:
    return _executable(
        path,
        f'#!/bin/sh\nprintf "%s\\n" "$0" >> "{log}"\nexec "{sys.executable}" "$@"\n',
    )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    )


def _layout(tmp_path: Path, *, primary_venv: bool, worktree_venv: bool) -> tuple[Path, Path, Path]:
    primary = tmp_path / "primary"
    primary.mkdir(parents=True)
    _git(primary, "init", "-q", "--initial-branch=main")
    _git(primary, "-c", "user.email=fixture@example.invalid", "-c", "user.name=Fixture", "commit", "--allow-empty", "-m", "fixture")
    worktree = tmp_path / "worktree"
    _git(primary, "worktree", "add", "-q", str(worktree), "-b", "task")
    log = tmp_path / "picked.log"
    log.write_text("", encoding="utf-8")
    if primary_venv:
        _wrapper(primary / ".venv" / "bin" / "python", log)
    if worktree_venv:
        _wrapper(worktree / ".venv" / "bin" / "python", log)
    return primary, worktree, log


def _bash(script: str, *, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _base_env(tmp_path: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path),
    }


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


def test_shell_resolver_matrix(tmp_path: Path, home: Path) -> None:
    """The shell resolver selects the override or the primary venv, or refuses."""
    primary, worktree, log = _layout(tmp_path, primary_venv=True, worktree_venv=True)
    override = _wrapper(tmp_path / "override-python", log)
    plain = tmp_path / "plain-file"
    plain.write_text("", encoding="utf-8")
    script = f'source "{LIB / "project_interpreter.sh"}" && project_interpreter_resolve "{worktree}"'
    cases = (
        ("valid-override", {"LEARN_UKRAINIAN_PYTHON": str(override)}, str(override)),
        ("invalid-file", {"LEARN_UKRAINIAN_PYTHON": str(plain)}, None),
        ("invalid-relative", {"LEARN_UKRAINIAN_PYTHON": "python"}, None),
        ("invalid-missing", {"LEARN_UKRAINIAN_PYTHON": str(tmp_path / "missing-python")}, None),
        ("unset", {}, str(primary / ".venv" / "bin" / "python")),
    )
    for name, extra, expected in cases:
        log.write_text("", encoding="utf-8")
        result = _bash(script, env={**_base_env(tmp_path), **extra}, cwd=worktree)
        result.args = (name, *result.args)
        if expected is None:
            assert result.returncode != 0, result.stderr
            assert str(primary / ".venv" / "bin" / "python") not in result.stdout
            assert str(worktree / ".venv" / "bin" / "python") not in result.stdout
        else:
            assert result.returncode == 0, result.stderr
            assert result.stdout.strip() == expected

    bare_primary, bare_worktree, _bare_log = _layout(tmp_path / "bare", primary_venv=False, worktree_venv=True)
    refused = _bash(
        f'source "{LIB / "project_interpreter.sh"}" && project_interpreter_resolve "{bare_worktree}"',
        env=_base_env(tmp_path),
        cwd=bare_worktree,
    )
    assert refused.returncode != 0, refused.stderr
    assert "is not used" in refused.stderr
    assert str(bare_worktree / ".venv" / "bin" / "python") not in refused.stdout
    assert not (bare_primary / ".venv" / "bin" / "python").exists()


def test_launcher_interpreter_matrix(tmp_path: Path) -> None:
    """launcher_project_python follows the same rule and ignores a worktree venv."""
    primary, worktree, log = _layout(tmp_path, primary_venv=True, worktree_venv=True)
    override = _wrapper(tmp_path / "override-python", log)
    plain = tmp_path / "plain-file"
    plain.write_text("", encoding="utf-8")
    script = (
        f'source "{LIB / "launcher_core.sh"}" && '
        'LC_DURABLE_HELPER_ROOT="$1" && launcher_project_python'
    )
    cases = (
        ("valid-override", {"LEARN_UKRAINIAN_PYTHON": str(override)}, str(override)),
        ("invalid-file", {"LEARN_UKRAINIAN_PYTHON": str(plain)}, None),
        ("unset", {}, str(primary / ".venv" / "bin" / "python")),
    )
    for name, extra, expected in cases:
        result = subprocess.run(
            ["bash", "-c", script, "launcher", str(primary)],
            cwd=worktree,
            env={**_base_env(tmp_path), **extra},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if expected is None:
            assert result.returncode != 0, (name, result.stderr)
            assert str(primary / ".venv" / "bin" / "python") not in result.stdout
        else:
            assert result.returncode == 0, (name, result.stderr)
            assert result.stdout.strip() == expected
    missing = _layout(tmp_path / "missing", primary_venv=False, worktree_venv=True)
    refused = subprocess.run(
        ["bash", "-c", script, "launcher", str(missing[0])],
        cwd=missing[1],
        env=_base_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert refused.returncode != 0
    assert str(missing[1] / ".venv" / "bin" / "python") not in refused.stdout


def test_session_setup_interpreter_matrix(tmp_path: Path) -> None:
    """SessionStart selects the override or the primary venv, or refuses."""
    primary, worktree, log = _layout(tmp_path, primary_venv=True, worktree_venv=True)
    override = _wrapper(tmp_path / "override-python", log)
    plain = tmp_path / "plain-file"
    plain.write_text("", encoding="utf-8")
    hook = HOOKS / "session-setup.sh"

    def run(extra: dict[str, str], project: Path) -> subprocess.CompletedProcess[str]:
        log.write_text("", encoding="utf-8")
        env = {
            **_base_env(tmp_path),
            "CLAUDE_PROJECT_DIR": str(project),
            "SESSION_START_BUDGET_SECONDS": "60",
            "LEARN_UKRAINIAN_CLAUDEX_RUN_ID": "interpreter-rule",
            "LEARN_UKRAINIAN_CLAUDEX_LAUNCH_GENERATION": "1",
            "CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS": "1",
            **extra,
        }
        return subprocess.run(
            ["bash", str(hook)],
            cwd=project,
            env=env,
            input=SESSION_STDIN,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )

    selected = run({"LEARN_UKRAINIAN_PYTHON": str(override)}, worktree)
    assert selected.returncode == 0, selected.stderr
    assert log.read_text(encoding="utf-8").split()[0] == str(override)
    assert str(worktree / ".venv" / "bin" / "python") not in log.read_text(encoding="utf-8")

    refused = run({"LEARN_UKRAINIAN_PYTHON": str(plain)}, worktree)
    assert refused.returncode != 0
    assert "not an executable file" in refused.stderr
    assert log.read_text(encoding="utf-8") == ""

    primary_selected = run({}, worktree)
    assert primary_selected.returncode == 0, primary_selected.stderr
    picked = set(log.read_text(encoding="utf-8").split())
    assert str(primary / ".venv" / "bin" / "python") in picked
    assert str(worktree / ".venv" / "bin" / "python") not in picked

    _bare_primary, bare_worktree, bare_log = _layout(tmp_path / "bare", primary_venv=False, worktree_venv=True)
    bare = subprocess.run(
        ["bash", str(hook)],
        cwd=bare_worktree,
        env={
            **_base_env(tmp_path),
            "CLAUDE_PROJECT_DIR": str(bare_worktree),
            "SESSION_START_BUDGET_SECONDS": "60",
        },
        input=SESSION_STDIN,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert bare.returncode != 0
    assert "project interpreter not found" in bare.stderr
    assert "is not used" in bare.stderr
    assert bare_log.read_text(encoding="utf-8") == ""


def test_deployed_guards_do_not_import_a_planted_stdlib_module(tmp_path: Path) -> None:
    """A dataclasses.py planted in the consumer parent is never imported (R72)."""
    product = tmp_path / "consumer" / "product"
    product.mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    for args in (
        ["init", "--initial-branch=main"],
        ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-m", "fixture"],
        ["branch", "topic"],
    ):
        subprocess.run(["git", "-C", str(product), *args], check=True, capture_output=True, env=env)
    release = tmp_path / "site-packages"
    shutil.copytree(SOURCE / "lu_machinery", release / "lu_machinery", ignore=shutil.ignore_patterns("__pycache__"))
    subprocess.run([sys.executable, "-I", "-m", "venv", "--without-pip", str(product / ".venv")], check=True)
    purelib = subprocess.run(
        [str(product / ".venv" / "bin" / "python"), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dependencies = subprocess.run(
        [sys.executable, "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    Path(purelib, "release.pth").write_text(f"{release}\n{dependencies}\n", encoding="utf-8")
    hooks = product / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    for source in HOOKS.glob("*.py"):
        shutil.copy2(source, hooks / source.name)
    plant_log = tmp_path / "plant.log"
    plant = product.parent / "dataclasses.py"
    plant.write_text(
        "import os\n"
        "open(os.environ['PLANT_LOG'], 'a').write('planted\\n')\n"
        "raise ImportError('planted dataclasses')\n",
        encoding="utf-8",
    )
    assert plant == Path(hooks / "heal-core-bare.py").resolve().parents[3] / "dataclasses.py"
    for name, (command, expected) in GUARDS.items():
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": str(product),
            "hook_event_name": "PreToolUse",
            "session_id": "plant",
            "tool_use_id": name,
        }
        result = subprocess.run(
            [str(product / ".venv" / "bin" / "python"), "-I", "-B", str(hooks / name)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            cwd=product,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(tmp_path / "home"),
                "LU_PRODUCT_ROOT": str(product),
                "PLANT_LOG": str(plant_log),
            },
            timeout=60,
            check=False,
        )
        assert result.returncode == expected, (name, result.returncode, result.stderr)
        assert "planted dataclasses" not in result.stderr, (name, result.stderr)
        assert "planted dataclasses" not in result.stdout, (name, result.stdout)
    assert not plant_log.exists()

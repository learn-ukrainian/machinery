"""SessionStart hook run from its own tree: code location and child import isolation (machinery #100 R44, R45).

The hook is run by file path from a machinery tree, the layout the wheel keeps in site-packages: this checkout
against a scratch project that holds no machinery code, or a copy of the tree that is also the cwd and the
project. Each spawned interpreter is the one running pytest.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HOOK = _REPO_ROOT / "agents_extensions" / "shared" / "hooks" / "session-setup.sh"
_STDIN = json.dumps({"session_id": "isolation-probe", "source": "startup", "model": "claude-opus-5-5"})

# A stdlib-named module that logs the importing process, then hands the import to the real module so the
# hook's flow is unchanged. ``sitecustomize`` only logs.
_PLANT = """\
import os as _os, sys as _sys
with open(_os.environ["PLANT_LOG"], "a") as _f:
    _f.write("%s %s\\n" % (__name__, _sys.argv[0] if getattr(_sys, "argv", None) else "<none>"))
if __name__ != "sitecustomize":
    import importlib as _il
    _here = _os.path.dirname(_os.path.abspath(__file__))
    _saved = _sys.path[:]
    _sys.path[:] = [p for p in _sys.path if _os.path.abspath(p or ".") != _here]
    del _sys.modules[__name__]
    try:
        _real = _il.import_module(__name__)
    finally:
        _sys.path[:] = _saved
    _sys.modules[__name__] = _real
"""
# Every stdlib name, so an import made late in a child (after code there changed sys.path) is caught too.
_PLANTED = [*sorted(set(sys.stdlib_module_names) - {"__future__"}), "sitecustomize"]


def _scratch_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    return project


def _tree_copy(tmp_path: Path) -> Path:
    """A machinery tree (``lu_machinery/`` beside the hook) that is also a Git checkout."""
    tree = tmp_path / "tree"
    shutil.copytree(_REPO_ROOT / "lu_machinery", tree / "lu_machinery", ignore=shutil.ignore_patterns("__pycache__"))
    hooks = tree / "agents_extensions" / "shared" / "hooks"
    hooks.mkdir(parents=True)
    shutil.copy2(_HOOK, hooks / _HOOK.name)
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    return tree


def _run_hook(
    *,
    cwd: Path,
    home: Path,
    project: Path | None,
    pythonpath: str | None,
    plant_log: Path,
    hook: Path = _HOOK,
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "PLANT_LOG": str(plant_log),
        "SESSION_START_BUDGET_SECONDS": "60",
        "CLAUDE_SESSION_RECORD_PYTHON": sys.executable,
        "CLAUDE_PROFILE_RESOLVER_PYTHON": sys.executable,
        "CLAUDEX_SUPERVISOR_PYTHON": sys.executable,
        "THREAD_ROLLOVER_PYTHON": sys.executable,
        "LEARN_UKRAINIAN_CLAUDEX_RUN_ID": "isolation-probe-run",
        "LEARN_UKRAINIAN_CLAUDEX_LAUNCH_GENERATION": "1",
        "CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS": "1",
    }
    if project is not None:
        env["CLAUDE_PROJECT_DIR"] = str(project)
    if pythonpath is not None:
        env["PYTHONPATH"] = pythonpath
    return subprocess.run(
        ["bash", str(hook)], cwd=cwd, env=env, input=_STDIN, capture_output=True, text=True, timeout=120, check=False
    )


@pytest.mark.parametrize("project_dir_set", [True, False], ids=["CLAUDE_PROJECT_DIR", "no-CLAUDE_PROJECT_DIR"])
def test_hook_tree_supplies_its_code_to_a_project_without_machinery(tmp_path: Path, project_dir_set: bool) -> None:
    """Defect class: code paths taken from the project, so the installed hook found no resolver (R44)."""
    project = _scratch_project(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    result = _run_hook(
        cwd=project,
        home=home,
        project=project if project_dir_set else None,
        pythonpath=None,
        plant_log=tmp_path / "log",
    )
    assert result.returncode == 0, result.stderr
    assert "resolver not found" not in result.stderr.lower(), result.stderr
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    # The bind ran and the supervisor refused it; an unreachable bind reports "runtime helper is unavailable".
    assert "CLAUDEX SUPERVISOR BIND FAILED: SessionStart did not match the owned child generation." in context
    # The project, never the hook's tree, holds the session state.
    assert (project / ".agent" / "sessions" / "isolation-probe.json").is_file()
    assert not (_REPO_ROOT / ".agent" / "sessions" / "isolation-probe.json").exists()


@pytest.mark.parametrize("pythonpath", [None, ".", ":"], ids=["unset", "dot", "colon"])
def test_hook_python_children_never_import_from_the_cwd(tmp_path: Path, pythonpath: str | None) -> None:
    """Defect class: a hook child resolves an import from the cwd (R45).

    The cwd is the machinery tree and the project, the checkout layout. A child is exposed when it starts with
    the cwd or a cwd ``PYTHONPATH`` entry on ``sys.path``, or when code it imports puts the tree there later.
    """
    tree = _tree_copy(tmp_path)
    for name in _PLANTED:
        (tree / f"{name}.py").write_text(_PLANT, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    plant_log = tmp_path / "plant.log"
    plant_log.write_text("", encoding="utf-8")
    hook = tree / "agents_extensions" / "shared" / "hooks" / _HOOK.name
    result = _run_hook(cwd=tree, home=home, project=tree, pythonpath=pythonpath, plant_log=plant_log, hook=hook)
    assert result.returncode == 0, result.stderr
    assert (tree / ".agent" / "sessions" / "isolation-probe.json").is_file(), "the session gate did not run"
    assert plant_log.read_text(encoding="utf-8") == ""

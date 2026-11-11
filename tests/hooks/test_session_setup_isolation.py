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

from lu_machinery.common.repo_root import machinery_root

_REPO_ROOT = machinery_root().parent
_HOOK = _REPO_ROOT / "agents_extensions" / "shared" / "hooks" / "session-setup.sh"
_STDIN = json.dumps({"session_id": "isolation-probe", "source": "startup", "model": "claude-opus-5-5"})

# A stdlib-named module that logs the importing process, then hands the import to the real module so the
# hook's flow is unchanged. ``sitecustomize`` only logs. It logs with ``os.write``: a planted ``encodings`` loads
# while the interpreter initializes, before ``open`` exists, and that child must still leave a line.
_PLANT = """\
import os as _os, sys as _sys
_fd = _os.open(_os.environ["PLANT_LOG"], _os.O_WRONLY | _os.O_APPEND | _os.O_CREAT, 0o600)
_os.write(_fd, ("%s %s\\n" % (__name__, _sys.argv[0] if getattr(_sys, "argv", None) else "<none>")).encode())
_os.close(_fd)
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
    lib = _HOOK.parent / "context-rollover-lib.sh"
    shutil.copy2(lib, hooks / lib.name)
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
        "LEARN_UKRAINIAN_PYTHON": sys.executable,
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


def _deploy_lines(project: Path, tmp_path: Path, cwd: Path | None = None) -> list[str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    result = _run_hook(cwd=cwd or project, home=home, project=project, pythonpath=None, plant_log=tmp_path / "log")
    assert result.returncode == 0, result.stderr
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    return [line.strip() for line in context.splitlines() if "DEPLOY DRIFT" in line]


def test_session_start_drift_check_matches_the_installed_deployer(tmp_path: Path) -> None:
    """Defect class: the SessionStart drift check gives false drift on a clean release deployment or hides real drift.

    A consumer deployed by the installed release keeps only its own sources, so only the deployer's check can
    tell. A clean deployment with preserved runtime state, reached through a symlinked root, and an undeployed
    linked worktree report nothing; one edited deployed file is drift.
    """
    from lu_machinery.agent_deploy.plan import release_inputs

    project = _scratch_project(tmp_path)
    _files, inventory, _identity = release_inputs()
    # The runtime this hook writes, and a driver's state directory, are preserved runtime content.
    preserve = [".claude/atlas-epic", ".agent/sessions", ".agent/claude-thread-lease.json"]
    descriptor = {"providers": ["claude", "codex", "agy"], "sources": {}, "preserve": preserve + [preserve[2] + ".lock"]}
    for provider, configs in inventory["required_hooks"].items():
        source = project / ("product-" + provider)
        source.mkdir()
        descriptor["sources"][provider] = {"path": source.name, "required": True}
        for destination, requirements in configs.items():
            config: dict = {}
            for requirement in requirements:
                current = config
                for key in requirement["path"][:-1]:
                    current = current.setdefault(key, {})
                current.setdefault(requirement["path"][-1], []).append({**requirement["group"], "hooks": requirement["hooks"]})
            (source / destination.split("/", 1)[1]).write_text(json.dumps(config), encoding="utf-8")
    # The product's own shared source holds only product files, never the release's fleet files.
    (project / "agents_extensions/shared/rules").mkdir(parents=True)
    (project / "agents_extensions/shared/rules/core-curriculum.md").write_text("# Product addendum\n", encoding="utf-8")
    descriptor["sources"]["shared"] = {"path": "agents_extensions/shared", "required": True}
    (project / "agents_extensions/deployment.v1.json").write_text(json.dumps(descriptor), encoding="utf-8")
    deploy = [sys.executable, "-I", "-m", "lu_machinery.agent_deploy", "--project-root", str(project)]
    applied = subprocess.run(deploy, capture_output=True, text=True, check=False)
    assert applied.returncode == 0, applied.stderr
    (project / ".claude/atlas-epic").mkdir()
    (project / ".claude/atlas-epic/DRIVER-STATE.md").write_text("runtime\n", encoding="utf-8")

    # The second start also sees the session state and lease the first one wrote.
    assert _deploy_lines(project, tmp_path) == []
    assert _deploy_lines(project, tmp_path) == []
    link = tmp_path / "project-link"
    link.symlink_to(project)
    assert _deploy_lines(Path(str(link) + "/"), tmp_path, cwd=project) == []
    # A root whose name ends in a newline is unsafe input: it is never checked as its clean namesake.
    unsafe = tmp_path / "project\n"
    shutil.copytree(project, unsafe, symlinks=True)
    rule = next(path for path in sorted((unsafe / ".claude/rules").iterdir()) if path.is_file())
    rule.write_bytes(rule.read_bytes() + b"edited\n")
    unsafe_lines = _deploy_lines(unsafe, tmp_path, cwd=unsafe)
    assert len(unsafe_lines) == 1 and unsafe_lines[0].startswith("- DEPLOY DRIFT CHECK COULD NOT RUN (rc=2):"), unsafe_lines

    for path in ("agents_extensions", "product-claude", "product-codex"):
        subprocess.run(["git", "-C", str(project), "add", path], check=True)
    subprocess.run(
        ["git", "-C", str(project), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "c"],
        check=True,
    )
    worktree = tmp_path / "worktree"
    subprocess.run(["git", "-C", str(project), "worktree", "add", "-q", str(worktree)], check=True)
    assert (worktree / "agents_extensions/deployment.v1.json").is_file()
    assert _deploy_lines(worktree, tmp_path) == []

    deployed = next(path for path in sorted((project / ".claude/rules").iterdir()) if path.is_file())
    deployed.write_bytes(deployed.read_bytes() + b"edited\n")
    assert _deploy_lines(project, tmp_path) == [
        "- DEPLOY DRIFT: 1 file(s) differ between agents_extensions/shared/ and .claude/. Run: npm run agents:deploy"
    ]


@pytest.mark.parametrize("pythonpath", [None, ".", ":"], ids=["unset", "dot", "colon"])
def test_hook_python_children_never_import_from_the_cwd(
    tmp_path: Path, pythonpath: str | None, fake_cursor_bin: Path
) -> None:
    """Defect class: a hook child resolves an import from the cwd (R45).

    The cwd is the machinery tree and the project, the checkout layout. A child is exposed when it starts with
    the cwd or a cwd ``PYTHONPATH`` entry on ``sys.path``, or when code it imports puts the tree there later.
    The suite's fake ``cursor-agent`` (``fake_cursor_bin``, a ``#!python`` script the hook may probe) stands in
    for the provider binary, which is not a Python child of the hook, so its lines are not counted.
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
    planted = [
        line
        for line in plant_log.read_text(encoding="utf-8").splitlines()
        if Path(line.partition(" ")[2]).parent != fake_cursor_bin
    ]
    assert planted == []


# The module bootstrap the hook and the launcher libraries use (#142), here in a child that drives rules_core.
_RULES_CORE_PROBE = """\
import importlib.util, sys
root = sys.argv[1]
pkg = root + "/lu_machinery"
spec = importlib.util.spec_from_file_location("lu_machinery", pkg + "/__init__.py", submodule_search_locations=[pkg])
sys.modules["lu_machinery"] = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sys.modules["lu_machinery"])
from lu_machinery.lib import rules_core
rules_core.resolve_seat(lane="infra", provider="claude", env={})
import os
on_path = [p for p in sys.path if os.path.abspath(p or ".") == root]
print("root_on_sys_path", bool(on_path))
"""


# One bash program per launcher-library site that starts a Python child (#100 R63-R65, R68). Each sources the
# library from the tree copy ($1); $1/.venv/bin/python is the interpreter. The Monitor and the plane point at a
# closed loopback port, so a site that talks to them fails without reaching a live service.
_LIB = "$1/lu_machinery/lib"
_PY = "$1/.venv/bin/python"
_LAUNCHER = (
    f"source {_LIB}/launcher_core.sh && source {_LIB}/handoff_identity.sh && "
    'LC_DURABLE_HELPER_ROOT="$1" LC_ROOT="$1" LC_SESSION_ROOT="$1" && '
)
_LIB_SITES = {
    "thread_rollover_link": f'source {_LIB}/thread_rollover_link.sh && resolve_codex_pending_rollover "$1" codex-infra infra',
    "handoff_identity": f'source {_LIB}/handoff_identity.sh && LC_DURABLE_HELPER_ROOT="$1" _handoff_slot_registry --slot claude-infra',
    "rules_core_sh": f"source {_LIB}/rules_core.sh && rules_core_load {_PY} infra claude && rules_core_toml {_PY} >/dev/null",
    "session_supervisor_claim": (
        f"source {_LIB}/session_supervisor.sh && "
        'claim_session_supervisor_env epic:1 claude claude-code probe-task claude-probe "$1" start-probe.sh infra'
    ),
    "session_supervisor_wrapper_signature": (
        f"{_LAUNCHER}source {_LIB}/session_supervisor.sh && "
        'LC_CTO_SUCCESSOR_WRAPPER="$1/wrapper" LC_DRIVER_ORIGINAL_ARGS=(--epic infra) && '
        "session_supervisor_cto_wrapper_signature"
    ),
    "session_supervisor_publish": (
        f'source {_LIB}/session_supervisor.sh && LC_DURABLE_HELPER_ROOT="$1" SESSION_HANDOFF_AGENT=claude-infra && '
        "session_supervisor_publish_start_failure epic:1 1 watcher-failed"
    ),
    "fleet_comms_plane_mode": f'source {_LIB}/fleet_comms_cold_start.sh && PROJECT_DIR="$1" fleet_comms_resolve_plane_mode',
    "fleet_comms_plane_probe": (
        f'source {_LIB}/fleet_comms_cold_start.sh && PROJECT_DIR="$1" fleet_comms_warn_if_plane_unreachable'
    ),
    "deploy_extensions": (
        f'source {_LIB}/deploy_extensions.sh && printf \'{{"scripts": {{"probe": "x"}}}}\' > "$1/package.json" && '
        'deploy_agent_extensions "$1" probe'
    ),
    "launcher_env_sanitize": (
        f"{_LAUNCHER}LC_PROVIDER=claude LC_MODE=worker && launcher_drop_unneeded_secrets && "
        'case "${LC_CHILD_ENV_COMMAND[*]}" in *PYTHONPATH=*) exit 9 ;; esac'
    ),
    "launcher_git_identity": f"{_LAUNCHER}LC_PROVIDER=claude LC_MODEL=claude-opus-5-5 && launcher_export_git_identity",
    "launcher_model_catalog": f"{_LAUNCHER}LC_PROVIDER=claude LC_MODEL=claude-opus-5-5 && launcher_normalize_model",
    "launcher_cursor_pin": (
        f"{_LAUNCHER}LC_PROVIDER=cursor LC_MODE=driver LC_MODEL=grok-4.7-high LC_CURSOR_SEAT_PIN=grok-4.7-high && "
        "(launcher_validate_cursor_pin)"
    ),
    "launcher_driver_state": (
        f"{_LAUNCHER}LC_MODE=driver LC_DRY_RUN=0 LC_EPIC=infra LU_DRIVER_STATE_PYTHON={_PY} "
        'LU_DRIVER_STATE_FILE="$1/.claude/infra-epic/DRIVER-STATE.md" && launcher_seed_driver_state'
    ),
    "launcher_rollover_bundle": f"{_LAUNCHER}LC_EPIC=infra && launcher_import_rollover_bundle",
    "launcher_observer_heartbeat": (
        f"{_LAUNCHER}LC_PROVIDER=cursor LC_MODE=driver LC_DRY_RUN=0 LC_EPIC=infra && launcher_cursor_observer_presence"
    ),
    "launcher_close_lease": f"{_LAUNCHER}LC_DRIVER_CLOSE_RETRY_SECONDS=0 && launcher_close_driver_lease",
    "launcher_driver_agent": f"{_LAUNCHER}LC_PROVIDER=claude LC_EPIC=infra && launcher_inject_driver_agent",
}


def _launcher_lib_child(site: str, tree: Path) -> list[str]:
    if site == "rules_core":
        return [sys.executable, "-I", "-c", _RULES_CORE_PROBE, str(tree)]
    return ["bash", "-c", _LIB_SITES[site], "probe", str(tree)]


@pytest.mark.parametrize("site", ["rules_core", *_LIB_SITES])
def test_launcher_library_python_children_never_import_from_the_cwd(tmp_path: Path, site: str) -> None:
    """Defect class: a launcher library's Python child resolves an import from the checkout (#100 R63-R65, R68).

    The cwd is the machinery tree and the project, and ``PYTHONPATH`` names it. Each launcher-library site that
    starts a Python child runs (``rules_core.resolve_seat`` with a lane runs in-process), and no planted stdlib
    name may load from the tree. A site whose child starts without ``-I``, runs package code by file path, or puts
    the tree on ``sys.path`` loads a plant.
    """
    tree = _tree_copy(tmp_path)
    for name in _PLANTED:
        (tree / f"{name}.py").write_text(_PLANT, encoding="utf-8")
    venv_bin = tree / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    # The project interpreter records the first argument of every child it starts.
    (venv_bin / "python").write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$1" >> "$CHILD_LOG"\nexec "{sys.executable}" "$@"\n', encoding="utf-8"
    )
    (venv_bin / "python").chmod(0o755)
    child_log = tmp_path / "children.log"
    child_log.write_text("", encoding="utf-8")
    npm = tmp_path / "bin" / "npm"
    npm.parent.mkdir()
    npm.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    npm.chmod(0o755)
    plant_log = tmp_path / "plant.log"
    plant_log.write_text("", encoding="utf-8")
    closed = "http://127.0.0.1:9"
    env = {
        "PATH": f"{npm.parent}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "PLANT_LOG": str(plant_log),
        "CHILD_LOG": str(child_log),
        "PYTHONPATH": ".",
        "LU_MONITOR_LOOPBACK": closed,
        "AB_MONITOR_URL": closed,
        "THREAD_ROLLOVER_COMMAND_RUNNER": str(tree / "lu_machinery" / "agent_runtime" / "bounded_command.py"),
        "THREAD_ROLLOVER_COMMAND_TIMEOUT_SECONDS": "60",
    }
    result = subprocess.run(
        _launcher_lib_child(site, tree), cwd=tree, env=env, capture_output=True, text=True, timeout=120, check=False
    )
    if site == "thread_rollover_link":
        assert result.returncode == 0, result.stdout + result.stderr
        assert "no pending packet for codex-infra" in result.stdout, result.stdout + result.stderr
    elif site == "handoff_identity":
        assert result.returncode in (0, 3), result.stdout + result.stderr
    elif site == "rules_core":
        assert result.returncode == 0, result.stderr
        assert "root_on_sys_path False" in result.stdout, result.stdout + result.stderr
    assert "module bootstrap:" not in result.stderr, result.stderr
    assert plant_log.read_text(encoding="utf-8") == ""
    if site != "rules_core":
        children = child_log.read_text(encoding="utf-8").splitlines()
        assert children, f"the site started no Python child: {result.stdout}{result.stderr}"
        assert set(children) == {"-I"}, children

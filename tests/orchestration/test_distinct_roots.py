"""Machinery roots come from one configuration (#12681, machinery move step 3).

AC-01: on learn-ukrainian and on kube-dojo, dispatch, the closeout control plane, the
hooks' interpreter, the systemd reporter's ExecStart and ``/api/rules`` honour a
non-default root. AC-02: with the configuration unset, every root is today's value.
The configuration is ``lu_machinery/common/repo_root.py``; these tests never touch host state.

Ported from the product repository's suite (C-4843 step 1). AC-02's location-derived
defaults belong to the in-tree source copy only, so those tests run with the resolver in
that mode; the installed package's refusals are in ``tests/test_repo_root.py``. The
product suite's ``/api/rules`` cases are not ported: they need a product API entry module
(``build_api``) that only a product checkout supplies.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lu_machinery.common import repo_root as rr
from lu_machinery.dispatcher import primary as dispatcher_primary
from lu_machinery.lib import rules_core
from lu_machinery.orchestration import worktree_claims, worktree_git

REPO = Path(__file__).resolve().parents[2]
UNIT = REPO / "packaging" / "systemd" / "learn-ukrainian-project-state-reporter.service"
DEFAULT_SLUG = "learn-ukrainian/learn-ukrainian.github.io"
# Fleet catalog keys and their GitHub slugs (lu_machinery/config/fleet_repos.yaml).
REPOS = {"public": DEFAULT_SLUG, "kube-dojo": "kube-dojo/kube-dojo.github.io"}
# A configured slug for the default repository that no catalog entry carries.
CONFIGURED_SLUG = "example-org/moved-primary"


class _Execed(Exception):
    """Raised by the ``os.execv`` stand-in so a re-exec is observed, not performed."""


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*rr.MACHINERY_ENV_VARS, rr.PRODUCT_ROOT_ENV, "LU_TASKS_DIR"):
        monkeypatch.delenv(name, raising=False)


def _subprocess_env(**configured: str) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in (*rr.MACHINERY_ENV_VARS, rr.PRODUCT_ROOT_ENV, "LU_TASKS_DIR")
    }
    return {**env, **configured}


def _import_time_roots(env: dict[str, str]) -> dict[str, str]:
    """Resolve dispatch consumers lazily in a fresh interpreter."""
    code = (
        "import json, sys\n"
        "from lu_machinery.ai_agent_bridge import _dispatch_wrappers as bridge\n"
        "from lu_machinery.common.task_store_paths import tasks_dir\n"
        "from lu_machinery.common.repo_root import MachineryConfigError\n"
        "from lu_machinery.dispatcher import git\n"
        "from lu_machinery.orchestration.task_identity import DEFAULT_REPOSITORY\n"
        "roots, errors, github = {}, {}, {}\n"
        "for key in ('public', 'kube-dojo'):\n"
        "    try:\n"
        "        path, item = git.resolve_repo(key)\n"
        "        roots[key], github[key] = str(path), item['github']\n"
        "    except MachineryConfigError as exc:\n"
        "        roots[key], errors[key] = None, type(exc).__name__\n"
        "for key, resolve in [('tasks_dir', tasks_dir), ('bridge_state', lambda: bridge._task_state_path('probe'))]:\n"
        "    try:\n"
        "        roots[key] = str(resolve())\n"
        "    except MachineryConfigError as exc:\n"
        "        roots[key], errors[key] = None, type(exc).__name__\n"
        "print(json.dumps({**roots, 'errors': errors, 'github': github, 'slug': DEFAULT_REPOSITORY,\n"
        "                  'bridge_dispatcher': bridge.build_ask_review_wait_command('probe', timeout=1)}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    """A configured primary with a kube-dojo sibling, a rules directory and an interpreter, all outside REPO."""
    primary = tmp_path / "estate" / "lu-primary"
    kube = tmp_path / "estate" / "kube-dojo"
    for checkout in (primary, kube):
        (checkout / ".git").mkdir(parents=True)
    rules = tmp_path / "policy" / "rules"
    rules.mkdir(parents=True)
    (rules / "core.md").write_text("# Configured core\n", encoding="utf-8")
    (rules / "core-curriculum.md").write_text("# Machinery decoy addendum\n", encoding="utf-8")
    product = tmp_path / "product"
    marker = product / rr.PRODUCT_MARKER
    marker.parent.mkdir(parents=True)
    marker.write_text("product: fixture\n", encoding="utf-8")
    addendum = product / rules_core.CONTENT_ADDENDUM_REL
    addendum.parent.mkdir(parents=True)
    addendum.write_text("# Configured curriculum addendum\n", encoding="utf-8")
    python = _executable(tmp_path / "interp" / "bin" / "python", "#!/bin/sh\nexit 0\n")
    return {
        "primary": primary,
        "kube-dojo": kube,
        "public": primary,
        "rules": rules,
        "python": python,
        "product": product,
    }


def _configured(layout: dict[str, Path], repo: str) -> dict[str, str]:
    return {
        rr.PRIMARY_ROOT_ENV: str(layout["primary"]),
        rr.GITHUB_SLUG_ENV: CONFIGURED_SLUG,
        rr.STATE_ROOT_ENV: str(layout["primary"]),
        rr.INTERPRETER_ENV: str(layout["python"]),
        rr.PRODUCT_ROOT_ENV: str(layout["product"]),
    }


def _linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """A primary with a dispatcher entry and one linked worktree of it."""
    primary = tmp_path / "runtime-primary"
    admin = primary / ".git" / "worktrees" / "wt"
    admin.mkdir(parents=True)
    _executable(primary / "lu_machinery" / "dispatch.py", "")
    tree = tmp_path / "runtime-wt"
    tree.mkdir()
    (tree / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    return primary, tree


def _reexec(monkeypatch: pytest.MonkeyPatch, runtime: Path) -> list[str]:
    calls: list[list[str]] = []

    def fake_execv(path: str, argv: list[str]) -> None:
        calls.append([path, *argv])
        raise _Execed

    monkeypatch.setattr(dispatcher_primary, "machinery_path", lambda relative: runtime / "lu_machinery" / relative)
    monkeypatch.setattr(dispatcher_primary.os, "execv", fake_execv)
    with pytest.raises(_Execed):
        dispatcher_primary.exec_primary(["list"])
    return calls[0]


def _unit_lines() -> dict[str, list[str]]:
    lines: dict[str, list[str]] = {}
    for line in UNIT.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            lines.setdefault(key, []).append(value)
    return lines


def _systemd_exec_start(home: Path) -> subprocess.CompletedProcess[str]:
    """Run the unit's ExecStart the way systemd does: Environment=, optional EnvironmentFile=, cwd."""
    unit = _unit_lines()
    env: dict[str, str] = {}
    for assignment in unit["Environment"]:
        key, value = assignment.split("=", 1)
        env[key] = value
    for path in unit["EnvironmentFile"]:
        optional = path.startswith("-")
        env_file = Path(path.removeprefix("-").replace("%h", str(home)))
        if not env_file.is_file():
            assert optional, f"required EnvironmentFile missing: {env_file}"
            continue
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#"):
                key, value = line.split("=", 1)
                env[key] = value
    (command,) = unit["ExecStart"]
    (cwd,) = unit["WorkingDirectory"]
    return subprocess.run(
        shlex.split(command.replace("%h", str(home))),
        cwd=cwd.replace("%h", str(home)),
        env={"HOME": str(home), **env},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _systemd_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / "projects").mkdir(parents=True)
    runtime = tmp_path / "runtime-real"
    wrapper = runtime / "lu_machinery/orchestration/run_project_state_reporter.sh"
    wrapper.parent.mkdir(parents=True)
    shutil.copy2(rr.machinery_path("orchestration/run_project_state_reporter.sh"), wrapper)
    library = runtime / "lu_machinery/lib/project_interpreter.sh"
    library.parent.mkdir(parents=True)
    shutil.copy2(rr.machinery_path("lib/project_interpreter.sh"), library)
    (home / "projects/learn-ukrainian").symlink_to(runtime, target_is_directory=True)
    return home


# --------------------------------------------------------------------------- AC-01: a non-default root is honoured


@pytest.mark.parametrize("repo", sorted(REPOS))
def test_dispatch_targets_state_and_slug_follow_the_configuration(monkeypatch, layout, repo: str) -> None:
    roots = _import_time_roots(_subprocess_env(**_configured(layout, repo)))
    assert Path(roots[repo]) == (layout["product"] if repo == "public" else layout[repo]).resolve()
    assert Path(roots["tasks_dir"]) == layout["primary"] / "batch_state" / "tasks"
    assert Path(roots["bridge_state"]) == layout["primary"] / "batch_state" / "tasks" / "probe.json"
    # One slug per process: dispatch identity and task identity both carry the configured one,
    # and a sibling keeps its own catalog slug.
    assert roots["slug"] == roots["github"]["public"] == CONFIGURED_SLUG
    assert roots["github"]["kube-dojo"] == REPOS["kube-dojo"]
    assert roots["bridge_dispatcher"] == [sys.executable, "-m", "lu_machinery.dispatch", "wait", "probe", "--timeout", "1"]

    from lu_machinery.ai_agent_bridge import _acp_compat, _cli
    from lu_machinery.review import target_resolution

    for key, value in _configured(layout, repo).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(_cli, "require_core_or_exit", Mock())
    monkeypatch.setattr(_acp_compat, "require_compat_target", Mock(return_value="claude"))
    resolve = Mock(side_effect=_Execed)
    monkeypatch.setattr(target_resolution, "resolve_review_repository", resolve)
    args = SimpleNamespace(content="review", task_id="root-probe", review=True, repo=repo, branch="feature")
    with pytest.raises(_Execed):
        _cli._handle_acp_compat(args, "claude")
    # The default key resolves from the product checkout, as dispatch does; siblings from the state root.
    assert resolve.call_args.args[0] == (layout["product"] if repo == "public" else layout["primary"])

    from lu_machinery.ai_agent_bridge import _dispatch_wrappers as bridge

    launch = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(bridge.github_client, "run", launch)
    command = [sys.executable, "-m", "lu_machinery.dispatch", "dispatch", "--task-id", "root-probe"]
    assert bridge._run_dispatch(command, False, Path("unused-prompt")) == 0
    assert launch.call_args.kwargs["cwd"] == rr.machinery_path("dispatch.py").parent.parent
    assert launch.call_args.kwargs["cwd"] not in {layout["primary"], layout["product"]}

    monkeypatch.delenv(rr.PRODUCT_ROOT_ENV)
    ask = Mock(return_value=SimpleNamespace(ok=True))
    monkeypatch.setattr(_acp_compat, "run_compat_ask", ask)
    monkeypatch.setattr(_cli, "_resolve_from_llm", Mock(return_value="root-probe"))
    args.repo, args.branch, args.review = None, None, False
    _cli._handle_acp_compat(args, "claude")
    ask.assert_called_once()


@pytest.mark.parametrize("repo", sorted(REPOS))
def test_dispatch_reexec_keeps_the_running_interpreter(monkeypatch, tmp_path: Path, layout, repo: str) -> None:
    for key, value in _configured(layout, repo).items():
        monkeypatch.setenv(key, value)
    runtime_primary, tree = _linked_worktree(tmp_path)
    call = _reexec(monkeypatch, tree)
    assert call == [
        sys.executable,
        sys.executable,
        str(runtime_primary / "lu_machinery/dispatch.py"),
        "list",
    ]


@pytest.mark.parametrize("repo", sorted(REPOS))
def test_closeout_control_plane_is_the_configured_state_root(monkeypatch, layout, repo: str) -> None:
    for key, value in _configured(layout, repo).items():
        monkeypatch.setenv(key, value)
    target = layout[repo]
    assert worktree_claims.public_primary_root() == layout["primary"]
    assert worktree_claims.control_plane_root(target) == layout["primary"].resolve()
    assert worktree_git.control_plane_root(target) == layout["primary"].resolve()


@pytest.mark.parametrize("repo", sorted(REPOS))
def test_hook_interpreter_is_the_configured_interpreter(monkeypatch, layout, repo: str) -> None:
    """``heal-core-bare.py`` and the other agent hooks spawn ``project_interpreter()``."""
    for key, value in _configured(layout, repo).items():
        monkeypatch.setenv(key, value)
    assert rr.project_interpreter() == layout["python"]
    assert rr.project_interpreter(layout[repo]) == layout["python"]


@pytest.mark.parametrize("repo", sorted(REPOS))
def test_systemd_exec_start_runs_with_the_configured_roots(tmp_path: Path, layout, repo: str) -> None:
    home = _systemd_home(tmp_path)
    record = tmp_path / "stand-in.json"
    stand_in = _executable(
        tmp_path / "stand-in" / "python",
        f"#!{sys.executable}\nimport json, os, sys\n"
        f"json.dump({{'argv': sys.argv[1:], 'primary': os.environ.get({rr.PRIMARY_ROOT_ENV!r}),"
        f" 'slug': os.environ.get({rr.GITHUB_SLUG_ENV!r})}},"
        f" open({str(record)!r}, 'w'))\n",
    )
    configured = {**_configured(layout, repo), rr.INTERPRETER_ENV: str(stand_in)}
    env_file = home / ".config" / "learn-ukrainian" / "machinery.env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text(
        "".join(f"{key}={value}\n" for key, value in configured.items()),
        encoding="utf-8",
    )
    result = _systemd_exec_start(home)
    assert result.returncode == 0, result.stderr
    ran = json.loads(record.read_text(encoding="utf-8"))
    # The relative ExecStart (machinery move, C-4755) starts bash in the physical WorkingDirectory: systemd
    # sets no PWD, so the wrapper resolves the physical runtime behind the %h symlink.
    reporter = tmp_path / "runtime-real/lu_machinery/api/project_state_local.py"
    assert ran == {
        "argv": [str(reporter), "report"],
        "primary": str(layout["primary"]),
        "slug": CONFIGURED_SLUG,
    }


@pytest.mark.parametrize("repo", sorted(REPOS))
def test_runtime_target_interpreter_policy_and_state_roots_are_distinct(monkeypatch, layout, repo: str) -> None:
    for key, value in _configured(layout, repo).items():
        monkeypatch.setenv(key, value)
    roots = rr.machinery_roots()
    assert (roots.runtime, roots.primary, roots.state) == (
        rr.RUNTIME_ROOT,
        layout["primary"],
        layout["primary"],
    )
    assert (roots.rules_dir, roots.interpreter, roots.github_slug) == (
        rr.machinery_path(rr.RULES_DIR_REL),
        layout["python"],
        CONFIGURED_SLUG,
    )
    target = layout[repo]
    distinct = {roots.runtime, roots.rules_dir, roots.interpreter, roots.state}
    assert len(distinct) == 4
    if repo == "kube-dojo":
        assert target not in distinct
        assert worktree_claims.control_plane_root(target) == roots.state.resolve() != target.resolve()


# --------------------------------------------------------------------------- AC-02: defaults are today's values


def test_unset_configuration_keeps_todays_roots(clean_env, monkeypatch) -> None:
    assert rr.RUNTIME_ROOT == Path(rr.__file__).resolve().parents[2]
    # This case deliberately models the legacy in-tree copy, independently
    # of where pytest imported the installed resolver.
    monkeypatch.setattr(rr, "_IN_TREE_SOURCE", True)
    monkeypatch.setattr(rr, "RUNTIME_ROOT", REPO)
    # Interpreter selection is not under test here and depends on the host (a linked worktree whose
    # primary has no .venv, run by another checkout's venv, is refused by design), so pin it.
    stand_in = Path(sys.executable)
    monkeypatch.setattr(rr, "project_interpreter", lambda *args, **kwargs: stand_in)
    runtime = REPO
    primary = rr.main_checkout_root(runtime)
    roots = rr.machinery_roots()
    assert roots.interpreter == stand_in
    assert roots.runtime == runtime == REPO
    assert (roots.primary, roots.state, roots.github_slug) == (
        primary,
        primary,
        DEFAULT_SLUG,
    )
    assert roots.rules_dir == Path(rr.__file__).resolve().parents[2] / "agents_extensions/shared/rules"
    assert rr.resolve_repo_root(REPO / "scripts/common/repo_root.py", 2) == primary
    assert worktree_claims.public_primary_root() == primary
    assert rr.rules_dir() == rules_core.ROOT / "agents_extensions/shared/rules"
    with pytest.raises(TypeError):
        rr.rules_dir(Path("/elsewhere"))
    assert rules_core.source_path("docs/x.md", REPO) == REPO / "docs/x.md"
    with pytest.raises(rules_core.RulesCoreMissing, match="LU_PRODUCT_ROOT"):
        rules_core.source_path("docs/x.md")


def test_unset_configuration_refuses_unanchored_dispatch_roots() -> None:
    """Installed dispatch consumers refuse state and product access without anchors."""
    roots = _import_time_roots(_subprocess_env())
    assert roots["public"] is None
    assert roots["kube-dojo"] is None
    assert roots["tasks_dir"] is None
    assert roots["bridge_state"] is None
    assert roots["errors"] == {
        "public": "ProductRootError", "kube-dojo": "StateRootError",
        "tasks_dir": "StateRootError", "bridge_state": "StateRootError",
    }
    assert roots["slug"] == DEFAULT_SLUG
    assert roots["bridge_dispatcher"] == [sys.executable, "-m", "lu_machinery.dispatch", "wait", "probe", "--timeout", "1"]


@pytest.mark.parametrize("git_metadata", ["gitfile", "symlink-to-admin-dir", "copied-admin-dir", "absent"])
def test_a_linked_worktree_is_never_the_configured_primary_or_state_root(
    monkeypatch, tmp_path: Path, git_metadata: str
) -> None:
    """State stays on a primary checkout: a configured worktree would split records from closeout's locks."""
    primary, tree = _linked_worktree(tmp_path)
    admin = primary / ".git" / "worktrees" / "wt"
    (admin / "commondir").write_text("../..\n", encoding="utf-8")
    if git_metadata != "gitfile":
        (tree / ".git").unlink()
    if git_metadata == "symlink-to-admin-dir":
        (tree / ".git").symlink_to(admin, target_is_directory=True)
    elif git_metadata == "copied-admin-dir":
        (tree / ".git").mkdir()
        (tree / ".git" / "commondir").write_text(f"{primary / '.git'}\n", encoding="utf-8")
    monkeypatch.setenv(rr.PRIMARY_ROOT_ENV, str(tree))
    monkeypatch.setenv(rr.STATE_ROOT_ENV, str(tree))
    for resolve in (rr.machinery_roots, rr.state_root, lambda: rr.resolve_repo_root(Path(rr.__file__), 2)):
        with pytest.raises(rr.MachineryConfigError, match="not a linked worktree"):
            resolve()
    with pytest.raises(worktree_claims.ControlPlaneError):
        worktree_claims.public_primary_root()


def test_unset_configuration_keeps_todays_interpreter_order(clean_env, tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    own = _executable(checkout / ".venv" / "bin" / "python", "#!/bin/sh\n")
    assert rr.project_interpreter(checkout) == own


def test_unset_configuration_reexecs_with_this_interpreter(clean_env, monkeypatch, tmp_path: Path) -> None:
    runtime_primary, tree = _linked_worktree(tmp_path)
    call = _reexec(monkeypatch, tree)
    assert call == [sys.executable, sys.executable, str(runtime_primary / "lu_machinery/dispatch.py"), "list"]


def test_unset_configuration_keeps_todays_systemd_exec_start() -> None:
    unit = _unit_lines()
    assert unit["ExecStart"] == [
        "/usr/bin/env bash --noprofile --norc lu_machinery/orchestration/run_project_state_reporter.sh"
    ]
    assert unit["WorkingDirectory"] == ["%h/projects/learn-ukrainian"]
    assert unit["EnvironmentFile"] == ["-%h/.config/learn-ukrainian/machinery.env"]


def test_machinery_path_refuses_absolute_traversal_and_escaping_symlink_inputs(monkeypatch, tmp_path: Path) -> None:
    """Defect class: a package-relative machinery lookup that names a file outside the package."""
    assert rr.machinery_root() == Path(rr.__file__).resolve().parents[1]
    package = (tmp_path / "package").resolve()
    (package / "config").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (package / "escape").symlink_to(outside)
    (package / "inside").symlink_to(package / "config")
    (package / "loop").symlink_to(package / "loop")
    monkeypatch.setattr(rr, "_MACHINERY_ROOT", package)
    assert rr.machinery_path("config/model_catalog.yaml") == package / "config" / "model_catalog.yaml"
    assert rr.machinery_path("inside/model_catalog.yaml") == package / "inside" / "model_catalog.yaml"
    for value in (
        "/etc/passwd",
        str(package / "config" / "model_catalog.yaml"),
        "../outside/x",
        "config/../../outside/x",
        "config/..",
        "./config",
        "config//x",
        "",
        "config/\x00",
        "escape",
        "escape/x",
        "loop/x",
    ):
        with pytest.raises(ValueError):
            rr.machinery_path(value)

"""Root contract cases ported from LU 5aba2adb.

Installed consumers use configured product and state roots. Fleet rules come
from the wheel; the product supplies its curriculum addendum. Legacy in-tree
fallback is exercised explicitly. The product integration_check assertion stays
in LU under PORT-T1-PRODUCT-01.
"""

from __future__ import annotations

import json
import os
import shlex
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
# Fleet catalog keys and their GitHub slugs (scripts/config/fleet_repos.yaml).
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
        # A real Git directory: Git needs HEAD, objects/ and refs/ before it treats .git as one.
        for entry in ("objects", "refs"):
            (checkout / ".git" / entry).mkdir(parents=True)
        (checkout / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
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


def _observe_reexec(monkeypatch: pytest.MonkeyPatch, runtime: Path) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_execv(path: str, argv: list[str]) -> None:
        calls.append([path, *argv])
        raise _Execed

    monkeypatch.setattr(dispatcher_primary, "machinery_path", lambda relative: runtime / "lu_machinery" / relative)
    monkeypatch.setattr(dispatcher_primary.os, "execv", fake_execv)
    return calls


def _unit_lines() -> dict[str, list[str]]:
    lines: dict[str, list[str]] = {}
    for line in UNIT.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            lines.setdefault(key, []).append(value)
    return lines


def _reporter_code_root() -> str:
    """Directory that holds the lu_machinery package a fresh interpreter imports.

    A normal import, from the interpreter's own directory, with no ``PYTHONPATH``.
    The path stays unresolved, including a symlink prefix: ``project_interpreter.sh``
    builds ``PROJECT_CODE_ROOT`` from the script's unresolved location. The unit
    finds its script with ``importlib.resources``, so this expectation does not
    repeat that lookup.
    """
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import lu_machinery, pathlib; "
            "print(pathlib.Path(lu_machinery.__file__).parents[1])",
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=Path(sys.executable).parent,
        env={"PATH": "/usr/bin:/bin"},
    )
    return probe.stdout.strip()


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
    product_python = f"{home}/projects/learn-ukrainian/.venv/bin/python"
    rendered = command.replace("%h", str(home)).replace(product_python, sys.executable, 1)
    return subprocess.run(
        shlex.split(rendered),
        cwd=cwd.replace("%h", str(home)),
        env={"HOME": str(home), **env},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _systemd_home(tmp_path: Path) -> Path:
    """A product checkout with no machinery tree. The unit must load its script from the package."""
    home = tmp_path / "home"
    (home / "projects").mkdir(parents=True)
    runtime = tmp_path / "runtime-real"
    runtime.mkdir()
    (home / "projects/learn-ukrainian").symlink_to(runtime, target_is_directory=True)
    return home


@pytest.fixture
def api_client(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from lu_machinery.api.monitor_context import fixture_context
    from lu_machinery.api.rules_router import router

    app = FastAPI()
    app.state.ctx = fixture_context(tmp_path / "api")
    app.include_router(router, prefix="/api/rules")
    return TestClient(app, raise_server_exceptions=False)


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
    assert roots["bridge_dispatcher"] == [sys.executable, "-I", "-m", "lu_machinery.dispatch", "wait", "probe", "--timeout", "1"]

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
    assert launch.call_args.kwargs["cwd"] == layout["primary"]
    assert launch.call_args.kwargs["cwd"] not in {
        layout["product"], rr.machinery_path("dispatch.py").parent.parent,
    }

    monkeypatch.delenv(rr.PRODUCT_ROOT_ENV)
    ask = Mock(return_value=SimpleNamespace(ok=True))
    monkeypatch.setattr(_acp_compat, "run_compat_ask", ask)
    monkeypatch.setattr(_cli, "_resolve_from_llm", Mock(return_value="root-probe"))
    args.repo, args.branch, args.review = None, None, False
    _cli._handle_acp_compat(args, "claude")
    ask.assert_called_once()


@pytest.mark.parametrize("repo", sorted(REPOS))
def test_installed_dispatch_validates_primary_without_reexec(monkeypatch, tmp_path: Path, layout, repo: str) -> None:
    for key, value in _configured(layout, repo).items():
        monkeypatch.setenv(key, value)
    _, tree = _linked_worktree(tmp_path)
    calls = _observe_reexec(monkeypatch, tree)
    dispatcher_primary.exec_primary(["list"])
    assert calls == []
    assert dispatcher_primary.primary_root() == layout["primary"]


@pytest.mark.parametrize("repo", sorted(REPOS))
def test_api_rules_serves_release_core_and_configured_product_addendum(monkeypatch, api_client, layout, repo: str) -> None:
    for key, value in _configured(layout, repo).items():
        monkeypatch.setenv(key, value)
    core = api_client.get("/api/rules?scope=core&format=json")
    assert core.status_code == 200, core.text
    assert core.json()["markdown"] == rules_core.core_text("core")
    content = api_client.get("/api/rules?scope=content&format=json")
    assert content.status_code == 200, content.text
    markdown = content.json()["markdown"]
    assert markdown == rules_core.core_text("content")
    assert markdown.startswith(rules_core.core_text("core"))
    assert markdown.endswith("# Configured curriculum addendum\n")


# --------------------------------------------------------------------------- AC-02: defaults are today's values


def test_unset_configuration_keeps_todays_roots(clean_env, monkeypatch) -> None:
    unpatched_runtime = Path(rr.__file__).resolve().parents[2]
    assert rr.RUNTIME_ROOT == unpatched_runtime
    # Model the legacy in-tree copy independently of where pytest imported the
    # installed resolver. Compare runtime with the derivation above before
    # replacing RUNTIME_ROOT: after that patch the equality only reads the patch back.
    monkeypatch.setattr(rr, "_IN_TREE_SOURCE", True)
    # Interpreter selection is not under test here and depends on the host (a linked worktree whose
    # primary has no .venv, run by another checkout's venv, is refused by design), so pin it.
    stand_in = Path(sys.executable)
    monkeypatch.setattr(rr, "project_interpreter", lambda *args, **kwargs: stand_in)
    derived = rr.machinery_roots()
    assert derived.interpreter == stand_in
    assert derived.runtime == unpatched_runtime
    monkeypatch.setattr(rr, "RUNTIME_ROOT", REPO)
    primary = rr.main_checkout_root(REPO)
    roots = rr.machinery_roots()
    assert roots.interpreter == stand_in
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


def test_installed_dispatch_without_primary_refuses_reexec(clean_env, monkeypatch, tmp_path: Path, capsys) -> None:
    _, tree = _linked_worktree(tmp_path)
    calls = _observe_reexec(monkeypatch, tree)
    with pytest.raises(SystemExit) as refused:
        dispatcher_primary.exec_primary(["list"])
    assert refused.value.code == 2
    assert capsys.readouterr().err.startswith("dispatcher: no shared-state anchor is configured")
    assert calls == []


def test_unset_configuration_serves_release_rules(clean_env, api_client) -> None:
    body = api_client.get("/api/rules?scope=core&format=json").json()
    assert body["markdown"] == (REPO / rules_core.CORE_REL).read_text(encoding="utf-8").rstrip() + "\n"

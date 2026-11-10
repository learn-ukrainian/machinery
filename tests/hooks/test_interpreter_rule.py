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

from lu_machinery.common.repo_root import machinery_root

SOURCE = machinery_root().parent
HOOKS = SOURCE / "agents_extensions" / "shared" / "hooks"
LIB = machinery_root() / "lib"
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
    assert str(_bare_primary / ".venv" / "bin" / "python") in bare.stderr
    assert str(bare_worktree / ".venv" / "bin" / "python") not in bare.stderr
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
    shutil.copytree(machinery_root(), release / "lu_machinery", ignore=shutil.ignore_patterns("__pycache__"))
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


def test_deploy_inventory_uses_installed_hook_modules() -> None:
    """Deployed guards import shared code from the wheel without a path loader (R72)."""
    inventory = json.loads((machinery_root() / "agent_deploy/release.v1.json").read_text(encoding="utf-8"))
    assert "hooks/load_hook_package.py" not in inventory["files"]
    for name in ("shell_shlex.py", "shell_redirects.py", "tool_names.py"):
        record = inventory["files"]["hooks/" + name]
        assert record["owner"] == "fleet"
        assert set(record["targets"]) == {"agy", "claude", "codex", "cursor"}


def test_deployed_probe_does_not_execute_a_worktree_venv_when_git_cannot_name_the_primary(tmp_path: Path) -> None:
    """A stale worktree gitfile is not an interpreter when git cannot name the primary."""
    product = tmp_path / "wt"
    log = tmp_path / "picked.log"
    _wrapper(product / ".venv" / "bin" / "python", log)
    (product / ".git").write_text("gitdir: /nonexistent/elsewhere\n", encoding="utf-8")
    deployed = tmp_path / "consumer" / ".claude" / "hooks"
    deployed.mkdir(parents=True)
    for name in ("goal-driver-stop.sh", "context-rollover-lib.sh"):
        shutil.copy2(HOOKS / name, deployed / name)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path / "home"),
        "CLAUDE_PROJECT_DIR": str(product),
    }
    result = subprocess.run(
        ["bash", str(deployed / "goal-driver-stop.sh")],
        input="{}\n",
        text=True,
        capture_output=True,
        cwd=tmp_path,
        env=env,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    assert "untrusted git metadata" in result.stderr
    assert not log.exists()


def _record(path: Path, log: Path, *, code: int = 0, args_path: Path | None = None) -> Path:
    args = f'printf "%s\\n" "$@" > "{args_path}"\n' if args_path is not None else ""
    return _executable(path, f'#!/bin/sh\nprintf "%s\\n" "$0" >> "{log}"\n{args}exit {code}\n')


def _run(argv: list[str], *, cwd: Path, env: dict[str, str], stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, cwd=cwd, env=env, input=stdin, capture_output=True, text=True, timeout=30, check=False,
    )


ROUTED = (
    "lu_machinery/agent_runtime/codex_hook_entry.sh",
    "lu_machinery/agy_hooks/driver_state_inject.sh",
    "lu_machinery/agy_hooks/pending_orders_inject.sh",
    "lu_machinery/ai_agent_bridge/inbox_watch.sh",
    "lu_machinery/api/run_monitor_api_supervisor.sh",
    "lu_machinery/deploy_prompts.sh",
    "lu_machinery/guardrails/primary_post_checkout_heal.sh",
    "lu_machinery/hooks/run_hook_timed.sh",
    "lu_machinery/ops/smoketest_bridge_stdout_only.sh",
    "lu_machinery/orchestration/run_mac_observer_heartbeat.sh",
    "lu_machinery/orchestration/run_project_state_reporter.sh",
    "lu_machinery/orchestration/run_scheduled_worktree_cleanup.sh",
    "lu_machinery/session_start.sh",
    "lu_machinery/wt.sh",
    "lu_machinery/agent_runtime/shims/gh",
    "lu_machinery/agent_runtime/shims/git",
    "lu_machinery/agent_runtime/shims/npm",
    "lu_machinery/agent_runtime/shims/npx",
    "lu_machinery/dispatcher/npm_shims/npm",
    "lu_machinery/dispatcher/npm_shims/npx",
    "lu_machinery/opsec/push_hooks/pre-push",
    "lu_machinery/dispatcher/claude_review_shell",
    ".githooks/_lib.sh",
    ".githooks/commit-msg",
    ".githooks/post-merge",
    ".githooks/pre-push",
    "lu_machinery/launchers/codex.sh",
    "lu_machinery/lib/handoff_identity.sh",
    "lu_machinery/lib/thread_rollover_link.sh",
    "agents_extensions/shared/hooks/auto-audit.sh",
    "agents_extensions/shared/hooks/check-agent-inbox.sh",
    "agents_extensions/shared/hooks/goal-driver-stop.sh",
    "agents_extensions/shared/hooks/post-compact.sh",
    "agents_extensions/shared/hooks/release-thread-lease.sh",
    "agents_extensions/shared/hooks/stamp-pytest.sh",
)


def _checkout() -> Path:
    """The git checkout that holds ``.githooks``. The installed package does not."""
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if (parent / ".githooks" / "_lib.sh").is_file():
            return parent
    raise AssertionError("the test is not inside a checkout that contains .githooks")


def test_routed_scripts_do_not_build_a_venv_interpreter_path() -> None:
    """Reverting a script to its own .venv/bin/python selector fails this census (R74)."""
    checkout = _checkout()
    for relative in ROUTED:
        if relative.startswith(".githooks/"):
            path = checkout / relative
        elif relative.startswith("agents_extensions/"):
            path = machinery_root().parent / relative
        else:
            path = machinery_root() / relative.removeprefix("lu_machinery/")
        text = path.read_text(encoding="utf-8")
        assert ".venv/bin/python" not in text, relative


def _tracked_pre_push_scans_the_outgoing_range(tmp_path: Path) -> None:
    """Defect class: a tracked pre-push hook that skips machinery's scanner, or pushes when that scanner cannot start.

    A temporary repository, synthetic matcher fixtures, and a local bare remote. The planted value is the
    fixture token, never a credential.
    """
    from tests.opsec_fixtures import CATALOG, TOKEN, make_tooling

    real_git = shutil.which("git", path=os.defpath)
    assert real_git is not None
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    shutil.copytree(machinery_root(), repo / "lu_machinery", ignore=shutil.ignore_patterns("__pycache__"))
    (repo / "lu_machinery/config/fleet_repos.yaml").write_text(json.dumps({"repos": CATALOG}), encoding="utf-8")
    shutil.copytree(_checkout() / ".githooks", repo / ".githooks", ignore=shutil.ignore_patterns("__pycache__"))
    (repo / ".venv").symlink_to(Path(sys.executable).resolve().parents[1], target_is_directory=True)
    make_tooling(tmp_path / "private-fixture/tools/public_opsec_scan")
    remote = tmp_path / "remote.git"
    env = {key: value for key, value in os.environ.items() if not key.startswith(("GIT_", "PRE_COMMIT", "LU_OPSEC", "PYTHON"))}
    env.update(
        PATH=os.pathsep.join([str(Path(real_git).parent), os.defpath]),
        LEARN_UKRAINIAN_PYTHON=sys.executable,
        LEARN_UKRAINIAN_PRIMARY_REPO_ROOT=str(repo),
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
    )

    def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [real_git, *args], cwd=repo, env=env, capture_output=True, text=True, timeout=60, check=False,
        )
        if check and result.returncode != 0:
            raise AssertionError(result.stderr or result.stdout)
        return result

    git("init", "-q", "-b", "main")
    git("config", "user.email", "hook-test@example.invalid")
    git("config", "user.name", "Hook Test")
    (repo / "file.txt").write_text("clean\n", encoding="utf-8")
    git("add", "file.txt")
    git("commit", "-q", "--no-verify", "-m", "base")
    git("init", "-q", "--bare", str(remote))
    git("remote", "add", "origin", str(remote))
    git("push", "-q", "--no-verify", "origin", "HEAD:refs/heads/main")
    git("config", "core.hooksPath", ".githooks")

    def remote_has(sha: str) -> bool:
        probe = subprocess.run(
            [real_git, "cat-file", "-e", sha], cwd=remote, env=env, capture_output=True, text=True, timeout=30,
        )
        return probe.returncode == 0

    (repo / "file.txt").write_text("clean\nsecret\n", encoding="utf-8")
    git("commit", "-q", "--no-verify", "-am", "clean subject\n\nbody " + TOKEN)
    secret = git("rev-parse", "HEAD").stdout.strip()
    refused = git("push", "origin", "HEAD:refs/heads/feature", check=False)
    assert refused.returncode != 0, refused.stderr
    assert "OPSEC blocked: rule=synthetic-rule" in refused.stderr, refused.stderr
    assert TOKEN not in refused.stderr and TOKEN not in refused.stdout
    assert not remote_has(secret)

    git("reset", "-q", "--hard", "HEAD~1")
    (repo / "file.txt").write_text("clean\nallowed\n", encoding="utf-8")
    git("commit", "-q", "--no-verify", "-am", "clean subject")
    clean = git("rev-parse", "HEAD").stdout.strip()
    allowed = git("push", "origin", "HEAD:refs/heads/feature", check=False)
    assert allowed.returncode == 0, allowed.stderr
    assert remote_has(clean)

    (repo / "lu_machinery/opsec/git_push.py").unlink()
    (repo / "file.txt").write_text("clean\nagain\n", encoding="utf-8")
    git("commit", "-q", "--no-verify", "-am", "clean subject again")
    missing = git("rev-parse", "HEAD").stdout.strip()
    blocked = git("push", "origin", "HEAD:refs/heads/feature", check=False)
    assert blocked.returncode != 0, blocked.stderr
    assert "OPSEC: push scanner unavailable; push refused." in blocked.stderr, blocked.stderr
    assert not remote_has(missing)


def test_project_python_and_githooks_follow_the_rule(tmp_path: Path) -> None:
    """The hook installer and the hooks use the selector, not a worktree venv.

    ``pre_commit/project_python.sh`` is gone. The installed ``install_git_hooks.sh``
    sources ``lib/project_interpreter.sh`` and resolves for the worktree, as
    ``.githooks/_lib.sh`` does. The checkout package is removed before CI runs
    this file, so the installer is read from the installed package.
    The same function proves the tracked pre-push hook: a planted fixture value is refused, a clean
    push is allowed, and a missing scanner refuses the push.
    """
    primary, worktree, _log = _layout(tmp_path, primary_venv=True, worktree_venv=True)
    override = _wrapper(tmp_path / "override-python", _log)
    plain = tmp_path / "plain-file"
    plain.write_text("", encoding="utf-8")
    installer = (machinery_root() / "install_git_hooks.sh").read_text(encoding="utf-8")
    assert "lu_machinery/lib/project_interpreter.sh" in installer
    assert "project_interpreter_resolve" in installer
    assert "project_python.sh" not in installer
    assert not (SOURCE / "lu_machinery/pre_commit/project_python.sh").exists()
    selector = (
        f'source "{LIB / "project_interpreter.sh"}" && project_interpreter_resolve "$1"'
    )
    selected = _run(
        ["bash", "-c", selector, "probe", str(worktree)],
        cwd=worktree,
        env={**_base_env(tmp_path), "LEARN_UKRAINIAN_PYTHON": str(override)},
    )
    assert selected.returncode == 0, selected.stderr
    assert selected.stdout.strip() == str(override)

    refused = _run(
        ["bash", "-c", selector, "probe", str(worktree)],
        cwd=worktree,
        env={**_base_env(tmp_path), "LEARN_UKRAINIAN_PYTHON": str(plain)},
    )
    assert refused.returncode != 0
    assert str(primary / ".venv/bin/python") not in refused.stdout

    primary_selected = _run(
        ["bash", "-c", selector, "probe", str(worktree)],
        cwd=worktree,
        env=_base_env(tmp_path),
    )
    assert primary_selected.returncode == 0, primary_selected.stderr
    assert primary_selected.stdout.strip() == str(primary / ".venv/bin/python")

    hook_log = tmp_path / "hook.log"
    hook_override = _wrapper(tmp_path / "hook-python", hook_log)
    packaged = machinery_root()
    library = primary / "lu_machinery" / "lib" / "project_interpreter.sh"
    library.parent.mkdir(parents=True)
    shutil.copy2(packaged / "lib" / "project_interpreter.sh", library)
    hook_lib = primary / ".githooks"
    hook_lib.mkdir()
    shutil.copy2(_checkout() / ".githooks" / "_lib.sh", hook_lib / "_lib.sh")
    hook = _run(
        ["bash", "-c", f'source "{hook_lib / "_lib.sh"}" && run_project_python -c "print(1)"'],
        cwd=worktree,
        env={**_base_env(tmp_path), "LEARN_UKRAINIAN_PYTHON": str(hook_override)},
    )
    assert hook.returncode == 0, hook.stderr
    assert hook_log.read_text(encoding="utf-8").split()[0] == str(hook_override)
    for relative in (".githooks/_lib.sh", ".githooks/commit-msg", ".githooks/post-merge", ".githooks/pre-push"):
        assert "run_project_python" in (_checkout() / relative).read_text(encoding="utf-8")
    assert "project_python.sh" not in (_checkout() / ".githooks" / "_lib.sh").read_text(encoding="utf-8")
    hook_text = (_checkout() / ".githooks" / "pre-push").read_text(encoding="utf-8")
    assert ".pre-commit-config.yaml" not in hook_text
    assert "lint_opsec" not in hook_text
    _tracked_pre_push_scans_the_outgoing_range(tmp_path / "push-proof")


def test_repo_root_launchers_follow_the_rule(tmp_path: Path) -> None:
    """Launchers that take a checkout use the override or the primary venv, and refuse an invalid one."""
    primary, worktree, log = _layout(tmp_path, primary_venv=True, worktree_venv=True)
    plain = tmp_path / "plain-file"
    plain.write_text("", encoding="utf-8")
    cases = (
        (SOURCE / "lu_machinery/api/run_monitor_api_supervisor.sh", ["--repo-root", str(worktree)]),
        (SOURCE / "lu_machinery/orchestration/run_mac_observer_heartbeat.sh", ["--repo-root", str(worktree)]),
        (SOURCE / "lu_machinery/orchestration/run_scheduled_worktree_cleanup.sh", ["--repo-root", str(worktree)]),
    )
    for script, argv in cases:
        log.write_text("", encoding="utf-8")
        refused = _run(
            [str(script), *argv],
            cwd=worktree,
            env={**_base_env(tmp_path), "LEARN_UKRAINIAN_PYTHON": str(plain)},
        )
        assert refused.returncode != 0, script
        assert log.read_text(encoding="utf-8") == "", script
        selected = _run([str(script), *argv], cwd=worktree, env=_base_env(tmp_path))
        picked = set(log.read_text(encoding="utf-8").split())
        assert str(primary / ".venv/bin/python") in picked, (script, selected.stderr)
        assert str(worktree / ".venv/bin/python") not in picked, script


def test_remaining_shell_programs_select_the_rule_and_refuse_invalid(tmp_path: Path) -> None:
    """Each remaining routed program executes the configured interpreter and does not fall back."""
    primary, worktree, log = _layout(tmp_path, primary_venv=True, worktree_venv=True)
    plain = tmp_path / "plain-file"
    plain.write_text("", encoding="utf-8")
    args_path = tmp_path / "args"
    override = _record(tmp_path / "override-python", log, code=1, args_path=args_path)
    base = _base_env(tmp_path)
    base["LU_PRODUCT_ROOT"] = str(worktree)

    def selected(argv: list[str], *, cwd: Path, extra: dict[str, str], stdin: str = "") -> subprocess.CompletedProcess[str]:
        log.write_text("", encoding="utf-8")
        args_path.write_text("", encoding="utf-8")
        return _run(argv, cwd=cwd, env={**base, "LEARN_UKRAINIAN_PYTHON": str(override), **extra}, stdin=stdin)

    def refused(argv: list[str], *, cwd: Path, extra: dict[str, str], stdin: str = "") -> subprocess.CompletedProcess[str]:
        log.write_text("", encoding="utf-8")
        return _run(argv, cwd=cwd, env={**base, "LEARN_UKRAINIAN_PYTHON": str(plain), **extra}, stdin=stdin)

    reporter = SOURCE / "lu_machinery/orchestration/run_project_state_reporter.sh"
    decoy_log = tmp_path / "decoy.log"
    decoy = _record(tmp_path / "decoy-root/.venv/bin/python", decoy_log, code=0)
    decoy_log.write_text("", encoding="utf-8")
    decoy_run = _run(
        [str(reporter)],
        cwd=worktree,
        env={**base, "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT": str(decoy.parent.parent.parent)},
    )
    assert decoy_run.returncode != 0, decoy_run.stderr
    assert decoy_log.read_text(encoding="utf-8") == ""
    chosen = selected([str(reporter)], cwd=worktree, extra={})
    assert str(override) in log.read_text(encoding="utf-8"), chosen.stderr
    assert refused([str(reporter)], cwd=worktree, extra={}).returncode != 0
    assert log.read_text(encoding="utf-8") == ""

    timed = SOURCE / "lu_machinery/hooks/run_hook_timed.sh"
    hook = _executable(tmp_path / "hook.sh", "#!/bin/sh\nprintf RAN\\n")
    log.write_text("", encoding="utf-8")
    timed_primary = _run(
        [str(timed), str(hook)],
        cwd=worktree,
        env={**base, "HOOK_TIMING": "1", "CLAUDE_PROJECT_DIR": str(worktree)},
    )
    assert "RAN" in timed_primary.stdout
    assert str(primary / ".venv/bin/python") in log.read_text(encoding="utf-8")
    assert str(worktree / ".venv/bin/python") not in log.read_text(encoding="utf-8")
    relative = _executable(worktree / "python", "#!/bin/sh\nprintf relative >> " + f'"{log}"\n')
    log.write_text("", encoding="utf-8")
    relative_run = _run(
        [str(timed), str(hook)],
        cwd=worktree,
        env={**base, "HOOK_TIMING": "1", "CLAUDE_PROJECT_DIR": str(worktree), "LEARN_UKRAINIAN_PYTHON": "python"},
    )
    assert "RAN" in relative_run.stdout
    assert log.read_text(encoding="utf-8") == ""
    assert str(relative) not in relative_run.stdout

    doctor = primary / "lu_machinery/guardrails/assert_primary_on_main.py"
    doctor.parent.mkdir(parents=True)
    doctor.write_text("raise SystemExit(0)\n", encoding="utf-8")
    heal = SOURCE / "lu_machinery/guardrails/primary_post_checkout_heal.sh"
    log.write_text("", encoding="utf-8")
    heal_refused = _run([str(heal)], cwd=primary, env={**base, "LEARN_UKRAINIAN_PYTHON": str(plain)})
    assert heal_refused.returncode == 0
    assert "unavailable" in heal_refused.stderr
    assert log.read_text(encoding="utf-8") == ""
    heal_selected = _run([str(heal)], cwd=primary, env=base)
    assert str(primary / ".venv/bin/python") in log.read_text(encoding="utf-8"), heal_selected.stderr

    codex = SOURCE / "lu_machinery/agent_runtime/codex_hook_entry.sh"
    payload = '{"cwd": "' + str(worktree) + '"}'
    codex_refused = refused([str(codex), "pre-tool-use"], cwd=worktree, extra={"CLAUDE_PROJECT_DIR": str(worktree)}, stdin=payload)
    assert codex_refused.returncode != 0
    assert log.read_text(encoding="utf-8") == ""
    codex_selected = selected([str(codex), "pre-tool-use"], cwd=worktree, extra={"CLAUDE_PROJECT_DIR": str(worktree)}, stdin=payload)
    assert str(override) in log.read_text(encoding="utf-8"), codex_selected.stderr

    deploy = str(SOURCE / "lu_machinery/deploy_prompts.sh")
    deploy_extra = {"LU_PRODUCT_ROOT": str(primary)}
    deployed = selected([deploy], cwd=primary, extra=deploy_extra)
    assert str(override) in log.read_text(encoding="utf-8"), deployed.stderr
    deploy_refused = refused([deploy], cwd=primary, extra=deploy_extra)
    assert deploy_refused.returncode != 0
    assert log.read_text(encoding="utf-8") == ""

    for script, argv, stdin, extra in (
        (SOURCE / "lu_machinery/ops/smoketest_bridge_stdout_only.sh", [], "", {}),
        (SOURCE / "lu_machinery/ai_agent_bridge/inbox_watch.sh", [], "", {}),
        (SOURCE / "lu_machinery/opsec/push_hooks/pre-push", [], "a b c d\n", {}),
        (SOURCE / "lu_machinery/agent_runtime/shims/npm", ["--version"], "", {}),
        (SOURCE / "lu_machinery/agent_runtime/shims/npx", ["--version"], "", {}),
        (SOURCE / "lu_machinery/dispatcher/npm_shims/npm", ["--version"], "", {}),
        (SOURCE / "lu_machinery/dispatcher/npm_shims/npx", ["--version"], "", {}),
        (SOURCE / "lu_machinery/agy_hooks/driver_state_inject.sh", [], "{}",
         {"LU_DRIVER_STATE_FILE": str(tmp_path / "state"), "SESSION_HANDOFF_AGENT": "probe",
          "LU_DRIVER_STATE_PYTHON": str(decoy)}),
        (SOURCE / "lu_machinery/agy_hooks/pending_orders_inject.sh", [], "{}",
         {"LU_DRIVER_STATE_PYTHON": str(decoy)}),
    ):
        run = selected(["bash", str(script), *argv], cwd=worktree, extra=extra, stdin=stdin)
        assert str(override) in log.read_text(encoding="utf-8"), (script, run.returncode, run.stderr)
        assert str(decoy) not in log.read_text(encoding="utf-8"), script
        deny = refused(["bash", str(script), *argv], cwd=worktree, extra=extra, stdin=stdin)
        assert str(override) not in log.read_text(encoding="utf-8"), script
        assert log.read_text(encoding="utf-8") == "", (script, deny.stderr)

    git_log = tmp_path / "git.log"
    git_python = _record(tmp_path / "git-python", git_log, code=2)
    git_run = _run(
        [str(SOURCE / "lu_machinery/agent_runtime/shims/git"), "checkout", "topic"],
        cwd=worktree,
        env={**base, "AGENT_NO_MERGE": "1", "LEARN_UKRAINIAN_PYTHON": str(git_python)},
    )
    assert git_run.returncode != 0
    assert str(git_python) in git_log.read_text(encoding="utf-8"), git_run.stderr
    git_log.write_text("", encoding="utf-8")
    git_refused = _run(
        [str(SOURCE / "lu_machinery/agent_runtime/shims/git"), "checkout", "topic"],
        cwd=worktree,
        env={**base, "AGENT_NO_MERGE": "1", "LEARN_UKRAINIAN_PYTHON": str(plain)},
    )
    assert git_refused.returncode != 0
    assert git_log.read_text(encoding="utf-8") == ""

    fake_gh = _executable(tmp_path / "fake-gh", "#!/bin/sh\nexit 0\n")
    gh_env = {"AGENT_REAL_GH": str(fake_gh)}
    gh_run = selected([str(SOURCE / "lu_machinery/agent_runtime/shims/gh"), "auth", "status"], cwd=worktree, extra=gh_env)
    assert str(override) in log.read_text(encoding="utf-8"), gh_run.stderr
    assert refused([str(SOURCE / "lu_machinery/agent_runtime/shims/gh"), "auth", "status"], cwd=worktree, extra=gh_env).returncode != 0
    assert log.read_text(encoding="utf-8") == ""

    shell_args = tmp_path / "shell-args"
    shell_python = _record(tmp_path / "shell-python", log, code=0, args_path=shell_args)
    reviewed = tmp_path / "reviewed"
    reviewed.mkdir()
    shell = _run(
        [str(SOURCE / "lu_machinery/dispatcher/claude_review_shell"), "true"],
        cwd=worktree,
        env={**base, "LEARN_UKRAINIAN_PYTHON": str(shell_python), "LU_REVIEW_SHELL_WORKTREE": str(reviewed)},
    )
    assert shell.returncode == 0, shell.stderr
    recorded = shell_args.read_text(encoding="utf-8")
    assert "sys.path" not in recorded
    assert recorded.splitlines()[0] == "-I"
    assert recorded.splitlines()[-1] == "true"
    log.write_text("", encoding="utf-8")
    shell_refused = _run(
        [str(SOURCE / "lu_machinery/dispatcher/claude_review_shell"), "true"],
        cwd=worktree,
        env={**base, "LEARN_UKRAINIAN_PYTHON": str(plain), "LU_REVIEW_SHELL_WORKTREE": str(reviewed)},
    )
    assert shell_refused.returncode == 125
    assert "claude_review_shell_interpreter_missing" in shell_refused.stderr

    _executable(tmp_path / "bin/curl", """#!/bin/sh
url=
for arg in "$@"; do url=$arg; done
case $url in
  */api/state/summary) printf '%s\\n' '{"totals":{"total":1,"research_done":0,"content_done":0,"audit_passing":0,"final_review_done":0},"tracks":{}}' ;;
  */api/batch/active) printf '%s\\n' '[]' ;;
  */api/state/failing) printf '%s\\n' '{"count":0,"modules":[]}' ;;
  */api/state/preparation) printf '%s\\n' '{"totals":{"manifest_active_tracks":0,"manifest_active_modules":0,"module_state_counts":{"built":0,"partial":0,"unbuilt":0},"publication_state_counts":{"published":0,"generated":0,"absent":0}}}' ;;
  *) printf '%s\\n' '{}' ;;
esac
""")
    session_log = tmp_path / "session.log"
    session_python = _record(tmp_path / "session-python", session_log, code=1)
    session = _run(
        [str(SOURCE / "lu_machinery/session_start.sh")],
        cwd=worktree,
        env={**base, "PATH": f"{tmp_path / 'bin'}:{base['PATH']}", "LEARN_UKRAINIAN_PYTHON": str(session_python)},
    )
    assert session.returncode == 0, session.stderr
    assert str(session_python) in session_log.read_text(encoding="utf-8"), session.stderr

    copied = primary / "lu_machinery/wt.sh"
    copied.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SOURCE / "lu_machinery/wt.sh", copied)
    lib = primary / "lu_machinery/lib"
    lib.mkdir(parents=True)
    shutil.copy2(SOURCE / "lu_machinery/lib/project_interpreter.sh", lib / "project_interpreter.sh")
    wt_dir = tmp_path / "learn-ukrainian-wt-7"
    wt_dir.mkdir()
    _git(wt_dir, "init", "-q", "--initial-branch=main")
    _git(wt_dir, "-c", "user.email=fixture@example.invalid", "-c", "user.name=Fixture", "commit", "--allow-empty", "-m", "fixture")
    wt_log = tmp_path / "wt.log"
    wt_python = _record(tmp_path / "wt-python", wt_log, code=1)
    wt = _run([str(copied), "clean", "7"], cwd=primary, env={**base, "LEARN_UKRAINIAN_PYTHON": str(wt_python)})
    assert str(wt_python) in wt_log.read_text(encoding="utf-8"), (wt.returncode, wt.stderr)
    wt_log.write_text("", encoding="utf-8")
    wt_refused = _run([str(copied), "clean", "7"], cwd=primary, env={**base, "LEARN_UKRAINIAN_PYTHON": str(plain)})
    assert wt_refused.returncode != 0
    assert wt_log.read_text(encoding="utf-8") == ""
    assert str(primary / ".venv/bin/python") not in wt_log.read_text(encoding="utf-8")


def test_gh_entry_does_not_put_the_checkout_on_sys_path() -> None:
    """Defect: the gh entry inserts the checkout on sys.path, so a planted module there is importable."""
    script = r"""
import importlib.util, os, sys
root, name = sys.argv[1], sys.argv[2]
package = os.path.join(root, name)
init = os.path.join(package, "__init__.py")
before = list(sys.path)
spec = importlib.util.spec_from_file_location(name, init, submodule_search_locations=[package])
sys.modules[name] = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sys.modules[name])
import lu_machinery.opsec.gh_entry as entry
checkout = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(entry.__file__))))
if sys.path.count(checkout) > before.count(checkout):
    sys.exit("checkout was inserted onto sys.path")
"""
    package = machinery_root()
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(package.parent), package.name],
        cwd=package.parent,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "sys.path.insert" not in (package / "opsec" / "gh_entry.py").read_text(encoding="utf-8")


def test_retired_model_check_uses_the_rule_not_the_helper_venv(tmp_path: Path) -> None:
    """Defect: the retired-model check runs the helper-root venv and ignores LEARN_UKRAINIAN_PYTHON."""
    decoy_log = tmp_path / "decoy.log"
    decoy_log.write_text("", encoding="utf-8")
    helper = tmp_path / "helper"
    _executable(
        helper / ".venv" / "bin" / "python",
        f'#!/bin/sh\nprintf decoy >> "{decoy_log}"\nexit 0\n',
    )
    override_log = tmp_path / "override.log"
    override = _wrapper(tmp_path / "override-python", override_log)
    plain = tmp_path / "plain-file"
    plain.write_text("", encoding="utf-8")
    script = (
        f'source "{LIB / "launcher_core.sh"}" && '
        'LC_DURABLE_HELPER_ROOT="$1" LC_PROVIDER=claude LC_MODEL=claude-opus-5-5 && '
        "launcher_normalize_model"
    )
    selected = subprocess.run(
        ["bash", "-c", script, "probe", str(helper)],
        cwd=tmp_path,
        env={**_base_env(tmp_path), "LEARN_UKRAINIAN_PYTHON": str(override)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert selected.returncode == 0, selected.stderr
    assert override_log.read_text(encoding="utf-8").strip() == str(override)
    assert decoy_log.read_text(encoding="utf-8") == ""
    refused = subprocess.run(
        ["bash", "-c", script, "probe", str(helper)],
        cwd=tmp_path,
        env={**_base_env(tmp_path), "LEARN_UKRAINIAN_PYTHON": str(plain)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert refused.returncode != 0, refused.stderr
    assert decoy_log.read_text(encoding="utf-8") == ""


def test_handoff_alias_error_names_the_packaged_map(tmp_path: Path) -> None:
    """Defect: a missing alias map is reported at the old scripts/config path."""
    dest = tmp_path / "lu_machinery" / "lib" / "handoff_identity.sh"
    dest.parent.mkdir(parents=True)
    shutil.copy2(LIB / "handoff_identity.sh", dest)
    result = subprocess.run(
        ["bash", "-c", 'source "$1" && launcher_selector_resolve atlas', "probe", str(dest)],
        cwd=tmp_path,
        env=_base_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert "lu_machinery/config/launcher_stream_aliases.tsv" in result.stderr
    assert "scripts/config/launcher_stream_aliases.tsv" not in result.stderr


def test_subdirectory_does_not_select_a_nested_venv(tmp_path: Path) -> None:
    """Defect: a subdirectory with no .git is its own primary, so a nested virtualenv bypasses the checkout."""
    primary, _worktree, log = _layout(tmp_path, primary_venv=True, worktree_venv=False)
    nested = primary / "nested"
    nested.mkdir()
    nested_log = tmp_path / "nested.log"
    nested_log.write_text("", encoding="utf-8")
    _record(nested / ".venv" / "bin" / "python", nested_log, code=0)
    base = _base_env(tmp_path)
    package = machinery_root()
    timed = _run(
        [str(package / "hooks" / "run_hook_timed.sh"), "/bin/false"],
        cwd=nested,
        env={**base, "HOOK_TIMING": "1"},
    )
    assert timed.returncode != 0, timed.stderr
    assert nested_log.read_text(encoding="utf-8") == ""
    assert str(primary / ".venv/bin/python") in log.read_text(encoding="utf-8")
    log.write_text("", encoding="utf-8")
    codex = _run(
        [str(package / "agent_runtime" / "codex_hook_entry.sh"), "pre-tool-use"],
        cwd=nested,
        env=base,
        stdin=json.dumps({"tool_input": {"workdir": str(nested)}}),
    )
    assert nested_log.read_text(encoding="utf-8") == "", codex.stderr
    assert str(primary / ".venv/bin/python") in log.read_text(encoding="utf-8"), codex.stderr

"""Release hooks must use product authority, never their installation directory."""

from __future__ import annotations

import json
import os
import shutil
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from lu_machinery.common.repo_root import machinery_root

SOURCE = Path(__file__).resolve().parents[2]
HOOKS = Path("agents_extensions/shared/hooks")


@pytest.fixture(scope="module")
def release(tmp_path_factory):
    """Separate site-packages layout without a product package or source-path imports."""
    site = tmp_path_factory.mktemp("release") / "site-packages"
    shutil.copytree(
        machinery_root(),
        site / "lu_machinery",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copytree(
        SOURCE / HOOKS, site / HOOKS, ignore=shutil.ignore_patterns("__pycache__")
    )
    interpreter = site.parent / "venv/bin/python"
    subprocess.run(
        [sys.executable, "-I", "-m", "venv", "--without-pip", str(interpreter.parent.parent)], check=True
    )
    purelib = subprocess.check_output(
        [str(interpreter), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True
    ).strip()
    dependencies = "\n".join(path for path in sys.path if path.endswith("site-packages"))
    Path(purelib, "release.pth").write_text(f"{site}\n{dependencies}\n")
    return site


@pytest.fixture
def product(tmp_path):
    root = tmp_path / "product"
    root.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    for args in (
        ["init", "--initial-branch=main"],
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "fixture",
        ],
        ["branch", "topic"],
    ):
        subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True, env=env
        )
    return root


def run_hook(
    release,
    product,
    name,
    command,
    *,
    missing=False,
    event="PreToolUse",
    tool_id="call",
    cwd=None,
):
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("GIT_", "PYTHON", "LEARN_UKRAINIAN_"))
    }
    env.update(
        LU_PRODUCT_ROOT=str(product / "absent" if missing else product),
        XDG_STATE_HOME=str(product.parent / "state"),
    )
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(cwd or product),
        "hook_event_name": event,
        "session_id": "release-root",
        "tool_use_id": tool_id,
    }
    return subprocess.run(
        [str(release.parent / "venv/bin/python"), "-I", "-B", str(release / HOOKS / name)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=cwd or product,
        env=env,
    )


def test_branch_guard_does_not_protect_release_instead_of_product(release, product):
    assert (
        run_hook(
            release, product, "guard-branch-switch-in-main.py", "git status"
        ).returncode
        == 0
    )
    assert (
        run_hook(
            release, product, "guard-branch-switch-in-main.py", "git checkout topic"
        ).returncode
        == 2
    )
    worktree = product.parent / "worker"
    subprocess.run(
        ["git", "-C", str(product), "worktree", "add", str(worktree), "topic"],
        check=True,
        capture_output=True,
    )
    assert (
        run_hook(
            release,
            product,
            "guard-branch-switch-in-main.py",
            "git checkout -b next",
            cwd=worktree,
        ).returncode
        == 0
    )
    result = run_hook(
        release,
        product,
        "guard-branch-switch-in-main.py",
        "git checkout topic",
        missing=True,
    )
    assert result.returncode == 2 and "configuration unusable" in result.stderr


def test_write_guard_does_not_anchor_git_mutations_to_release(release, product):
    name = "guard-primary-checkout-write.py"
    assert run_hook(release, product, name, "git status").returncode == 0
    assert run_hook(release, product, name, "git reset --hard").returncode == 2
    result = run_hook(release, product, name, "echo safe", missing=True)
    assert result.returncode == 2 and "product_root_unavailable" in result.stderr


def test_dirt_audit_does_not_select_release_as_default_product(release, product):
    name = "audit-primary-checkout-dirt.py"
    assert run_hook(release, product, name, "echo safe").returncode == 0
    (product / "new-dirt").write_text("fixture")
    result = run_hook(release, product, name, "echo safe", event="PostToolUse")
    assert result.returncode == 2 and "new-dirt" in result.stderr
    result = run_hook(
        release, product, name, "echo safe", missing=True, tool_id="missing"
    )
    assert result.returncode == 2 and "product root unavailable" in result.stderr

    # A missing installed audit dependency remains non-blocking, without a traceback.
    unavailable = subprocess.run(
        [sys.executable, "-I", "-S", str(release / HOOKS / name)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo safe"}}),
        text=True, capture_output=True, check=False, timeout=10,
    )
    assert unavailable.returncode == 0
    assert "audit skipped" in unavailable.stderr
    assert "Traceback" not in unavailable.stderr


def test_bare_healer_does_not_heal_release_instead_of_product(release, product):
    name = "heal-core-bare.py"
    assert run_hook(release, product, name, "git status").returncode == 0
    subprocess.run(
        ["git", "-C", str(product), "config", "core.bare", "true"], check=True
    )
    alias = product.parent / "product-alias"
    alias.symlink_to(product, target_is_directory=True)
    assert run_hook(release, alias, name, "git status").returncode == 0
    value = subprocess.run(
        ["git", "-C", str(product), "config", "core.bare"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert value.stdout.strip() == "false"
    result = run_hook(release, product, name, "git status", missing=True)
    assert result.returncode == 2 and "authority unavailable" in result.stderr


def test_merge_guard_requires_product_authority_for_merge(release, product):
    name = "guard-pr-merge.py"
    assert run_hook(release, product, name, "gh pr view 1").returncode == 0
    assert run_hook(release, product, name, "gh pr merge 1 --auto").returncode == 2
    result = run_hook(release, product, name, "gh pr merge 1 --auto", missing=True)
    assert result.returncode == 2 and "product root" in result.stderr


def test_public_guard_uses_release_shim_and_requires_product_authority(
    release, product
):
    name = "guard-public-github-text.py"
    result = run_hook(release, product, name, "gh pr view 1")
    assert result.returncode == 0, result.stderr
    command = json.loads(result.stdout)["hookSpecificOutput"]["updatedInput"]["command"]
    assert str(release / "lu_machinery/agent_runtime/shims") in command
    assert str(product / "lu_machinery") not in command
    result = run_hook(release, product, name, "gh pr view 1", missing=True)
    assert result.returncode == 2 and "OPSEC" in result.stderr


SHELL_HOOKS = (
    "auto-deploy-agent-extensions.sh",
    "auto-audit.sh",
    "stamp-pytest.sh",
    "post-compact.sh",
    "precompact-handoff-guard.sh",
    "goal-driver-stop.sh",
    "context-rollover-guard.sh",
    "check-agent-inbox.sh",
    "context-monitor.sh",
    "release-thread-lease.sh",
    "session-setup.sh",
)
ADAPTERS = ("deploy_prompts.sh", "check_rules_deployment.sh")


def run_shell(path, *args, env=None, cwd):
    """A bare host environment: no harness, product, dispatch or Python variables."""
    base = {"PATH": os.environ["PATH"], "HOME": str(cwd)}
    return subprocess.run(
        ["bash", str(path), *args],
        input=json.dumps({"session_id": "release-root", "hook_event_name": "Stop"}),
        text=True,
        capture_output=True,
        cwd=cwd,
        env={**base, **(env or {})},
    )


@pytest.mark.parametrize("name", SHELL_HOOKS)
@pytest.mark.parametrize("layout", ["primary", "no-git", "empty-git", "hook-location", "empty-git-planted"])
@pytest.mark.parametrize("interpreter", ["configured", "primary-venv"])
def test_shell_hook_refuses_location_derived_project(release, product, tmp_path, name, layout, interpreter):
    """Premature root refusal must yield to the rule, without trusting hook location."""
    cwd = product / "nested" if layout == "primary" else tmp_path / layout
    cwd.mkdir()
    marker = tmp_path / "planted-interpreter-ran"
    if layout in {"empty-git", "empty-git-planted"}:
        (cwd / ".git").mkdir()
    if layout == "empty-git-planted":
        planted = cwd / ".venv/bin/python"
        planted.parent.mkdir(parents=True)
        planted.write_text(f"#!/bin/sh\nprintf ran > {shlex.quote(str(marker))}\nexit 1\n")
        planted.chmod(0o755)
    shutil.copytree(release.parent / "venv", product / ".venv", symlinks=True)
    hooks = release / HOOKS
    if layout == "hook-location":
        hooks = product / ".claude/hooks"
        shutil.copytree(release / HOOKS, hooks)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("x" * 700)
    env = {
        "PATH": os.environ["PATH"], "HOME": str(tmp_path),
        "LEARN_UKRAINIAN_CLAUDEX_RUN_ID": "release-root-run",
        "LEARN_UKRAINIAN_CLAUDEX_LAUNCH_GENERATION": "1",
        "AB_MONITOR_URL": "http://127.0.0.1:9",
        "LU_MONITOR_LOOPBACK": "http://127.0.0.1:9",
        "AB_DB_PATH": str(tmp_path / "messages.db"),
    }
    if interpreter == "configured":
        env["LEARN_UKRAINIAN_PYTHON"] = str(release.parent / "venv/bin/python")
    result = subprocess.run(
        ["bash", "-x", str(hooks / name)],
        input=json.dumps({"session_id": "release-root", "hook_event_name": "Stop",
                          "transcript_path": str(transcript)}),
        text=True, capture_output=True, cwd=cwd, env=env, timeout=30,
    )
    assert not marker.exists(), result.stderr
    if layout == "primary":
        assert result.returncode == 0, result.stderr
        assert f"export LU_PRODUCT_ROOT={product}\n" in result.stderr
    else:
        assert result.returncode == 1, result.stderr
        assert "export LU_PRODUCT_ROOT=" not in result.stderr


def test_shell_hook_runs_the_configured_product(release, product):
    """Location-derived project root: the configured product's helper must be the one run."""
    marker = product / "stamped"
    (product / ".githooks").mkdir()
    (product / ".githooks/pytest_stamp.py").write_text(
        f"open({str(marker)!r}, 'w').write('ok')\n"
    )
    (product / ".venv/bin").mkdir(parents=True)
    (product / ".venv/bin/python").symlink_to(sys.executable)
    result = run_shell(
        release / HOOKS / "stamp-pytest.sh",
        env={"LU_PRODUCT_ROOT": str(product),
             "LEARN_UKRAINIAN_PYTHON": str(release.parent / "venv/bin/python")},
        cwd=product.parent,
    )
    assert result.returncode == 0, result.stderr
    assert marker.read_text() == "ok"


@pytest.mark.parametrize("name", ADAPTERS)
def test_adapter_requires_a_selected_consumer(release, product, name):
    """Location-derived --project-root: the adapter must not target its own release tree."""
    adapter = release / "lu_machinery" / name
    result = run_shell(adapter, cwd=product.parent)
    assert result.returncode == 2
    assert f"{name}: no consumer is selected" in result.stderr
    for env, args in (
        ({"LU_PRODUCT_ROOT": str(product)}, ()),
        ({}, ("--project-root", str(product))),
    ):
        result = run_shell(adapter, *args, env=env, cwd=product.parent)
        assert result.returncode == 1
        assert f"{product}/.venv/bin/python" in result.stderr
        assert str(release) not in result.stderr
    # argparse accepts abbreviations: the interpreter must never come from another root.
    for args in (("--project-ro", "/other"), ("--p=/other",)):
        result = run_shell(
            adapter, *args, env={"LU_PRODUCT_ROOT": str(product)}, cwd=product.parent
        )
        assert result.returncode == 2
        assert f"{name}: spell the option --project-root in full" in result.stderr


def test_deployed_session_setup_runs_installed_release_code(release, product):
    """Code taken from the consumer checkout: a deployed hook found no lu_machinery (R62)."""
    hooks = product / ".claude/hooks"
    hooks.mkdir(parents=True)
    shutil.copy2(SOURCE / HOOKS / "session-setup.sh", hooks / "session-setup.sh")
    shutil.copy2(SOURCE / HOOKS / "context-rollover-lib.sh", hooks / "context-rollover-lib.sh")
    subprocess.run(
        [sys.executable, "-I", "-m", "venv", "--without-pip", str(product / ".venv")],
        check=True,
    )
    purelib = subprocess.run(
        [
            str(product / ".venv/bin/python"),
            "-I",
            "-c",
            "import sysconfig; print(sysconfig.get_path('purelib'))",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dependencies = "\n".join(path for path in sys.path if path.endswith("site-packages"))
    Path(purelib, "release.pth").write_text(f"{release}\n{dependencies}\n")
    env = {
        "SESSION_START_BUDGET_SECONDS": "60",
        "LEARN_UKRAINIAN_CLAUDEX_RUN_ID": "release-root-run",
        "LEARN_UKRAINIAN_CLAUDEX_LAUNCH_GENERATION": "1",
    }
    result = run_shell(
        hooks / "session-setup.sh",
        env={**env, "CLAUDE_PROJECT_DIR": str(product)},
        cwd=product,
    )
    assert result.returncode == 0, result.stderr
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "did not match the owned child generation" in context
    Path(purelib, "release.pth").unlink()
    result = run_shell(
        hooks / "session-setup.sh",
        env={**env, "CLAUDE_PROJECT_DIR": str(product)},
        cwd=product,
    )
    assert result.returncode == 1
    assert "no installed lu_machinery release" in result.stderr


# Hooks that run machinery code, with a stdin payload that reaches it (#100 R69).
CODE_HOOKS = {
    "release-thread-lease.sh": {"session_id": "r69", "hook_event_name": "SessionEnd"},
    "goal-driver-stop.sh": {"session_id": "r69", "hook_event_name": "Stop"},
    "precompact-handoff-guard.sh": {
        "session_id": "r69",
        "hook_event_name": "PreCompact",
        "trigger": "auto",
    },
    "post-compact.sh": {
        "session_id": "r69",
        "hook_event_name": "SessionStart",
        "source": "compact",
    },
    "check-agent-inbox.sh": {
        "session_id": "r69",
        "hook_event_name": "UserPromptSubmit",
    },
    "context-monitor.sh": {"session_id": "r69", "hook_event_name": "PostToolUse"},
}
# Every project-relative machinery path those hooks took their code from before R69.
DECOY_FILES = (
    "__init__.py",
    "orchestration/__init__.py",
    "orchestration/thread_handoff.py",
    "agent_runtime/__init__.py",
    "agent_runtime/bounded_command.py",
    "lib/handoff_identity.sh",
    "lib/profile_resolver.sh",
    "lib/project_interpreter.sh",
)
_DECOY_PY = (
    "import os\nopen(os.environ['LU_TEST_DECOY_LOG'], 'a').write(__file__ + '\\n')\n"
)
_DECOY_SH = 'printf "%s\\n" "${BASH_SOURCE[0]}" >> "$LU_TEST_DECOY_LOG"\n'


@pytest.fixture(scope="module")
def marked_release(tmp_path_factory):
    """A release whose package import is logged, to show the hook ran release code."""
    site = tmp_path_factory.mktemp("marked") / "site-packages"
    shutil.copytree(
        machinery_root(),
        site / "lu_machinery",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copytree(
        SOURCE / HOOKS, site / HOOKS, ignore=shutil.ignore_patterns("__pycache__")
    )
    init = site / "lu_machinery" / "__init__.py"
    init.write_text(
        "import os as _os\n"
        "if _os.environ.get('LU_TEST_RELEASE_LOG'):\n"
        "    open(_os.environ['LU_TEST_RELEASE_LOG'], 'a').write('release\\n')\n"
        + init.read_text()
    )
    return site


def consumer_with_decoy(product, release):
    """A consumer checkout holding a decoy lu_machinery/, its interpreter importing the release."""
    for relative in DECOY_FILES:
        path = product / "lu_machinery" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_DECOY_SH if relative.endswith(".sh") else _DECOY_PY)
    subprocess.run(
        [sys.executable, "-I", "-m", "venv", "--without-pip", str(product / ".venv")],
        check=True,
    )
    purelib = subprocess.run(
        [
            str(product / ".venv/bin/python"),
            "-I",
            "-c",
            "import sysconfig; print(sysconfig.get_path('purelib'))",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dependencies = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sysconfig; print(sysconfig.get_path('purelib'))",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    Path(purelib, "release.pth").write_text(f"{release}\n{dependencies}\n")


@pytest.mark.parametrize("name", CODE_HOOKS)
def test_deployed_hook_runs_release_code_not_the_project_copy(
    marked_release, product, tmp_path, name
):
    """Code taken from $PROJECT_DIR/lu_machinery: a deployed hook ran the consumer's copy (R69).

    The consumer's own agents_extensions/shared/hooks copy has the machinery tree's path suffix,
    so a hook that took its code from beside itself sourced the decoy there (R51).
    """
    consumer_with_decoy(product, marked_release)
    for hooks in (product / ".claude/hooks", product / HOOKS):
        hooks.mkdir(parents=True)
        for helper in (name, "context-rollover-lib.sh", "context-hook-state.py"):
            shutil.copy2(SOURCE / HOOKS / helper, hooks / helper)
        _run_code_hook(hooks, product, tmp_path / hooks.parent.name, name)


def _run_code_hook(hooks, product, tmp_path, name):
    tmp_path.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        "x" * 700
    )  # a size-based token estimate above zero reaches the code
    payload = {
        **CODE_HOOKS[name],
        "transcript_path": str(transcript),
        "cwd": str(product),
    }
    decoy_log, release_log = tmp_path / "decoy.log", tmp_path / "release.log"
    closed = "http://127.0.0.1:9"
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "CLAUDE_PROJECT_DIR": str(product),
        "LU_TEST_DECOY_LOG": str(decoy_log),
        "LU_TEST_RELEASE_LOG": str(release_log),
        "LU_MONITOR_LOOPBACK": closed,
        "AB_MONITOR_URL": closed,
        "AB_DB_PATH": str(tmp_path / "messages.db"),
    }
    result = subprocess.run(
        ["bash", str(hooks / name)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=product,
        env=env,
    )
    assert not decoy_log.exists(), (decoy_log.read_text(), result.stderr)
    if name != "context-monitor.sh":
        assert release_log.exists(), (
            f"the release was never imported: rc={result.returncode} {result.stderr}"
        )


def test_codex_entry_hands_hooks_the_payload_project(release, product):
    """Location-derived project: the Codex entry gave PostToolUse hooks its own tree (R70)."""
    marker = product / "stamped"
    (product / ".githooks").mkdir()
    (product / ".githooks/pytest_stamp.py").write_text(
        f"open({str(marker)!r}, 'w').write('ok')\n"
    )
    (product / ".venv/bin").mkdir(parents=True)
    (product / ".venv/bin/python").symlink_to(sys.executable)
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "true"},
        "cwd": str(product),
        "session_id": "r70",
    }
    result = subprocess.run(
        [
            "bash",
            str(release / "lu_machinery/agent_runtime/codex_hook_entry.sh"),
            "post-tool-use",
        ],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=product,
        env={"PATH": os.environ["PATH"], "HOME": str(product.parent),
             "LEARN_UKRAINIAN_PYTHON": str(release.parent / "venv/bin/python")},
    )
    assert marker.read_text() == "ok", result.stderr
    assert not (release / ".githooks").exists()


def test_codex_entry_audit_child_keeps_the_tree_off_sys_path(product, tmp_path):
    """Tree root on sys.path: the audit child imported a stdlib-named file from it (#161 F1)."""
    tree = tmp_path / "tree"
    shutil.copytree(
        machinery_root(),
        tree / "lu_machinery",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copytree(
        SOURCE / HOOKS, tree / HOOKS, ignore=shutil.ignore_patterns("__pycache__")
    )
    marker = tmp_path / "planted"
    # repo_root imports dataclasses late, after the audit has started.
    (tree / "dataclasses.py").write_text(
        f"open({str(marker)!r}, 'w').write('loaded')\nraise ImportError('plant')\n"
    )
    (product / ".venv/bin").mkdir(parents=True)
    interpreter = product / ".venv/bin/python"
    interpreter.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    interpreter.chmod(0o755)
    (product / ".git/info/exclude").write_text(
        ".venv/\n"
    )  # a clean product: no dirt to report
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "true"},
        "hook_event_name": "PostToolUse",
        "session_id": "f1",
        "tool_use_id": "f1-call",
        "cwd": str(product),
    }
    result = subprocess.run(
        [
            "bash",
            str(tree / "lu_machinery/agent_runtime/codex_hook_entry.sh"),
            "post-tool-use",
        ],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=product,
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "LU_PRODUCT_ROOT": str(product),
        },
    )
    assert not marker.exists(), result.stderr
    assert result.returncode == 0, result.stderr

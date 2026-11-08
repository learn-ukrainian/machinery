"""Release hooks must use product authority, never their installation directory."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2]
HOOKS = Path("agents_extensions/shared/hooks")


@pytest.fixture(scope="module")
def release(tmp_path_factory):
    """Separate site-packages layout without a product package or source-path imports."""
    site = tmp_path_factory.mktemp("release") / "site-packages"
    shutil.copytree(
        SOURCE / "lu_machinery",
        site / "lu_machinery",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copytree(
        SOURCE / HOOKS, site / HOOKS, ignore=shutil.ignore_patterns("__pycache__")
    )
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
        [sys.executable, "-I", "-B", str(release / HOOKS / name)],
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
def test_shell_hook_refuses_location_derived_project(release, tmp_path, name):
    """Location-derived project root: unset configuration silently used the release tree."""
    result = run_shell(release / HOOKS / name, "agents_extensions/x", cwd=tmp_path)
    assert result.returncode == 1, result.stderr
    assert (
        f"{name}: no project root: set CLAUDE_PROJECT_DIR or LU_PRODUCT_ROOT"
        in result.stderr
    )


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
        env={"LU_PRODUCT_ROOT": str(product)},
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

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

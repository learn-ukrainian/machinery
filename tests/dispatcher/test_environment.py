"""The provider environment carries the sense-proof directory and nothing else that is ambient."""

from dataclasses import replace
import sys

import pytest
from lu_machinery.dispatcher import environment, worker, write_boundary
from lu_machinery.dispatcher.types import Invocation, TaskSpec
from tests.helpers.product_tree import configured_roots  # noqa: F401




PROOF_DIR = "LU_SENSE_PROOF_DIR"
SENTINEL = "/fixture/sense-proofs"
LOGIN_NAMES = sorted({name for names in environment._AUTH.values() for name in names} | {"CODEX_ACCESS_TOKEN"})
PROVIDERS = [(agent, None) for agent in environment._AUTH] + [("opencode", "openrouter")]


@pytest.mark.parametrize(("agent", "provider"), PROVIDERS)
def test_child_env_carries_sense_proof_dir_and_strips_logins(tmp_path, monkeypatch, agent, provider):
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setenv("LEARN_UKRAINIAN_PYTHON", sys.executable)
    for name in (*LOGIN_NAMES, "GH_TOKEN"):
        monkeypatch.setenv(name, "fixture-credential")
    spec = TaskSpec("fixture", agent, "stub", None, "read-only", tmp_path, tmp_path / "prompt", tmp_path / "final",
                    provider=provider)

    monkeypatch.setenv(PROOF_DIR, SENTINEL)
    env = environment.child_env(spec)
    assert env[PROOF_DIR] == SENTINEL
    own = set(environment._AUTH[agent]) if provider is None else {"OPENROUTER_API_KEY"}
    assert set(LOGIN_NAMES) & env.keys() <= own
    assert "GH_TOKEN" not in env

    monkeypatch.delenv(PROOF_DIR)
    assert PROOF_DIR not in environment.child_env(spec)

    # Every worker's hooks refuse without the configured shared-state anchor (#100), so it
    # reaches each provider in either mode.
    monkeypatch.setenv("LEARN_UKRAINIAN_PRIMARY_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(tmp_path / "product"))
    monkeypatch.setenv("LEARN_UKRAINIAN_STATE_ROOT", str(tmp_path))
    for mode in ("read-only", "workspace-write"):
        monkeypatch.setenv(write_boundary.BROKER_ENV, "untrusted-inherited-marker")
        child = worker._provider_env(replace(spec, mode=mode))
        assert child.get(write_boundary.BROKER_ENV) == (
            write_boundary._BROKER_INSIDE if agent == "claude" and mode == "read-only" else None)
        assert child["LEARN_UKRAINIAN_PRIMARY_REPO_ROOT"] == str(tmp_path)
        if mode == "workspace-write":
            assert child["LEARN_UKRAINIAN_PYTHON"] == sys.executable
            assert child["LU_PRODUCT_ROOT"] == str(tmp_path / "product")
            assert child["LEARN_UKRAINIAN_STATE_ROOT"] == str(tmp_path)
            assert "GH_TOKEN" not in child
            assert set(LOGIN_NAMES) & child.keys() <= own
            # The launcher must not mark hook configuration as host-only, which would strip it
            # before the contained provider starts. No sandbox or credential store is needed here.
            monkeypatch.setattr(write_boundary, "credential_files", lambda *_: [])
            argv = write_boundary.launcher(spec, Invocation(["/bin/sh"], None, child, None), tmp_path / "task.json")
            for name in ("LEARN_UKRAINIAN_PRIMARY_REPO_ROOT", "LEARN_UKRAINIAN_PYTHON", "LU_PRODUCT_ROOT",
                         "LEARN_UKRAINIAN_STATE_ROOT"):
                assert f"{name}={child[name]}" not in argv

    # A missing anchor stays missing; worker Git metadata must never fill it in.
    monkeypatch.delenv("LEARN_UKRAINIAN_PRIMARY_REPO_ROOT")
    assert "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT" not in environment.child_env(replace(spec, mode="workspace-write"))


pytestmark = pytest.mark.usefixtures("configured_roots")

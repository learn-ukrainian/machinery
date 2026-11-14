"""Runtime command diagnostics must not retain argv values (#10751)."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.agent_runtime.product_roots import agent_runtime_roots, configured_roots  # noqa: F401



from lu_machinery.agent_runtime.adapters.cursor import CursorAdapter
from lu_machinery.agent_runtime.adapters.deepseek import DeepSeekAdapter
from lu_machinery.agent_runtime.adapters.glm import GlmAdapter
from lu_machinery.agent_runtime.adapters.grok_build import GrokBuildAdapter
from lu_machinery.agent_runtime.adapters import acpx as acpx_module
from lu_machinery.agent_runtime.adapters import agy as agy_module
from lu_machinery.agent_runtime.adapters.acpx import AcpxGemmaShadowAdapter
from lu_machinery.agent_runtime.adapters.agy import AgyAdapter

pytestmark = pytest.mark.usefixtures("agent_runtime_roots")


@pytest.mark.parametrize(
    ("adapter_type", "expected_message"),
    [
        (DeepSeekAdapter, "deepseek invocation"),
        (GlmAdapter, "glm invocation"),
    ],
)
def test_sibling_invocation_logs_constant_message(tmp_path, monkeypatch, caplog, adapter_type, expected_message):
    """Defect class: violation of the sibling invocation logs constant message contract."""
    for variable in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "BUILDKITE", "JENKINS_URL"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    environment_value = "private-environment-value"
    monkeypatch.setenv("LOG_REDACTION_TEST_VALUE", environment_value)
    secret = "secret-looking-command-option-value"
    adapter = adapter_type()
    if adapter_type is DeepSeekAdapter:
        monkeypatch.setattr(f"{adapter_type.__module__}._cached_flash_name", lambda: "DeepSeek V4.1 Flash")
    if adapter_type is GlmAdapter:
        with pytest.raises(ValueError, match="is retired in the model catalog") as refused:
            adapter.build_invocation(prompt="private-prompt-value", mode="workspace-write", cwd=tmp_path,
                                     model=None, task_id="private-task-value", session_id="private-session-value",
                                     tool_config={}, effort=secret)
        assert secret not in str(refused.value)
        assert environment_value not in str(refused.value)
        assert caplog.records == []
        return
    tool_config = {}
    effort = secret if adapter_type in (DeepSeekAdapter, GlmAdapter) else None
    with (
        patch(f"{adapter_type.__module__}.shutil.which", return_value=str(tmp_path / "runtime-cli")),
        caplog.at_level(logging.DEBUG),
    ):
        plan = adapter.build_invocation(
            prompt="private-prompt-value",
            mode="workspace-write",
            cwd=tmp_path,
            model=None,
            task_id="private-task-value",
            session_id="private-session-value",
            tool_config=tool_config,
            effort=effort,
        )
    assert secret in plan.cmd
    records = [record for record in caplog.records if record.name == adapter_type.__module__]
    assert len(records) == 1
    assert records[0].getMessage() == expected_message
    assert records[0].args == ()
    assert records[0].exc_info is None
    private_values = (
        secret,
        Path(plan.cmd[0]).name,
        adapter.default_model,
        "private-prompt-value",
        "private-task-value",
        "private-session-value",
        str(tmp_path),
        environment_value,
        *plan.env_overrides.values(),
    )
    for record in caplog.records:
        assert record.args == ()
        for value in plan.cmd:
            assert value not in record.getMessage()
            # Match whole values: the fixed lu_machinery.agent_runtime source path contains "run".
            assert repr(value) not in repr(vars(record))
        for value in private_values:
            assert value not in record.getMessage()
            assert value not in repr(vars(record))


def _assert_single_constant_warning(caplog, adapter_type, secret, expected_message):
    warnings = [
        record
        for record in caplog.records
        if record.name == adapter_type.__module__ and record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    record = warnings[0]
    assert record.getMessage() == expected_message
    assert record.args == ()
    assert record.exc_info is None
    assert secret not in record.getMessage()
    assert secret not in repr(vars(record))


def test_deepseek_max_budget_warning_is_constant(tmp_path, monkeypatch, caplog):
    """Defect class: violation of the deepseek max budget warning is constant contract."""
    for variable in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "BUILDKITE", "JENKINS_URL"):
        monkeypatch.delenv(variable, raising=False)
    secret = "secret-looking-budget-value"
    with (
        patch(f"{DeepSeekAdapter.__module__}.shutil.which", return_value=str(tmp_path / "runtime-cli")),
        patch(f"{DeepSeekAdapter.__module__}._cached_flash_name", return_value=None),
        caplog.at_level(logging.DEBUG),
    ):
        DeepSeekAdapter().build_invocation(
            prompt="private-prompt-value",
            mode="workspace-write",
            cwd=tmp_path,
            tool_config={"max_budget_usd": secret},
        )
    _assert_single_constant_warning(
        caplog,
        DeepSeekAdapter,
        secret,
        "deepseek: max_budget_usd ignored; use hard-timeout/silence-timeout instead",
    )


_NO_CI_VARIABLES = ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "BUILDKITE", "JENKINS_URL")
_BUDGET_MESSAGE = "non-claude adapter ignoring max_budget_usd; use hard-timeout/silence-timeout instead"


def _isolated_environment(monkeypatch, tmp_path):
    for variable in _NO_CI_VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    # The AGY hook wiring is not under test; the warning fires before it.
    monkeypatch.setattr(agy_module, "require_hook_config", lambda *_args, **_kwargs: None)


def _assert_constant_record(caplog, adapter_type, level, message, *secrets):
    """Exactly one record carries ``message``, and nothing logged by the adapter module leaks a secret."""
    module_name = adapter_type.__module__
    matching = [
        record
        for record in caplog.records
        if record.name == module_name and record.levelno == level and record.getMessage() == message
    ]
    assert len(matching) == 1
    assert matching[0].args == ()
    assert matching[0].exc_info is None
    for record in caplog.records:
        if record.name != module_name:
            continue
        for secret in secrets:
            assert secret not in record.getMessage()
            assert secret not in repr(vars(record))


def _build_plan(
    adapter_type, tmp_path, *, tool_config=None, effort=None, mode="workspace-write", model=None, task_id=None
):
    return adapter_type().build_invocation(
        prompt="private-prompt-value",
        mode=mode,
        cwd=tmp_path,
        model=model,
        task_id=task_id,
        session_id=None,
        tool_config=tool_config,
        effort=effort,
    )


@pytest.mark.parametrize(
    ("adapter_type", "mode", "model"),
    [
        (AgyAdapter, "workspace-write", None),
    ],
)
def test_non_claude_max_budget_warning_is_constant(tmp_path, monkeypatch, caplog, adapter_type, mode, model):
    """Defect class: violation of the non claude max budget warning is constant contract."""
    _isolated_environment(monkeypatch, tmp_path)
    secret = "secret-looking-budget-value"
    tool_config = {"max_budget_usd": secret}
    with caplog.at_level(logging.DEBUG):
        _build_plan(adapter_type, tmp_path, tool_config=tool_config, mode=mode, model=model)
    _assert_constant_record(caplog, adapter_type, logging.WARNING, _BUDGET_MESSAGE, secret)


@pytest.mark.parametrize(
    ("adapter_type", "mode", "model", "expected_message"),
    [
        (
            AgyAdapter,
            "workspace-write",
            None,
            "agy effort not yet wired through CLI — using TUI-selected model default (#1396 follow-up)",
        ),
        (CursorAdapter, "read-only", None, "cursor adapter ignoring effort (not supported by CLI)"),
    ],
)
def test_ignored_effort_debug_is_constant(tmp_path, monkeypatch, caplog, adapter_type, mode, model, expected_message):
    """Defect class: violation of the ignored effort debug is constant contract."""
    _isolated_environment(monkeypatch, tmp_path)
    secret = "secret-looking-effort-value"
    with caplog.at_level(logging.DEBUG):
        _build_plan(adapter_type, tmp_path, effort=secret, mode=mode, model=model, task_id="private-task-value")
    _assert_constant_record(caplog, adapter_type, logging.DEBUG, expected_message, secret, "private-task-value")


def test_acpx_ignored_effort_debug_is_constant(tmp_path, monkeypatch, caplog):
    """Defect class: violation of the acpx ignored effort debug is constant contract."""
    from tests.agent_runtime.test_acpx_adapter import _patch_provider_binary_lookup, _stub_binary

    _isolated_environment(monkeypatch, tmp_path)
    _stub_binary(monkeypatch, tmp_path)
    opencode = tmp_path / "opencode"
    opencode.write_text("#!/bin/sh\n", encoding="utf-8")
    opencode.chmod(0o755)
    _patch_provider_binary_lookup(monkeypatch, lambda name, *_a, **_k: {"opencode": str(opencode)}.get(name))
    monkeypatch.setattr(acpx_module, "_probe_participant_cli_compatibility", lambda _path, _executable: ("1.17.13", ()))
    monkeypatch.setenv(acpx_module.TRANSPORT_ENV, "active")
    secret = "secret-looking-effort-value"
    with acpx_module.active_discussion_scope(), caplog.at_level(logging.DEBUG):
        _build_plan(
            AcpxGemmaShadowAdapter,
            tmp_path,
            tool_config={
                "acpx_discussion": True,
                "target_agent": "gemma",
                "correlation_id": "corr-1",
                "idempotency_key": "idem-1",
            },
            effort=secret,
            mode="read-only",
            model="google-ais/gemma-4-31b-it",
            task_id="t-1",
        )
    _assert_constant_record(
        caplog,
        AcpxGemmaShadowAdapter,
        logging.DEBUG,
        "ACPX adapter effort has no ACPX flag equivalent; ignoring",
        secret,
    )


def test_grok_build_invocation_debug_is_constant(tmp_path, monkeypatch, caplog):
    """Defect class: violation of the grok build invocation debug is constant contract."""
    _isolated_environment(monkeypatch, tmp_path)
    secret = "secret-looking-task-value"
    with (
        patch(f"{GrokBuildAdapter.__module__}.shutil.which", return_value=str(tmp_path / "runtime-cli")),
        caplog.at_level(logging.DEBUG),
    ):
        _build_plan(GrokBuildAdapter, tmp_path, mode="danger", task_id=secret)
    _assert_constant_record(caplog, GrokBuildAdapter, logging.DEBUG, "grok invocation", secret)


def test_agy_interim_language_warning_is_constant(caplog):
    """Defect class: violation of the agy interim language warning is constant contract."""
    secret = "pending-work wording: 'secret-looking-reply-excerpt'"
    with caplog.at_level(logging.DEBUG):
        excerpt = agy_module._with_language_warning(secret, None)
    assert excerpt.splitlines() == [agy_module.AGY_INTERIM_LANGUAGE_WARNING, secret]
    _assert_constant_record(
        caplog, AgyAdapter, logging.WARNING, agy_module.AGY_INTERIM_LANGUAGE_WARNING, "secret-looking-reply-excerpt"
    )

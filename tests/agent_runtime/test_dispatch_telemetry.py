"""Focused tests for dispatch telemetry resolution and persistence."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lu_machinery.agent_runtime.adapters.base import InvocationPlan
from lu_machinery.agent_runtime.result import ParseResult
from lu_machinery.agent_runtime.runner import invoke
from lu_machinery.agent_runtime.telemetry import (
    _reset_version_cache_for_tests,
    resolve_dispatch_start_telemetry,
    resolve_invocation_telemetry,
)


@pytest.fixture(autouse=True)
def _isolate_version_cache():
    """Keep mocked probe results from leaking between tests or xdist batches."""
    _reset_version_cache_for_tests()
    yield
    _reset_version_cache_for_tests()


def test_resolve_dispatch_start_telemetry_codex_model_from_registry_effort_from_config(tmp_path, monkeypatch):
    """Model must record what the adapter actually sends (registry default),
    NOT the user's config.toml — the adapter always passes an explicit ``-m``.
    Effort genuinely falls through to config.toml, so it stays config-sourced.
    Regression: 2026-07-09 state files recorded config.toml's gpt-5.6-sol while
    the CLI ran -m gpt-5.6-terra."""
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text(
        'model = "gpt-5.6-sol"\nmodel_reasoning_effort = "high"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("lu_machinery.agent_runtime.telemetry.Path.home", lambda: home)

    with patch("lu_machinery.agent_runtime.telemetry.codex_cli_version", return_value="0.123.0"):
        telemetry = resolve_dispatch_start_telemetry(
            agent_name="codex",
            requested_model=None,
            requested_effort=None,
        )

    from lu_machinery.agent_runtime.registry import AGENTS

    assert telemetry.model == AGENTS["codex"]["default_model"]  # what -m actually sends
    assert telemetry.model != "gpt-5.6-sol"  # never the config.toml value
    assert telemetry.effort == "high"  # effort DOES fall through to config.toml
    assert telemetry.cli_version == "0.123.0"


def test_resolve_dispatch_start_telemetry_codex_explicit_model_wins(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text('model = "gpt-5.6-sol"\n', encoding="utf-8")
    monkeypatch.setattr("lu_machinery.agent_runtime.telemetry.Path.home", lambda: home)

    with patch("lu_machinery.agent_runtime.telemetry.codex_cli_version", return_value="0.123.0"):
        telemetry = resolve_dispatch_start_telemetry(
            agent_name="codex",
            requested_model="gpt-5.6-luna",
            requested_effort="medium",
        )

    assert telemetry.model == "gpt-5.6-luna"
    assert telemetry.effort == "medium"


def test_resolve_invocation_telemetry_reads_claude_plan_flags():
    plan = InvocationPlan(
        cmd=[
            "claude",
            "-p",
            "hi",
            "--model",
            "claude-opus-4-6",
            "--effort",
            "xhigh",
        ],
        cwd=Path("."),
    )

    with patch("lu_machinery.agent_runtime.telemetry._probe_version", return_value="2.1.89"):
        telemetry = resolve_invocation_telemetry(
            agent_name="claude",
            plan=plan,
            requested_model=None,
            requested_effort=None,
        )

    assert telemetry.model == "claude-opus-4-6"
    assert telemetry.effort == "xhigh"
    assert telemetry.cli_version == "2.1.89"


def test_deepseek_telemetry_records_catalog_identity_not_moving_invocation_alias(tmp_path):
    for route, identity in (
        ("deepseek/deepseek-flash", "deepseek-v4.1-flash"),
        ("deepseek/deepseek-v4-pro", "deepseek-v4-pro"),
        ("deepseek/deepseek-v4-flash", "deepseek-v4-flash"),
        ("deepseek/unknown-model", "deepseek/unknown-model"),
    ):
        plan = InvocationPlan(
            cmd=["opencode", "run", "--model", route, "--variant", "high"],
            cwd=tmp_path,
        )
        with patch("lu_machinery.agent_runtime.telemetry._probe_version", return_value="1.18.0"):
            telemetry = resolve_invocation_telemetry(
                agent_name="deepseek",
                plan=plan,
                requested_model=identity,
                requested_effort=None,
            )
        assert telemetry.model == identity
        assert telemetry.effort == "high"
        assert telemetry.cli_version == "1.18.0"


def test_expected_effort_markers_and_version_probes_do_not_warn(tmp_path, monkeypatch, caplog):
    """Known CLI limits are explicit metadata, not dispatch warnings (#4837)."""
    probed: list[tuple[str, ...]] = []

    def probe(prefix: tuple[str, ...]) -> str | None:
        probed.append(prefix)
        return {
            "agy": "1.1.1",
            "cursor-agent": "2026.07.09",
            "opencode": "1.18.0",
        }.get(Path(prefix[0]).name)

    caplog.set_level(logging.WARNING, logger="lu_machinery.agent_runtime.telemetry")
    monkeypatch.setattr(
        "lu_machinery.agent_runtime.adapters.cursor.resolve_cursor_agent_binary",
        lambda: "/usr/local/bin/cursor-agent",
    )
    with patch("lu_machinery.agent_runtime.telemetry._probe_version", side_effect=probe):
        agy = resolve_dispatch_start_telemetry(
            agent_name="agy",
            requested_model=None,
            requested_effort=None,
        )
        cursor = resolve_invocation_telemetry(
            agent_name="cursor",
            plan=InvocationPlan(cmd=["cursor-agent", "-p"], cwd=tmp_path),
            requested_model=None,
            requested_effort=None,
        )
        deepseek = resolve_dispatch_start_telemetry(
            agent_name="deepseek",
            requested_model=None,
            requested_effort=None,
        )

    assert (agy.effort, agy.cli_version) == ("not-exposed", "1.1.1")
    assert (cursor.effort, cursor.cli_version) == ("not-exposed", "2026.07.09")
    assert ("/usr/local/bin/cursor-agent",) in probed
    assert (deepseek.effort, deepseek.cli_version) == ("high", "1.18.0")
    assert "dispatch telemetry for" not in caplog.text


def test_unexpected_version_probe_failure_still_warns(caplog):
    """Only a genuine resolution failure may produce an unknown warning."""
    caplog.set_level(logging.WARNING, logger="lu_machinery.agent_runtime.telemetry")
    with patch("lu_machinery.agent_runtime.telemetry._probe_version", return_value=None):
        telemetry = resolve_dispatch_start_telemetry(
            agent_name="agy",
            requested_model=None,
            requested_effort=None,
        )

    assert telemetry.effort == "not-exposed"
    assert telemetry.cli_version == "unknown"
    assert (
        "dispatch telemetry for agy could not resolve cli_version: version probe failed; recording 'unknown'"
    ) in caplog.text


def test_runner_invoke_preserves_observed_terminal_returncode_and_telemetry(tmp_path):
    from lu_machinery.agent_runtime import runner as runner_mod

    spy_adapter = MagicMock()
    spy_adapter.supported_modes = frozenset({"read-only", "workspace-write", "danger"})
    spy_adapter.default_model = "gpt-5.4"
    fake_plan = InvocationPlan(
        cmd=["codex", "exec", "-m", "gpt-5.5", "-"],
        cwd=tmp_path,
    )
    spy_adapter.build_invocation.return_value = fake_plan
    spy_adapter.liveness_signal_paths.return_value = ()
    spy_adapter.parse_response.return_value = MagicMock(
        ok=True,
        response="ok",
        stderr_excerpt=None,
        rate_limited=False,
        session_id=None,
        tokens=None,
    )

    fake_proc = MagicMock()
    fake_proc.poll.return_value = 0
    # The completed poll result is authoritative even when a Popen wrapper
    # has not reflected it through ``.returncode`` yet (#4837).
    fake_proc.returncode = None
    fake_proc.stdin = None
    # Patching runner.subprocess.Popen also replaces the stdlib Popen that
    # ``subprocess.run`` uses inside ``build_agent_env`` / git isolation, so
    # communicate() must return a real (stdout, stderr) pair (#7020).
    fake_proc.communicate.return_value = ("", "")
    fake_proc.__enter__.return_value = fake_proc
    fake_proc.__exit__.return_value = False

    with (
        patch("lu_machinery.agent_runtime.runner._load_adapter", return_value=spy_adapter),
        patch(
            "lu_machinery.agent_runtime.runner.has_headroom",
            return_value=(True, ""),
        ),
        patch("lu_machinery.agent_runtime.runner.write_record"),
        patch(
            "lu_machinery.agent_runtime.runner.subprocess.Popen",
            return_value=fake_proc,
        ),
        patch(
            "lu_machinery.agent_runtime.runner.start_watchdog",
            return_value=(MagicMock(stdout_lines=[], stderr_lines=[]), []),
        ),
        patch("lu_machinery.agent_runtime.runner.stop_watchdog"),
        patch(
            "lu_machinery.agent_runtime.runner.resolve_invocation_telemetry",
            return_value=runner_mod.InvocationTelemetry(
                model="gpt-5.5",
                effort="high",
                cli_version="0.123.0",
            ),
        ),
    ):
        result = invoke(
            "codex",
            "hi",
            mode="read-only",
            cwd=tmp_path,
            model="gpt-5.4",
            entrypoint="delegate",
        )

    assert result.model_identity == {
        "source": "configured_request",
        "provider_observed_model": None,
        "provider_observed_model_version": None,
        "provider_observation": "unknown",
    }
    assert result.usage_record["model_identity"] == result.model_identity
    assert result.model == "gpt-5.5"
    assert result.effort == "high"
    assert result.cli_version == "0.123.0"
    assert result.returncode == 0
    assert result.usage_record["returncode"] == 0


def test_runner_usage_record_includes_correlation_ids_without_prompt_change(
    tmp_path,
    monkeypatch,
):
    from lu_machinery.agent_runtime import runner as runner_mod

    monkeypatch.setenv("LU_RUN_ID", "run-123")
    monkeypatch.setenv("LU_SESSION_ID", "session-456")
    monkeypatch.setenv("LU_TELEMETRY_LEVEL", "b1")
    monkeypatch.setenv("LU_TELEMETRY_SLUG", "telemetry-module")
    monkeypatch.setenv("LU_TELEMETRY_TRACK", "core")
    monkeypatch.setenv("LU_TELEMETRY_SOURCE", "pytest")

    spy_adapter = MagicMock()
    spy_adapter.supported_modes = frozenset({"read-only", "workspace-write", "danger"})
    spy_adapter.default_model = "gpt-5.4"
    fake_plan = InvocationPlan(cmd=["codex", "exec", "-m", "gpt-5.5", "-"], cwd=tmp_path)
    spy_adapter.build_invocation.return_value = fake_plan
    spy_adapter.liveness_signal_paths.return_value = ()
    spy_adapter.parse_response.return_value = ParseResult(
        ok=True,
        response="ok",
        stderr_excerpt=None,
        rate_limited=False,
        session_id=None,
        tokens=321,
    )

    fake_proc = MagicMock()
    fake_proc.poll.return_value = 0
    fake_proc.returncode = 0
    fake_proc.stdin = None
    # Same stdlib-Popen coupling as the returncode telemetry test (#7020).
    fake_proc.communicate.return_value = ("", "")
    fake_proc.__enter__.return_value = fake_proc
    fake_proc.__exit__.return_value = False
    captured_records: list[dict] = []
    captured_env: dict[str, str] = {}

    def fake_popen(*_args, **kwargs):
        captured_env.update(kwargs.get("env") or {})
        return fake_proc

    with (
        patch("lu_machinery.agent_runtime.runner._load_adapter", return_value=spy_adapter),
        patch(
            "lu_machinery.agent_runtime.runner.has_headroom",
            return_value=(True, ""),
        ),
        patch("lu_machinery.agent_runtime.runner.write_record", side_effect=captured_records.append),
        patch(
            "lu_machinery.agent_runtime.runner.subprocess.Popen",
            side_effect=fake_popen,
        ),
        patch(
            "lu_machinery.agent_runtime.runner.start_watchdog",
            return_value=(MagicMock(stdout_lines=[], stderr_lines=[]), []),
        ),
        patch("lu_machinery.agent_runtime.runner.stop_watchdog"),
        patch(
            "lu_machinery.agent_runtime.runner.resolve_invocation_telemetry",
            return_value=runner_mod.InvocationTelemetry(
                model="gpt-5.5",
                effort="high",
                cli_version="0.123.0",
            ),
        ),
    ):
        result = invoke(
            "codex",
            "hi",
            mode="read-only",
            cwd=tmp_path,
            model="gpt-5.4",
            entrypoint="delegate",
        )

    assert result.ok is True
    from lu_machinery.lib import rules_core

    prompt = spy_adapter.build_invocation.call_args.kwargs["prompt"]
    assert prompt == rules_core.core_block(rules_core.resolve_seat()) + "\n\nhi"
    assert prompt.count(rules_core.BLOCK_OPEN) == 1
    assert captured_records == [result.usage_record]
    record = captured_records[0]
    assert record["run_id"] == "run-123"
    assert record["session_id"] == "session-456"
    assert record["level"] == "b1"
    assert record["slug"] == "telemetry-module"
    assert record["track"] == "core"
    assert record["source"] == "pytest"
    assert captured_env["LU_RUN_ID"] == "run-123"
    assert captured_env["LU_SESSION_ID"] == "session-456"


def test_acp_gemma_shadow_reads_stamped_plan_metadata(tmp_path, caplog):
    """#6852: gemma ACP plans already carry model/version; do not warn unknown."""
    plan = InvocationPlan(
        cmd=["acpx", "--model", "google-ais/gemma-4-31b-it", "gemma", "exec", "-f", "-"],
        cwd=tmp_path,
        metadata={
            "acpx_transport": True,
            "target_agent": "gemma",
            "model": "google-ais/gemma-4-31b-it",
            "acpx_cli_version": "0.14.7",
            "provider_cli": "opencode",
            "provider_cli_version": "1.2.3",
        },
    )
    caplog.set_level(logging.WARNING, logger="lu_machinery.agent_runtime.telemetry")
    telemetry = resolve_invocation_telemetry(
        agent_name="acpx-gemma-shadow",
        plan=plan,
        requested_model=None,
        requested_effort=None,
    )
    assert telemetry.model == "google-ais/gemma-4-31b-it"
    assert telemetry.effort == "not-exposed"
    assert telemetry.cli_version == "1.2.3"
    assert "dispatch telemetry for" not in caplog.text


def test_acp_shadow_adapters_resolve_effort_and_cli_version_from_identity(tmp_path, caplog):
    """Sibling sweep: every ACP shadow seat maps identity, never warns unknown."""
    from lu_machinery.agent_runtime.adapters.acpx import (
        ACPX_PARTICIPANT_EFFORTS,
        ACPX_SUPPORTED_PARTICIPANTS,
    )

    caplog.set_level(logging.WARNING, logger="lu_machinery.agent_runtime.telemetry")
    for seat, spec in ACPX_SUPPORTED_PARTICIPANTS.items():
        adapter = str(spec["seat"])
        stamped_effort = ACPX_PARTICIPANT_EFFORTS.get(seat)
        metadata = {
            "acpx_transport": True,
            "target_agent": spec["agent"],
            "acpx_cli_version": "0.14.7",
        }
        if spec["model"]:
            metadata["model"] = spec["model"]
        if stamped_effort:
            metadata["effort"] = stamped_effort
        plan = InvocationPlan(
            cmd=["acpx", seat, "exec", "-f", "-"],
            cwd=tmp_path,
            metadata=metadata,
        )
        telemetry = resolve_invocation_telemetry(
            agent_name=adapter,
            plan=plan,
            requested_model=None,
            requested_effort=None,
        )
        assert telemetry.effort == (stamped_effort or "not-exposed"), adapter
        assert telemetry.cli_version == "0.14.7", adapter
        if spec["model"]:
            assert telemetry.model == spec["model"], adapter
        else:
            assert telemetry.model == "not-exposed", adapter
    assert "dispatch telemetry for" not in caplog.text



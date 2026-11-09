"""Helpers copied from learn-ukrainian ``tests/agent_runtime/test_runner_failover.py`` at 5aba2adb.

The source file is product-kept or owned by another tranche; its tests do not move here.
Only the helpers that tests in this directory import are copied. Their bodies are verbatim; the import block keeps what they use, with machinery import paths.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lu_machinery.agent_runtime import runner as runner_mod
from lu_machinery.agent_runtime.adapters.base import InvocationPlan
from lu_machinery.agent_runtime.failover import (
    FAILOVER_CONFIG_ENV,
    FAILOVER_COOLDOWN_DB_ENV,
)
from lu_machinery.agent_runtime.result import ParseResult
from lu_machinery.agent_runtime.routes import RUNTIME_ROUTE_TOOL_CONFIG_KEY


class _FailoverTestAdapter:
    name = "failover-test"
    default_model = "primary-model"
    supported_modes = frozenset({"read-only", "workspace-write", "danger"})

    def __init__(self) -> None:
        self.attempts: list[str] = []

    def build_invocation(
        self,
        *,
        prompt: str,
        mode: str,
        cwd: Path,
        model: str | None,
        task_id: str | None,
        session_id: str | None,
        tool_config: dict | None,
        effort: str | None = None,
    ) -> InvocationPlan:
        _ = prompt
        _ = mode
        _ = task_id
        _ = session_id
        _ = effort
        route = dict((tool_config or {}).get(RUNTIME_ROUTE_TOOL_CONFIG_KEY) or {})
        route_model = str(route.get("model") or model or self.default_model)
        self.attempts.append(route_model)
        return InvocationPlan(
            cmd=["fake-agent", route_model],
            cwd=cwd,
            metadata={"route": route},
        )

    def parse_response(
        self,
        *,
        stdout: str,
        stderr: str,
        returncode: int,
        output_file: Path | None,
        plan: InvocationPlan | None = None,
        call_start_time: float | None = None,
    ) -> ParseResult:
        _ = stdout
        _ = stderr
        _ = returncode
        _ = output_file
        _ = plan
        _ = call_start_time
        raise AssertionError("_execute_invocation_plan is stubbed in these tests")

    def liveness_signal_paths(self, plan: InvocationPlan) -> tuple[Path, ...]:
        _ = plan
        return ()


def _write_failover_config(tmp_path: Path, *, forbidden: bool = False) -> Path:
    fallback_provider = "zai" if forbidden else "fallback-provider"
    fallback_model = "glm-4.6" if forbidden else "fallback-model"
    config_path = tmp_path / "agent_runtime_failover.yaml"
    config_path.write_text(
        "\n".join(
            [
                "chains:",
                "  failover-test:",
                "    cooldown_ttl_s: 600",
                "    routes:",
                "      - provider: primary-provider",
                "        model: primary-model",
                f"      - provider: {fallback_provider}",
                f"        model: {fallback_model}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config_path


def _install_fake_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    outcomes_by_model: dict[str, dict[str, Any]],
    *,
    forbidden_config: bool = False,
) -> tuple[_FailoverTestAdapter, list[dict[str, Any]], list[tuple[str, dict[str, Any]]]]:
    config_path = _write_failover_config(tmp_path, forbidden=forbidden_config)
    monkeypatch.setenv(FAILOVER_CONFIG_ENV, str(config_path))
    monkeypatch.setenv(
        FAILOVER_COOLDOWN_DB_ENV,
        str(tmp_path / "cooldowns.sqlite3"),
    )

    adapter = _FailoverTestAdapter()
    monkeypatch.setitem(
        runner_mod.AGENTS,
        "failover-test",
        {
            "adapter": "tests.fake:FailoverTestAdapter",
            "default_model": "primary-model",
            "cost_tier": "test",
            "capabilities": frozenset(),
            "cli_available": True,
            "resume_policy": "never",
        },
    )
    runner_mod._ADAPTER_CACHE["failover-test"] = adapter

    records: list[dict[str, Any]] = []
    emitted_events: list[tuple[str, dict[str, Any]]] = []
    fake_emit = types.ModuleType("lu_machinery.telemetry.emit")
    fake_emit.emit_event = lambda event_name, payload: emitted_events.append((event_name, payload))
    monkeypatch.setitem(sys.modules, "lu_machinery.telemetry.emit", fake_emit)
    monkeypatch.setattr(runner_mod, "write_record", records.append)
    monkeypatch.setattr(runner_mod, "has_headroom", lambda _agent, _model: (True, ""))
    monkeypatch.setattr(
        runner_mod,
        "resolve_invocation_telemetry",
        lambda agent_name, plan, requested_model, requested_effort: SimpleNamespace(
            model=requested_model,
            effort=requested_effort or "unknown",
            cli_version=f"{agent_name}-test",
        ),
    )

    def fake_execute(*, plan: InvocationPlan, **_kwargs: Any) -> runner_mod._ExecutionOutcome:
        route = plan.metadata["route"]
        outcome = outcomes_by_model[str(route["model"])]
        return runner_mod._ExecutionOutcome(
            parse=outcome["parse"],
            duration_s=0.01,
            returncode=outcome.get("returncode", 1),
            kill_reason=outcome.get("kill_reason"),
            stdout_text=outcome.get("stdout_text", ""),
            stderr_text=outcome.get("stderr_text", ""),
            liveness_paths=(),
        )

    monkeypatch.setattr(runner_mod, "_execute_invocation_plan", fake_execute)
    return adapter, records, emitted_events


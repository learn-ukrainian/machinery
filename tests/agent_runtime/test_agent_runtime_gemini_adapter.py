"""Gemini adapter: the native CLI argv is refused; auth resolution stays."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from lu_machinery.agent_runtime.adapters.gemini import (
    GeminiAdapter,
    resolve_gemini_auth_mode,
)
from lu_machinery.agent_runtime.errors import AgentUnavailableError


def _refuse(prompt: str, tmp_path: Path, **kwargs) -> None:
    GeminiAdapter().build_invocation(
        prompt=prompt,
        mode=kwargs.pop("mode", "workspace-write"),
        cwd=tmp_path,
        model=kwargs.pop("model", "gemini-3.1-pro-high"),
        task_id=None,
        session_id=None,
        tool_config=kwargs.pop("tool_config", None),
        effort=kwargs.pop("effort", None),
    )


def test_build_invocation_refuses_the_retired_gemini_cli(tmp_path):
    """The retired CLI must not return an argv, including one that names gemini."""
    prompt = "do not put this prompt on a command line"
    with pytest.raises(
        AgentUnavailableError, match="The gemini CLI is retired"
    ) as refused:
        _refuse(prompt, tmp_path)
    assert prompt not in str(refused.value)


def test_large_prompt_refusal_does_not_create_temp_file(tmp_path, monkeypatch):
    """Refusal happens before any prompt file is created."""
    monkeypatch.setenv("GEMINI_AUTH_MODE", "api")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    with pytest.raises(AgentUnavailableError, match="retired"):
        GeminiAdapter().build_invocation(
            prompt="x" * 200_000,
            mode="workspace-write",
            cwd=tmp_path,
            model="gemini-3.1-pro-high",
            task_id=None,
            session_id=None,
            tool_config=None,
        )

    assert not list(tmp_path.glob("learn-ukrainian-gemini-prompt-*"))


def test_mcp_and_subscription_callers_also_refuse(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_AUTH_MODE", "subscription")
    monkeypatch.setenv("GEMINI_API_KEY", "secret")
    with pytest.raises(AgentUnavailableError, match="retired"):
        _refuse("hello", tmp_path, tool_config={"mcp_server_names": ["sources"]})


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, "subscription"),
        ({"GEMINI_API_KEY": "secret"}, "api"),
        ({"GOOGLE_API_KEY": "secret"}, "api"),
        (
            {"GEMINI_AUTH_MODE": "subscription", "GEMINI_API_KEY": "secret"},
            "subscription",
        ),
        ({"GEMINI_AUTH_MODE": "api"}, "api"),
    ],
)
def test_resolve_gemini_auth_mode_api_first_matrix(env, expected):
    assert resolve_gemini_auth_mode(env, cooldown_active=False) == expected

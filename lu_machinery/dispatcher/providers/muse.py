"""Muse exec transport confined by the shared OS file sandbox."""

from __future__ import annotations

import json

from lu_machinery.agent_runtime.adapters.muse import _PROFILES, _TOOLS, confined_muse_argv
from lu_machinery.common.jsonl import jsonl_lines
from lu_machinery.dispatcher.types import Invocation, ParsedResult, TaskSpec


def build(spec: TaskSpec, env: dict[str, str]) -> Invocation:
    if spec.mode != "read-only":
        raise ValueError("muse_worker_mode_refused")
    argv = ["muse", "exec", "--json", "--model", spec.model, "--disable-web-tools"]
    argv += ["--workspace", str(spec.worktree), "--worktree", "off", "--prompt-file", str(spec.prompt_file)]
    # The admission check forces this option off for every private input class.
    argv += ["--contributor-mode", spec.options.get("contributor_mode", "off")]
    if spec.effort:
        argv += ["--reasoning-effort", spec.effort]
    argv += ["--permission-profile", _PROFILES[spec.mode], "--disable-shell",
             "--disable-reminders", "--disable-request-user-input", "--no-foreign-personal-context",
             "--config", "run.toolset=" + json.dumps(_TOOLS[spec.mode])]
    argv.append("--disable-write")
    return Invocation(
        confined_muse_argv(argv, spec.worktree, spec.mode, spec.task_id),
        None, {**env, "MUSE_NO_AUTO_UPDATE": "1"}, None,
    )


def parse(stdout: str, stderr: str, returncode: int, final_text: str | None) -> ParsedResult:
    text = usage = None
    error = f"muse exited with code {returncode}" if returncode else None
    for line in jsonl_lines(stdout):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind, payload = event.get("payload_type", ""), event.get("payload")
        if not isinstance(kind, str):
            continue
        if kind == "run.usage" and isinstance(payload, dict):
            usage = payload
        elif kind.startswith("run.terminal."):
            if (
                kind != "run.terminal.completed"
                or not isinstance(payload, dict)
                or payload.get("terminal", "completed") != "completed"
                or payload.get("reason") is not None
            ):
                error = error or "muse terminal failure"
            else:
                text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        text = None
        error = error or "muse missing final response"
    return ParsedResult(text, usage, error)

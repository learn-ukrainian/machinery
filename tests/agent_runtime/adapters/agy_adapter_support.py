"""Helpers copied from learn-ukrainian ``tests/agent_runtime/adapters/test_agy_adapter.py`` at 5aba2adb.

The source file is product-kept or owned by another tranche; its tests do not move here.
Only the helpers that tests in this directory import are copied. Their bodies are verbatim; the import block keeps what they use, with machinery import paths.
"""

from __future__ import annotations

import json
from pathlib import Path

from lu_machinery.agent_runtime.adapters import agy as agy_module
from lu_machinery.agent_runtime.adapters.base import InvocationPlan

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "agy"


def _plan(
    tmp_path: Path,
    *,
    log_file: Path,
    app_data: Path,
    cmd: list[str] | None = None,
) -> InvocationPlan:
    return InvocationPlan(
        cmd=cmd or ["agy"],
        cwd=tmp_path,
        stdin_payload="",
        output_file=None,
        env_overrides={
            "AGY_RUNTIME_LOG_FILE": str(log_file),
            "AGY_APP_DATA_DIR": str(app_data),
        },
        env_unsets=(),
        liveness_paths=(log_file,),
        metadata={},
    )


_FINISHED_CONVERSATION_ID = "3aca585b-1812-499a-8884-4a49d135a486"


def _background_plan(tmp_path: Path, conversation_id: str, transcript_lines: list[str]) -> InvocationPlan:
    app_data = tmp_path / "antigravity-cli"
    transcript = agy_module._brain_transcript_path(app_data, conversation_id)
    transcript.parent.mkdir(parents=True)
    transcript.write_text("\n".join(transcript_lines) + "\n", encoding="utf-8")
    log_file = tmp_path / "agy.log"
    log_file.write_text(f"I0924 server.go:1185] Created conversation {conversation_id}\n", encoding="utf-8")
    return _plan(tmp_path, log_file=log_file, app_data=app_data)


def _fixture_lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


_TASK_2 = f"{_FINISHED_CONVERSATION_ID}/task-2"


_TASK_3 = f"{_FINISHED_CONVERSATION_ID}/task-3"


def _event(event_type: str, content: str, *, step_index: int | None = None, **extra: object) -> str:
    event: dict[str, object] = {"type": event_type, "status": "DONE", "content": content, **extra}
    if step_index is not None:
        event["step_index"] = step_index
    return json.dumps(event)


def _start(task_id: str, *, step_index: int | None = None, description: str = "pytest -q") -> str:
    return _event(
        "GENERIC",
        f"Created At: 2026-09-24T10:10:12Z\nTool is running as a background task with task id: {task_id}\n"
        f"Task Description: {description}\nTask logs are available at: file:///agy-app-data/tasks/x.log",
        step_index=step_index,
        status="RUNNING",
        source="MODEL",
    )


_SYSTEM_MESSAGE_PREAMBLE = (
    "The following is a <SYSTEM_MESSAGE> not actually sent by the user. "
    "It is provided by the system as important information to pay attention to."
)


def _message_header(sender: str, first_line: str) -> str:
    return (
        f"[Message] timestamp=2026-09-24T10:10:57Z sender={sender} priority=MESSAGE_PRIORITY_HIGH content={first_line}"
    )


def _task_message(sender: str, body: str, *, step_index: int | None = None) -> str:
    return _event(
        "SYSTEM_MESSAGE",
        f"{_SYSTEM_MESSAGE_PREAMBLE}\n\n<SYSTEM_MESSAGE>\n{_message_header(sender, body)}\n</SYSTEM_MESSAGE>",
        step_index=step_index,
        source="SYSTEM",
    )


def _finish(task_id: str, *, step_index: int | None = None) -> str:
    return _task_message(task_id, f'Task id "{task_id}" finished with result:', step_index=step_index)


def _reply(content: str, *, step_index: int | None = None) -> str:
    return _event("PLANNER_RESPONSE", content, step_index=step_index, source="MODEL")


def _prompt() -> str:
    return _fixture_lines("background_task_finished_transcript.jsonl")[0]


def _stream_stdout(result: dict[str, object]) -> str:
    return "\n".join(
        [
            json.dumps({"event": "init", "conversation_id": result.get("conversation_id")}),
            json.dumps({"event": "step_update", "step_update": {"state": "DONE"}}),
            json.dumps({"event": "result", "result": result}),
        ]
    )


def _canceled(task_id: str) -> str:
    return _task_message(task_id, f'Task id "{task_id}" was canceled with result:\nTool execution was canceled')


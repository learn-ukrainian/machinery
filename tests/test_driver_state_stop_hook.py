"""Product PR 12940 Stop-hook regression, ported without changing its four cases."""

import json

import pytest

from lu_machinery import driver_state
from lu_machinery.ai_agent_bridge.monitor_client import MonitorClient

SYNTHETIC_WORKER_TARGET = 7


@pytest.fixture
def state(tmp_path, monkeypatch):
    path = tmp_path / ".claude" / "infra-epic" / "DRIVER-STATE.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "# Driver state: infra\n\n## Epic\n- Stream: infra\n\n"
        "## Current goals\n1. fix X — DONE WHEN: CI green\n\n## Next step\n- open PR\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(driver_state.STATE_ENV, str(path))
    monkeypatch.setenv("SESSION_EPIC", "infra")
    monkeypatch.setenv("SESSION_HANDOFF_AGENT", "gemini-infra")
    monkeypatch.delenv(driver_state.MIN_WORKERS_ENV, raising=False)
    return path



def _workers(root, seat, n, status="running"):
    tasks = root / "batch_state" / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (tasks / f"t{i}.json").write_text(
            json.dumps({"task_id": f"t{i}", "run_nonce": f"nonce-{i}", "initiator": seat, "status": status}),
            encoding="utf-8",
        )
    (tasks / "other.json").write_text(json.dumps({"initiator": "codex-devops", "status": "running"}), encoding="utf-8")



@pytest.fixture(autouse=True)
def live_monitor(monkeypatch):
    """Replace only the live Monitor transport; never contact another lane."""

    def get(_client, path, **_kwargs):
        task_id = path.split("/")[-1].split("?")[0]
        record = driver_state.tasks_dir() / f"{task_id}.json"
        if not record.is_file():
            return 404, "", {}
        task = json.loads(record.read_text(encoding="utf-8"))
        return 200, json.dumps({"task": task, "alive": True}), {}

    monkeypatch.setattr(MonitorClient, "get", get)



@pytest.fixture
def driver(state, tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_HANDOFF_AGENT", "gemini-infra")
    monkeypatch.setenv(driver_state.MIN_WORKERS_ENV, str(SYNTHETIC_WORKER_TARGET))
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path / "batch_state" / "tasks"))
    return tmp_path



_HUMAN_PROMPT = {"type": "user", "origin": {"kind": "human"}, "turnOrigin": "human", "promptSource": "typed"}



_NOTIFICATION_PROMPT = {
    "type": "user",
    "origin": {"kind": "task-notification", "producer": "session-task"},
    "turnOrigin": "task_notification",
    "promptSource": "system",
}



_TOOL_RESULT = {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}}



_STOP_FEEDBACK = {"type": "user", "isMeta": True, "message": {"role": "user", "content": "Stop hook feedback: ..."}}



@pytest.mark.parametrize(
    ("turn", "blocked"),
    [
        pytest.param([_HUMAN_PROMPT, _TOOL_RESULT, _STOP_FEEDBACK], False, id="operator-turn-allows-question"),
        pytest.param([_HUMAN_PROMPT, _NOTIFICATION_PROMPT, _TOOL_RESULT], True, id="notification-turn-blocks"),
        pytest.param(None, True, id="unreadable-transcript-blocks"),
        pytest.param([_HUMAN_PROMPT, "not json"], True, id="undecodable-row-blocks"),
    ],
)
def test_claude_stop_hook_blocks_operator_conversation_or_goes_silent_on_autonomous_turns(
    driver, monkeypatch, turn, blocked
):
    """Defect class: the Stop hook nags the operator's own turn, or falls silent on an autonomous one."""
    _workers(driver, "gemini-infra", SYNTHETIC_WORKER_TARGET)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(driver))
    transcript = driver / "claude-transcript.jsonl"
    if turn is not None:
        rows = [{**row, "message": {"role": "user", "content": "go"}} if "origin" in row else row for row in turn]
        rows.append({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "?"}]}})
        transcript.write_text(
            "".join(f"{r if isinstance(r, str) else json.dumps(r)}\n" for r in rows), encoding="utf-8"
        )
    payload = {
        "session_id": "s1",
        "transcript_path": str(transcript),
        "cwd": str(driver),
        "hook_event_name": "Stop",
        "stop_hook_active": False,
        "last_assistant_message": "Option A or option B for #10117. Which should I take?",
    }
    out = driver_state.cmd_claude_stop_hook(json.dumps(payload))
    if blocked:
        assert out["decision"] == "block"
        assert "ends on a question" in out["reason"]
    else:
        assert out == {}


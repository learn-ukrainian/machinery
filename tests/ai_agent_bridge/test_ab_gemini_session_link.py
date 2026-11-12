from __future__ import annotations

import os
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


from lu_machinery.ai_agent_bridge import _channels, _channels_cli, _db, _gemini_session_link, _inbox
from tests.helpers.product_tree import configured_roots  # noqa: F401

pytestmark = pytest.mark.usefixtures("configured_roots", "bridge_wake_dir")


@pytest.fixture(autouse=True)
def isolate_db(tmp_path):
    db_file = tmp_path / "messages.db"
    with patch.dict(os.environ, {"AB_DB_PATH": str(db_file)}):
        _db.init_db()
        yield


def _session_filename(start_time: datetime, suffix: str) -> str:
    safe = start_time.astimezone(UTC).strftime("%Y-%m-%dT%H-%M-%S.%fZ")
    return f"session-{safe}-{suffix}.json"


def _write_session(
    chats_root: Path,
    *,
    start_time: datetime,
    session_id: str,
    messages: list[dict[str, object]],
    project_name: str = "learn-ukrainian",
) -> Path:
    chats_dir = chats_root / project_name / "chats"
    chats_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "sessionId": session_id,
        "startTime": start_time.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "messages": messages,
    }
    path = chats_dir / _session_filename(start_time, session_id[-4:])
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _make_thread(agent: str, *, body: str = "please review this") -> dict[str, object]:
    if _channels.get_channel("topic") is None:
        _channels.create_channel("topic")
    return _channels.post(
        "topic",
        "user",
        body,
        to_agents=[agent],
        auto_snapshot=False,
    )


def _delivery_row(message_id: str) -> sqlite3.Row:
    conn = _db.get_db()
    try:
        row = conn.execute(
            "SELECT status, error, last_error_kind, to_model FROM deliveries WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        assert row is not None
        return row
    finally:
        conn.close()


def test_find_session_recovery_returns_last_non_empty_gemini_message(tmp_path):
    started_at = datetime.now(UTC)
    _write_session(
        tmp_path,
        start_time=started_at,
        session_id="session-final",
        messages=[
            {
                "type": "user",
                "content": [{"text": "please review this carefully"}],
            },
            {
                "type": "gemini",
                "model": "gemini-3.1-pro-preview",
                "content": [{"text": "draft"}],
            },
            {
                "type": "gemini",
                "model": "gemini-3.1-pro-preview",
                "content": [],
            },
            {
                "type": "gemini",
                "model": "gemini-3.1-pro-preview",
                "content": [{"text": "final answer"}],
            },
        ],
    )

    recovery = _gemini_session_link.find_session_recovery(
        delivery_brief="please review this carefully",
        started_at=started_at,
        chats_root=tmp_path,
    )

    assert recovery is not None
    assert recovery.session_id == "session-final"
    assert recovery.text == "final answer"


def test_find_session_recovery_returns_none_when_no_candidate_matches(tmp_path):
    started_at = datetime.now(UTC)
    _write_session(
        tmp_path,
        start_time=started_at + timedelta(minutes=5),
        session_id="session-far",
        messages=[
            {"type": "user", "content": [{"text": "unrelated"}]},
            {"type": "gemini", "content": [{"text": "answer"}]},
        ],
    )

    recovery = _gemini_session_link.find_session_recovery(
        delivery_brief="please review this carefully",
        started_at=started_at,
        chats_root=tmp_path,
    )

    assert recovery is None


def test_find_session_recovery_returns_none_when_last_gemini_message_is_empty(tmp_path):
    started_at = datetime.now(UTC)
    _write_session(
        tmp_path,
        start_time=started_at,
        session_id="session-empty",
        messages=[
            {"type": "user", "content": [{"text": "please review this carefully"}]},
            {"type": "gemini", "content": []},
        ],
    )

    recovery = _gemini_session_link.find_session_recovery(
        delivery_brief="please review this carefully",
        started_at=started_at,
        chats_root=tmp_path,
    )

    assert recovery is None


def test_find_session_recovery_prefers_brief_match_when_windows_overlap(tmp_path):
    started_at = datetime.now(UTC)
    brief = "Please review module #1235 and summarize the blockers clearly."
    _write_session(
        tmp_path,
        start_time=started_at + timedelta(seconds=1),
        session_id="session-wrong",
        messages=[
            {"type": "user", "content": [{"text": "Completely different task"}]},
            {"type": "gemini", "content": [{"text": "wrong reply"}]},
        ],
    )
    _write_session(
        tmp_path,
        start_time=started_at + timedelta(seconds=20),
        session_id="session-right",
        messages=[
            {"type": "user", "content": [{"text": f"Context\n\n{brief}\n\nReply once."}]},
            {"type": "gemini", "content": [{"text": "right reply"}]},
        ],
    )

    recovery = _gemini_session_link.find_session_recovery(
        delivery_brief=brief,
        started_at=started_at,
        chats_root=tmp_path,
    )

    assert recovery is not None
    assert recovery.session_id == "session-right"
    assert recovery.text == "right reply"


@pytest.mark.parametrize("agent", ["agy", "gemini", "gemini-infra"])
def test_run_inbox_refuses_agy_recipient_before_claiming_delivery(agent, monkeypatch, capsys):
    """Defect class: an AGY inbox delivery expired, claimed or terminal-failed before the read-only refusal (#100).

    A Gemini role slot (``gemini-infra``) is no runtime agent, so it refuses the same way;
    ``ab sync --all`` drains no AGY inbox, so it never reports that refusal.
    """
    thread = _make_thread(agent, body="please review this carefully")
    conn = _db.get_db()
    try:
        # Past the channel TTL, so any sweep before the refusal would expire it.
        conn.execute(
            "UPDATE channel_messages SET created_at = ? WHERE message_id = ?",
            ((datetime.now(UTC) - timedelta(hours=48)).isoformat(), str(thread["message_id"])),
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("FLEET_COMMS_MESSAGE_PLANE", "shadow")
    args = SimpleNamespace(
        inbox_command="run", agent=agent, once=True, until_idle=False,
        max_messages=None, stop_after_seconds=None, deadline=None, auth=None,
    )

    with (
        patch("lu_machinery.ai_agent_bridge._inbox.runtime_invoke") as mock_invoke,
        patch("lu_machinery.ai_agent_bridge._inbox._claim_next_thread") as mock_claim,
    ):
        assert _channels_cli._dispatch_inbox_command(args) == 1
        with pytest.raises(ValueError, match="agy_inbox_unsupported"):
            _inbox.run_inbox(agent)

    mock_claim.assert_not_called()
    mock_invoke.assert_not_called()
    row = _delivery_row(str(thread["message_id"]))
    assert row["status"] == "pending"
    assert row["last_error_kind"] is None

    capsys.readouterr()
    with patch("lu_machinery.ai_agent_bridge._channels_cli._handle_inbox_run", return_value=0) as drain:
        assert _channels_cli._handle_sync(SimpleNamespace(all=True, agent=None, auth=None)) == 0
    drained = {call.args[0].agent for call in drain.call_args_list}
    assert drained and not drained & {"agy", "gemini", "gemini-infra"}
    assert "agy_inbox_unsupported" not in capsys.readouterr().err

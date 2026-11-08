"""Bridge runtime files belong to shared state, not the reviewed product (#100 R28)."""

import subprocess
import sys
from types import SimpleNamespace

import pytest

from lu_machinery.ai_agent_bridge import (
    _ask_lifecycle,
    _broker,
    _claude,
    _config,
    _gemini,
    _reply_sidecar,
)


@pytest.fixture
def roots(tmp_path, monkeypatch):
    product = tmp_path / "product"
    state = tmp_path / "state"
    product.mkdir()
    state.mkdir()
    subprocess.run(["git", "init", "-q", str(state)], check=True)
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(product))
    monkeypatch.setenv("LEARN_UKRAINIAN_PRIMARY_REPO_ROOT", str(state))
    for name in ("LEARN_UKRAINIAN_STATE_ROOT", "AB_REPO_ROOT", "AB_PID_DIR", "FLEET_COMMS_ROOT"):
        monkeypatch.delenv(name, raising=False)
    return product, state


def test_reply_and_ask_files_are_written_only_to_state(roots, monkeypatch):
    product, state = roots
    result = _reply_sidecar.maybe_sidecare_response(
        "complete reply\n" * 1000, task_id="root-proof", from_llm="test", msg_type="response",
    )
    path = state / "batch_state/asks/root-proof"
    assert len(list(path.glob("reply-test-*.md"))) == 1
    assert next(path.glob("reply-test-*.md")).read_text() == "complete reply\n" * 1000
    assert "batch_state/asks/root-proof/" in result.content
    assert _ask_lifecycle._ask_state_dir(42) == state / "batch_state/asks/message-42"
    assert _ask_lifecycle._ask_log_path(42) == state / ".mcp/servers/message-broker/logs/ask-42.log"
    # Exercise the fallback as well as the real fleet path resolver.
    subprocess.run(["git", "init", "-q", str(state)], check=True)
    assert _ask_lifecycle._plane_root() == state / "batch_state/fleet-comms/v1"
    monkeypatch.setattr(_ask_lifecycle, "_import_message_plane", lambda: None)
    assert _ask_lifecycle._plane_root() == state / "batch_state/fleet-comms/v1"
    assert list(product.rglob("*")) == []
    subprocess.run(["git", "init", "-q", str(product)], check=True)
    monkeypatch.setenv("LEARN_UKRAINIAN_PRIMARY_REPO_ROOT", str(product))
    assert _ask_lifecycle._ask_log_path(42) == product / ".mcp/servers/message-broker/logs/ask-42.log"
    assert _ask_lifecycle._plane_root() == product / "batch_state/fleet-comms/v1"


@pytest.mark.parametrize("kind", ["ask", "claude", "gemini"])
def test_background_logs_use_state_but_child_cwd_stays_product(roots, monkeypatch, kind):
    product, state = roots
    captured = []

    def popen(command, **kwargs):
        captured.append((command, kwargs))
        kwargs["stdout"].write("child log\n")
        kwargs["stdout"].flush()
        return SimpleNamespace(pid=12345)

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setenv("LEARN_UKRAINIAN_PYTHON", sys.executable)
    monkeypatch.setattr(_config, "_PARENT_ENV", {})
    for module in (_claude, _gemini):
        monkeypatch.setattr(module, "_is_task_locked", lambda *args: False)
    monkeypatch.setattr(_ask_lifecycle, "_load_ask_row", lambda message_id: None)
    monkeypatch.setattr(_ask_lifecycle, "_confirm_worker_started", lambda *args: True)
    if kind == "ask":
        _ask_lifecycle.launch_background_ask(42, "claude", {})
        log_name = "ask-42.log"
    elif kind == "claude":
        _claude._launch_claude_background({"task_id": "root-proof"}, 42, False)
        log_name = "claude-root-proof.log"
    else:
        _gemini._launch_gemini_background({"task_id": "root-proof"}, 42, "test-model", "test", auth_mode=None)
        log_name = "gemini-root-proof.log"
    assert len(captured) == 1
    assert captured[0][1]["cwd"] == str(product)
    log = state / ".mcp/servers/message-broker/logs" / log_name
    assert log.read_text() == "child log\n"
    assert list(product.rglob("*")) == []


def test_broker_reads_log_tail_from_state(roots, capsys):
    product, state = roots
    for root, text in ((state, "state-log"), (product, "product-decoy")):
        log = root / ".mcp/servers/message-broker/logs/claude-root-proof.log"
        log.parent.mkdir(parents=True)
        log.write_text(text)
    _broker._print_process_info("test", {"agent": "claude", "task_id": "root-proof"})
    output = capsys.readouterr().out
    assert "Last output: state-log" in output
    assert "product-decoy" not in output

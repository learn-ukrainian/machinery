"""Bridge runtime files belong to shared state, not the reviewed product (#100 R28)."""

import subprocess
import sys
from pathlib import Path
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
from lu_machinery.common.repo_root import StateRootError


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
    assert Path(result.sidecar_path).is_absolute()
    assert Path(result.sidecar_path).read_text() == "complete reply\n" * 1000
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
    same_root = _reply_sidecar.maybe_sidecare_response(
        "complete reply\n" * 1000, task_id="same-root", from_llm="test", msg_type="response",
    )
    assert not Path(same_root.sidecar_path).is_absolute()
    assert (product / same_root.sidecar_path).is_file()


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
    assert captured[0][0][1:3] == ["-m", "lu_machinery.ai_agent_bridge"]
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


def test_consumer_import_order_preserves_package_identity(tmp_path):
    """A consumer's runtime must not shadow machinery or be shadowed by the bridge."""
    consumer = tmp_path / "consumer"
    runtime = consumer / "agent_runtime"
    common = consumer / "common"
    (runtime / "adapters").mkdir(parents=True)
    common.mkdir()
    for name in ("__init__.py", "adapters/__init__.py"):
        (runtime / name).write_text("")
    (runtime / "redact.py").write_text("CONSUMER = True\n")
    (common / "__init__.py").write_text("")
    (common / "repo_root.py").write_text(
        "POISON = True\n"
        "def project_interpreter():\n"
        "    raise RuntimeError('consumer common')\n"
    )
    probe = """
import importlib
import sys
sys.path[:0] = sys.argv[1:3]
before = sys.path.copy()
modules = ("agent_runtime.redact", "lu_machinery.ai_agent_bridge")
if sys.argv[3] == "bridge-first":
    modules = modules[::-1]
for name in modules:
    importlib.import_module(name)
    if name == "lu_machinery.ai_agent_bridge":
        importlib.import_module("lu_machinery.agent_runtime.acpx_discuss")
        importlib.import_module("lu_machinery.agent_runtime.lane_probe")
        importlib.import_module("lu_machinery.agent_runtime.trail_isolation")
from lu_machinery.ai_agent_bridge import _claude, _gemini
from lu_machinery.agent_runtime import runner
from lu_machinery.agent_runtime.adapters.claude import ClaudeAdapter
from lu_machinery.agent_runtime.errors import AgentTimeoutError
assert _claude.runtime_invoke is runner.invoke
assert _gemini.runtime_invoke is runner.invoke
assert _claude.AgentTimeoutError is AgentTimeoutError
assert importlib.import_module("agent_runtime.redact").CONSUMER is True
assert "common.repo_root" not in sys.modules
assert sys.path == before
# A failed package import must not bind the consumer's common.repo_root.
sys.modules.pop("lu_machinery.agent_runtime.trail_isolation", None)
sys.modules.pop("lu_machinery.common.repo_root", None)

class _BlockRepoRoot:
    def find_spec(self, fullname, path, target=None):
        if fullname == "lu_machinery.common.repo_root":
            raise ModuleNotFoundError(fullname, name=fullname)
        return None

sys.meta_path.insert(0, _BlockRepoRoot())
try:
    importlib.import_module("lu_machinery.agent_runtime.trail_isolation")
except ModuleNotFoundError as exc:
    if exc.name != "lu_machinery.common.repo_root":
        raise
else:
    raise SystemExit("trail_isolation fell back to a bare common import")
poison = sys.modules.get("common.repo_root")
if poison is not None and getattr(poison, "POISON", False):
    raise SystemExit("consumer common.repo_root was bound")
print("PASS", sys.argv[3])
"""
    failures = []
    for order in ("consumer-first", "bridge-first"):
        result = subprocess.run(
            [sys.executable, "-I", "-c", probe, str(consumer), str(Path(__file__).resolve().parents[2]), order],
            cwd=tmp_path, capture_output=True, text=True, check=False,
        )
        if result.returncode != 0 or result.stdout.strip() != f"PASS {order}":
            failures.append(f"{order}: {result.stdout}{result.stderr}")
    assert not failures, "\n".join(failures)


def test_exit_cleanup_removes_written_pid_file_without_state_root(tmp_path, monkeypatch):
    """Exit cleanup must not re-resolve a state root that is gone at interpreter exit."""
    pid_dir = tmp_path / "pids"
    monkeypatch.setenv("AB_PID_DIR", str(pid_dir))
    _broker._write_pid_file("claude", "exit-task", {"task": "exit-task"})
    pid_file = pid_dir / "claude-exit-task.json"
    assert pid_file.exists()

    for name in (
        "AB_PID_DIR",
        "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT",
        "LEARN_UKRAINIAN_STATE_ROOT",
        "AB_REPO_ROOT",
        "LU_PRODUCT_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(StateRootError):
        _config.PID_DIR  # noqa: B018 - resolution is the point: no anchor remains

    _broker._remove_pid_file("claude", "exit-task")

    assert not pid_file.exists()


def test_exit_cleanup_of_a_pid_file_never_written_uses_the_directory_fixed_at_registration(tmp_path, monkeypatch):
    """Defect class: the atexit hook resolved the state root at interpreter exit, where it is gone, and raised
    StateRootError ("Exception ignored in atexit callback") for a PID file this process never wrote (#100 R93-c)."""
    pid_dir = tmp_path / "pids"
    pid_dir.mkdir()
    pid_file = pid_dir / "gemini-unwritten.json"
    pid_file.write_text("{}")
    for name in ("AB_PID_DIR", "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT", "LEARN_UKRAINIAN_STATE_ROOT", "AB_REPO_ROOT",
                 "LU_PRODUCT_ROOT"):
        monkeypatch.delenv(name, raising=False)
    _broker._remove_pid_file("gemini", "unwritten", pid_dir)
    assert not pid_file.exists()

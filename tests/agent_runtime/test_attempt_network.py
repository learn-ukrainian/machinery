"""Real Unix endpoint protocol tests and bounded fault injection for #9251."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import socket
import sys
import tempfile
import threading
import time
from dataclasses import replace

import pytest

from lu_machinery.agent_runtime.attempt_network import (
    AttemptEgress,
    Limits,
    canonical_host,
    connect_target,
    host_addresses,
    validate_answers,
)
from lu_machinery.common.repo_root import machinery_path
from tests.agent_runtime.product_roots import agent_runtime_roots, configured_roots  # noqa: F401
from lu_machinery.dispatcher import cli, git, store, worker
from lu_machinery.dispatcher.types import TaskSpec
from lu_machinery.agent_runtime import runner
from lu_machinery.agent_runtime.adapters.base import InvocationPlan
from lu_machinery.agent_runtime.result import ParseResult
from tests.agent_runtime.attempt_boundary_support import attempt_config, manifest_world, world  # noqa: F401

# Liveness guard (#9549): the longest a test waits on a child process, socket or
# event-loop call before declaring it hung. It is never a performance
# assertion, so it is sized for a loaded CI runner (the production resolver
# child resolves in ~30 ms locally) rather than for a fast host. The fixture's
# 0.2 s proxy limits and `connect_seconds=.1` are deliberate fault-injection
# timings and stay short.
LIVENESS_SECONDS = 30


def request(proxy, data):
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(LIVENESS_SECONDS)
        client.connect(str(proxy.endpoint))
        client.sendall(data)
        return client.recv(16384)


@pytest.fixture
def proxy():
    from pathlib import Path

    # AF_UNIX has a small path limit independent of pytest's temp base.
    with tempfile.TemporaryDirectory(prefix="eg-", dir="/tmp") as directory:
        result = AttemptEgress(
            Path(directory) / "egress.sock",
            limits=replace(Limits(), header_seconds=0.2, resolve_seconds=0.2, idle_seconds=0.2),
        )
        try:
            yield result
        finally:
            result.cleanup()
            assert not result.thread.is_alive()


@pytest.mark.parametrize("target", [
    "api.provider.example:80",
    "127.0.0.1:443", "[::1]:443", "user@api.provider.example:443", "API.provider.example:443",
    "api.provider.example.:443", "api.provider.example:0443", "api.provider.example:443/path",
    "api.provider.example:443:443", "api.provider.example:443?x", "api.provider.example:443#x",
])
def test_real_endpoint_refuses_authority_lookalikes(proxy, target):
    assert request(proxy, f"CONNECT {target} HTTP/1.1\r\n\r\n".encode()).startswith(b"HTTP/1.1 403")


@pytest.mark.parametrize("header", [
    b"GET https://api.provider.example/ HTTP/1.1\r\n\r\n",
    b"POST / HTTP/1.1\r\n\r\n",
    b"CONNECT api.provider.example:443 HTTP/1.1\r\nHost: other.example:443\r\n\r\n",
    b"CONNECT api.provider.example:443 HTTP/1.1\r\nHost: api.provider.example:443\r\nHost: api.provider.example:443\r\n\r\n",
    b"CONNECT api.provider.example:443 HTTP/1.1\r\n bad: header\r\n\r\n",
    b"CONNECT api.provider.example:443 HTTP/1.1\r\nContent-Length: 1\r\n\r\nx",
    b"CONNECT api.provider.example:443 HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n",
    b"CONNECT api.provider.example:443 HTTP/2\r\n\r\n",
    b"CONNECT api.provider.example:443 HTTP/1.1\r\nx: \xff\r\n\r\n",
])
def test_real_endpoint_refuses_methods_and_conflicting_headers(proxy, header):
    assert request(proxy, header).startswith(b"HTTP/1.1 403")


@pytest.mark.parametrize("answer", [
    "127.0.0.1", "::1", "::ffff:127.0.0.1", "fc00::1", "fe80::1", "ff02::1", "0.0.0.0", "::",
    # Synthetic address-class fixtures, never literal host addresses.
    pytest.param(str(ipaddress.IPv4Address((10 << 24) | 1)), id="rfc1918"),
    pytest.param(str(ipaddress.IPv4Address((100 << 24) | (64 << 16) | 1)), id="cgnat"),
    pytest.param(str(ipaddress.IPv4Address((169 << 24) | (254 << 16) | (1 << 8) | 1)), id="link-local"),
    pytest.param(str(ipaddress.IPv4Address((224 << 24) | 1)), id="multicast"),
    pytest.param(str(ipaddress.IPv4Address((240 << 24) | 1)), id="reserved"),
])
@pytest.mark.parametrize("mixed", [False, True])
def test_real_endpoint_denies_any_bad_dns_answer(proxy, monkeypatch, answer, mixed):
    async def resolve(host):
        return ["8.8.8.8", answer] if mixed else [answer]

    monkeypatch.setattr(proxy, "_resolve", resolve)
    assert request(proxy, b"CONNECT api.provider.example:443 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")


def test_host_addresses_and_mapped_global_are_denied(proxy, monkeypatch):
    assert host_addresses()
    local = ipaddress.ip_address("8.8.8.8")
    proxy.local_addresses = frozenset({local})
    for answer in (str(local), "::ffff:8.8.8.8"):
        async def resolve(host, answer=answer):
            return [answer]
        monkeypatch.setattr(proxy, "_resolve", resolve)
        assert request(proxy, b"CONNECT api.provider.example:443 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")


def test_numeric_connect_prevents_rebinding(proxy, monkeypatch):
    calls = []

    async def resolve(host):
        calls.append(host)
        return ["8.8.8.8"] if len(calls) == 1 else ["127.0.0.1"]

    async def numeric(sock, address):
        calls.append(address)
        raise OSError("fixture connection refused")

    monkeypatch.setattr(proxy, "_resolve", resolve)
    monkeypatch.setattr(proxy.loop, "sock_connect", numeric)
    assert request(proxy, b"CONNECT api.provider.example:443 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")
    assert calls == ["api.provider.example", ("8.8.8.8", 443)]


def test_success_is_byte_only_and_logs_no_payload(proxy, monkeypatch):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    stopped = threading.Event()

    def echo():
        with listener.accept()[0] as connection:
            data = connection.recv(1024)
            connection.sendall(data)
            stopped.wait(LIVENESS_SECONDS)

    thread = threading.Thread(target=echo)
    thread.start()

    async def resolve(host):
        return ["8.8.8.8"]

    async def connect(addresses):
        assert addresses == [ipaddress.ip_address("8.8.8.8")]
        return await asyncio.open_connection(*listener.getsockname())

    monkeypatch.setattr(proxy, "_resolve", resolve)
    monkeypatch.setattr(proxy, "_connect", connect)
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(LIVENESS_SECONDS)
            client.connect(str(proxy.endpoint))
            client.sendall(b"CONNECT api.provider.example:443 HTTP/1.1\r\nHost: api.provider.example:443\r\n\r\n")
            assert client.recv(1024).startswith(b"HTTP/1.1 200")
            client.sendall(b"PRIVATE_PAYLOAD")
            assert client.recv(1024) == b"PRIVATE_PAYLOAD"
        deadline = time.monotonic() + LIVENESS_SECONDS
        while proxy.records[0]["bytes"] < 30 and time.monotonic() < deadline:
            time.sleep(.01)
        assert proxy.records == [{"destination": "api.provider.example:443", "bytes": 30}]
        assert proxy.overflow_count == 0
    finally:
        stopped.set()
        listener.close()
        thread.join(LIVENESS_SECONDS)


def test_resolver_hang_and_cancellation_reap_child(proxy, monkeypatch):
    """Defect class: shutdown repeats cancellation while a resolver child is being reaped."""
    children = []
    exit_ready = threading.Event()
    exit_notification = []
    real_spawn = asyncio.create_subprocess_exec

    async def hung(*args, **kwargs):
        # Keep the real killable subprocess path; simulate a libc DNS hang.
        proc = await real_spawn(args[0], "-c", "import time; time.sleep(60)", **kwargs)
        children.append(proc)
        if len(children) == 2:
            real_exit = proc._transport._process_exited

            def hold_exit(returncode):
                exit_notification.append((real_exit, returncode))
                exit_ready.set()

            monkeypatch.setattr(proc._transport, "_process_exited", hold_exit)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", hung)
    assert request(proxy, b"CONNECT api.provider.example:443 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")
    assert children and children[0].returncode is not None
    client = socket.socket(socket.AF_UNIX)
    try:
        client.connect(str(proxy.endpoint))
        client.sendall(b"CONNECT api.provider.example:443 HTTP/1.1\r\n\r\n")
        deadline = time.monotonic() + LIVENESS_SECONDS
        while len(children) < 2 and time.monotonic() < deadline:
            time.sleep(.01)
        assert exit_ready.wait(LIVENESS_SECONDS)

        def release_exit(turns):
            if turns:
                proxy.loop.call_soon(release_exit, turns - 1)
            else:
                real_exit, returncode = exit_notification[0]
                real_exit(returncode)

        real_stop = proxy.loop.stop
        release_started = False

        def stop_with_exit_pending():
            nonlocal release_started
            real_stop()
            if not release_started:
                release_started = True
                proxy.loop.call_soon(release_exit, 10)

        monkeypatch.setattr(proxy.loop, "stop", stop_with_exit_pending)
        proxy.cleanup()
        assert len(children) == 2 and all(p.returncode is not None for p in children)
    finally:
        client.close()


def test_slow_client_large_header_and_flood_are_bounded(proxy):
    assert request(proxy, b"CONNECT api.provider.example:443 HTTP/1.1\r\nx: " + b"x" * 9000).startswith(b"HTTP/1.1 403")
    clients = []
    try:
        for _ in range(proxy.limits.connections + 5):
            client = socket.socket(socket.AF_UNIX)
            client.settimeout(LIVENESS_SECONDS)
            client.connect(str(proxy.endpoint))
            clients.append(client)
        assert len(proxy.tasks) <= proxy.limits.connections
        for client in clients:
            with contextlib.suppress(ConnectionResetError):
                data = client.recv(1024)
                assert data == b"" or data.startswith(b"HTTP/1.1 403")
    finally:
        for client in clients:
            client.close()


def test_config_and_validation_contract():
    # Any public hostname is a destination (operator decision 2026-11-10); addresses are checked after DNS.
    for host in ("api.github.com", "www.example.org", "api.provider.example"):
        assert connect_target(f"CONNECT {host}:443 HTTP/1.1\r\n\r\n".encode()) == host
    assert not canonical_host("1.2.3.4")
    assert not canonical_host("provider.example.")
    assert not canonical_host("a" * 64 + ".example")
    with pytest.raises(ValueError):
        validate_answers([], frozenset())


def test_real_resolver_is_parent_only_and_numeric(proxy):
    # No external lookup: libc's local hosts entry is sufficient to exercise
    # the production child; the subsequent address contract must refuse it.
    # The fixture's 0.2 s resolve limit is for the hung-resolver fault tests; a
    # real interpreter child must be allowed to start on a loaded runner.
    proxy.limits = replace(proxy.limits, resolve_seconds=LIVENESS_SECONDS)
    future = asyncio.run_coroutine_threadsafe(proxy._resolve("localhost"), proxy.loop)
    answers = future.result(timeout=LIVENESS_SECONDS)
    assert answers and all(ipaddress.ip_address(a).is_loopback for a in answers)


def test_proxy_start_failure_and_forwarder_help(tmp_path, monkeypatch):
    import subprocess
    import sys

    from lu_machinery.review.isolation import ReviewIsolationError

    bad = tmp_path / "missing" / "egress.sock"
    with pytest.raises(ReviewIsolationError, match="start_failed"):
        AttemptEgress(bad)
    forwarder = machinery_path("agent_runtime/attempt_forwarder.py")
    help_run = subprocess.run([sys.executable, str(forwarder), "--help"], capture_output=True, text=True, timeout=LIVENESS_SECONDS)
    assert help_run.returncode == 0
    assert "Exit codes:" in help_run.stdout and "Outputs:" in help_run.stdout


def test_projected_forwarder_run_replaces_environment_and_preserves_exit(proxy, monkeypatch, tmp_path):
    import subprocess
    import sys

    from lu_machinery.agent_runtime.attempt_forwarder import run

    # The child opens the local TCP relay and exercises denial through the
    # actual parent Unix endpoint. No provider request is made here.
    script = (
        "import os,socket,urllib.parse\n"
        "p=urllib.parse.urlsplit(os.environ['HTTPS_PROXY'])\n"
        "assert os.environ['NO_PROXY']==os.environ['no_proxy']==''\n"
        "assert all(os.environ[k]==os.environ['HTTPS_PROXY'] for k in "
        "['https_proxy','HTTP_PROXY','http_proxy','ALL_PROXY','all_proxy'])\n"
        f"with socket.create_connection((p.hostname,p.port),timeout={LIVENESS_SECONDS}) as s:\n"
        " s.sendall(b'CONNECT not-allowed.example:443 HTTP/1.1\\r\\n\\r\\n')\n"
        " assert s.recv(1024).startswith(b'HTTP/1.1 403')\n"
        "raise SystemExit(7)\n"
    )
    monkeypatch.setenv("HTTPS_PROXY", "http://bad-proxy.example")
    monkeypatch.setenv("NO_PROXY", "*")
    assert asyncio.run(run(str(proxy.endpoint), [sys.executable, "-c", script])) == 7
    missing = tmp_path / "missing.sock"
    with pytest.raises(OSError):
        asyncio.run(run(str(missing), [sys.executable, "-c", "raise SystemExit(99)"]))
    # Internal launcher main must refuse before child startup on missing proxy.
    module = __import__("lu_machinery.agent_runtime.attempt_forwarder", fromlist=["main"])
    result = subprocess.run([sys.executable, module.__file__, str(missing), "/bin/true"],
                            capture_output=True, text=True, timeout=LIVENESS_SECONDS)
    assert result.returncode == 125 and "attempt_forwarder_start_failed" in result.stderr


def test_dns_oversized_response_is_bounded_and_reaped(proxy, monkeypatch):
    real_spawn = asyncio.create_subprocess_exec
    children = []

    async def large(*args, **kwargs):
        proc = await real_spawn(args[0], "-c", "import sys,time; sys.stdout.write('x'*100000); "
                                "sys.stdout.flush(); time.sleep(60)", **kwargs)
        children.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", large)
    assert request(proxy, b"CONNECT api.provider.example:443 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")
    assert children and children[0].returncode is not None


def test_forwarder_cli_success_refusal_and_argument_contract(proxy, tmp_path, monkeypatch):
    import sys

    from lu_machinery.agent_runtime import attempt_forwarder

    monkeypatch.setattr(sys, "argv", [attempt_forwarder.__file__, str(proxy.endpoint), "/bin/true"])
    assert attempt_forwarder.main() == 0
    monkeypatch.setattr(sys, "argv", [attempt_forwarder.__file__, str(tmp_path / "missing.sock"), "/bin/true"])
    assert attempt_forwarder.main() == 125
    monkeypatch.setattr(sys, "argv", [attempt_forwarder.__file__, str(proxy.endpoint)])
    with pytest.raises(SystemExit) as error:
        attempt_forwarder.main()
    assert error.value.code == 2


# Ported from learn-ukrainian PR 12953; exercise the persisted task record.


@pytest.fixture(autouse=True)
def egress_test_roots(agent_runtime_roots, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LEARN_UKRAINIAN_PYTHON", sys.executable)


@pytest.fixture
def attempt(tmp_path, monkeypatch):
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path / "tasks"))
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("inputs: []\n")
    return ["--review-id", "review", "--attempt-id", "attempt", "--review-attempt", str(manifest)]


@pytest.mark.parametrize("path_status", ["done", "failed", "timeout", "cancelled", "overcap"])
@pytest.mark.parametrize("harness", ["codex", "claude", "agy"])
def test_bound_attempt_runs_only_inside_the_attempt_boundary_with_the_exact_prompt(
    attempt, tmp_path, monkeypatch, path_status, harness,
):
    """A bound attempt is claimed, prepared and run by the runtime's boundary, and its record binds the result.

    Defect class: a bound review loses egress evidence on success, provider failure, timeout or cancellation (#12947).
    """
    import os
    import socket
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace

    from lu_machinery.agent_runtime import attempt_boundary, review_mcp, runner
    from lu_machinery.agent_runtime.attempt_network import AttemptEgress

    from lu_machinery.agent_runtime.errors import AgentTimeoutError, AgentUnavailableError
    from lu_machinery.dispatcher import headroom
    from lu_machinery.review.render_contract import trusted_core_context

    destination_limit = 256
    args = cli.build_parser().parse_args(["dispatch", "--agent", "sol", "--model", "gpt-6.1-sol", *attempt,
                                          "--review-access", "full"])
    binding = store.review_binding(vars(args))
    prefix, dependency = trusted_core_context()
    rendered = (prefix + "Rendered review prompt\nattempt: review/attempt\n").encode()
    prompt = cli._PromptPath(tmp_path / "tasks" / "task.prompt")
    prompt.content = rendered
    workspace = tmp_path / "checkout"
    workspace.mkdir()
    spec = TaskSpec("task", harness, "gpt-6.1-sol", "high", "read-only", workspace, prompt,
                    tmp_path / "tasks" / "task.result", options=dict(review_attempt=str(tmp_path / "manifest.yaml")))
    record = store.new_record(spec, dict(key="public", github="example/public"), "codex/task", "a" * 40,
                              dict(vars(args), research_owned_path=[]), binding=binding)
    record["review_contract"] = dict(input_root=str(tmp_path), machinery_dependencies={"rules_core": dependency},
                                     prompt_sha256=store.digest(rendered))
    path = store.create(record, rendered)

    for name in ("check_plan", "check_runtime"):
        monkeypatch.setattr(headroom, name, lambda *_a, **_k: None)
    monkeypatch.setattr(worker, "_reap_settled", lambda *_a, **_k: None)
    monkeypatch.setattr(worker, "settle_left_worktrees", lambda *_a, **_k: None)
    monkeypatch.setattr(git, "snapshot_delivery", lambda *_a: {})
    trees = []
    monkeypatch.setattr(attempt_boundary, "verify_full_review_tree", lambda *a: trees.append(a))
    receipts = tmp_path / "review-receipts" / "review"

    def prepare(review_id, attempt_id, manifest, harness, **kwargs):
        # The supervisor has claimed the attempt before preparing it on that claim.
        assert (receipts / "attempt.jsonl").read_bytes() == b"" and kwargs["claimed"] is True
        assert kwargs["receipts_root"] == tmp_path / "review-receipts" and kwargs["review_access"] == "full"
        config = receipts / "attempt.mcp.json"
        config.write_text("{}")
        (receipts / "attempt.jsonl.sha256").write_text(store.digest(b"") + "\n")  # as preparation writes it
        return review_mcp.ReviewMcpPlan(config, {}, receipts / "attempt.jsonl", receipts / "attempt.jsonl.sha256",
                                        binding["manifest_sha256"], review_id, attempt_id, harness)

    monkeypatch.setattr(review_mcp, "prepare_review_attempt", prepare)
    calls = []

    boundaries = []

    def prepare_boundary(agent, mode, session_id, config):
        # Real proxy + real cleanup; only the provider and OS launch are planted.
        boundary = object.__new__(attempt_boundary.AttemptBoundary)
        boundary.temp = tempfile.TemporaryDirectory(prefix="eg-", dir=tmp_path)
        boundary.workspace = workspace
        boundary.connection = None
        boundary.egress_evidence = {}
        # AF_UNIX paths must stay below the kernel's small length limit.
        boundary.egress = AttemptEgress(Path(boundary.temp.name) / "egress.sock")
        boundary.tool_config = config
        boundaries.append(boundary)
        return boundary

    def invoke_runtime(agent, text, **kwargs):
        calls.append((agent, text, kwargs))
        proxy = boundaries[-1].egress

        async def refuse_dns(host):
            raise ValueError("planted DNS failure")

        monkeypatch.setattr(proxy, "_resolve", refuse_dns)
        hosts = ["audit.example"]
        if path_status == "overcap":
            hosts = [f"audit-{index}.example" for index in range(destination_limit + 3)]
            hosts += ["audit-0.example", f"audit-{destination_limit}.example"]
        for host in hosts:
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(5)
                directory = os.open(proxy.endpoint.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    client.connect(f"/proc/self/fd/{directory}/{proxy.endpoint.name}")
                finally:
                    os.close(directory)
                client.sendall(f"CONNECT {host}:443 HTTP/1.1\r\n\r\n".encode())
                assert client.recv(1024).startswith(b"HTTP/1.1 403")
        if path_status == "failed":
            raise AgentUnavailableError("planted provider failure")
        if path_status == "timeout":
            raise AgentTimeoutError(agent, 60)
        if path_status == "cancelled":
            import signal

            os.kill(os.getpid(), signal.SIGTERM)
            raise AssertionError("supervisor cancellation must unwind")
        return SimpleNamespace(ok=True, response="review: done\n", identity_state="unknown", observed_model=None,
                               observed_model_source=None, failure_code=None, stderr_excerpt=None)

    monkeypatch.setattr(attempt_boundary, "prepare_attempt_boundary", prepare_boundary)
    monkeypatch.setattr(runner, "prepare_trail_isolation", lambda **kwargs: None)
    monkeypatch.setattr(runner, "_invoke_impl", invoke_runtime)
    worker.supervise(path, {}, 60)

    assert trees == [(tmp_path / "manifest.yaml", workspace)], store.read("task").get("error")
    [(agent, text, kwargs)] = calls
    assert (agent, text) == (harness, rendered.decode())
    assert kwargs["rules_core_delivery"] is runner.RulesCoreDelivery.BOUND
    config = kwargs["tool_config"]
    assert {key: config[key] for key in ("review_id", "attempt_id", "review_access", "review_cwd")} == dict(
        review_id="review", attempt_id="attempt", review_access="full", review_cwd=str(workspace))
    assert config["strict_mcp_config"] is True and config["mcp_server_names"] == ["sources"]
    assert (kwargs["mode"], kwargs["cwd"], kwargs["allow_runner_failover"]) == ("read-only", workspace, False)
    done = store.read("task")
    assert done["status"] == ("done" if path_status == "overcap" else path_status), done.get("error")
    expected_records = [{"destination": "audit.example:443", "bytes": 0}]
    if path_status == "overcap":
        expected_records = [{"destination": f"audit-{index}.example:443", "bytes": 0}
                            for index in range(destination_limit)]
    assert done["review_attempt_egress"] == {
        "records": expected_records, "overflow_count": 4 if path_status == "overcap" else 0,
    }
    assert not boundaries[0].egress.thread.is_alive()
    assert not Path(boundaries[0].temp.name).exists()
    print(json.dumps({"task_record": done}, sort_keys=True))
    if path_status in {"done", "overcap"}:
        assert done["resolved_model_known"] is False
        assert spec.final_file.read_bytes() == b"review: done\n"
        assert done["result_sha256"] == store.digest(b"review: done\n")


@pytest.mark.parametrize("agent", ["agy", "codex"])
def test_runner_executes_attempt_boundary_and_cleans_after_parse(world, tmp_path, monkeypatch, agent):  # noqa: F811
    root, home = world
    forbidden = home / "previous-session.txt"
    forbidden.write_text("Forbidden")
    tc = attempt_config(root, tmp_path, manifest_world(root, "plan"), agent)
    observed = []

    class Adapter:
        default_model = "fixture-model"
        supported_modes = frozenset({"read-only"})

        def build_invocation(self, **kwargs):
            workspace = kwargs["cwd"]
            boundary = kwargs["tool_config"]["review_attempt_boundary"]
            observed.append(boundary)
            script = (
                "import pathlib,sys\n"
                f"assert not pathlib.Path({str(forbidden)!r}).exists()\n"
                "assert pathlib.Path('curriculum/l2-uk-en/lesson-plans/a1/sample.yaml').read_bytes()\n"
                "print(sys.stdin.read(),flush=True)\n"
            )
            return InvocationPlan(cmd=[sys.executable, "-c", script], cwd=workspace, stdin_payload="own return")

        def parse_response(self, **kwargs):
            assert observed[0].workspace.exists()  # extraction happens before cleanup
            return ParseResult(ok=kwargs["returncode"] == 0, response=kwargs["stdout"].strip())

        def liveness_signal_paths(self, plan):
            return ()

    monkeypatch.setattr(runner, "_load_adapter", lambda *args, **kwargs: Adapter())
    monkeypatch.setattr(runner, "has_headroom", lambda *args, **kwargs: (True, "fixture"))
    monkeypatch.setattr(runner, "write_record", lambda record: None)
    evidence = {}
    result = runner.invoke(agent, "probe", cwd=root, tool_config=tc, model="fixture-model", hard_timeout=30,
                           attempt_evidence=evidence)
    assert evidence == {"records": [], "overflow_count": 0}
    assert result.ok and result.response == "own return"
    assert observed and not observed[0].workspace.exists()


@pytest.mark.parametrize("shell", [False, True])
def test_sandbox_cleanup_preserves_egress_overflow_count(proxy, monkeypatch, tmp_path, shell):
    """Defect class: the shared cap silently truncates AGY and Claude sandbox receipts."""
    from lu_machinery.dispatcher import agy_sandbox

    async def refuse_dns(host):
        raise ValueError("planted DNS failure")

    monkeypatch.setattr(proxy, "_resolve", refuse_dns)
    hosts = [f"audit-{index}.example" for index in range(259)]
    hosts += ["audit-0.example", "audit-256.example"]
    for host in hosts:
        assert request(proxy, f"CONNECT {host}:443 HTTP/1.1\r\n\r\n".encode()).startswith(b"HTTP/1.1 403")

    boundary = object.__new__(agy_sandbox._Boundary)
    boundary.process = boundary.relay = boundary.gh = boundary.fixture = None
    boundary.egress = proxy
    boundary.receipt = {}
    boundary.created = False
    boundary.shell = shell
    boundary.cleanup()
    receipt_path = tmp_path / "sandbox-receipt.json"
    agy_sandbox._write_receipt(receipt_path, boundary.receipt)
    receipt = json.loads(receipt_path.read_bytes())
    assert receipt["egress"] == [
        {"destination": f"audit-{index}.example:443", "bytes": 0} for index in range(256)
    ]
    assert receipt["egress_overflow_count"] == 4
    assert not proxy.thread.is_alive()

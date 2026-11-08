"""API state must not follow a separately selected product snapshot (#100 R15)."""

import asyncio
import importlib
import json
import subprocess
import sys
import types

import pytest

from lu_machinery.api import (
    config,
    coordination_router,
    monitor_context,
    occupancy_local,
    rollover_router,
    telemetry_router,
)
from lu_machinery.api.telemetry import response


@pytest.fixture
def roots(tmp_path, monkeypatch):
    product = tmp_path / "product"
    state = tmp_path / "state"
    product.mkdir()
    state.mkdir()
    subprocess.run(["git", "init", "-q", str(state)], check=True)
    subprocess.run(["git", "init", "-q", str(product)], check=True)
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(product))
    monkeypatch.setenv("LEARN_UKRAINIAN_PRIMARY_REPO_ROOT", str(state))
    for name in ("LEARN_UKRAINIAN_STATE_ROOT", "LEARN_UKRAINIAN_BATCH_STATE_DIR", "BACKUP_DIR",
                 "LEARN_UK_REPO_ROOT", "AB_DB_PATH", "MONITOR_OCCUPANCY_MARKERS"):
        monkeypatch.delenv(name, raising=False)
    # config caches its lazy product paths. Each test starts with its own selection.
    for name in ("PROJECT_ROOT", "PRODUCT_ROOT", "LIVE_REPO_ROOT", "CURRICULUM_ROOT", "DASHBOARDS_DIR",
                 "BATCH_STATE_DIR", "MESSAGE_DB"):
        if name in vars(config):
            monkeypatch.delitem(vars(config), name)
    monitor_context.production_context.cache_clear()
    yield product, state
    monitor_context.production_context.cache_clear()
    for name in ("PROJECT_ROOT", "PRODUCT_ROOT", "LIVE_REPO_ROOT", "CURRICULUM_ROOT", "DASHBOARDS_DIR",
                 "BATCH_STATE_DIR", "MESSAGE_DB"):
        vars(config).pop(name, None)


def test_context_keeps_state_stores_out_of_product_and_refreshes_anchor(roots, tmp_path, monkeypatch):
    product, state = roots
    before = {p for root in roots for p in root.rglob("*")}
    ctx = monitor_context.production_context()
    assert ctx.roots.state_root == state
    assert ctx.roots.project_root == product
    assert ctx.roots.live_repo_root == product
    assert ctx.roots.curriculum_root == product / "curriculum/l2-uk-en"
    assert ctx.roots.plans_root == product / "curriculum/l2-uk-en/plans"
    assert ctx.roots.dashboards_dir == product / "dashboards"
    assert ctx.roots.logs_dir == state / "logs"
    assert ctx.roots.queue_dir == state / "agents_extensions/shared/consultation-queue"
    assert ctx.roots.pid_dir == state / ".mcp/servers/message-broker/pids"
    assert ctx.roots.backup_dir == state / "data/backups"
    assert ctx.roots.batch_state_dir == state / "batch_state"
    assert ctx.roots.message_db_path == state / ".mcp/servers/message-broker/messages.db"
    assert ctx.roots.session_streams_db_path == state / ".agent/session-streams/v1/session-streams.sqlite3"
    assert ctx.stores.session_streams_database.path == ctx.roots.session_streams_db_path
    assert {p for root in roots for p in root.rglob("*")} == before
    other = tmp_path / "other-state"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    monkeypatch.setenv("LEARN_UKRAINIAN_PRIMARY_REPO_ROOT", str(other))
    fresh = monitor_context.production_context()
    assert fresh is not ctx
    assert fresh.roots.state_root == other
    assert fresh.roots.batch_state_dir == other / "batch_state"
    assert fresh.roots.message_db_path == other / ".mcp/servers/message-broker/messages.db"
    assert fresh.roots.project_root == product
    fixture = monitor_context.fixture_context(tmp_path / "fixture")
    assert fixture.roots.state_root == tmp_path / "fixture"
    assert fixture.roots.logs_dir == tmp_path / "fixture/logs"


def test_ledger_reads_and_timing_database_writes_use_state(roots):
    product, state = roots
    for root, task_id in ((state, "state-task"), (product, "product-decoy")):
        directory = root / "batch_state/agent-ledger/tasks"
        directory.mkdir(parents=True)
        (directory / f"{task_id}.json").write_text(json.dumps({"task_id": task_id, "status": "running"}))
    ctx = monitor_context.production_context()
    result = asyncio.run(coordination_router.coordination_summary(ctx))
    assert [task["task_id"] for task in result["tasks"]] == ["state-task"]
    assert asyncio.run(coordination_router.coordination_task("state-task", ctx))["task_id"] == "state-task"
    assert telemetry_router._tool_timings_path(ctx) == state / "data/telemetry/tool_timings.db"
    telemetry_router._init_db(ctx)
    assert (state / "data/telemetry/tool_timings.db").is_file()
    assert not (product / "data").exists()


def test_occupancy_and_rollover_paths_follow_state_at_use_time(roots, monkeypatch):
    product, state = roots
    ctx = monitor_context.production_context()
    assert occupancy_local.session_streams_db_path() == state / ".agent/session-streams/v1/session-streams.sqlite3"
    assert occupancy_local.markers_root() == state / ".agent/occupancy/markers"
    seen = []

    def audit(root, **kwargs):
        seen.append(root)
        return {"entries": [], "generated_at": "now", "counts": {}, "errors": []}

    monkeypatch.setattr(rollover_router.registry, "audit_fleet", audit)
    monkeypatch.setattr(rollover_router.thread_handoff, "rollover_identity_snapshot", lambda root: {"schema_version": 1, "candidate_count": 0, "errors": []})
    rollover_router.collect_rollover_orient_data()
    rollover_router.rollover_audit(agent=None, source_thread_id=None, replacement_thread_id=None,
                                   lineage_id=None, rollover_id=None, stale_hours=24, ctx=ctx)
    assert seen == [state, state]
    monkeypatch.setenv("LEARN_UKRAINIAN_PRIMARY_REPO_ROOT", str(product))
    assert occupancy_local.markers_root() == product / ".agent/occupancy/markers"
    rollover_router.collect_rollover_orient_data()
    assert seen[-1] == product


def test_response_telemetry_keeps_product_transcript_identity_and_state_records(roots, tmp_path, monkeypatch):
    product, state = roots
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LEARN_UKRAINIAN_TELEMETRY_FOOTER", "1")
    for name in ("AGENT_NO_TELEMETRY_FOOTER", "LEARN_UKRAINIAN_TRANSCRIPT_PATH", "CLAUDE_TRANSCRIPT_PATH"):
        monkeypatch.delenv(name, raising=False)
    project_dir = home / ".claude/projects" / str(product.resolve()).replace("/", "-")
    project_dir.mkdir(parents=True)
    transcript = project_dir / "session-1.jsonl"
    transcript.write_text(json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 1234}}}) + "\n")
    # Missing SessionStart records must retain the product-cwd transcript fallback.
    assert response.build_telemetry_payload("session-1")["ctx"] == 1234
    assert response.build_telemetry_payload()["newest_transcript"]["ctx"] == 1234
    assert response.append_telemetry_footer("hello", "session-1") != "hello"
    assert response.append_telemetry_footer("hello") != "hello"
    # Session metadata remains primary-owned even though transcript identity is product.
    record = state / ".agent/sessions/session-1.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"schema_version": 1, "session_id": "session-1",
                                  "transcript_path": str(transcript), "observed_model_id": "state-model"}))
    decoy = product / ".agent/sessions/session-1.json"
    decoy.parent.mkdir(parents=True)
    decoy.write_text(json.dumps({"schema_version": 1, "session_id": "session-1",
                                 "observed_model_id": "product-decoy"}))
    payload = response.build_telemetry_payload("session-1")
    assert payload["ctx"] == 1234
    assert payload["observed_model"] == "state-model"


class _ImportGap(types.ModuleType):
    """Stand-in for a third-party module the slim pytest job does not install."""

    def __call__(self, *args, **kwargs):
        return _ImportGap(self.__name__ + "()")

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        child = _ImportGap(f"{self.__name__}.{name}")
        setattr(self, name, child)
        return child

    def __iter__(self):
        return iter(())

    def __or__(self, other):
        return _ImportGap("union")

    def __ror__(self, other):
        return _ImportGap("union")

    def __mro_entries__(self, bases):
        return (object,)


_MISSING = object()


_OPTIONAL_IMPORTS = ("prometheus_client", "psycopg")


def _fill_optional(monkeypatch):
    """Fill third-party modules the machinery pytest job does not install.

    The orient and plane calls do not use these modules. A later import inside
    plane status still has to resolve, so the fill stays in place for the test.
    An installed copy is left untouched.
    """
    for name in _OPTIONAL_IMPORTS:
        current = sys.modules.get(name, _MISSING)
        if isinstance(current, _ImportGap):
            continue
        if current is not _MISSING and current is not None:
            continue
        if current is None:
            monkeypatch.setitem(sys.modules, name, _ImportGap(name))
            continue
        try:
            __import__(name)
        except ModuleNotFoundError:
            monkeypatch.setitem(sys.modules, name, _ImportGap(name))


def _load_api(name, monkeypatch):
    """Import an API module, stubbing only the named slim-job gaps."""
    _fill_optional(monkeypatch)
    pending = set(_OPTIONAL_IMPORTS)
    while True:
        try:
            return importlib.import_module(name)
        except ModuleNotFoundError as exc:
            missing = exc.name
            if missing not in pending:
                raise
            pending.remove(missing)
            monkeypatch.setitem(sys.modules, missing, _ImportGap(missing))


def test_rollover_orient_reads_state_registry_not_the_product_checkout(roots, monkeypatch):
    """Distinct anchors must not send rollover orient at the product checkout (#100 R40)."""
    product, state = roots
    seen = []

    def audit(root, **kwargs):
        seen.append(root)
        return {"entries": [], "generated_at": "now", "counts": {}, "errors": []}

    monkeypatch.setattr(rollover_router.registry, "audit_fleet", audit)
    monkeypatch.setattr(
        rollover_router.thread_handoff,
        "rollover_identity_snapshot",
        lambda root: {"schema_version": 1, "candidate_count": 0, "errors": []},
    )
    main = _load_api("lu_machinery.api.main", monkeypatch)

    ctx = monitor_context.production_context()
    main._collect_rollovers_orient_data(ctx)
    assert seen == [state]
    assert product not in seen


def test_fleet_plane_reads_state_root_not_the_product_checkout(roots, monkeypatch):
    """Distinct anchors must not anchor the Fleet plane on the product checkout (#100 R40)."""
    comms_router = _load_api("lu_machinery.api.comms_router", monkeypatch)
    fleet_router = _load_api("lu_machinery.api.fleet_router", monkeypatch)
    runtime_router = _load_api("lu_machinery.api.runtime_router", monkeypatch)
    from lu_machinery.fleet_comms import cold_start_board, message_plane, routing_reservations

    product, state = roots
    monkeypatch.delenv("FLEET_COMMS_ROOT", raising=False)
    ctx = monitor_context.production_context()
    plane = (state / "batch_state/fleet-comms/v1").resolve()
    database = plane / "comms.sqlite3"

    assert fleet_router._plane_root(ctx).resolve() == plane
    assert runtime_router._acp_db_path(ctx).resolve() == database
    assert comms_router._authority_plane_db(ctx).resolve() == database
    assert not (product / "batch_state/fleet-comms/v1/comms.sqlite3").exists()

    seen = []

    def spy(*args, repo_root=None, root=None, **kwargs):
        if repo_root is not None:
            seen.append(repo_root.resolve())
        if root is not None:
            seen.append(root.resolve())
        return {
            "mode": "off",
            "enabled": False,
            "read_only": True,
            "response_schema_version": 1,
            "store": {},
            "schema": {"db_exists": False, "authority": "sqlite"},
            "parity_telemetry": {
                "exists": False,
                "event_count": 0,
                "parity_ok_count": 0,
                "parity_fail_count": 0,
            },
        }

    monkeypatch.setattr(message_plane, "read_plane_status", spy)
    monkeypatch.setattr(fleet_router, "read_plane_status", spy)
    monkeypatch.setattr(runtime_router, "read_plane_status", spy)
    monkeypatch.setattr(comms_router, "read_plane_status", spy)
    monkeypatch.setattr(cold_start_board, "read_plane_status", spy)
    fleet_router._safe_plane_status(ctx)
    fleet_router.fleet_facade_status(ctx)
    routing = runtime_router._routing_plane_status(ctx)
    asyncio.run(comms_router.message_plane_status(ctx))
    assert routing["mode"] == "off"
    assert seen == [state.resolve()] * 4

    roots_seen = []

    def reader(*, root=None, limit=100):
        roots_seen.append(root.resolve())
        return []

    monkeypatch.setattr(routing_reservations, "list_routing_decisions", reader)
    listed = runtime_router.list_routing_assignments(limit=1, ctx=ctx)
    assert roots_seen == [plane]
    assert listed["availability"] == "empty"

    seen.clear()
    board = fleet_router.fleet_facade_board(None, None, None, ctx)
    assert seen == [state.resolve(), plane]
    assert board["probes"]["plane_status"]["status"] == "ok"
    assert not (product / "batch_state").exists()

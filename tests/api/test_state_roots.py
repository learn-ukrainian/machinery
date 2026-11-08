"""API state must not follow a separately selected product snapshot (#100 R15)."""

import asyncio
import json
import subprocess

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

"""Unmocked TestClient probe for /api/fleet/workers/v1 (#7187, #7265)."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from lu_machinery.api.fleet_workers_collect import UNATTRIBUTED_HOST_ID, reset_workers_payload_cache
from lu_machinery.api.main import app
from lu_machinery.api import fleet_workers_router
from lu_machinery.api.observer_presence import PresenceRequest, reset_observer_presence, upsert_presence
from lu_machinery.api.occupancy_local import write_marker
from lu_machinery.api.project_state_router import reset_local_document_cache
from lu_machinery.api.project_state_store import reset_project_state_store

def _snapshot_client(ctx=None):
    snapshot_app = FastAPI()
    snapshot_app.state.ctx = ctx or app.state.ctx
    snapshot_app.include_router(fleet_workers_router.router, prefix="/api/fleet")
    return TestClient(snapshot_app)


def _workers_get(path):
    with _snapshot_client() as running:
        return running.get(path)


def _host_by_id(payload: dict, host_id: str) -> dict:
    hosts = payload["hosts"]
    assert isinstance(hosts, list)
    for host in hosts:
        if host.get("host_id") == host_id:
            return host
    raise AssertionError(f"host {host_id!r} not in {hosts!r}")


def test_unmocked_workers_route_with_fixture_stores(tmp_path: Path, monkeypatch) -> None:
    """Exercise real adapters on fixture stores without monkeypatching the collector."""
    reset_project_state_store()
    reset_observer_presence()
    reset_local_document_cache()
    reset_workers_payload_cache()

    tasks = tmp_path / "tasks"
    tasks.mkdir()
    started = datetime.now(UTC).isoformat()
    (tasks / "live-task.json").write_text(
        json.dumps(
            {
                "task_id": "live-task",
                "schema_version": 2,
                "agent": "cursor",
                "status": "running",
                "pid": os.getpid(),
                "started_at": started,
                "run_nonce": "probe-nonce-1234",
            }
        ),
        encoding="utf-8",
    )

    markers = tmp_path / "markers"
    write_marker(kind="service", task_id="svc-1", host_id="host-worker", agent="codex", path=markers)

    upsert_presence(PresenceRequest(agent="cursor", task_id="observe-1", status="working"))

    monkeypatch.setenv("MONITOR_OCCUPANCY_HOST_IDS", "teach-box=host-teacher,worker-box=host-worker")
    monkeypatch.setenv("LU_MONITOR_HOST_ID", "host-worker")
    monkeypatch.setenv("MONITOR_OCCUPANCY_MARKERS", str(markers))
    monkeypatch.setattr("lu_machinery.api.delegate_router._tasks_dir", lambda ctx=None: tasks)

    response = _workers_get("/api/fleet/workers/v1?host_id=host-worker")
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema"] == "monitor-fleet-workers.v1"
    assert isinstance(payload["hosts"], list)
    host = _host_by_id(payload, "host-worker")
    assert host["workers_status"] == "reported"
    kinds = {row["kind"] for row in host["workers"]}
    assert "delegate" in kinds
    delegate = next(row for row in host["workers"] if row["kind"] == "delegate")
    assert delegate["state"] == "live"
    assert delegate["harness"] is None and delegate["epic"] is None

    observer = _host_by_id(_workers_get("/api/fleet/workers/v1?host_id=cloud-observer").json(), "cloud-observer")
    assert any(row["kind"] == "observer" for row in observer["workers"])

    print(
        "UNMOCKED_WORKERS_PROBE_OK",
        json.dumps({"delegate_kinds": sorted(kinds), "observer_count": len(observer["workers"])}),
    )


def _seed_local_driver_lease(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            stream_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            state TEXT NOT NULL,
            PRIMARY KEY (stream_id, session_id)
        );
        CREATE TABLE IF NOT EXISTS stream_leases (
            stream_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            state TEXT NOT NULL,
            holder_agent TEXT,
            holder_harness TEXT,
            holder_instance_id TEXT,
            holder_task_id TEXT,
            holder_host_id TEXT,
            heartbeat_at TEXT,
            expires_at TEXT
        );
        """
    )
    expires = (datetime.now(UTC) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    heartbeat = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    conn.execute("INSERT OR REPLACE INTO sessions VALUES ('epic:7265', 'sess-local', 'open')")
    conn.execute(
        """
        INSERT INTO stream_leases VALUES (
            'epic:7265', 'sess-local', 'active', 'grok', 'grok-tools',
            'inst-local-7265', 'task-local', 'local', ?, ?
        )
        """,
        (heartbeat, expires),
    )
    conn.commit()
    conn.close()


def test_unmocked_local_host_driver_lease_in_unattributed_bucket(tmp_path: Path, monkeypatch) -> None:
    """Driver lease with holder_host_id local must surface under unattributed, not vanish."""
    reset_project_state_store()
    reset_observer_presence()
    reset_local_document_cache()
    reset_workers_payload_cache()

    db = tmp_path / "session_streams.db"
    _seed_local_driver_lease(db)
    monkeypatch.setattr(app.state, "ctx", app.state.ctx.with_roots(session_streams_db_path=db))
    monkeypatch.setenv("MONITOR_OCCUPANCY_HOST_IDS", "teach-box=host-teacher,worker-box=host-worker")
    monkeypatch.setenv("LU_MONITOR_HOST_ID", "host-teacher")

    response = _workers_get("/api/fleet/workers/v1")
    assert response.status_code == 200
    payload = response.json()
    unattributed = _host_by_id(payload, UNATTRIBUTED_HOST_ID)
    assert unattributed["reason"] == "lease has no host claim"
    worker_ids = {row["id"] for row in unattributed["workers"]}
    assert worker_ids == {"inst-local-7265"}
    assert unattributed["freshness"] == "unknown"
    filtered_response = _workers_get("/api/fleet/workers/v1?host_id=unattributed")
    assert filtered_response.status_code == 200
    filtered = filtered_response.json()
    assert [host["host_id"] for host in filtered["hosts"]] == [UNATTRIBUTED_HOST_ID]
    assert {row["id"] for row in filtered["hosts"][0]["workers"]} == {"inst-local-7265"}
    assert filtered["counts"] == {
        "live": 0,
        "hosts_unknown": 1,
        "workers_total": 1,
        "attention": 0,
        "skipped": 0,
    }
    teacher = _host_by_id(payload, "host-teacher")
    assert not any(row.get("id") == "inst-local-7265" for row in teacher["workers"])

    print(
        "UNMOCKED_LOCAL_LEASE_PROBE_OK",
        json.dumps({"live": payload["counts"]["live"], "worker_ids": sorted(worker_ids)}),
    )


def test_workers_snapshot_never_collects_on_requests(tmp_path, monkeypatch):
    """First and expired filtered GETs must not collect workers or local reports."""
    from lu_machinery.api import fleet_workers_collect, occupancy_local, state_helpers
    from lu_machinery.api.monitor_context import fixture_context

    ctx = fixture_context(tmp_path)
    tasks = ctx.roots.batch_state_dir / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "fixture-task.json").write_text(json.dumps({
        "task_id": "fixture-task", "schema_version": 2, "agent": "codex", "status": "spawning",
    }))
    _seed_local_driver_lease(ctx.roots.session_streams_db_path)
    monkeypatch.setenv("LU_MONITOR_HOST_ID", "host-teacher")
    monkeypatch.setenv("MONITOR_OCCUPANCY_HOST_IDS", "fixture=host-teacher")
    with _snapshot_client(ctx) as running:
        expected, _ = fleet_workers_router.workers_snapshot(ctx).read()
        ids = {row["id"] for host in expected[None]["hosts"] for row in host["workers"]}
        assert {"fixture-task", "inst-local-7265"} <= ids
        def forbidden(*args, **kwargs):
            raise AssertionError("request collected workers")
        with monkeypatch.context() as reads:
            reads.setattr(fleet_workers_collect, "_build_workers_payload", forbidden)
            reads.setattr(fleet_workers_router, "get_cached_local_document", forbidden)
            reads.setattr(state_helpers, "_ttl_clock", lambda: 1e12)
            for host_id in (None, "cloud-observer", "unattributed", "unknown-host"):
                query = "" if host_id is None else f"?host_id={host_id}"
                response = running.get("/api/fleet/workers/v1" + query)
                assert response.status_code == 200
                assert response.json() == expected.get(host_id, expected[""])

    # A single-host filter can resolve driver affinity that is ambiguous in
    # the all-host view. An unreadable store must retain that cold diagnostic.
    monkeypatch.setenv("MONITOR_OCCUPANCY_HOST_IDS", "fixture=host-teacher,alias=host-worker")
    monkeypatch.delenv(occupancy_local.ENV_DRIVER_HOST_ID, raising=False)
    monkeypatch.setattr(fleet_workers_collect, "_self_host_ids", lambda: {"host-teacher", "host-worker"})
    monkeypatch.setattr(occupancy_local, "self_host_opaque_ids", lambda mapping: {"host-teacher", "host-worker"})
    missing = ctx.with_roots(session_streams_db_path=tmp_path / "missing.db")
    with _snapshot_client(missing) as running:
        for host_id in ("host-teacher", "host-worker"):
            response = running.get(f"/api/fleet/workers/v1?host_id={host_id}")
            assert response.status_code == 200
            host = _host_by_id(response.json(), host_id)
            assert host["unattributed_burn"] == {
                "driver": {"state": "unknown", "count": 0, "reason": "session stream store unreadable"},
            }

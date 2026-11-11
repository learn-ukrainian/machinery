"""Read-only fleet worker map route."""

from __future__ import annotations

import copy
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from lu_machinery.api import fleet_workers_collect as workers_collect
from lu_machinery.api.fleet_workers_collect import WORKERS_SCHEMA
from lu_machinery.api.fleet_workers_collect import workers_payload as workers_payload
from lu_machinery.api.monitor_context import MonitorContext, get_ctx
from lu_machinery.api.project_state_router import LocalDocumentSnapshot, get_cached_local_document
from lu_machinery.api.state_helpers import PeriodicSnapshot

WORKERS_SNAPSHOT_REFRESH_S = 2.0


def _collect_snapshots(ctx: MonitorContext) -> dict[str | None, dict[str, Any]]:
    payloads: dict[str | None, dict[str, Any]] = {}
    workers_collect._build_workers_payload(
        tasks_dir=workers_collect.delegate_router._tasks_dir(ctx),
        session_db=ctx.roots.session_streams_db_path,
        presence_store=ctx.stores.presence_store,
        report_store=ctx.stores.report_store,
        host_payloads=payloads,
    )
    self_report_ids = workers_collect._self_host_ids() - {workers_collect.UNATTRIBUTED_HOST_ID}
    self_reports: dict[str, LocalDocumentSnapshot] = {
        opaque: get_cached_local_document(opaque) for opaque in self_report_ids
    }
    for payload in payloads.values():
        for host in payload.get("hosts", []):
            snapshot = self_reports.get(host.get("host_id"))
            if snapshot is not None:
                host["freshness"] = snapshot.freshness
                host["age_s"] = snapshot.age_s
        workers_collect.reconcile_worker_counts(payload)
    return payloads


def workers_snapshot(ctx: MonitorContext) -> PeriodicSnapshot[dict[str | None, dict[str, Any]]]:
    snapshot: PeriodicSnapshot[dict[str | None, dict[str, Any]]] = ctx.runtime.get_or_create_derived(
        "fleet-workers-snapshot",
        lambda: PeriodicSnapshot(lambda: _collect_snapshots(ctx), WORKERS_SNAPSHOT_REFRESH_S),
    )
    return snapshot


@asynccontextmanager
async def _workers_lifespan(app):
    async with workers_snapshot(app.state.ctx).lifespan():
        yield


router = APIRouter(prefix="/workers/v1", tags=["fleet-workers"], lifespan=_workers_lifespan)


@router.get("")
def get_workers(
    host_id: str | None = Query(default=None),
    ctx: MonitorContext = Depends(get_ctx),
) -> JSONResponse:
    snapshots, age = workers_snapshot(ctx).read()
    payload = copy.deepcopy(snapshots.get(host_id, snapshots[""]))
    return JSONResponse(
        content=payload,
        headers={"Cache-Control": "no-store", "X-Workers-Schema": WORKERS_SCHEMA, "X-Snapshot-Age-S": f"{age:.3f}"},
    )

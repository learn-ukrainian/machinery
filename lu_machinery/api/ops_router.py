"""Read-only ops surfaces: retention plans, test slots, high-load samples and firing alerts."""

from __future__ import annotations

import http.client
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from lu_machinery.common import test_slots

from . import load_samples, panel_values
from .monitor_context import MonitorContext, get_ctx
from .state_helpers import cache_get_or_compute_async, ctx_scoped_ttl_key

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ops"])


def _plan_dir(ctx: MonitorContext) -> Path:
    return ctx.roots.batch_state_dir / "fleet-comms" / "retention"


@router.get("/v1/retention/latest")
async def retention_latest(ctx: MonitorContext = Depends(get_ctx)) -> dict:
    """Return the latest retention plan JSON if present (no mutations)."""
    plan_dir = _plan_dir(ctx)
    latest = plan_dir / "latest.json"
    if not latest.is_file():
        return {
            "schema": "fleet-comms.retention.plan.v1",
            "missing": True,
            "plan_dir": str(plan_dir),
            "hint": (
                ".venv/bin/python "
                f"{Path(__file__).parents[1].name}/hygiene/retention_engine.py plan"
            ),
        }
    try:
        payload = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # Do not return exception text to HTTP clients (CodeQL py/stack-trace-exposure).
        logger.exception("retention plan unreadable: %s", latest)
        return {
            "schema": "fleet-comms.retention.plan.v1",
            "error": "unreadable_plan",
            "plan_path": str(latest),
        }
    if isinstance(payload, dict):
        payload.setdefault("plan_path", str(latest))
        payload["missing"] = False
        return payload
    return {
        "schema": "fleet-comms.retention.plan.v1",
        "error": "invalid_plan",
        "plan_path": str(latest),
    }


@router.get("/v1/retention/plan-dir")
async def retention_plan_dir(ctx: MonitorContext = Depends(get_ctx)) -> dict:
    """Expose the on-disk plan directory path (for operators)."""
    plan_dir = _plan_dir(ctx)
    return {
        "plan_dir": str(plan_dir),
        "exists": plan_dir.is_dir(),
        "archive_root_default": str(
            Path.home()
            / "Library"
            / "Application Support"
            / "learn-ukrainian"
            / "retention-archives"
        ),
        "scheduled_apply_default": False,
    }


@router.get("/v1/test-slots")
def test_slots_status(_ctx: MonitorContext = Depends(get_ctx)) -> dict:
    """Host test-slot pool (#10662): holders by task, the FIFO queue and wait times; never a path.

    A plain ``def``: the status read touches files, so FastAPI runs it off the event loop.
    ``observed_at`` and ``age_s`` describe the source observation. A failed read
    leaves both unknown instead of presenting the request time as freshness;
    ``requested_at`` records the request separately.
    """
    requested_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    payload = test_slots.status()
    payload["requested_at"] = requested_at
    if payload.get("error") is None:
        payload["observed_at"] = requested_at
        payload["age_s"] = 0.0
    else:
        payload["observed_at"] = None
        payload["age_s"] = None
    return payload


# The host Alertmanager listens on loopback only.
ALERTMANAGER_HOST = "127.0.0.1"
ALERTMANAGER_PORT = 9093
ALERTMANAGER_ALERTS_PATH = "/api/v2/alerts?active=true&silenced=false&inhibited=false"
ALERTS_TTL_S = 15.0
ALERTS_TIMEOUT_S = 3.0
# The dead-man heartbeat fires all the time by design; it proves Alertmanager
# is evaluating, so it is reported as a flag and not listed as a firing alert.
HEARTBEAT_ALERTNAMES = frozenset({"Watchdog"})
# Alertmanager v2 alert states; anything else is a malformed reading.
ALERT_STATES = frozenset({"active", "suppressed", "unprocessed"})
# Names our own alerting rules define. Anything else stays UnrecognizedAlert;
# labels are never passed through.
_ALERT_NAMES = frozenset({
    "ApprovedNotLanded30m",
    "BackupSuccessOverdue",
    "CappedScopeOomKill",
    "CertExpirySoon",
    "CfRunnerStuck",
    "CgroupCapOomKill",
    "CiRecoveriesOver3PerDay",
    "DiskAlmostFull",
    "DiskCritical",
    "DriverKeeperCollectErrors",
    "DriverKeeperCtoInboxWait",
    "DriverKeeperCtoInboxWaitLong",
    "DriverKeeperHealthStale",
    "DriverKeeperLaneAlert",
    "DriverKeeperStale",
    "DriverPaneNoLiveCli",
    "DriverTextfileStale",
    "HostMemoryPressure",
    "HostOomKill",
    "ListenerEvidenceUnavailable",
    "LuApiProbeDown",
    "LuApiProbeSlow",
    "LuPaceStale",
    "MergeQueueStalled",
    "NonLoopbackListener",
    "OffsiteBackupFreshnessUnknown",
    "OffsiteBackupOverdue",
    "OffsiteBackupRunFailed",
    "OffsiteSecondaryCoverageStale",
    "OomEvidenceUnavailable",
    "OomKillUnclassified",
    "PRBacklogRising3Days",
    "PROpenOver60m",
    "ReviewRoundsOver2",
    "ScorecardSourceDown",
    "ShutDownUnitBecameActive",
    "SreWatchTimerMissed",
    "StalePRsIdleOver24h",
    "SystemdUnitCrashLooping",
    "SystemdUnitFailed",
    "TeacherApiProbeDown",
    "TeacherApiScrapeDown",
    "ThroughputReportTimerMissed",
    "UsageCacheStale",
    "UsagePoolExpiringUnspent",
    "UsagePoolLowBeforeReset",
    "UsagePoolReadingStale",
    "UsagePoolRestored",
    "Watchdog",
})


def _alerts_reading() -> dict:
    """Read firing alerts from the loopback Alertmanager and keep only allow-listed fields.

    Labels such as ``instance`` and ``job`` and the generator URL name hosts and
    scrape targets, so they never leave this function.
    """
    observed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    try:
        connection = http.client.HTTPConnection(ALERTMANAGER_HOST, ALERTMANAGER_PORT, timeout=ALERTS_TIMEOUT_S)
        try:
            connection.request("GET", ALERTMANAGER_ALERTS_PATH, headers={"Accept": "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError(f"HTTP {response.status}")
            raw = json.loads(response.read(2_000_000))
        finally:
            connection.close()
    except (OSError, ValueError, http.client.HTTPException) as exc:
        logger.warning("alertmanager read failed: %s", type(exc).__name__)
        return {"status": "unknown", "reason_code": "alertmanager_unreachable", "observed_at": observed_at,
                "heartbeat": None, "alerts": []}
    if not isinstance(raw, list):
        return {"status": "unknown", "reason_code": "alertmanager_invalid_response", "observed_at": observed_at,
                "heartbeat": None, "alerts": []}
    alerts: list[dict] = []
    heartbeat = False
    for item in raw:
        labels = item.get("labels") if isinstance(item, dict) else None
        status = item.get("status") if isinstance(item, dict) else None
        state = status.get("state") if isinstance(status, dict) else None
        annotations = item.get("annotations", {}) if isinstance(item, dict) else None
        # A malformed item means the reading cannot be trusted as a whole: an
        # "ok" reading with a dropped alert would show "Nothing firing".
        if (
            not isinstance(labels, dict)
            or not isinstance(labels.get("alertname"), str)
            or not isinstance(state, str)
            or state not in ALERT_STATES
            or not isinstance(annotations, dict)
        ):
            return {"status": "unknown", "reason_code": "alertmanager_invalid_response", "observed_at": observed_at,
                    "heartbeat": None, "alerts": []}
        if state != "active":
            continue
        if labels["alertname"] in HEARTBEAT_ALERTNAMES:
            heartbeat = True
            continue
        row = {
            "alertname": panel_values.choice(labels.get("alertname"), _ALERT_NAMES, "UnrecognizedAlert"),
            "severity": panel_values.choice(labels.get("severity"), {"critical", "warning", "info", "none"}, "unknown"),
            "starts_at": panel_values.timestamp(item.get("startsAt")),
        }
        alerts.append(row)
    alerts.sort(key=lambda row: (row.get("severity") != "critical", row.get("starts_at") or ""))
    return {"status": "ok", "reason_code": None, "observed_at": observed_at, "heartbeat": heartbeat, "alerts": alerts}


@router.get("/v1/alerts")
async def firing_alerts(ctx: MonitorContext = Depends(get_ctx)) -> JSONResponse:
    """Alerts firing now in the loopback Alertmanager, allow-listed fields only.

    ``status`` is ``unknown`` with a ``reason`` when Alertmanager cannot be read,
    so an outage never reads as "nothing firing".
    """
    key = ctx_scoped_ttl_key(ctx, "ops", "alerts")
    # This loopback read is cheap. Waiting for it on expiry avoids serving the
    # previous page refresh's reading on every 30-second UI cycle.
    payload = await cache_get_or_compute_async(key, ALERTS_TTL_S, _alerts_reading)
    return JSONResponse(content=payload, headers=panel_values.snapshot_headers(
        panel_values.observation_age_s(payload.get("observed_at")), 45.0,
        unavailable=payload.get("status") != "ok",
    ))


@router.get("/v1/load-samples")
def load_samples_tail(
    limit: int = Query(50, ge=1, le=load_samples.MAX_SAMPLES), ctx: MonitorContext = Depends(get_ctx)
) -> dict:
    """Read the bounded attribution ring without triggering process collection."""
    return load_samples.tail(ctx.roots.batch_state_dir, limit)

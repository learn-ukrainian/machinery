"""Unified, read-only observer for the durable fleet communications plane.

Health and overview read the resolved authority through read-only storage
handles. SQLite-shaped detail routes refuse PostgreSQL explicitly. Only the
guarded message body route reads artifacts. No read initializes schema, writes
a receipt or invokes an agent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import BoundedSemaphore
from time import monotonic
from typing import Annotated, Any, get_args

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from lu_machinery import agent_identity, state_paths
from lu_machinery.agent_runtime.accounting import recorded_model_accounting_fields
from lu_machinery.common import json_depth
from lu_machinery.control_plane import storage
from lu_machinery.control_plane.health import read_failure_code
from lu_machinery.fleet_comms import message_plane, migrations
from lu_machinery.fleet_comms.artifacts import (
    ArtifactExpiredError,
    ArtifactStoreError,
    ArtifactTooLargeError,
    read_message_body_bounded,
)
from lu_machinery.fleet_comms.authority import (
    AUTHORITY_MESSAGE_ID_RE,
    DURABLE_CTO_RECIPIENT,
    AuthorityDeliveryState,
    is_session_cto_recipient,
    load_external_recipients,
)
from lu_machinery.fleet_comms.endpoints import load_endpoint_registry
from lu_machinery.fleet_comms.message_plane import read_plane_status
from lu_machinery.fleet_comms.migrations import MIGRATIONS
from lu_machinery.fleet_comms.opsec_store import COMMS_RESPONSE_SCHEMA_VERSION, store_descriptor
from lu_machinery.fleet_comms.paths import (
    RETIRED_LOCAL_PLANE_MESSAGE,
    PlaneRootAnchorError,
    local_plane_is_retired,
    validate_plane_root,
)
from lu_machinery.guardrails.worktree_containment import (
    NotAGitRepositoryError,
    resolve_main_root_without_git,
)
from lu_machinery.lib.readonly_sqlite import SQLiteConnection, is_sqlite_connection, open_readonly
from lu_machinery.ops import driver_lanes
from lu_machinery.orchestration.launcher_aliases import load_launcher_bindings
from lu_machinery.session_streams.inventory import stream_anchor_id
from lu_machinery.session_streams.model import STREAM_ID_RE, isoformat_z, parse_timestamp

from . import fleet_workers_collect, panel_values
from .monitor_context import MonitorContext, get_ctx, resolve_context
from .product_contribution import fleet_batches
from .runtime_router import (
    _AcpBodyEncodingError,
    _AcpRedactionDepthExceeded,
    _redact_and_sanitize_text,
    get_acp_conversation,
    list_acp_conversations,
    local_body_read_refusal_response,
    recent_runtime_records,
)
from .state_helpers import swr_route

_DATABASE_ERRORS: tuple[type[Exception], ...] = (sqlite3.Error,)
try:
    from psycopg import Error as PostgresError
except ModuleNotFoundError as exc:
    if exc.name != "psycopg":
        raise
else:
    _DATABASE_ERRORS += (PostgresError,)
_OVERVIEW_ERRORS: tuple[type[Exception], ...] = (*_DATABASE_ERRORS, storage.ControlPlaneError)

router = APIRouter(tags=["fleet"])
# Fleet read models served stale-while-revalidate (``state_helpers.swr_route``):
# a read gets the last projection with its true age and an older one queues
# one refresh. Each projection reads the whole plane or task store.
FACADE_TTL_S = 15.0
logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
MAX_DETAIL_ROWS = 100
MAX_ACP_SCAN = 500
MAX_ACTIVITY_SCAN = 500
MAX_OPERATIONS_ITEMS = 50
AUTHORITY_HEALTH_WINDOW_HOURS = 24

_DELIVERY_STATES = frozenset(get_args(AuthorityDeliveryState))
_DELIVERY_WINDOWS = {"24h": timedelta(days=1), "7d": timedelta(days=7)}
_DELIVERY_KINDS = frozenset({
    "message", "request", "system", "supervisory-request", "delivery-error", "discussion",
    "discussion-root", "discussion-result", "ownership_requeue",
})

_BATCH_HEALTH = frozenset({"complete", "healthy", "stalled", "dead", "unknown"})
_TRACK_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
_PROBE_ERROR = re.compile(r"^ps: [A-Za-z][A-Za-z0-9_]{0,63}(?: \d{1,6})?$")
_SAFE_FAILURE_PHASES = frozenset(
    {"admission", "transport", "provider", "result_parse", "postprocess"}
)
_SAFE_FAILURE_CODES = frozenset(
    {
        "adapter_refused",
        "conversation_state_missing",
        "primary_cwd_rejected",
        "protocol_output_limit",
        "provider_unavailable",
        "rate_limited",
        "result_invalid",
        "route_effort_conflict",
        "route_model_conflict",
        "timeout",
        "transport_error",
        "unknown",
    }
)

_SAFE_METADATA_KEYS = frozenset(
    {
        "agent",
        "completion_state",
        "harness",
        "legacy_message_id",
        "model",
        "plane_mode",
        "pr_number",
        "source",
        "source_agent",
        "stream_epic",
        "task_family",
        "task_id",
        "transport",
        "transport_mode",
        "via",
    }
)
_PROVENANCE_KEY_ALIASES = {"source": "Source", "agent": "Agent", "via": "Via"}
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|authorization|password|secret|token)\b(\s*[:=]\s*)([^,;\s]+)"
)
_BEARER_TOKEN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_TOKEN_LITERAL = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{16,}|sk-[A-Za-z0-9_-]{16,})\b")




def _plane_root(ctx: MonitorContext | None = None) -> Path:
    """Resolve the existing durable plane root without creating it."""
    resolved_ctx = resolve_context(ctx)
    if not os.environ.get(message_plane.ENV_ROOT):
        try:
            base = resolve_main_root_without_git(resolved_ctx.roots.state_root)
        except NotAGitRepositoryError:
            pass
        else:
            try:
                root = validate_plane_root(state_paths.fleet_comms_dir(base))
                if local_plane_is_retired(root=root):
                    raise PlaneRootAnchorError(RETIRED_LOCAL_PLANE_MESSAGE)
            except PlaneRootAnchorError:
                raise
            except (OSError, RuntimeError):
                raise PlaneRootAnchorError("plane_root_unresolvable") from None
            return root
    return message_plane.default_plane_root(repo_root=resolved_ctx.roots.state_root)


def _plane_db_path(ctx: MonitorContext | None = None) -> Path:
    return _plane_root(ctx) / "comms.sqlite3"


@contextmanager
def _read_connection(
    ctx: MonitorContext | None = None,
    *,
    pg_supported: bool = False,
    plane_root: Path | None = None,
) -> Iterator[tuple[Any | None, str]]:
    """Yield a query-only connection, never creating a database or schema."""
    authority = storage.resolve_authority(storage.StoreId.FLEET_COMMS)
    if authority is storage.Authority.PG and not pg_supported:
        # Detail routes still use SQLite-shaped SQL. Do not read a stale
        # local file or mistake this component limit for database absence.
        yield None, "authority_unsupported_component"
        return
    resolved_ctx = resolve_context(ctx)
    db_path = (
        plane_root / "comms.sqlite3"
        if plane_root is not None
        else _plane_db_path(resolved_ctx)
    )
    if authority is not storage.Authority.PG and not db_path.is_file():
        yield None, "db_missing"
        return

    connection = None
    availability = "available"
    try:
        connection = (
            storage.connect(storage.StoreId.FLEET_COMMS, path=db_path, read_only=True, timeout=0.25)
            if authority is storage.Authority.PG
            else open_readonly(db_path, timeout=0.25)
        )
        if authority is storage.Authority.PG:
            connection.autocommit = True
            connection.execute("SET statement_timeout = 2000")
        else:
            connection.row_factory = sqlite3.Row
    except Exception as exc:
        availability = read_failure_code(exc, authority)
    try:
        yield connection if availability == "available" else None, availability
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                logger.warning("Fleet observer read-only connection close failed")


def _table_exists(connection: Any, table: str) -> bool:
    query = (
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?"
        if is_sqlite_connection(connection)
        else "SELECT 1 FROM pg_class WHERE oid = to_regclass(%s) AND relkind IN ('r', 'p')"
    )
    row = connection.execute(query, (table,)).fetchone()
    return row is not None


def _normalize_time(value: str | None, label: str) -> str | None:
    """Normalize an ISO-8601 filter to lexical UTC form used by fleet rows."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{label} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _time_clauses(
    column: str,
    *,
    since: str | None,
    until: str | None,
    clauses: list[str],
    params: list[Any],
) -> None:
    if since is not None:
        clauses.append(f"{column} >= ?")
        params.append(since)
    if until is not None:
        clauses.append(f"{column} <= ?")
        params.append(until)


def _safe_text(value: Any, *, limit: int = 160, fallback: str = "unknown") -> str:
    """Return a compact display label without reflecting arbitrary multiline data."""
    if value is None:
        return fallback
    text = " ".join(str(value).split())
    if not text:
        return fallback
    return text[:limit]




def _safe_metadata(value: Any) -> dict[str, Any]:
    """Allowlist terse metadata fields rather than exposing arbitrary JSON."""
    if not isinstance(value, str) or not value:
        return {}
    try:
        raw = json_depth.loads(value, max_depth=512)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}

    metadata: dict[str, Any] = {}
    for key in sorted(_SAFE_METADATA_KEYS):
        alias = _PROVENANCE_KEY_ALIASES.get(key)
        item = raw.get(alias) if alias in raw else raw.get(key)
        if isinstance(item, str):
            metadata[key] = _safe_text(item, limit=120, fallback="")
        elif isinstance(item, (int, bool)):
            metadata[key] = item
    return metadata


def _first_label(*values: Any, fallback: str) -> str:
    for value in values:
        text = _safe_text(value, fallback="")
        if text:
            return text
    return fallback


def _provenance(
    *,
    metadata: dict[str, Any] | None = None,
    source: Any = None,
    agent: Any = None,
    via: Any = None,
) -> dict[str, str]:
    """Normalize the Source / Agent / Via triplet used throughout the observer."""
    metadata = metadata or {}
    return {
        "source": _first_label(
            source,
            metadata.get("source"),
            metadata.get("source_agent"),
            fallback="fleet-comms",
        ),
        "agent": _first_label(metadata.get("agent"), agent, fallback="unknown"),
        "via": _first_label(
            metadata.get("via"),
            metadata.get("transport_mode"),
            metadata.get("transport"),
            metadata.get("harness"),
            via,
            fallback="fleet-comms",
        ),
    }


def _collection(
    key: str,
    items: list[dict[str, Any]],
    *,
    total: int | None,
    limit: int,
    offset: int,
    availability: str,
    filters: dict[str, Any],
) -> dict[str, Any]:
    next_offset = offset + len(items) if total is not None and offset + len(items) < total else None
    return {
        "read_only": True,
        "availability": availability,
        "db_exists": (
            availability not in {"db_missing", "db_unavailable"}
            if total is not None or availability == "db_missing"
            else None
        ),
        key: items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "next_offset": next_offset,
        "filters": filters,
    }


def _empty_collection(
    key: str,
    *,
    limit: int,
    offset: int,
    availability: str,
    filters: dict[str, Any],
) -> dict[str, Any]:
    return _collection(
        key,
        [],
        total=0,
        limit=limit,
        offset=offset,
        availability=availability,
        filters=filters,
    )


def _paged_query(
    connection: SQLiteConnection,
    *,
    key: str,
    select_sql: str,
    from_sql: str,
    clauses: list[str],
    params: list[Any],
    order_sql: str,
    limit: int,
    offset: int,
    filters: dict[str, Any],
    transform: Callable[[sqlite3.Row], dict[str, Any]],
) -> dict[str, Any]:
    where_sql = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        total_row = connection.execute(f"SELECT COUNT(*){from_sql}{where_sql}", params).fetchone()
        total = int(total_row[0]) if total_row is not None else 0
        query = f"{select_sql}{from_sql}{where_sql} ORDER BY {order_sql} LIMIT ? OFFSET ?"
        rows = connection.execute(query, [*params, limit, offset]).fetchall()
    except sqlite3.Error:
        logger.warning("Fleet observer query failed")
        return _empty_collection(
            key,
            limit=limit,
            offset=offset,
            availability="db_unavailable",
            filters=filters,
        )
    return _collection(
        key,
        [transform(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
        availability="available" if total else "empty",
        filters=filters,
    )


def _safe_plane_status(
    ctx: MonitorContext | None = None,
    *,
    plane_root: Path | None = None,
) -> dict[str, Any]:
    """Project the existing plane read model without paths or raw telemetry rows."""
    resolved_ctx = resolve_context(ctx)
    status = (
        read_plane_status(root=plane_root, recent_limit=0)
        if plane_root is not None
        else read_plane_status(repo_root=resolved_ctx.roots.state_root, recent_limit=0)
    )
    mode = _safe_text(status.get("mode"), fallback="invalid")
    authority_active = mode == "authority"
    schema = status.get("schema") if isinstance(status.get("schema"), dict) else {}
    telemetry = (
        status.get("parity_telemetry")
        if isinstance(status.get("parity_telemetry"), dict)
        else {}
    )
    return {
        "mode": mode,
        "enabled": bool(status.get("enabled")),
        "read_only": True,
        "response_schema_version": status.get("response_schema_version"),
        "store": status.get("store"),
        "authority": (
            "fleet_comms_authoritative" if authority_active else "file_handoffs_authoritative"
        ),
        "cutover": "authority_active" if authority_active else "pre_flip_operator_gated",
        "schema": {
            "known_version": schema.get("known_version"),
            "applied_version": schema.get("applied_version"),
            "applied_name": schema.get("applied_name"),
            "db_exists": schema.get("db_exists"),
            "authority": schema.get("authority"),
            "db_error": schema.get("db_error"),
            "store": schema.get("store"),
        },
        "parity_telemetry": {
            "store": telemetry.get("store"),
            "exists": bool(telemetry.get("exists")),
            "event_count": int(telemetry.get("event_count") or 0),
            "parity_ok_count": int(telemetry.get("parity_ok_count") or 0),
            "parity_fail_count": int(telemetry.get("parity_fail_count") or 0),
        },
    }








# The bottleneck slice is lifecycle statistics over every task record: a few seconds
# old is fine, and a cold scan of today's ledger outlasts the request timeout. Serve it
# stale-while-revalidate; the stored copy is refreshed by one background flight.
BOARD_BOTTLENECK_TTL_S = 60.0
# A cold process waits this long for the first scan, then reports the probe as warming.
BOARD_BOTTLENECK_WAIT_S = 2.0
# ``gh pr list`` is a network round trip on every board; the five newest PRs may lag this much.
BOARD_PR_LIST_TTL_S = 30.0








def _count_by_state(connection: Any, table: str, column: str) -> dict[str, int]:
    if not _table_exists(connection, table):
        return {}
    try:
        rows = connection.execute(
            f"SELECT {column}, COUNT(*) FROM {table} GROUP BY {column} ORDER BY {column}"
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {_safe_text(row[0], fallback="unknown"): int(row[1]) for row in rows}


def _count_by_state_where(
    connection: Any,
    table: str,
    column: str,
    *,
    where: str,
    params: tuple[Any, ...] = (),
) -> dict[str, int]:
    if not _table_exists(connection, table):
        return {}
    rows = connection.execute(
        f"SELECT {column}, COUNT(*) FROM {table} WHERE {where} "
        f"GROUP BY {column} ORDER BY {column}",
        params,
    ).fetchall()
    return {_safe_text(row[0], fallback="unknown"): int(row[1]) for row in rows}


def _authority_health_window(now: datetime | None = None) -> dict[str, Any]:
    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    since = observed_at - timedelta(hours=AUTHORITY_HEALTH_WINDOW_HOURS)
    return {
        "hours": AUTHORITY_HEALTH_WINDOW_HOURS,
        "since": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "until": observed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _authority_health_payload(
    states: dict[str, int],
    *,
    overdue: int,
    dead_letters: int,
    window: dict[str, Any],
) -> dict[str, Any]:
    total = sum(states.values())
    failures = sum(
        states.get(name, 0) for name in ("failed", "expired", "dead_lettered")
    )
    if failures or overdue or dead_letters:
        state = "degraded"
    elif total:
        state = "healthy"
    else:
        state = "idle"
    return {
        "state": state,
        "ok": state in {"healthy", "idle"},
        "availability": "available",
        "window": window,
        "jobs": {"total": total, "by_state": states},
        "overdue_nonterminal": overdue,
        "dead_letters": dead_letters,
    }


def _authority_health_snapshot(
    connection: Any | None,
    *,
    now: datetime | None = None,
    availability: str = "available",
) -> dict[str, Any]:
    """Derive current health only from a fixed authority evidence window."""
    window = _authority_health_window(now)
    unavailable = {
        "state": "unavailable",
        "ok": False,
        "availability": availability,
        "window": window,
        "jobs": {"total": 0, "by_state": {}},
        "overdue_nonterminal": 0,
        "dead_letters": 0,
    }
    if connection is None:
        return unavailable
    placeholder = "?" if is_sqlite_connection(connection) else "%s"
    try:
        if not _table_exists(connection, "authority_jobs"):
            return {**unavailable, "availability": "schema_read_failed"}
        states = _count_by_state_where(
            connection,
            "authority_jobs",
            "state",
            where=f"updated_at >= {placeholder}",
            params=(window["since"],),
        )
        overdue = int(
            connection.execute(
                f"""SELECT COUNT(*) FROM authority_jobs
                   WHERE state IN ('queued', 'running')
                     AND deadline_at IS NOT NULL AND deadline_at < {placeholder}""",
                (window["until"],),
            ).fetchone()[0]
        )
        dead_letters = 0
        if _table_exists(connection, "authority_dead_letters"):
            dead_letters = int(
                connection.execute(
                    # A deliberate expire-stale cleanup is not a failure (authority.CLEANUP_EXPIRY_REASON).
                    "SELECT COUNT(*) FROM authority_dead_letters"
                    f" WHERE created_at >= {placeholder} AND reason_code <> {placeholder}",
                    (window["since"], "expired_by_cleanup"),
                ).fetchone()[0]
            )
    except _DATABASE_ERRORS:
        logger.warning("Fleet observer could not derive authority health")
        return {**unavailable, "availability": "schema_read_failed"}
    return _authority_health_payload(
        states,
        overdue=overdue,
        dead_letters=dead_letters,
        window=window,
    )


def _runtime_activity_snapshot(since: str, ctx: MonitorContext | None = None) -> dict[str, Any]:
    """Count runtime/delegate ledger records inside the authority evidence window.

    The authority health window counts only fleet-comms ``authority_jobs``.
    Runtime and delegate work is recorded in a separate usage ledger, so a
    "0 jobs" authority window must not be read as "no fleet work today".
    """
    try:
        records = recent_runtime_records(limit=MAX_ACTIVITY_SCAN, ctx=ctx).get("records", [])
    except Exception:
        logger.warning("Fleet observer could not derive runtime activity")
        return {
            "ledger": "runtime_delegate_usage",
            "records_in_window": None,
            "availability": "unavailable",
        }
    total = sum(
        1
        for record in records
        if isinstance(record, dict)
        and isinstance(record.get("ts"), str)
        and record["ts"] >= since
    )
    return {
        "ledger": "runtime_delegate_usage",
        "records_in_window": total,
        "availability": "available",
    }


def _non_negative_int(value: Any) -> int:
    """Normalize legacy numeric fields without reflecting arbitrary values."""
    if isinstance(value, bool):
        return int(value)
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _non_negative_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return max(0.0, round(number, 1))


def _safe_probe_errors(raw: Any) -> list[str]:
    """Keep process-probe failures that name the failure class, not a path or secret."""
    if not isinstance(raw, list):
        return []
    errors: list[str] = []
    for item in raw[:8]:
        if not isinstance(item, str):
            continue
        text = " ".join(item.split())
        if _PROBE_ERROR.fullmatch(text):
            errors.append(text)
    return errors


def _safe_batch_projection(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {
            "availability": "unavailable",
            "total": 0,
            "returned": 0,
            "limit": MAX_OPERATIONS_ITEMS,
            "truncated": False,
            "running_processes": 0,
            "by_health": {},
            "tracks": [],
        }

    source = raw.get("tracks")
    source_tracks = source if isinstance(source, dict) else {}
    tracks: list[dict[str, Any]] = []
    by_health: Counter[str] = Counter()
    for raw_track, entry in sorted(source_tracks.items(), key=lambda item: str(item[0])):
        if not isinstance(entry, dict):
            continue
        track = str(raw_track)
        if not _TRACK_LABEL.fullmatch(track):
            track = "unknown"
        health = entry.get("health")
        if health not in _BATCH_HEALTH:
            health = "unknown"
        by_health[health] += 1
        if len(tracks) >= MAX_OPERATIONS_ITEMS:
            continue
        tracks.append(
            {
                "track": track,
                "health": health,
                "total_expected": _non_negative_int(entry.get("total_expected")),
                "research_done": _non_negative_int(entry.get("research_done")),
                "remaining": _non_negative_int(entry.get("remaining")),
                "recent_30min": _non_negative_int(entry.get("recent_30min")),
                "throughput_per_hour": _non_negative_float(
                    entry.get("throughput_per_hour")
                ),
            }
        )

    errors = _safe_probe_errors(raw.get("errors"))
    probe_failed = raw.get("running_processes", 0) is None or bool(errors)
    projected = {
        "availability": "degraded" if probe_failed else "available",
        "total": sum(by_health.values()),
        "returned": len(tracks),
        "limit": MAX_OPERATIONS_ITEMS,
        "truncated": sum(by_health.values()) > len(tracks),
        "running_processes": (
            None
            if raw.get("running_processes", 0) is None
            else _non_negative_int(raw.get("running_processes"))
        ),
        "by_health": dict(sorted(by_health.items())),
        "tracks": tracks,
    }
    if errors:
        projected["errors"] = errors
    return projected


async def _product_batch_snapshot(ctx: MonitorContext) -> dict[str, Any] | None:
    """The product's raw curriculum batch snapshot, or ``None`` when no product supplies one."""
    collect = fleet_batches()
    if collect is None:
        return None
    return await asyncio.to_thread(collect, ctx)


@router.get("/operations")
async def fleet_operations(ctx: MonitorContext = Depends(get_ctx)) -> dict[str, Any]:
    """Sanitized curriculum batch operations projection.

    The legacy broker and zombie sections are removed with the legacy store (#515).
    """
    try:
        batch_result = await _product_batch_snapshot(ctx)
    except Exception:  # a failed product collector reads as unavailable, never a 500
        logger.warning("Fleet operations could not collect the product batch snapshot")
        batch_result = None
    batches = _safe_batch_projection(batch_result)
    return {
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "read_only": True,
        "writes_enabled": False,
        "availability": batches["availability"],
        "batches": batches,
    }


@router.get("/health")
def fleet_health(ctx: MonitorContext = Depends(get_ctx)) -> dict[str, Any]:
    """Read-only health, mode, schema, and current authority posture."""
    status = _safe_plane_status(ctx)
    with _read_connection(ctx, pg_supported=True) as (connection, availability):
        authority_health = (
            _authority_health_snapshot(connection, availability=availability)
            if status["mode"] == "authority"
            else {
                "state": "not_applicable",
                "ok": True,
                "availability": availability,
                "window": None,
                "jobs": {"total": 0, "by_state": {}},
                "overdue_nonterminal": 0,
                "dead_letters": 0,
            }
        )
    window = authority_health.get("window") or {}
    since = window.get("since")
    if not isinstance(since, str):
        since = (datetime.now(UTC) - timedelta(hours=AUTHORITY_HEALTH_WINDOW_HOURS)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    runtime_activity = _runtime_activity_snapshot(since, ctx)
    return {
        "ok": (
            bool(status["enabled"]) and bool(authority_health["ok"])
            and status["store"]["reachable"] is True
            and not status["schema"].get("db_error")
        ),
        "observer": "fleet-comms-v1",
        "read_only": True,
        "writes_enabled": False,
        "authority_health": authority_health,
        "runtime_activity": runtime_activity,
        **status,
    }


_SQLITE_OVERVIEW_REQUIRED_TABLE_COUNT = 8
_SQLITE_OVERVIEW_TABLES_SQL = """
SELECT COUNT(*) FROM sqlite_master
WHERE type = 'table' AND name IN (
    'comms_schema_migrations',
    'requests',
    'comms_messages',
    'formal_review_jobs',
    'authority_jobs',
    'authority_dead_letters',
    'dead_letters',
    'acp_conversations'
)
"""
_SQLITE_OVERVIEW_SNAPSHOT_SQL = """
SELECT
    (SELECT CASE
        WHEN MAX(
            typeof(version) <> 'integer'
            OR typeof(name) <> 'text'
            OR typeof(checksum) <> 'text'
        ) THEN NULL
        ELSE json_group_array(json_array(version, name, checksum))
     END
     FROM (SELECT version, name, checksum FROM comms_schema_migrations ORDER BY version LIMIT ?)),
    (SELECT CASE
        WHEN MAX(typeof(k) NOT IN ('text', 'null')) THEN NULL
        ELSE json_group_array(json_array(k, c))
     END
     FROM (SELECT state AS k, COUNT(*) AS c FROM requests GROUP BY state ORDER BY state)),
    (SELECT CASE
        WHEN MAX(typeof(k) NOT IN ('text', 'null')) THEN NULL
        ELSE json_group_array(json_array(k, c))
     END
     FROM (SELECT state AS k, COUNT(*) AS c FROM authority_jobs WHERE job_kind = 'request' GROUP BY state ORDER BY state)),
    (SELECT CASE
        WHEN MAX(typeof(k) NOT IN ('text', 'null')) THEN NULL
        ELSE json_group_array(json_array(k, c))
     END
     FROM (SELECT kind AS k, COUNT(*) AS c FROM comms_messages GROUP BY kind ORDER BY kind)),
    (SELECT CASE
        WHEN MAX(typeof(k) NOT IN ('text', 'null')) THEN NULL
        ELSE json_group_array(json_array(k, c))
     END
     FROM (SELECT state AS k, COUNT(*) AS c FROM formal_review_jobs GROUP BY state ORDER BY state)),
    (SELECT CASE
        WHEN MAX(typeof(k) NOT IN ('text', 'null')) THEN NULL
        ELSE json_group_array(json_array(k, c))
     END
     FROM (SELECT state AS k, COUNT(*) AS c FROM authority_jobs GROUP BY state ORDER BY state)),
    (SELECT COUNT(*) FROM authority_dead_letters),
    (SELECT COUNT(*) FROM dead_letters),
    (SELECT COUNT(*) FROM acp_conversations),
    (SELECT CASE
        WHEN MAX(typeof(k) NOT IN ('text', 'null')) THEN NULL
        ELSE json_group_array(json_array(k, c))
     END
     FROM (SELECT state AS k, COUNT(*) AS c FROM authority_jobs WHERE updated_at >= ? GROUP BY state ORDER BY state)),
    (SELECT COUNT(*) FROM authority_jobs
     WHERE state IN ('queued', 'running') AND deadline_at IS NOT NULL AND deadline_at < ?),
    (SELECT COUNT(*) FROM authority_dead_letters
     WHERE created_at >= ? AND reason_code <> 'expired_by_cleanup')
"""


def _decode_json_counts(raw: Any) -> dict[str, int]:
    if not isinstance(raw, str) or not raw:
        raise ValueError("overview count payload is missing")
    pairs = json_depth.loads(raw, max_depth=8)
    if not isinstance(pairs, list):
        raise ValueError("overview count payload is not a JSON list")
    counts: dict[str, int] = {}
    for pair in pairs:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("overview count entry is not a key/count pair")
        key, count = pair
        if (key is not None and not isinstance(key, str)) or type(count) is not int:
            raise ValueError("overview count entry has non-canonical types")
        counts[_safe_text(key, fallback="unknown")] = count
    return counts


def _decode_json_migrations(raw: Any) -> list[tuple[int, str, str]]:
    if not isinstance(raw, str) or not raw:
        raise ValueError("overview migration payload is missing")
    entries = json_depth.loads(raw, max_depth=8)
    if not isinstance(entries, list):
        raise ValueError("overview migration payload is not a JSON list")
    decoded: list[tuple[int, str, str]] = []
    for entry in entries:
        if not isinstance(entry, list) or len(entry) != 3:
            raise ValueError("overview migration entry is not a version/name/checksum triple")
        entry_version, entry_name, entry_checksum = entry
        if (
            type(entry_version) is not int
            or not isinstance(entry_name, str)
            or not isinstance(entry_checksum, str)
        ):
            raise ValueError("overview migration entry has non-canonical types")
        decoded.append((entry_version, entry_name, entry_checksum))
    return decoded


def _empty_overview_result(status: dict[str, Any]) -> dict[str, Any]:
    return {
        "read_only": True,
        "authority": status["authority"],
        "cutover": status["cutover"],
        "health": status,
        "counts": {
            "requests": {"total": 0, "by_state": {}},
            "legacy_requests": {"total": 0, "by_state": {}},
            "messages": {"total": 0, "by_kind": {}},
            "reviews": {"total": 0, "by_state": {}},
            "dead_letters": {"total": 0},
            "acp_conversations": {"total": 0},
            "authority_jobs": {"total": 0, "by_state": {}},
        },
        "availability": "db_missing",
    }


def _assign_overview_counts(
    result: dict[str, Any],
    *,
    request_states: dict[str, int],
    request_source: str,
    legacy_request_states: dict[str, int],
    message_kinds: dict[str, int],
    review_states: dict[str, int],
    authority_job_states: dict[str, int],
    dead_letters_total: int,
    acp_conversations_total: int,
    authority_health: dict[str, Any] | None,
) -> dict[str, Any]:
    counts = {key: dict(value) for key, value in result["counts"].items()}
    counts["requests"] = {
        "total": sum(request_states.values()),
        "by_state": request_states,
        "source": request_source,
    }
    counts["legacy_requests"] = {
        "total": sum(legacy_request_states.values()),
        "by_state": legacy_request_states,
        "excluded_from_authority_health": True,
    }
    counts["messages"] = {"total": sum(message_kinds.values()), "by_kind": message_kinds}
    counts["reviews"] = {"total": sum(review_states.values()), "by_state": review_states}
    counts["authority_jobs"] = {
        "total": sum(authority_job_states.values()),
        "by_state": authority_job_states,
    }
    counts["dead_letters"]["total"] = dead_letters_total
    counts["acp_conversations"]["total"] = acp_conversations_total
    result["counts"] = counts
    result["availability"] = "available" if any(
        section["total"] for section in counts.values()
    ) else "empty"
    result["authority_health"] = authority_health
    return result


def _sqlite_overview_snapshot(plane_root: Path) -> dict[str, Any] | None:
    """Single-open, single-row SQLite overview avoiding per-row GIL handoffs."""
    if read_plane_status is not message_plane.read_plane_status:
        return None
    authority = storage.resolve_authority(storage.StoreId.FLEET_COMMS)
    if authority is storage.Authority.PG:
        return None
    try:
        storage.assert_component_supported(storage.StoreId.FLEET_COMMS, "plane_status")
    except storage.ControlPlaneError:
        return None
    db_path = plane_root / "comms.sqlite3"
    if not db_path.is_file():
        return None

    known = {migration.version: migration for migration in migrations.MIGRATIONS}
    version = max(known, default=0)
    window = _authority_health_window()

    connection = None
    close_failed = False
    try:
        connection = open_readonly(db_path, timeout=0.25)
        table_count_row = connection.execute(_SQLITE_OVERVIEW_TABLES_SQL).fetchone()
        if (
            table_count_row is None
            or int(table_count_row[0]) != _SQLITE_OVERVIEW_REQUIRED_TABLE_COUNT
        ):
            return None
        row = connection.execute(
            _SQLITE_OVERVIEW_SNAPSHOT_SQL,
            (len(known) + 1, window["since"], window["until"], window["since"]),
        ).fetchone()
        if row is None:
            return None
    except (sqlite3.Error, OSError, RuntimeError):
        return None
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                close_failed = True
                logger.warning("Fleet observer read-only connection close failed")
    if close_failed:
        return None

    if any(row[idx] is None for idx in (0, 1, 2, 3, 4, 5, 9)):
        return None
    try:
        migration_entries = _decode_json_migrations(row[0])
        legacy_request_states = _decode_json_counts(row[1])
        authority_request_states = _decode_json_counts(row[2])
        message_kinds = _decode_json_counts(row[3])
        review_states = _decode_json_counts(row[4])
        authority_job_states = _decode_json_counts(row[5])
        authority_dead_letters_total = int(row[6] or 0)
        legacy_dead_letters_total = int(row[7] or 0)
        acp_conversations_total = int(row[8] or 0)
        authority_window_states = _decode_json_counts(row[9])
        authority_overdue = int(row[10] or 0)
        authority_window_dead_letters = int(row[11] or 0)
    except (ValueError, TypeError, OverflowError, json.JSONDecodeError):
        return None

    applied_version: int | None = None
    applied_name: str | None = None
    db_error: str | None = None
    try:
        applied = {
            int(entry_version): (str(entry_name), str(entry_checksum))
            for entry_version, entry_name, entry_checksum in migration_entries
        }
        migrations._validate_applied_migrations(applied, known)
        if set(applied) != set(known) or len(migration_entries) != len(known):
            db_error = "schema_incompatible"
        else:
            applied_version = version
            applied_name = known[version].name if known else None
    except (migrations.CommsMigrationError, ValueError, TypeError, OverflowError):
        db_error = "schema_incompatible"

    try:
        raw_mode: str = message_plane.resolve_plane_mode()
    except ValueError:
        logger.warning("invalid FLEET_COMMS_MESSAGE_PLANE value; reporting mode=invalid")
        raw_mode = "invalid"

    mode = _safe_text(raw_mode, fallback="invalid")
    authority_active = mode == "authority"
    store = {
        **store_descriptor(
            kind="comms-plane",
            reachable=True,
            schema_versions={"known": version, "applied": applied_version},
        ),
        "authority": authority.value,
    }
    telemetry = message_plane._summarize_parity_telemetry(
        message_plane.default_parity_telemetry_path(plane_root),
        limit=0,
    )
    status: dict[str, Any] = {
        "mode": mode,
        "enabled": raw_mode not in {"off", "invalid"},
        "read_only": True,
        "response_schema_version": COMMS_RESPONSE_SCHEMA_VERSION,
        "store": store,
        "authority": (
            "fleet_comms_authoritative" if authority_active else "file_handoffs_authoritative"
        ),
        "cutover": "authority_active" if authority_active else "pre_flip_operator_gated",
        "schema": {
            "known_version": version,
            "applied_version": applied_version,
            "applied_name": applied_name,
            "db_exists": True,
            "authority": authority.value,
            "db_error": db_error,
            "store": store,
        },
        "parity_telemetry": {
            "store": telemetry.get("store"),
            "exists": bool(telemetry.get("exists")),
            "event_count": int(telemetry.get("event_count") or 0),
            "parity_ok_count": int(telemetry.get("parity_ok_count") or 0),
            "parity_fail_count": int(telemetry.get("parity_fail_count") or 0),
        },
    }

    if authority_active:
        request_states = authority_request_states
        request_source = "authority_jobs"
        dead_letters_total = authority_dead_letters_total
        authority_health: dict[str, Any] | None = _authority_health_payload(
            authority_window_states,
            overdue=authority_overdue,
            dead_letters=authority_window_dead_letters,
            window=window,
        )
    else:
        request_states = legacy_request_states
        request_source = "legacy_requests"
        dead_letters_total = legacy_dead_letters_total
        authority_health = None

    return _assign_overview_counts(
        _empty_overview_result(status),
        request_states=request_states,
        request_source=request_source,
        legacy_request_states=legacy_request_states,
        message_kinds=message_kinds,
        review_states=review_states,
        authority_job_states=authority_job_states,
        dead_letters_total=dead_letters_total,
        acp_conversations_total=acp_conversations_total,
        authority_health=authority_health,
    )


@router.get("/overview")
def fleet_overview(ctx: MonitorContext = Depends(get_ctx)) -> dict[str, Any]:
    """Compact durable-plane counts for the consolidated fleet dashboard."""
    resolved_ctx = resolve_context(ctx)
    plane_root = _plane_root(resolved_ctx)
    snapshot = _sqlite_overview_snapshot(plane_root)
    if snapshot is not None:
        return snapshot

    status = _safe_plane_status(resolved_ctx, plane_root=plane_root)
    result = _empty_overview_result(status)
    try:
        return _populate_fleet_overview(result, status, resolved_ctx, plane_root=plane_root)
    except _OVERVIEW_ERRORS:
        # Never expose a driver exception or leave partial counts looking complete.
        result["availability"] = "schema_read_failed"
        return result


def _populate_fleet_overview(
    result: dict[str, Any],
    status: dict[str, Any],
    ctx: MonitorContext,
    *,
    plane_root: Path | None = None,
) -> dict[str, Any]:
    with _read_connection(
        ctx,
        pg_supported=True,
        plane_root=plane_root,
    ) as (connection, availability):
        if connection is None:
            result["availability"] = availability
            return result
        legacy_request_states = _count_by_state(connection, "requests", "state")
        if status["mode"] == "authority":
            request_states = _count_by_state_where(
                connection,
                "authority_jobs",
                "state",
                where="job_kind = 'request'",
            )
            request_source = "authority_jobs"
        else:
            request_states = legacy_request_states
            request_source = "legacy_requests"
        dead_letter_table = (
            "authority_dead_letters" if status["mode"] == "authority" else "dead_letters"
        )
        dead_letters_total = (
            int(connection.execute(f"SELECT COUNT(*) FROM {dead_letter_table}").fetchone()[0])
            if _table_exists(connection, dead_letter_table)
            else 0
        )
        acp_conversations_total = (
            int(connection.execute("SELECT COUNT(*) FROM acp_conversations").fetchone()[0])
            if _table_exists(connection, "acp_conversations")
            else 0
        )
        return _assign_overview_counts(
            result,
            request_states=request_states,
            request_source=request_source,
            legacy_request_states=legacy_request_states,
            message_kinds=_count_by_state(connection, "comms_messages", "kind"),
            review_states=_count_by_state(connection, "formal_review_jobs", "state"),
            authority_job_states=_count_by_state(connection, "authority_jobs", "state"),
            dead_letters_total=dead_letters_total,
            acp_conversations_total=acp_conversations_total,
            authority_health=(
                _authority_health_snapshot(connection)
                if status["mode"] == "authority"
                else None
            ),
        )


def _registered_endpoints(connection: SQLiteConnection | None) -> dict[str, dict[str, Any]]:
    if connection is None or not _table_exists(connection, "agent_endpoints"):
        return {}
    try:
        rows = connection.execute(
            """
            SELECT canonical_name, registry_version, state, successor, created_at
            FROM agent_endpoints
            ORDER BY canonical_name ASC
            """
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {
        _safe_text(row["canonical_name"]): {
            "registry_version": row["registry_version"],
            "state": _safe_text(row["state"]),
            "successor": _safe_text(row["successor"], fallback="") or None,
            "created_at": row["created_at"],
        }
        for row in rows
    }


@router.get("/agents")
@router.get("/endpoints")
def fleet_agents(
    agent: str | None = Query(default=None),
    state: str | None = Query(default=None),
    ctx: MonitorContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Configured and durable endpoint inventory with configuration secrets omitted."""
    with _read_connection(ctx) as (connection, availability):
        registered = _registered_endpoints(connection)
    try:
        registry = load_endpoint_registry()
    except (OSError, ValueError):
        logger.warning("Fleet observer could not load the endpoint registry")
        registry = None

    endpoints: list[dict[str, Any]] = []
    if registry is not None:
        for endpoint in registry.endpoints:
            durable = registered.pop(endpoint.name, None)
            item = {
                "agent": endpoint.name,
                "aliases": list(endpoint.aliases),
                "state": durable["state"] if durable else endpoint.state,
                "successor": durable["successor"] if durable else endpoint.successor,
                "registry_version": registry.version,
                "transports": list(endpoint.transports),
                "completion_evidence": list(endpoint.completion_evidence),
                "default_ttl_seconds": endpoint.default_ttl_seconds,
                "concurrency_limit": endpoint.concurrency_limit,
                "formal_review_eligible": endpoint.formal_review_eligible,
                "durable_registration": durable is not None,
                "created_at": durable["created_at"] if durable else None,
                "read_only": True,
            }
            endpoints.append(item)
    for name, durable in registered.items():
        endpoints.append(
            {
                "agent": name,
                "aliases": [],
                "state": durable["state"],
                "successor": durable["successor"],
                "registry_version": durable["registry_version"],
                "transports": [],
                "completion_evidence": [],
                "default_ttl_seconds": None,
                "concurrency_limit": None,
                "formal_review_eligible": False,
                "durable_registration": True,
                "created_at": durable["created_at"],
                "read_only": True,
            }
        )
    if agent is not None:
        endpoints = [
            item for item in endpoints if agent == item["agent"] or agent in item["aliases"]
        ]
    if state is not None:
        endpoints = [item for item in endpoints if state == item["state"]]
    endpoints.sort(key=lambda item: str(item["agent"]))
    return {
        "read_only": True,
        "availability": availability if registry is None else "available",
        "registry_version": registry.version if registry is not None else None,
        "endpoints": endpoints,
        "total": len(endpoints),
        "filters": {"agent": agent, "state": state},
        "configuration_policy": "configuration_json_omitted",
    }


@dataclass(frozen=True)
class _FleetSeat:
    seat: str
    kind: str
    epic: str | None
    recipients: tuple[str, ...]


def _fleet_seat_roster() -> tuple[list[_FleetSeat], str, str]:
    seats: list[_FleetSeat] = []
    driver_roster = "available"
    try:
        lanes = driver_lanes.load_registry().lanes
    except (OSError, ValueError, RuntimeError):
        lanes = ()
        driver_roster = "unavailable"
    for lane in lanes:
        cto = lane.name == "cto"
        recipients = (
            (DURABLE_CTO_RECIPIENT, lane.sender)
            if cto and lane.sender != DURABLE_CTO_RECIPIENT
            else (lane.sender,)
        )
        seats.append(_FleetSeat(lane.name, "cto" if cto else "driver", lane.epic, recipients))

    external: tuple[str, ...]
    external_roster: str
    try:
        assignments_path = agent_identity.default_assignments_path()
    except (OSError, ValueError, RuntimeError):
        external, external_roster = (), "unavailable"
    else:
        external, external_roster = load_external_recipients(assignments_path)
    lane_names = {name for seat in seats for name in (seat.seat, *seat.recipients)}
    if lane_names.intersection(external):
        return seats, driver_roster, "unavailable"
    seats.extend(_FleetSeat(name, "external", None, (name,)) for name in external)
    return seats, driver_roster, external_roster


def _seat_streams(seats: list[_FleetSeat], ctx: MonitorContext) -> dict[str, str]:
    try:
        bindings = load_launcher_bindings()
    except (OSError, ValueError, RuntimeError):
        return {}
    streams = {}
    for seat in seats:
        if seat.epic is None:
            continue
        stream, _lane = bindings.get(seat.epic, (seat.epic, ""))
        try:
            stream_id = stream_anchor_id(stream, ctx.roots.state_root)
        except (OSError, ValueError, RuntimeError):
            continue
        if STREAM_ID_RE.fullmatch(stream_id):
            streams[seat.seat] = stream_id
    return streams


def _panel_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        return parse_timestamp(value)
    except ValueError:
        return None


def _fleet_panel(payload: dict[str, Any], *, unavailable: bool) -> JSONResponse:
    headers = panel_values.snapshot_headers(None if unavailable else 0.0, FACADE_TTL_S)
    return JSONResponse(payload, headers=headers)


@router.get("/seats")
def fleet_seats(ctx: MonitorContext = Depends(get_ctx)) -> JSONResponse:
    """Project exact recipient backlog and live lease signals for the configured seats."""
    seats, driver_roster, external_roster = _fleet_seat_roster()
    roster_available = driver_roster == external_roster == "available"
    seat_recipients = {seat.seat: seat.recipients for seat in seats}
    mail = {}
    try:
        with _read_connection(ctx, pg_supported=False) as (connection, availability):
            if connection is not None:
                mail = {
                    row["seat"]: row
                    for row in connection.execute(
                        """
                        SELECT seat.key AS seat,
                               COUNT(*) FILTER (WHERE d.state = 'queued') AS queued,
                               MIN(d.created_at) FILTER (WHERE d.state = 'queued') AS oldest,
                               MAX(d.completed_at) FILTER (
                                   WHERE d.state = 'acknowledged' AND m.imported_source IS NULL
                               ) AS last_ack
                        FROM json_each(?) AS seat
                        CROSS JOIN json_each(seat.value) AS recipient
                        JOIN authority_deliveries AS d ON d.recipient = recipient.value
                        LEFT JOIN authority_message_metadata AS m
                            ON m.message_id = d.message_id AND d.state = 'acknowledged'
                        WHERE d.state IN ('queued', 'acknowledged')
                        GROUP BY seat.key
                        """,
                        (json.dumps(seat_recipients),),
                    )
                }
    except PlaneRootAnchorError:
        availability = "plane_root_unavailable"
    except (sqlite3.Error, storage.ControlPlaneError, OSError, ValueError):
        availability = "db_unavailable"

    now = datetime.now(UTC)
    leases, lease_availability = fleet_workers_collect._read_driver_leases(
        db_path=ctx.roots.session_streams_db_path, now=now,
    )
    lease_by_stream = {lease["stream_id"]: lease for lease in leases} if lease_availability == "available" else {}
    stream_by_seat = _seat_streams(seats, ctx)
    items = []
    for seat in seats:
        stream_id = stream_by_seat.get(seat.seat)
        lease = lease_by_stream.get(stream_id)
        lease_state, lease_reason = "unknown", None
        if seat.kind != "external":
            if stream_id is None:
                lease_reason = "stream_unresolved"
            elif lease_availability != "available":
                lease_reason = lease_availability
            else:
                lease_state = "live" if lease is not None else "no_lease"

        stats = mail.get(seat.seat)
        ack_at = _panel_datetime(stats["last_ack"]) if stats is not None else None
        lease_at = _panel_datetime(lease["heartbeat_at"]) if lease is not None else None
        last_activity_at, source = ack_at, "ack" if ack_at is not None else None
        if lease_at is not None and (ack_at is None or lease_at > ack_at):
            last_activity_at, source = lease_at, "lease"
        queued = None
        if availability == "available":
            queued = stats["queued"] if stats is not None else 0
        items.append({
            "seat": seat.seat,
            "kind": seat.kind,
            "epic": seat.epic,
            "queued": queued,
            "oldest_queued_at": panel_values.timestamp(stats["oldest"]) if stats is not None else None,
            "last_activity_at": panel_values.timestamp(last_activity_at.isoformat()) if last_activity_at else None,
            "last_activity_source": source,
            "lease_state": lease_state,
            "lease_reason": lease_reason,
        })
    return _fleet_panel(
        {
            "read_only": True,
            "availability": availability,
            "driver_roster": driver_roster,
            "external_roster": external_roster,
            "lease_availability": lease_availability,
            "seats": items,
            "total": len(items) if availability == "available" and roster_available else None,
        },
        unavailable=not roster_available or availability != "available" or lease_availability != "available",
    )


_MESSAGE_MAX_SENDER = 256


def _normalize_sender_alias(name: str) -> str:
    return " ".join(name.split())


def _seat_ids_map(seats: list[_FleetSeat]) -> tuple[dict[str, str], frozenset[str]]:
    recipient_counts = Counter(name for item in seats for name in item.recipients)
    seat_ids = {
        name: item.seat
        for item in seats
        for name in item.recipients
        if recipient_counts[name] == 1
    }
    normalized_owners: dict[str, set[str]] = {}
    for item in seats:
        for name in item.recipients:
            normalized = _normalize_sender_alias(name)
            if normalized:
                normalized_owners.setdefault(normalized, set()).add(item.seat)
    ambiguous = frozenset(name for name, owners in normalized_owners.items() if len(owners) > 1)
    return seat_ids, ambiguous


def _projected_sender(
    sender: object,
    seat_ids: dict[str, str],
    ambiguous: frozenset[str] = frozenset(),
) -> str:
    if not isinstance(sender, str) or len(sender.encode("utf-8")) > _MESSAGE_MAX_SENDER:
        return "other"
    normalized = _normalize_sender_alias(sender)
    if not normalized or normalized in ambiguous:
        return "other"
    return "cto" if is_session_cto_recipient(normalized) else seat_ids.get(sender, "other")


def _delivery_item(
    row: sqlite3.Row,
    seat_ids: dict[str, str],
    ambiguous: frozenset[str],
    now: datetime,
) -> dict[str, Any]:
    message_id = row["message_id"]
    created = _panel_datetime(row["created_at"])
    completed = _panel_datetime(row["completed_at"])
    end = now if row["completed_at"] is None else completed
    return {
        "message_id": message_id if isinstance(message_id, str) and AUTHORITY_MESSAGE_ID_RE.fullmatch(message_id) else None,
        "sender": _projected_sender(row["sender"], seat_ids, ambiguous),
        "recipient": seat_ids.get(row["recipient"], "other"),
        "kind": panel_values.choice(row["kind"], _DELIVERY_KINDS, "other"),
        "state": panel_values.choice(row["state"], _DELIVERY_STATES, "other"),
        "attempt_count": panel_values.number(row["attempt_count"]),
        "created_at": panel_values.timestamp(row["created_at"]),
        "completed_at": panel_values.timestamp(row["completed_at"]),
        "waited_s": (end - created).total_seconds() if created is not None and end is not None else None,
    }


@router.get("/deliveries")
def fleet_deliveries(
    seat: str | None = Query(default=None),
    state: str | None = Query(default=None),
    since: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0, le=2_147_483_647),
    ctx: MonitorContext = Depends(get_ctx),
) -> JSONResponse:
    """Project a page of delivery records with fixed columns and labels."""
    seats, driver_roster, external_roster = _fleet_seat_roster()
    roster_available = driver_roster == external_roster == "available"
    selected = next((item for item in seats if item.seat == seat), None)
    if seat is not None and selected is None:
        raise HTTPException(status_code=422, detail="unknown_seat")
    if state is not None and state not in _DELIVERY_STATES:
        raise HTTPException(status_code=422, detail="unknown_state")
    if since is None:
        since = "all" if state == "queued" else "7d"
    elif since != "all" and since not in _DELIVERY_WINDOWS:
        raise HTTPException(status_code=422, detail="unknown_since")

    all_recipients = sorted({name for item in seats for name in item.recipients})
    seat_ids, ambiguous = _seat_ids_map(seats)
    now = datetime.now(UTC)
    clauses = ["d.recipient IN (SELECT value FROM json_each(?))"]
    params = [json.dumps(selected.recipients if selected is not None else all_recipients)]
    if state is not None:
        clauses.append("d.state = ?")
        params.append(state)
    if since != "all":
        clauses.append("d.created_at >= ?")
        params.append(isoformat_z(now - _DELIVERY_WINDOWS[since]))
    where_sql = " WHERE " + " AND ".join(clauses)

    total = None
    items: list[dict[str, Any]] = []
    try:
        with _read_connection(ctx, pg_supported=False) as (connection, availability):
            if connection is not None and selected is None and not roster_available:
                availability = "roster_unavailable"
            elif connection is not None:
                connection.execute("BEGIN")
                rows = connection.execute(
                    """
                    SELECT d.message_id, d.recipient, d.state, d.attempt_count,
                           d.created_at, d.completed_at,
                           CASE WHEN length(CAST(m.sender AS BLOB)) <= ? THEN m.sender END AS sender,
                           m.kind
                    FROM authority_deliveries AS d
                    LEFT JOIN comms_messages AS m ON m.message_id = d.message_id
                    """
                    + where_sql
                    + " ORDER BY d.created_at DESC, d.message_id, d.recipient LIMIT ? OFFSET ?",
                    (_MESSAGE_MAX_SENDER, *params, limit, offset),
                ).fetchall()
                count = connection.execute(
                    "SELECT COUNT(*) FROM authority_deliveries AS d" + where_sql, params,
                ).fetchone()[0]
                items = [_delivery_item(row, seat_ids, ambiguous, now) for row in rows]
                total = count
    except PlaneRootAnchorError:
        availability = "plane_root_unavailable"
    except (sqlite3.Error, storage.ControlPlaneError, OSError, ValueError):
        availability = "db_unavailable"

    payload = _collection(
        "deliveries",
        items,
        total=total,
        limit=limit,
        offset=offset,
        availability=availability,
        filters={"seat": selected.seat if selected is not None else None, "state": state, "since": since},
    )
    payload.update(driver_roster=driver_roster, external_roster=external_roster)
    return _fleet_panel(payload, unavailable=not roster_available or availability != "available")


def _request_item(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    metadata = _safe_metadata(data.get("invocation_spec_json"))
    authority_provenance = _safe_metadata(data.get("authority_provenance_json"))
    metadata = {**metadata, **authority_provenance}
    provenance = _provenance(
        metadata=metadata,
        source=authority_provenance.get("source") or data.get("conversation_source"),
        agent=data.get("sender") or data.get("resolved_recipient"),
    )
    return {
        "request_id": data["request_id"],
        "request_message_id": data["request_message_id"],
        "conversation_id": data.get("conversation_id"),
        "requested_recipient": data["requested_recipient"],
        "resolved_recipient": data["resolved_recipient"],
        "state": data["state"],
        "completion_state": data["completion_state"],
        "expires_at": data["expires_at"],
        "created_at": data["created_at"],
        "updated_at": data["updated_at"],
        "metadata": metadata,
        "read_only": True,
        **provenance,
    }


@router.get("/requests")
def fleet_requests(
    kind: str | None = Query(default=None),
    state: str | None = Query(default=None),
    agent: str | None = Query(default=None),
    source: str | None = Query(default=None),
    conversation: str | None = Query(default=None),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    ctx: MonitorContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Queued, in-flight, and terminal request projection without request bodies."""
    since_value = _normalize_time(since, "since")
    until_value = _normalize_time(until, "until")
    filters = {
        "kind": kind,
        "state": state,
        "agent": agent,
        "source": source,
        "conversation": conversation,
        "since": since_value,
        "until": until_value,
    }
    with _read_connection(ctx) as (connection, availability):
        if connection is None:
            return _empty_collection(
                "requests", limit=limit, offset=offset, availability=availability, filters=filters
            )
        if not _table_exists(connection, "requests"):
            return _empty_collection(
                "requests", limit=limit, offset=offset, availability="table_missing", filters=filters
            )
        has_messages = _table_exists(connection, "comms_messages")
        has_conversations = _table_exists(connection, "conversations")
        has_authority_metadata = _table_exists(connection, "authority_message_metadata")
        select_extras = [
            "NULL AS conversation_id",
            "NULL AS sender",
            "NULL AS conversation_source",
            "NULL AS authority_provenance_json",
        ]
        from_sql = " FROM requests AS request"
        if has_messages:
            select_extras[:2] = [
                "message.conversation_id AS conversation_id",
                "message.sender AS sender",
            ]
            from_sql += " LEFT JOIN comms_messages AS message ON message.message_id = request.request_message_id"
        if has_messages and has_conversations:
            select_extras[2] = "conversation.source AS conversation_source"
            from_sql += " LEFT JOIN conversations AS conversation ON conversation.conversation_id = message.conversation_id"
        if has_messages and has_authority_metadata:
            select_extras[3] = "authority_meta.provenance_json AS authority_provenance_json"
            from_sql += (
                " LEFT JOIN authority_message_metadata AS authority_meta"
                " ON authority_meta.message_id = message.message_id"
            )
        clauses: list[str] = []
        params: list[Any] = []
        if state is not None:
            clauses.append("request.state = ?")
            params.append(state)
        if kind is not None:
            if has_messages:
                clauses.append("message.kind = ?")
                params.append(kind)
            elif kind != "request":
                clauses.append("1 = 0")
        if agent is not None:
            agent_clause = "request.requested_recipient = ? OR request.resolved_recipient = ?"
            params.extend([agent, agent])
            if has_messages:
                agent_clause += " OR message.sender = ? OR message.recipient = ?"
                params.extend([agent, agent])
            if has_messages and has_authority_metadata:
                agent_clause += " OR json_extract(authority_meta.provenance_json, '$.Agent') = ?"
                params.append(agent)
            clauses.append(f"({agent_clause})")
        if source is not None:
            if has_messages and has_conversations:
                source_expression = "conversation.source"
                if has_authority_metadata:
                    source_expression = (
                        "COALESCE(json_extract(authority_meta.provenance_json, '$.Source'), "
                        f"{source_expression})"
                    )
                clauses.append(f"{source_expression} = ?")
                params.append(source)
            else:
                clauses.append("1 = 0")
        if conversation is not None:
            if has_messages:
                clauses.append("message.conversation_id = ?")
                params.append(conversation)
            else:
                clauses.append("1 = 0")
        _time_clauses(
            "request.created_at",
            since=since_value,
            until=until_value,
            clauses=clauses,
            params=params,
        )
        return _paged_query(
            connection,
            key="requests",
            select_sql=(
                "SELECT request.request_id, request.request_message_id, request.requested_recipient, "
                "request.resolved_recipient, request.state, request.expires_at, request.completion_state, "
                "request.invocation_spec_json, request.created_at, request.updated_at, "
                + ", ".join(select_extras)
            ),
            from_sql=from_sql,
            clauses=clauses,
            params=params,
            order_sql="request.created_at DESC, request.request_id ASC",
            limit=limit,
            offset=offset,
            filters=filters,
            transform=_request_item,
        )


def _authority_job_item(row: sqlite3.Row) -> dict[str, Any]:
    """Project a durable authority job without its payload or result artifacts."""
    data = dict(row)
    metadata = _safe_metadata(data.get("authority_provenance_json"))
    job_kind = _safe_text(data.get("job_kind"), fallback="unknown")
    default_source = {
        "request": "authority",
        "discussion": "authority-discussion",
        "formal_review": "formal-review",
    }.get(job_kind, "authority")
    default_agent = "review-gate" if job_kind == "formal_review" else "authority-service"
    conversation_id = data.get("conversation_id")
    if conversation_id is None and job_kind == "discussion":
        conversation_id = data.get("subject_id")
    provenance = _provenance(
        metadata=metadata,
        source=metadata.get("source") or data.get("conversation_source") or default_source,
        agent=data.get("message_sender") or default_agent,
        via="queue",
    )
    failure = _safe_authority_failure(data.get("terminal_event_metadata_json"))
    return {
        "job_id": data["job_id"],
        "kind": job_kind,
        "subject_id": data["subject_id"],
        "conversation_id": conversation_id,
        "review_id": data.get("review_id"),
        "pr_number": data.get("pr_number"),
        "state": data["state"],
        "deadline_at": data.get("deadline_at"),
        "attempt_count": int(data.get("attempt_count") or 0),
        "task_id": metadata.get("task_id"),
        "failure": failure,
        "created_at": data["created_at"],
        "updated_at": data["updated_at"],
        "completed_at": data.get("completed_at"),
        "payload_content": "omitted",
        "result_content": "omitted",
        "read_only": True,
        **provenance,
    }


def _safe_authority_failure(value: Any) -> dict[str, Any] | None:
    """Project only the closed body-free failure record from a terminal event."""
    if not isinstance(value, str) or not value:
        return None
    try:
        event_metadata = json_depth.loads(value, max_depth=512)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(event_metadata, dict):
        return None
    failure = event_metadata.get("failure")
    if not isinstance(failure, dict) or set(failure) != {"phase", "code", "retryable"}:
        return None
    phase = failure.get("phase")
    code = failure.get("code")
    retryable = failure.get("retryable")
    if (
        phase not in _SAFE_FAILURE_PHASES
        or code not in _SAFE_FAILURE_CODES
        or not isinstance(retryable, bool)
    ):
        return None
    return {"phase": phase, "code": code, "retryable": retryable}


@router.get("/authority/jobs")
def fleet_authority_jobs(
    kind: str | None = Query(default=None),
    state: str | None = Query(default=None),
    agent: str | None = Query(default=None),
    source: str | None = Query(default=None),
    pr: int | None = Query(default=None, ge=1),
    conversation: str | None = Query(default=None),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    ctx: MonitorContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Read-only authority queue projection; payload and result blobs remain sealed."""
    since_value = _normalize_time(since, "since")
    until_value = _normalize_time(until, "until")
    filters = {
        "kind": kind,
        "state": state,
        "agent": agent,
        "source": source,
        "pr": pr,
        "conversation": conversation,
        "since": since_value,
        "until": until_value,
    }
    with _read_connection(ctx) as (connection, availability):
        if connection is None:
            return _empty_collection(
                "jobs", limit=limit, offset=offset, availability=availability, filters=filters
            )
        if not _table_exists(connection, "authority_jobs"):
            return _empty_collection(
                "jobs", limit=limit, offset=offset, availability="table_missing", filters=filters
            )
        has_messages = _table_exists(connection, "comms_messages")
        has_metadata = _table_exists(connection, "authority_message_metadata")
        has_conversations = _table_exists(connection, "conversations")
        has_reviews = _table_exists(connection, "formal_review_jobs")
        has_job_events = _table_exists(connection, "authority_job_events")
        from_sql = " FROM authority_jobs AS job"
        message_sender_select = "NULL AS message_sender"
        conversation_select = "NULL AS conversation_id"
        conversation_source_select = "NULL AS conversation_source"
        provenance_select = "NULL AS authority_provenance_json"
        review_select = "NULL AS review_id, NULL AS pr_number"
        terminal_metadata_select = "NULL AS terminal_event_metadata_json"
        if has_messages:
            from_sql += (
                " LEFT JOIN comms_messages AS message"
                " ON (job.job_kind = 'request' AND message.message_id = job.subject_id)"
                " OR (job.job_kind = 'discussion'"
                " AND message.conversation_id = job.subject_id"
                " AND message.kind = 'discussion')"
            )
            message_sender_select = "message.sender AS message_sender"
            conversation_select = "message.conversation_id AS conversation_id"
        if has_metadata and has_messages:
            from_sql += (
                " LEFT JOIN authority_message_metadata AS authority_meta"
                " ON authority_meta.message_id = message.message_id"
            )
            provenance_select = "authority_meta.provenance_json AS authority_provenance_json"
        if has_conversations:
            conversation_join = "(job.job_kind = 'discussion' AND conversation.conversation_id = job.subject_id)"
            if has_messages:
                conversation_join = (
                    "conversation.conversation_id = message.conversation_id OR "
                    "(job.job_kind = 'discussion' AND conversation.conversation_id = job.subject_id)"
                )
            from_sql += (
                " LEFT JOIN conversations AS conversation ON " + conversation_join
            )
            conversation_select = "conversation.conversation_id AS conversation_id"
            if has_messages:
                conversation_select = (
                    "COALESCE(message.conversation_id, conversation.conversation_id) AS conversation_id"
                )
            conversation_source_select = "conversation.source AS conversation_source"
        if has_reviews:
            from_sql += (
                " LEFT JOIN formal_review_jobs AS review"
                " ON job.job_kind = 'formal_review' AND review.review_id = job.subject_id"
            )
            review_select = "review.review_id AS review_id, review.pr_number AS pr_number"
        if has_job_events:
            terminal_metadata_select = (
                "(SELECT event.metadata_json FROM authority_job_events AS event "
                "WHERE event.job_id = job.job_id AND event.event_type = 'finished' "
                "ORDER BY event.created_at DESC, event.rowid DESC LIMIT 1) "
                "AS terminal_event_metadata_json"
            )
        clauses: list[str] = []
        params: list[Any] = []
        if kind is not None:
            clauses.append("job.job_kind = ?")
            params.append(kind)
        if state is not None:
            clauses.append("job.state = ?")
            params.append(state)
        if agent is not None:
            default_agent = (
                "CASE WHEN job.job_kind = 'formal_review' THEN 'review-gate' "
                "ELSE 'authority-service' END"
            )
            agent_expression = default_agent
            if has_messages:
                agent_expression = f"COALESCE(message.sender, {default_agent})"
            if has_metadata and has_messages:
                agent_expression = (
                    "COALESCE(json_extract(authority_meta.provenance_json, '$.Agent'), "
                    f"{agent_expression})"
                )
            agent_clause = f"{agent_expression} = ?"
            params.append(agent)
            if has_messages:
                agent_clause += " OR message.recipient = ?"
                params.append(agent)
            clauses.append(f"({agent_clause})")
        if source is not None:
            default_source = (
                "CASE WHEN job.job_kind = 'formal_review' THEN 'formal-review' "
                "WHEN job.job_kind = 'discussion' THEN 'authority-discussion' ELSE 'authority' END"
            )
            source_expression = default_source
            if has_conversations:
                source_expression = f"COALESCE(conversation.source, {default_source})"
            if has_metadata and has_messages:
                source_expression = (
                    "COALESCE(json_extract(authority_meta.provenance_json, '$.Source'), "
                    f"{source_expression})"
                )
            clauses.append(f"{source_expression} = ?")
            params.append(source)
        if pr is not None:
            if has_reviews:
                clauses.append("review.pr_number = ?")
                params.append(pr)
            else:
                clauses.append("1 = 0")
        if conversation is not None:
            if has_conversations:
                conversation_expression = "conversation.conversation_id"
                if has_messages:
                    conversation_expression = "COALESCE(message.conversation_id, conversation.conversation_id)"
                clauses.append(f"{conversation_expression} = ?")
                params.append(conversation)
            elif has_messages:
                clauses.append("message.conversation_id = ?")
                params.append(conversation)
            else:
                clauses.append("1 = 0")
        _time_clauses(
            "job.created_at",
            since=since_value,
            until=until_value,
            clauses=clauses,
            params=params,
        )
        return _paged_query(
            connection,
            key="jobs",
            select_sql=(
                "SELECT job.job_id, job.job_kind, job.subject_id, job.state, job.deadline_at, "
                "job.attempt_count, job.created_at, job.updated_at, job.completed_at, "
                f"{message_sender_select}, {conversation_select}, {conversation_source_select}, "
                f"{provenance_select}, {review_select}, {terminal_metadata_select}"
            ),
            from_sql=from_sql,
            clauses=clauses,
            params=params,
            order_sql="job.created_at DESC, job.job_id ASC",
            limit=limit,
            offset=offset,
            filters=filters,
            transform=_authority_job_item,
        )









def _review_item(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    return {
        "review_id": data["review_id"],
        "repository": data["repository"],
        "pr_number": data["pr_number"],
        "head_sha": data["head_sha"],
        "gate_kind": data["gate_kind"],
        "state": data["state"],
        "created_at": data["created_at"],
        "attempt_count": int(data.get("attempt_count") or 0),
        "latest_attempt_state": data.get("latest_attempt_state"),
        "publication_count": int(data.get("publication_count") or 0),
        "sealed_verdict_available": bool(data.get("sealed_verdict_artifact_id")),
        "source": "formal-review",
        "agent": "review-gate",
        "via": "fleet-comms",
        "read_only": True,
    }


def fleet_reviews(
    kind: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    source: Annotated[str | None, Query()] = None,
    repository: Annotated[
        str | None,
        Query(
            description="Exact repository identity (owner/name). Applied in SQL before count/pagination.",
        ),
    ] = None,
    pr: Annotated[int | None, Query(ge=1)] = None,
    since: Annotated[str | None, Query()] = None,
    until: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
    ctx: MonitorContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Formal-review jobs, attempts, and publication counts without sealed blobs.

    Optional ``repository`` is an exact match on ``formal_review_jobs.repository``
    and participates in COUNT / LIMIT / OFFSET so foreign rows cannot consume the
    page budget of a repository-scoped consumer (e.g. public Work projection).
    """
    ctx = resolve_context(ctx)
    since_value = _normalize_time(since, "since")
    until_value = _normalize_time(until, "until")
    filters = {
        "kind": kind,
        "state": state,
        "source": source,
        "repository": repository,
        "pr": pr,
        "since": since_value,
        "until": until_value,
    }
    with _read_connection(ctx) as (connection, availability):
        if connection is None:
            return _empty_collection(
                "reviews", limit=limit, offset=offset, availability=availability, filters=filters
            )
        if not _table_exists(connection, "formal_review_jobs"):
            return _empty_collection(
                "reviews", limit=limit, offset=offset, availability="table_missing", filters=filters
            )
        has_attempts = _table_exists(connection, "formal_review_attempts")
        has_publications = _table_exists(connection, "github_publications")
        attempt_count = "0 AS attempt_count"
        latest_attempt = "NULL AS latest_attempt_state"
        publication_count = "0 AS publication_count"
        if has_attempts:
            attempt_count = (
                "(SELECT COUNT(*) FROM formal_review_attempts AS attempt "
                "WHERE attempt.review_id = review.review_id) AS attempt_count"
            )
            latest_attempt = (
                "(SELECT completion_state FROM formal_review_attempts AS latest_attempt "
                "WHERE latest_attempt.review_id = review.review_id "
                "ORDER BY latest_attempt.attempt_number DESC LIMIT 1) AS latest_attempt_state"
            )
        if has_publications:
            publication_count = (
                "(SELECT COUNT(*) FROM github_publications AS publication "
                "WHERE publication.review_id = review.review_id) AS publication_count"
            )
        clauses: list[str] = []
        params: list[Any] = []
        if kind is not None:
            clauses.append("review.gate_kind = ?")
            params.append(kind)
        if state is not None:
            clauses.append("review.state = ?")
            params.append(state)
        if source is not None and source != "formal-review":
            clauses.append("1 = 0")
        if repository is not None:
            # Exact match only — no LIKE / suffix / casefold — privacy boundary.
            clauses.append("review.repository = ?")
            params.append(repository)
        if pr is not None:
            clauses.append("review.pr_number = ?")
            params.append(pr)
        _time_clauses(
            "review.created_at",
            since=since_value,
            until=until_value,
            clauses=clauses,
            params=params,
        )
        return _paged_query(
            connection,
            key="reviews",
            select_sql=(
                "SELECT review.review_id, review.repository, review.pr_number, review.head_sha, review.gate_kind, "
                "review.state, review.sealed_verdict_artifact_id, review.created_at, "
                f"{attempt_count}, {latest_attempt}, {publication_count}"
            ),
            from_sql=" FROM formal_review_jobs AS review",
            clauses=clauses,
            params=params,
            order_sql="review.created_at DESC, review.review_id ASC",
            limit=limit,
            offset=offset,
            filters=filters,
            transform=_review_item,
        )





@router.get("/migrations")
def fleet_migrations(ctx: MonitorContext = Depends(get_ctx)) -> dict[str, Any]:
    """Schema migration status from the existing plane, without applying a migration."""
    known = [{"version": item.version, "name": item.name} for item in MIGRATIONS]
    with _read_connection(ctx) as (connection, availability):
        if connection is None:
            return {
                "read_only": True,
                "availability": availability,
                "known_migrations": known,
                "migrations": [],
                "applied_version": None,
            }
        if not _table_exists(connection, "comms_schema_migrations"):
            return {
                "read_only": True,
                "availability": "table_missing",
                "known_migrations": known,
                "migrations": [],
                "applied_version": None,
            }
        try:
            rows = connection.execute(
                "SELECT version, name, applied_at FROM comms_schema_migrations ORDER BY version ASC"
            ).fetchall()
        except sqlite3.Error:
            rows = []
            availability = "db_unavailable"
    migrations = [
        {"version": int(row["version"]), "name": row["name"], "applied_at": row["applied_at"]}
        for row in rows
    ]
    return {
        "read_only": True,
        "availability": "available" if migrations else availability,
        "known_migrations": known,
        "migrations": migrations,
        "applied_version": migrations[-1]["version"] if migrations else None,
    }


def _acp_item(item: dict[str, Any]) -> dict[str, Any]:
    participants = item.get("participants") if isinstance(item.get("participants"), list) else []
    agent = " / ".join(_safe_text(value, fallback="") for value in participants if value) or "unknown"
    return {
        **item,
        "source": "acpx",
        "agent": agent,
        "via": "fleet-comms",
        "read_only": True,
    }


@router.get("/acp/conversations")
@swr_route("fleet-acp-conversations", FACADE_TTL_S)
def fleet_acp_conversations(
    state: str | None = Query(default=None),
    agent: str | None = Query(default=None),
    source: str | None = Query(default=None),
    conversation: str | None = Query(default=None),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    ctx: MonitorContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Reuse the ACP read model for body-free conversation and round observability."""
    since_value = _normalize_time(since, "since")
    until_value = _normalize_time(until, "until")
    filters = {
        "state": state,
        "agent": agent,
        "source": source,
        "conversation": conversation,
        "since": since_value,
        "until": until_value,
    }
    payload = list_acp_conversations(limit=MAX_ACP_SCAN, ctx=ctx)
    availability = _safe_text(payload.get("availability"), fallback="unavailable")
    records = payload.get("conversations") if isinstance(payload.get("conversations"), list) else []
    filtered: list[dict[str, Any]] = []
    for raw in records:
        if not isinstance(raw, dict):
            continue
        record = _acp_item(raw)
        created_at = record.get("created_at")
        if state is not None and state not in {record.get("classification"), record.get("current_state")}:
            continue
        if agent is not None and agent not in record.get("participants", []):
            continue
        if source is not None and source != "acpx":
            continue
        if conversation is not None and conversation != record.get("conversation_id"):
            continue
        if since_value is not None and (not isinstance(created_at, str) or created_at < since_value):
            continue
        if until_value is not None and (not isinstance(created_at, str) or created_at > until_value):
            continue
        filtered.append(record)
    return _collection(
        "conversations",
        filtered[offset : offset + limit],
        total=len(filtered),
        limit=limit,
        offset=offset,
        availability=availability,
        filters=filters,
    )


@router.get("/acp/conversations/{conversation_id}")
def fleet_acp_conversation_detail(
    conversation_id: str,
    ctx: MonitorContext = Depends(get_ctx),
) -> JSONResponse:
    """Reuse the existing body-free ACP event timeline for one conversation."""
    record = get_acp_conversation(conversation_id, ctx=ctx)
    if record is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return JSONResponse(
        content={
            "read_only": True,
            "body_policy": "events_only_no_transcript_or_artifact_content",
            "conversation": _acp_item(record),
        },
        headers={"Cache-Control": "no-store"},
    )


_MESSAGE_MAX_BODY_BYTES = 256 * 1024
_MESSAGE_MAX_RESPONSE_BYTES = 512 * 1024
_MESSAGE_MAX_ID = 128
_MESSAGE_MAX_SCOPE = 512
_MESSAGE_READ_SLOTS = BoundedSemaphore(4)
_VISIBLE_MESSAGE_FROM = """
    FROM authority_message_metadata AS meta
    JOIN comms_messages AS m ON m.message_id = meta.message_id
    JOIN conversations AS c
        ON c.conversation_id = m.conversation_id AND c.source IN ('authority', 'authority-import')
    WHERE EXISTS (
        SELECT 1 FROM authority_deliveries AS d
        WHERE d.message_id = m.message_id AND d.recipient IN (SELECT value FROM json_each(?))
    ) AND NOT EXISTS (
        SELECT 1 FROM acp_conversations AS acp WHERE acp.conversation_id = m.conversation_id
    ) AND (meta.channel_id IS NULL OR EXISTS (
        SELECT 1 FROM authority_channels AS ch WHERE ch.channel_id = meta.channel_id
    ))
"""


def _message_response(status: int, **payload: Any) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"read_only": True, **payload}, headers={"Cache-Control": "no-store"}
    )


def _message_not_available(reason: str) -> JSONResponse:
    return _message_response(503, state="backend_unavailable", reason=reason)


def _message_id_valid(value: object) -> bool:
    if not isinstance(value, str) or not 34 <= len(value) <= _MESSAGE_MAX_ID:
        return False
    prefix, digest = value[:-32], value[-32:]
    if any(char not in "0123456789abcdef" for char in digest):
        return False
    if prefix == "legacy-message-":
        return True
    return prefix.endswith("_") and "a" <= prefix[0] <= "z" and all(
        "a" <= char <= "z" or "0" <= char <= "9" or char == "-" for char in prefix[1:-1]
    )


def _message_thread(
    connection: SQLiteConnection,
    recipients: str,
    seat_ids: dict[str, str],
    ambiguous: frozenset[str],
    scope: tuple[str, str, str | None],
    limit: int,
    offset: int,
) -> JSONResponse:
    rows = connection.execute(
        """
        SELECT m.message_id,
               CASE WHEN length(CAST(m.sender AS BLOB)) <= ? THEN m.sender END AS sender,
               CASE WHEN length(CAST(m.kind AS BLOB)) <= 40 THEN m.kind END AS kind,
               CASE WHEN length(CAST(meta.created_at AS BLOB)) <= 40 THEN meta.created_at END AS created_at
        """
        + _VISIBLE_MESSAGE_FROM
        + """
        AND meta.thread_id = ? AND m.conversation_id = ? AND meta.channel_id IS ?
        AND length(CAST(m.message_id AS BLOB)) <= ?
        ORDER BY meta.created_at, meta.message_id LIMIT ? OFFSET ?
        """,
        (_MESSAGE_MAX_SENDER, recipients, *scope, _MESSAGE_MAX_ID, limit + 1, offset),
    ).fetchall()
    items = [
        {
            "message_id": row["message_id"],
            "sender": _projected_sender(row["sender"], seat_ids, ambiguous),
            "kind": panel_values.choice(row["kind"], _DELIVERY_KINDS, "other"),
            "created_at": panel_values.timestamp(row["created_at"]),
        }
        for row in rows[:limit]
        if _message_id_valid(row["message_id"])
    ]
    return _message_response(
        200, state="ok", messages=items, limit=limit, offset=offset,
        next_offset=offset + limit if len(rows) > limit else None,
    )


def _message_body(connection: SQLiteConnection, root: Path, message_id: str) -> JSONResponse:
    try:
        raw = read_message_body_bounded(connection, root, message_id, max_bytes=_MESSAGE_MAX_BODY_BYTES)
        body = _redact_and_sanitize_text(raw.decode("utf-8"))
        if len(body.encode("utf-8")) > _MESSAGE_MAX_BODY_BYTES:
            raise ArtifactTooLargeError("redacted body exceeds read limit")
    except ArtifactExpiredError:
        return _message_response(410, state="expired")
    except ArtifactTooLargeError:
        return _message_response(413, state="oversized")
    except _AcpRedactionDepthExceeded:
        return _message_response(422, state="withheld", reason="redaction_depth_exceeded")
    except (ArtifactStoreError, _AcpBodyEncodingError, UnicodeError, ValueError):
        return _message_response(422, state="missing_or_corrupt")
    response = _message_response(200, state="ok", message_id=message_id, body=body)
    if len(response.body) > _MESSAGE_MAX_RESPONSE_BYTES:
        return _message_response(413, state="oversized")
    return response


def _read_visible_message(
    ctx: MonitorContext, message_id: str, *, page: tuple[int, int] | None = None
) -> JSONResponse:
    if not _message_id_valid(message_id):
        return _message_response(400, state="invalid_message_id")
    if not _MESSAGE_READ_SLOTS.acquire(blocking=False):
        return _message_not_available("concurrency_limit_exceeded")
    try:
        if storage.resolve_authority(storage.StoreId.FLEET_COMMS) is storage.Authority.PG:
            return _message_not_available("authority_unsupported_component")
        if message_plane.resolve_plane_mode() != "authority":
            return _message_not_available("message_plane_not_authoritative")
        root = message_plane.default_plane_root(repo_root=ctx.roots.state_root)
        if (root / "comms.sqlite3").is_symlink():
            return _message_not_available("plane_root_unavailable")
        seats, drivers, external = _fleet_seat_roster()
        if drivers != "available" or external != "available":
            return _message_not_available("roster_unavailable")
        recipients = json.dumps(sorted({name for seat in seats for name in seat.recipients}))
        seat_ids, ambiguous = _seat_ids_map(seats)
        with _read_connection(ctx, plane_root=root) as (connection, availability):
            if connection is None:
                return _message_not_available(availability)
            deadline = monotonic() + 0.5
            connection.set_progress_handler(lambda: monotonic() > deadline, 1000)
            if page is not None:
                connection.execute("BEGIN")
            anchor = connection.execute(
                """
                SELECT CASE WHEN length(CAST(meta.thread_id AS BLOB)) BETWEEN 1 AND ? THEN meta.thread_id END,
                       CASE WHEN length(CAST(m.conversation_id AS BLOB)) BETWEEN 1 AND ? THEN m.conversation_id END,
                       CASE WHEN length(CAST(meta.channel_id AS BLOB)) BETWEEN 1 AND ? THEN meta.channel_id END,
                       meta.channel_id IS NULL
                """
                + _VISIBLE_MESSAGE_FROM
                + " AND m.message_id = ?",
                (_MESSAGE_MAX_SCOPE, _MESSAGE_MAX_SCOPE, _MESSAGE_MAX_SCOPE, recipients, message_id),
            ).fetchone()
            if anchor is None:
                return _message_response(404, state="not_found")
            thread, conversation, channel, direct = anchor
            if not isinstance(thread, str) or not isinstance(conversation, str) or (
                not direct and not isinstance(channel, str)
            ):
                return _message_response(422, state="missing_or_corrupt")
            if page is not None:
                return _message_thread(connection, recipients, seat_ids, ambiguous, (thread, conversation, channel), *page)
            return _message_body(connection, root, message_id)
    except PlaneRootAnchorError:
        return _message_not_available("plane_root_unavailable")
    except (OSError, sqlite3.Error, storage.ControlPlaneError, ValueError):
        return _message_not_available("store_unavailable")
    finally:
        _MESSAGE_READ_SLOTS.release()


@router.get("/messages/{message_id}/body")
def fleet_message_body(
    message_id: str, request: Request, ctx: MonitorContext = Depends(get_ctx),
) -> JSONResponse:
    """Read one selected message through the local-only body-read guard."""
    refusal = local_body_read_refusal_response(request)
    return refusal if refusal is not None else _read_visible_message(ctx, message_id)


@router.get("/messages/{message_id}/thread")
def fleet_message_thread(
    message_id: str, request: Request, ctx: MonitorContext = Depends(get_ctx),
) -> JSONResponse:
    """List visible thread metadata, anchored to a visible message ID."""
    refusal = local_body_read_refusal_response(request)
    if refusal is not None:
        return refusal
    raw_limit = request.query_params.get("limit", str(DEFAULT_PAGE_SIZE))
    raw_offset = request.query_params.get("offset", "0")
    if not re.fullmatch(r"[0-9]{1,3}", raw_limit) or not re.fullmatch(r"[0-9]{1,10}", raw_offset):
        return _message_response(400, state="invalid_page")
    limit, offset = int(raw_limit), int(raw_offset)
    if not 1 <= limit <= MAX_PAGE_SIZE or offset > 2_147_483_647:
        return _message_response(400, state="invalid_page")
    return _read_visible_message(ctx, message_id, page=(limit, offset))


@router.get("/activity")
def fleet_activity(
    state: str | None = Query(default=None),
    agent: str | None = Query(default=None),
    source: str | None = Query(default=None),
    via: str | None = Query(default=None),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    ctx: MonitorContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Recent runtime provenance records projected under the unified observer."""
    since_value = _normalize_time(since, "since")
    until_value = _normalize_time(until, "until")
    filters = {
        "state": state,
        "agent": agent,
        "source": source,
        "via": via,
        "since": since_value,
        "until": until_value,
    }
    raw_records = recent_runtime_records(limit=MAX_ACTIVITY_SCAN, ctx=ctx).get("records", [])
    records: list[dict[str, Any]] = []
    for raw in raw_records:
        if not isinstance(raw, dict):
            continue
        record = {
            "ts": raw.get("ts"),
            "source": _safe_text(raw.get("source")),
            "agent": _safe_text(raw.get("agent")),
            "via": _safe_text(raw.get("via") or raw.get("entrypoint")),
            "model": _safe_text(raw.get("model"), fallback="") or None,
            **recorded_model_accounting_fields(raw),
            "outcome": _safe_text(raw.get("outcome"), fallback="") or None,
            "duration_s": raw.get("duration_s"),
            "source_provenance": _safe_text(raw.get("source_provenance"), fallback="unknown"),
            "source_task_id": _safe_text(raw.get("source_task_id"), fallback="") or None,
            "read_only": True,
        }
        timestamp = record["ts"]
        if state is not None and state != record["outcome"]:
            continue
        if agent is not None and agent != record["agent"]:
            continue
        if source is not None and source != record["source"]:
            continue
        if via is not None and via != record["via"]:
            continue
        if since_value is not None and (not isinstance(timestamp, str) or timestamp < since_value):
            continue
        if until_value is not None and (not isinstance(timestamp, str) or timestamp > until_value):
            continue
        records.append(record)
    return _collection(
        "records",
        records[offset : offset + limit],
        total=len(records),
        limit=limit,
        offset=offset,
        availability="available" if records else "empty",
        filters=filters,
    )

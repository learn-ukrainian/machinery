"""Authority-mode service for the Fleet Communications durable plane.

The service is deliberately a layer over the existing Fleet Comms SQLite and
artifact store.  It does not read legacy bridge files, does not write legacy
broker tables, and never turns a file diary into an authority.  Callers may
use it only after explicitly selecting ``FLEET_COMMS_MESSAGE_PLANE=authority``.

Every writer uses ``BEGIN IMMEDIATE`` and immutable content-addressed artifacts:
message fan-out, durable jobs, lease hand-off, and historical import are
therefore recoverable after a process crash.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, Literal

from lu_machinery.agent_runtime.retired_seat_admission import ACP_MODE, BRIDGE_MODE, KimiAdmissionRefused
from lu_machinery.agent_runtime.target_admission import (
    AdmittedTarget,
    require_admitted,
    resolve_and_admit,
    stored_kimi_request,
    stored_kimi_row,
)
from lu_machinery.common import json_depth
from lu_machinery.common.github_client import loads_strict_json
from lu_machinery.control_plane.storage import (
    Authority,
    ControlPlaneUnsupportedComponentError,
    StoreId,
    assert_component_supported,
)
from lu_machinery.fleet_comms.artifacts import (
    RETENTION_EXPIRED_PREFIX,
    ArtifactRecord,
    ArtifactStore,
    ArtifactStoreError,
)
from lu_machinery.fleet_comms.contracts import new_id
from lu_machinery.fleet_comms.migrations import MIGRATIONS, apply_migrations
from lu_machinery.lib.readonly_sqlite import SQLiteConnection, open_readonly

AuthorityJobKind = Literal["request", "discussion", "formal_review"]
AuthorityJobState = Literal[
    "queued", "running", "complete", "failed", "expired", "dead_lettered"
]
AuthorityDeliveryState = Literal[
    "queued", "running", "acknowledged", "failed", "expired", "dead_lettered"
]

_JOB_KINDS = frozenset({"request", "discussion", "formal_review"})
_JOB_TERMINAL = frozenset({"complete", "failed", "expired", "dead_lettered"})
_DELIVERY_TERMINAL = frozenset({"acknowledged", "failed", "expired", "dead_lettered"})
_WAKE_STATES = {"emitted": 0, "received": 1, "consumed": 2}
_AUTHORITY_SCHEMA_VERSION = 5
_FAILURE_PHASES = frozenset(
    {"admission", "transport", "provider", "result_parse", "postprocess"}
)
_FAILURE_CODES = frozenset(
    {
        "adapter_refused",
        "conversation_state_missing",
        "non_evidentiary",
        "primary_cwd_rejected",
        "protocol_output_limit",
        "provider_error",
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


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None = None) -> str:
    return (value or _utc_now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise AuthorityServiceError(f"invalid_{field}") from exc
    if parsed.tzinfo is None:
        raise AuthorityServiceError(f"invalid_{field}")
    return parsed.astimezone(UTC)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_canonical_json(value).encode("utf-8"))


def _nonempty(value: str | None, *, field: str) -> str:
    normalized = " ".join(str(value or "").split())
    if not normalized:
        raise AuthorityServiceError(f"{field}_required")
    return normalized


def _normalize_recipients(recipients: Iterable[str] | None) -> tuple[str, ...]:
    if recipients is None:
        return ()
    normalized = {_nonempty(recipient, field="recipient") for recipient in recipients}
    return tuple(sorted(normalized))


DURABLE_CTO_RECIPIENT = "claude-cto"
"""The one CTO recipient. A session identity ``claude-cto-<id>`` only claims and acknowledges."""

AUTHORITY_MESSAGE_ID_RE = re.compile(r"(?:[a-z][a-z0-9-]*_|legacy-message-)[0-9a-f]{32}")
_EXTERNAL_RECIPIENT_RE = re.compile(r"[a-z0-9][a-z0-9-]*")

CLEANUP_EXPIRY_REASON = "expired_by_cleanup"
"""Dead-letter reason for rows the operator's ``deliveries expire-stale --apply`` expired.

It is a deliberate cleanup, not a delivery failure. Recent-failure readers
(``efficiency_metrics``, the fleet authority-health window) exclude it by this literal.
"""


def is_session_cto_recipient(name: str | None) -> bool:
    """True for a session-specific CTO name such as ``claude-cto-27b21508``."""
    normalized = " ".join(str(name or "").split())
    return normalized.startswith(f"{DURABLE_CTO_RECIPIENT}-")


def _refuse_session_cto_recipients(names: Iterable[str | None]) -> None:
    """Refuse a session-specific CTO recipient with a pointer to the durable one."""
    for name in names:
        if is_session_cto_recipient(name):
            raise AuthorityServiceError(
                f"session_cto_recipient_refused: address {DURABLE_CTO_RECIPIENT}, not "
                f"{' '.join(str(name).split())}; the session identity only claims and acknowledges"
            )


def _durable_subscriber(name: str, sender: str) -> str | None:
    """Map a stored session-specific CTO subscriber to the durable CTO; drop the sender's own seat."""
    if not is_session_cto_recipient(name):
        return name
    if is_session_cto_recipient(sender) or sender == DURABLE_CTO_RECIPIENT:
        return None
    return DURABLE_CTO_RECIPIENT


def _admit_recipients(
    recipients: Iterable[str | None],
    *,
    mode: str,
    attachments: Iterable[Any] = (),
    claiming: bool = False,
) -> tuple[AdmittedTarget, ...]:
    """Resolve and admit recipients in one step (whitespace-normalized names, as rows store them).

    Raises ``KimiAdmissionRefused`` for a Kimi seat or model before any write, and
    ``AuthorityServiceError`` for a session-specific CTO name unless ``claiming``:
    legacy deliveries to old names stay claimable, but nothing new may address them.
    """
    names = tuple(" ".join(str(item or "").split()) for item in recipients)
    if not claiming:
        _refuse_session_cto_recipients(names)
    return resolve_and_admit(names, mode=mode, attachments=attachments)


def _admitted_subscribers(names: Iterable[str]) -> list[AdmittedTarget]:
    """Admitted fan-out targets for stored subscribers; a stored Kimi subscriber gets no delivery."""
    admitted: list[AdmittedTarget] = []
    for name in names:
        try:
            admitted.extend(resolve_and_admit((name,), mode=BRIDGE_MODE))
        except KimiAdmissionRefused:
            continue
    return admitted


RECIPIENT_NOT_REGISTERED_REASON = "recipient_not_registered"
"""Refusal and dead-letter reason for a recipient that is neither a registered slot nor a task (#594)."""

RECIPIENT_SETTLED_REASON = "recipient_settled"
"""Refusal and dead-letter reason for a task recipient whose task has settled (#594)."""

_TASK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
# The supervisory wake watcher's inbox (``ai_agent_bridge._inbox_watch.supervisory_recipient``).
_SUPERVISORY_RECIPIENT_RE = re.compile(r"supervisor:epic:[1-9][0-9]*")
_SUPERVISORY_REQUEST_KIND = "supervisory-request"
_SUPERVISORY_CLAIM_PREDICATE = "(? OR m.kind <> ?)"
RecipientStanding = Literal["registered", "live_task", "settled_task", "not_registered", "unknown"]
_ROSTER_CACHE: dict[tuple[object, ...], tuple[frozenset[str], bool]] = {}
# Dispatch v2 terminal statuses; only these prove a task has settled. Any other
# value, or none, leaves its standing unknown (#594).
_TASK_TERMINAL_STATUSES = frozenset({
    "done", "failed", "timeout", "rate_limited", "cancelled", "crashed", "no_deliverable",
})
_LOG = logging.getLogger(__name__)


def _roster_sources() -> tuple[Path | None, Path | None, Path | None, Path | None]:
    """The assignments and driver-lane files the roster reads, the profile file that names them, and the schema.

    The schema is the one the strict assignments read validates against.
    ``None`` for a source whose setting is unusable, for the profile when none is configured, and for
    a schema that cannot be resolved (the strict read then fails too, so the roster is incomplete).
    Each source path comes from its own profile read, so a caller compares the sources taken before
    and after its reads (and the profile's identity) to know they belong to one profile version.
    """
    from lu_machinery import agent_identity
    from lu_machinery.common import profile, repo_root
    from lu_machinery.common.repo_root import MachineryConfigError
    from lu_machinery.ops import driver_lanes

    schema: Path | None
    try:
        from lu_machinery.orchestration.validate_fleet_taxonomy import ASSIGNMENTS_SCHEMA_PATH

        schema = Path(ASSIGNMENTS_SCHEMA_PATH)
    except Exception:
        schema = None
    profile_file: Path | None
    try:
        profile_file = profile._configured_profile_path(None) or repo_root.state_root() / profile.PROFILE_REL
    except Exception:
        profile_file = None

    try:
        assignments: Path | None = Path(agent_identity.default_assignments_path())
    except MachineryConfigError:
        assignments = None
    try:
        lane_data: Path | None = Path(driver_lanes.default_data_path())
    except MachineryConfigError:
        lane_data = None
    return assignments, lane_data, profile_file, schema


def _roster_identity(sources: tuple[Path | None, ...]) -> tuple[object, ...]:
    """Each roster file's (path, device, inode, ctime, mtime, size), or ``None`` fields when it cannot be stated.

    Any write, replacement or permission change moves ctime, so equal identities taken
    before and after a read prove the read saw one version.
    """
    key: list[object] = []
    for path in sources:
        fields: tuple[int, ...] | None = None
        if path is not None:
            with contextlib.suppress(OSError):
                stat = path.stat()
                fields = (stat.st_dev, stat.st_ino, stat.st_ctime_ns, stat.st_mtime_ns, stat.st_size)
        key.append((str(path), fields))
    return tuple(key)


def _load_roster() -> tuple[frozenset[str], bool]:
    """The durable CTO, registered slots, external recipients and driver lanes, and whether both sources loaded.

    Slots come from ``config/area_assignments.yaml`` (the roster every slot
    reader uses), external drain recipients from that file's
    ``external_recipients`` list (agents outside the launcher fleet whose inbox
    an external bridge drains), and lane senders from ``config/driver_lanes.json``
    (the lanes the context guard hands off). The roster is complete only when the slot
    file passes the assignments contract whole and names at least one slot, the
    external list reads, and the lane file loads with at least one lane; an
    incomplete roster proves no name unregistered. The result is
    cached per file identity (path, device, inode, ctime, mtime, size), so every
    claim does not reparse both files; ctime moves on any rewrite or permission
    change, so a file that became unreadable is read again. A file whose
    identity changed while it was read gives an incomplete, uncached roster.
    """
    from lu_machinery import agent_identity
    from lu_machinery.ops import driver_lanes

    # An unusable profile entry or root setting leaves that roster source unavailable, so the roster is incomplete.
    sources = _roster_sources()
    assignments, lane_data, _profile_file, _schema = sources
    key = _roster_identity(sources)
    cached = _ROSTER_CACHE.get(key)
    if cached is not None:
        return cached
    slots: tuple[str, ...] = ()
    external: tuple[str, ...] = ()
    external_availability: Literal["available", "unavailable"] = "unavailable"
    if assignments is not None:
        # Strict: a file that fails the assignments contract (one malformed area included) yields no
        # slots, so the roster is incomplete rather than complete without that area's slots.
        try:
            slots = agent_identity._load_registry_slots(assignments, strict=True)
        except Exception as exc:
            _LOG.warning("fleet_comms slot roster unreadable: %s", type(exc).__name__)
        external, external_availability = load_external_recipients(assignments)
    lanes: tuple[str, ...] = ()
    if lane_data is not None:
        with contextlib.suppress(Exception):
            lanes = tuple(lane.sender for lane in driver_lanes.load_registry(lane_data).lanes)
    complete = bool(slots) and bool(lanes) and external_availability == "available"
    # A replacement between the reads, or a profile that now names other files, mixes two versions.
    changed = _roster_sources() != sources or _roster_identity(sources) != key
    complete = complete and not changed
    if not complete:
        _LOG.warning(
            "fleet_comms recipient roster incomplete (slots=%d, lanes=%d, external=%s): "
            "unregistered names are admitted and no subscription or delivery is ended",
            len(slots), len(lanes), external_availability,
        )
    roster = (frozenset({DURABLE_CTO_RECIPIENT, *slots, *external, *lanes}), complete)
    if not changed:
        _ROSTER_CACHE.clear()
        _ROSTER_CACHE[key] = roster
    return roster


def load_external_recipients(
    path: Path,
) -> tuple[tuple[str, ...], Literal["available", "unavailable"]]:
    """Read exact external recipient names, distinguishing an empty list from a failed read.

    The file is read as UTF-8 without blocking and parsed by the fleet-config YAML
    parser the strict assignments read uses: a FIFO, a directory or any other
    non-regular entry, a duplicate key, a root that is not a mapping and any
    construction failure are a failed read.
    """
    try:
        from lu_machinery.orchestration.validate_fleet_taxonomy import load_yaml_mapping_bytes, read_regular_file_bytes
    except Exception:  # a missing or broken dependency of the reader
        return (), "unavailable"
    try:
        _, raw = read_regular_file_bytes(path)  # its refusals are ValueErrors
        data = load_yaml_mapping_bytes(raw, path)  # every parse or construction failure is a ValueError
    except (OSError, ValueError, RecursionError):
        return (), "unavailable"
    names = data.get("external_recipients", [])
    if not isinstance(names, list) or any(
        not isinstance(name, str) or not _EXTERNAL_RECIPIENT_RE.fullmatch(name) for name in names
    ):
        return (), "unavailable"
    if len(set(names)) != len(names):
        return (), "unavailable"
    return tuple(names), "available"


def _registered_recipients() -> frozenset[str]:
    """Recipients a drain reads besides live tasks: the durable CTO, registered slots, external recipients and driver lanes."""
    return _load_roster()[0]


def _roster_complete() -> bool:
    """True only when both the slot roster and the driver-lane file loaded (#594)."""
    return _load_roster()[1]


def _task_standing(name: str) -> Literal["live", "settled", "unknown"] | None:
    """A dispatch task's standing by its record: live while spawning or running, settled at a terminal status.

    ``None`` only when the store proves no task record has this id; ``unknown``
    when the record or its absence cannot be read (an access failure included)
    or its status is missing, not a string or not a known status.
    """
    if not _TASK_ID_RE.fullmatch(name) or ".." in name:
        return None
    from lu_machinery.dispatcher import store

    try:
        record = store.read(name, missing_ok=True)
    except (OSError, ValueError):
        return "unknown"
    if record is None:
        return None
    status = record.get("status") if isinstance(record, Mapping) else None
    if not isinstance(status, str):
        return "unknown"
    if status in store.ACTIVE:
        return "live"
    if status in _TASK_TERMINAL_STATUSES:
        return "settled"
    return "unknown"


@dataclass(frozen=True, slots=True)
class _RecipientRoster:
    """Roster and file identities shared by a batch of standing decisions."""

    registered: frozenset[str]
    complete: bool
    sources: tuple[Path | None, ...]
    identity: tuple[object, ...]


def _load_recipient_roster() -> _RecipientRoster:
    sources = _roster_sources()
    identity = _roster_identity(sources)
    registered, complete = _load_roster()
    return _RecipientRoster(registered, complete, sources, identity)


def _recipient_standing(name: str, roster: _RecipientRoster) -> RecipientStanding:
    """Whether a drain will read ``name``: the one classifier every admission and removal path uses.

    Registered names and dedicated supervisory and session inboxes already
    have a drain. For other names, a verified alias is readable under its
    stored name, even if its bare provider has no registered inbox. An
    unreadable or changed alias source leaves these names unverified.

    ``not_registered`` and ``settled_task`` both need a complete roster (an
    incomplete one may hold the same name as a registered slot).
    ``settled_task`` also needs a known terminal status.
    """
    if name in roster.registered or is_session_cto_recipient(name) or _SUPERVISORY_RECIPIENT_RE.fullmatch(name):
        return "registered"

    from lu_machinery.agent_identity import resolve_recipient_alias

    assignments = roster.sources[0]
    if assignments is None:
        return "unknown"
    try:
        resolved = resolve_recipient_alias(name, assignments_path=assignments, strict=True)
    except Exception:
        return "unknown"
    if _roster_sources() != roster.sources or _roster_identity(roster.sources) != roster.identity:
        return "unknown"
    if resolved != name:
        return "registered"
    task = _task_standing(name)
    if task == "live":
        return "live_task"
    if task == "unknown" or not roster.complete:
        return "unknown"
    if task == "settled":
        return "settled_task"
    return "not_registered"


def _require_readable_recipients(names: Iterable[str]) -> None:
    """Refuse, before any write, a recipient proven unreadable (#594).

    A recipient whose standing is unknown (damaged task record, incomplete
    roster) is admitted and logged: doubt never drops mail.
    """
    roster = _load_recipient_roster()
    for name in names:
        standing = _recipient_standing(name, roster)
        if standing in {"registered", "live_task"}:
            continue
        if standing == "unknown":
            _LOG.warning("fleet_comms recipient %s admitted with unverified standing", name)
            continue
        raise AuthorityRecipientRefused(name, standing)


def _safe_json_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    try:
        encoded = _canonical_json(dict(value))
        loaded = loads_strict_json(encoded, max_depth=512)
    except (TypeError, ValueError) as exc:
        raise AuthorityServiceError("metadata_must_be_json_serializable") from exc
    if not isinstance(loaded, dict):  # pragma: no cover - dict(value) guarantees this
        raise AuthorityServiceError("metadata_must_be_object")
    return loaded


def _safe_failure_metadata(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Validate one body-free terminal failure record."""
    if value is None:
        return None
    loaded = _safe_json_mapping(value)
    if set(loaded) != {"phase", "code", "retryable"}:
        raise AuthorityServiceError("failure_metadata_fields_invalid")
    phase = loaded.get("phase")
    code = loaded.get("code")
    retryable = loaded.get("retryable")
    if phase not in _FAILURE_PHASES:
        raise AuthorityServiceError("failure_phase_invalid")
    if code not in _FAILURE_CODES:
        raise AuthorityServiceError("failure_code_invalid")
    if not isinstance(retryable, bool):
        raise AuthorityServiceError("failure_retryable_must_be_boolean")
    return {"phase": phase, "code": code, "retryable": retryable}


class AuthorityServiceError(RuntimeError):
    """Authority writer rejected an invalid or unsafe operation."""


class AuthorityStaleLeaseError(AuthorityServiceError):
    """A worker attempted to complete a superseded lease."""


class AuthorityRecipientRefused(AuthorityServiceError):
    """A publish or subscription named a recipient no drain reads; ``cause`` names why."""

    _CAUSES: ClassVar[dict[str, tuple[str, str]]] = {
        "not_registered": (RECIPIENT_NOT_REGISTERED_REASON, "is neither a registered slot nor a live task id"),
        "settled_task": (RECIPIENT_SETTLED_REASON, "is a task that has settled"),
    }

    def __init__(self, recipient: str, standing: str) -> None:
        self.recipient = recipient
        self.cause, detail = self._CAUSES[standing]
        super().__init__(f"{self.cause}: {recipient} {detail}")


@dataclass(frozen=True, slots=True)
class AuthorityChannel:
    channel_id: str
    name: str
    metadata: dict[str, Any]
    current_context_revision_id: str | None
    created_at: str
    subscribers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ContextRevision:
    context_revision_id: str
    channel_id: str
    sha256: str
    artifact_id: str
    created_at: str


@dataclass(frozen=True, slots=True)
class AuthorityMessage:
    message_id: str
    conversation_id: str
    in_reply_to: str | None
    channel_id: str | None
    thread_id: str
    correlation_id: str | None
    sender: str
    recipient: str | None
    kind: str
    body_artifact_id: str
    content_sha256: str
    context_revisions: dict[str, str]
    provenance: dict[str, Any]
    created_at: str
    delivery_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AuthorityDelivery:
    delivery_id: str
    message_id: str
    recipient: str
    state: str
    deadline_at: str | None
    lease_owner: str | None
    lease_expires_at: str | None
    fence_token: int
    attempt_count: int
    acknowledgment_artifact_id: str | None
    terminal_sha256: str | None
    created_at: str
    updated_at: str
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class AuthorityDeliveryLease:
    delivery: AuthorityDelivery
    body_artifact_id: str
    content_sha256: str

    @property
    def fence_token(self) -> int:
        return self.delivery.fence_token


@dataclass(frozen=True, slots=True)
class AuthorityJob:
    job_id: str
    job_kind: str
    subject_id: str
    payload_artifact_id: str
    state: str
    deadline_at: str | None
    lease_owner: str | None
    lease_expires_at: str | None
    fence_token: int
    attempt_count: int
    result_artifact_id: str | None
    terminal_sha256: str | None
    idempotency_key: str
    created_at: str
    updated_at: str
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class AuthorityJobLease:
    job: AuthorityJob

    @property
    def fence_token(self) -> int:
        return self.job.fence_token


@dataclass(frozen=True, slots=True)
class HistoricalImportResult:
    imported: int
    replayed: int
    message_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "imported": self.imported,
            "replayed": self.replayed,
            "message_ids": list(self.message_ids),
            "content_included": False,
        }


def pending_delivery_snapshot(recipient: str, *, root: Path | None = None, limit: int = 5) -> dict[str, Any]:
    """Read counts and identifiers only, without creating, migrating or claiming.

    Unknown storage or schema is an explicit refusal to report an empty inbox.
    Supervisory consumption is distinct from merely observing a wake receipt.
    """
    from lu_machinery.fleet_comms.paths import default_plane_root, local_plane_is_retired, validate_plane_root

    unknown: dict[str, Any] = {"count": None, "ids": [], "probe": "unknown", "content_included": False}
    try:
        if type(limit) is not int or not 1 <= limit <= 500:
            return {**unknown, "reason": "invalid_pending_limit"}
        recipient = _nonempty(recipient, field="recipient")
        _admit_recipients((recipient,), mode=BRIDGE_MODE, claiming=True)
        assert_component_supported(StoreId.FLEET_COMMS, "authority_service")
        standing = _recipient_standing(recipient, _load_recipient_roster())
        if standing in {"not_registered", "settled_task"}:
            return {**unknown, "reason": AuthorityRecipientRefused(recipient, standing).cause}
        if standing == "unknown":
            return {**unknown, "reason": "recipient_standing_unverified"}
        plane_root = root
        override = os.environ.get("FLEET_COMMS_ROOT") if root is None else None
        if (override and not Path(override).is_absolute()) or (plane_root is not None and not plane_root.is_absolute()):
            return {**unknown, "reason": "pending_delivery_store_unavailable"}
        plane_root = validate_plane_root(plane_root) if plane_root is not None else default_plane_root()
        if local_plane_is_retired(root=plane_root):
            return {**unknown, "reason": "pending_delivery_store_unavailable"}
        path = plane_root / "comms.sqlite3"
        with contextlib.closing(open_readonly(path, timeout=1.0)) as conn:
            conn.execute("BEGIN")
            conn.execute("SELECT delivery_id, state FROM authority_wake_receipts LIMIT 0")
            where = """d.recipient = ? AND d.state IN ('queued', 'running')
                AND (d.deadline_at IS NULL OR d.deadline_at > ?)
                AND NOT EXISTS (SELECT 1 FROM authority_idempotency i
                    WHERE i.namespace = 'supervisory-consumption:' || d.delivery_id)
                AND """ + _SUPERVISORY_CLAIM_PREDICATE
            params = (recipient, _iso(), bool(_SUPERVISORY_RECIPIENT_RE.fullmatch(recipient)), _SUPERVISORY_REQUEST_KIND)
            count = conn.execute(
                f"SELECT COUNT(*) FROM authority_deliveries d JOIN comms_messages m USING (message_id) WHERE {where}", params,
            ).fetchone()[0]
            ids = [row[0] for row in conn.execute(
                f"SELECT d.message_id FROM authority_deliveries d JOIN comms_messages m USING (message_id) WHERE {where} ORDER BY d.message_id LIMIT ?",
                (*params, limit),
            )]
            if any(not isinstance(mid, str) or not AUTHORITY_MESSAGE_ID_RE.fullmatch(mid) for mid in ids):
                return {**unknown, "reason": "invalid_pending_message_id"}
        return {
            "count": count, "ids": ids, "truncated": count > len(ids),
            "probe": "ok", "content_included": False,
        }
    except Exception:
        # Paths, SQL and message content never leave this read boundary.
        return {**unknown, "reason": "pending_delivery_store_unavailable"}


class AuthorityService:
    """Transactional authority API backed by the shared Fleet Comms store."""

    def __init__(
        self,
        *,
        store: ArtifactStore | None = None,
        root: Path | None = None,
    ) -> None:
        # #7482 interlock: authority tables/queries are sqlite-only in this
        # slice (BEGIN IMMEDIATE, triggers, ``?`` placeholders) and are not in
        # the pg migration path at all; refuse pg at the boundary. The assert
        # runs for INJECTED stores too (CF r1 finding, PR #7498).
        assert_component_supported(StoreId.FLEET_COMMS, "authority_service")
        if store is not None and store.authority is Authority.PG:
            raise ControlPlaneUnsupportedComponentError(
                "control-plane store 'fleet_comms': authority 'pg' is not "
                "supported by component 'authority_service' in this slice "
                "(#7482 interlock)"
            )
        self.store = store or ArtifactStore(root=root)
        self._owns_store = store is None
        self._conn = self.store.connection
        # payload_artifact_id -> addressed to Kimi; payloads are immutable.
        self._kimi_payloads: dict[str, bool] = {}
        if self._owns_store:
            apply_migrations(self._conn)
        self._require_authority_schema()

    def close(self) -> None:
        if self._owns_store:
            self.store.close()

    def __enter__(self) -> AuthorityService:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Channel / immutable message surface

    def create_channel(
        self,
        name: str,
        *,
        subscribers: Iterable[str] = (),
        metadata: Mapping[str, Any] | None = None,
        legacy_import: bool = False,
    ) -> AuthorityChannel:
        """Create a channel once; exact repeats are idempotent.

        A session-specific CTO subscriber is refused; only the legacy import
        (``legacy_import=True``) may replay one, and fan-out maps it to the
        durable CTO recipient.

        A Kimi subscriber is kept as given (the legacy import replays history
        through here) but never receives a delivery: ``publish_message`` skips
        it. New Kimi subscriptions are refused by ``subscribe`` and the CLI.
        Outside the legacy import a subscriber no drain reads is refused (#594).
        """
        channel_name = _nonempty(name, field="channel")
        normalized_subscribers = _normalize_recipients(subscribers)
        if not legacy_import:
            _refuse_session_cto_recipients(normalized_subscribers)
            _require_readable_recipients(normalized_subscribers)
        meta = _safe_json_mapping(metadata)
        with self._write_transaction():
            row = self._conn.execute(
                "SELECT * FROM authority_channels WHERE name = ?", (channel_name,)
            ).fetchone()
            if row is None:
                channel_id = new_id("authority-channel")
                created_at = _iso()
                self._conn.execute(
                    """INSERT INTO authority_channels(
                        channel_id, name, metadata_json, created_at
                    ) VALUES (?, ?, ?, ?)""",
                    (channel_id, channel_name, _canonical_json(meta), created_at),
                )
            else:
                channel_id = str(row["channel_id"])
                existing = self._decode_mapping(row["metadata_json"], field="channel_metadata")
                if metadata is not None and existing != meta:
                    raise AuthorityServiceError("channel_metadata_conflict")
            for recipient in normalized_subscribers:
                self._conn.execute(
                    """INSERT OR IGNORE INTO authority_channel_subscribers(
                        channel_id, recipient, metadata_json, created_at
                    ) VALUES (?, ?, '{}', ?)""",
                    (channel_id, recipient, _iso()),
                )
        return self.get_channel(channel_name)

    def subscribe(
        self,
        channel: str,
        recipients: Iterable[str],
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> AuthorityChannel:
        """Add durable future fan-out subscribers without rewriting history.

        A Kimi subscriber is refused before any write: Kimi is not a bridge recipient.
        So is one no drain reads: neither a registered slot nor a live task (#594).
        """
        targets = _admit_recipients(recipients, mode=BRIDGE_MODE)
        channel_name = _nonempty(channel, field="channel")
        names = _normalize_recipients(target.recipient for target in targets)
        if not names:
            raise AuthorityServiceError("subscriber_required")
        _require_readable_recipients(names)
        subscriber_metadata = _safe_json_mapping(metadata)
        with self._write_transaction():
            channel_row = self._require_channel_tx(channel_name)
            # A repeated recipient finds its own row and is skipped.
            for target in targets:
                self._subscribe_tx(channel_row, target, metadata=metadata, subscriber_metadata=subscriber_metadata)
        return self.get_channel(channel_name)

    def unsubscribe(
        self, channel: str, recipients: Iterable[str], *, idempotency_key: str,
    ) -> AuthorityChannel:
        """Remove future fan-out only, atomically retaining a retry/audit receipt.

        A fresh operation refuses unknown subscribers. Replaying its key never
        removes a subscription added after that operation completed.
        """
        channel_name = _nonempty(channel, field="channel")
        names = _normalize_recipients(recipients)
        if not names:
            raise AuthorityServiceError("subscriber_required")
        key = _nonempty(idempotency_key, field="idempotency_key")
        digest = _sha256_json({"channel": channel_name, "recipients": list(names)})
        with self._write_transaction():
            row = self._require_channel_tx(channel_name)
            if self._check_idempotency_tx("channel-unsubscribe", key, digest) is None:
                channel_id = str(row["channel_id"])
                for recipient in names:
                    if self._conn.execute(
                        "SELECT 1 FROM authority_channel_subscribers WHERE channel_id = ? AND recipient = ?",
                        (channel_id, recipient),
                    ).fetchone() is None:
                        raise AuthorityServiceError("subscriber_not_found")
                for recipient in names:
                    self._conn.execute(
                        "DELETE FROM authority_channel_subscribers WHERE channel_id = ? AND recipient = ?",
                        (channel_id, recipient),
                    )
                self._insert_idempotency_tx("channel-unsubscribe", key, digest, channel_id)
        return self.get_channel(channel_name)

    def _subscribe_tx(
        self,
        channel_row: sqlite3.Row,
        target: AdmittedTarget,
        *,
        metadata: Mapping[str, Any] | None,
        subscriber_metadata: dict[str, Any],
    ) -> None:
        """Insert one admitted subscriber unless it is already subscribed with the same metadata."""
        recipient = require_admitted(target).recipient
        existing = self._conn.execute(
            """SELECT metadata_json FROM authority_channel_subscribers
               WHERE channel_id = ? AND recipient = ?""",
            (str(channel_row["channel_id"]), recipient),
        ).fetchone()
        if existing is not None:
            existing_metadata = self._decode_mapping(existing["metadata_json"], field="subscriber_metadata")
            if metadata is not None and existing_metadata != subscriber_metadata:
                raise AuthorityServiceError("subscriber_metadata_conflict")
            return
        self._conn.execute(
            """INSERT INTO authority_channel_subscribers(
                channel_id, recipient, metadata_json, created_at
            ) VALUES (?, ?, ?, ?)""",
            (
                str(channel_row["channel_id"]),
                recipient,
                _canonical_json(subscriber_metadata),
                _iso(),
            ),
        )

    def get_channel(self, name: str) -> AuthorityChannel:
        channel_name = _nonempty(name, field="channel")
        row = self._conn.execute(
            "SELECT * FROM authority_channels WHERE name = ?", (channel_name,)
        ).fetchone()
        if row is None:
            raise AuthorityServiceError("channel_not_found")
        subscriber_rows = self._conn.execute(
            """SELECT recipient FROM authority_channel_subscribers
               WHERE channel_id = ? ORDER BY recipient ASC""",
            (str(row["channel_id"]),),
        ).fetchall()
        return AuthorityChannel(
            channel_id=str(row["channel_id"]),
            name=str(row["name"]),
            metadata=self._decode_mapping(row["metadata_json"], field="channel_metadata"),
            current_context_revision_id=(
                str(row["current_context_revision_id"])
                if row["current_context_revision_id"] is not None
                else None
            ),
            created_at=str(row["created_at"]),
            subscribers=tuple(str(item["recipient"]) for item in subscriber_rows),
        )

    def set_channel_context(
        self,
        channel: str,
        body: str,
        *,
        producer: str = "authority-service",
    ) -> ContextRevision:
        """Store an immutable context revision and make it the channel current revision."""
        if not isinstance(body, str):
            raise AuthorityServiceError("context_body_must_be_text")
        channel_name = _nonempty(channel, field="channel")
        producer_name = _nonempty(producer, field="producer")
        body_bytes = body.encode("utf-8")
        digest = _sha256_bytes(body_bytes)
        with self._write_transaction():
            channel_row = self._require_channel_tx(channel_name)
            existing = self._conn.execute(
                """SELECT * FROM authority_context_revisions
                   WHERE channel_id = ? AND sha256 = ?""",
                (str(channel_row["channel_id"]), digest),
            ).fetchone()
            if existing is None:
                artifact = self.store.store_bytes(
                    body_bytes,
                    producer=producer_name,
                    retention_class="channel-context",
                    mime_type="text/plain; charset=utf-8",
                    logical_filename=f"{channel_row['channel_id']!s}.context.txt",
                    commit=False,
                )
                revision_id = new_id("context-revision")
                created_at = _iso()
                self._conn.execute(
                    """INSERT INTO authority_context_revisions(
                        context_revision_id, channel_id, sha256, artifact_id, created_at
                    ) VALUES (?, ?, ?, ?, ?)""",
                    (
                        revision_id,
                        str(channel_row["channel_id"]),
                        digest,
                        artifact.artifact_id,
                        created_at,
                    ),
                )
                self._conn.execute(
                    """UPDATE authority_channels
                       SET current_context_revision_id = ? WHERE channel_id = ?""",
                    (revision_id, str(channel_row["channel_id"])),
                )
                return ContextRevision(
                    context_revision_id=revision_id,
                    channel_id=str(channel_row["channel_id"]),
                    sha256=digest,
                    artifact_id=artifact.artifact_id,
                    created_at=created_at,
                )
            revision = self._context_revision_from_row(existing)
            self._conn.execute(
                """UPDATE authority_channels
                   SET current_context_revision_id = ? WHERE channel_id = ?""",
                (revision.context_revision_id, revision.channel_id),
            )
            return revision

    def read_channel_context(self, channel: str, *, revision_id: str | None = None) -> str:
        """Read an integrity-checked pinned revision, or the channel's current context.

        A channel without pinned context has an empty body. A named revision
        must exist and belong to that channel; it never falls back to a file.
        """
        record = self.get_channel(channel)
        revision = revision_id or record.current_context_revision_id
        if revision is None:
            return ""
        row = self._conn.execute(
            """SELECT artifact_id FROM authority_context_revisions
               WHERE context_revision_id = ? AND channel_id = ?""",
            (revision, record.channel_id),
        ).fetchone()
        if row is None:
            raise AuthorityServiceError("channel_context_revision_not_found")
        try:
            return self.store.read_bytes(str(row["artifact_id"])).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AuthorityServiceError("channel_context_not_utf8") from exc

    def publish_message(
        self,
        *,
        sender: str,
        body: str,
        channel: str | None = None,
        recipients: Iterable[str] | None = None,
        kind: str = "message",
        conversation_id: str | None = None,
        in_reply_to: str | None = None,
        correlation_id: str | None = None,
        provenance: Mapping[str, Any] | None = None,
        context_revisions: Mapping[str, str] | None = None,
        deadline_at: str | None = None,
        created_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> AuthorityMessage:
        """Append an immutable message and atomically fan it out to subscribers.

        An explicit Kimi recipient is refused before any write, and a Kimi
        subscriber gets no delivery: Kimi is not a bridge recipient.

        Only a reader gets a delivery (#594): an explicit recipient that is
        neither a registered slot nor a live task id is refused with
        ``AuthorityRecipientRefused`` before any write. A channel subscriber
        that no drain reads gets no delivery, and its subscription ends.
        """
        targets = _admit_recipients(recipients, mode=BRIDGE_MODE) if recipients is not None else None
        message_key = idempotency_key or new_id("authority-message-key")
        with self._write_transaction():
            return self._publish_message_tx(
                sender=sender,
                body=body,
                channel=channel,
                targets=targets,
                kind=kind,
                conversation_id=conversation_id,
                in_reply_to=in_reply_to,
                correlation_id=correlation_id,
                provenance=provenance,
                context_revisions=context_revisions,
                deadline_at=deadline_at,
                created_at=created_at,
                idempotency_key=message_key,
            )

    def enqueue_supervisory_request(
        self,
        *,
        sender: str,
        recipient: str,
        body: str,
        correlation_id: str,
        idempotency_key: str,
        deadline_at: str | None = None,
        created_at: str | None = None,
    ) -> AuthorityMessage:
        """Enqueue a launcher event to its supervisory inbox, even while its driver is offline."""
        return self.publish_message(
            sender=sender,
            recipients=(_nonempty(recipient, field="recipient"),),
            body=body,
            kind=_SUPERVISORY_REQUEST_KIND,
            correlation_id=_nonempty(correlation_id, field="correlation_id"),
            idempotency_key=_nonempty(idempotency_key, field="idempotency_key"),
            deadline_at=deadline_at,
            created_at=created_at,
        )

    def get_message(self, message_id: str) -> AuthorityMessage:
        mid = _nonempty(message_id, field="message_id")
        row = self._conn.execute(
            """SELECT m.message_id, m.conversation_id, m.in_reply_to, m.kind, m.sender,
                      m.recipient, m.body_artifact_id, m.content_sha256, m.created_at,
                      meta.channel_id, meta.thread_id, meta.correlation_id,
                      meta.context_revisions_json, meta.provenance_json
               FROM comms_messages m
               JOIN authority_message_metadata meta ON meta.message_id = m.message_id
               WHERE m.message_id = ?""",
            (mid,),
        ).fetchone()
        if row is None:
            raise AuthorityServiceError("message_not_found")
        delivery_rows = self._conn.execute(
            """SELECT delivery_id FROM authority_deliveries
               WHERE message_id = ? ORDER BY recipient ASC, delivery_id ASC""",
            (mid,),
        ).fetchall()
        artifact_id = row["body_artifact_id"]
        if artifact_id is None:
            raise AuthorityServiceError("message_body_artifact_missing")
        return AuthorityMessage(
            message_id=str(row["message_id"]),
            conversation_id=str(row["conversation_id"]),
            in_reply_to=str(row["in_reply_to"]) if row["in_reply_to"] is not None else None,
            channel_id=str(row["channel_id"]) if row["channel_id"] is not None else None,
            thread_id=str(row["thread_id"]),
            correlation_id=(str(row["correlation_id"]) if row["correlation_id"] else None),
            sender=str(row["sender"]),
            recipient=str(row["recipient"]) if row["recipient"] is not None else None,
            kind=str(row["kind"]),
            body_artifact_id=str(artifact_id),
            content_sha256=str(row["content_sha256"]),
            context_revisions=self._decode_string_mapping(row["context_revisions_json"]),
            provenance=self._decode_mapping(row["provenance_json"], field="provenance"),
            created_at=str(row["created_at"]),
            delivery_ids=tuple(str(item["delivery_id"]) for item in delivery_rows),
        )

    def read_message_body(self, message_id: str) -> str:
        """Read and integrity-check a message body without logging it."""
        message = self.get_message(message_id)
        try:
            return self.store.read_bytes(message.body_artifact_id).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AuthorityServiceError("message_body_not_utf8") from exc

    # ------------------------------------------------------------------
    # Worker queue surface

    def enqueue_request(
        self,
        *,
        recipient: str,
        body: str,
        sender: str = "authority-service",
        channel: str = "requests",
        deadline_at: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> AuthorityJob:
        """Queue an ordinary request atomically with its immutable message.

        A Kimi recipient (or a Kimi ``requested_model``) is refused before any
        write: Kimi seats take web, UI and backend coding only.

        The job is the queue: its message names the recipient but creates no
        delivery, because no drain reads a provider seat's deliveries (#594).
        """
        (target,) = _admit_recipients((recipient,), mode=ACP_MODE, attachments=(metadata,))
        key = idempotency_key or new_id("authority-request-key")
        recipient_name = _nonempty(target.recipient, field="recipient")
        self._ensure_channel(channel)
        request_metadata = _safe_json_mapping(metadata)
        payload = {
            "recipient": recipient_name,
            "metadata": request_metadata,
            "message_body_sha256": _sha256_bytes(body.encode("utf-8")),
        }
        with self._write_transaction():
            existing = self._find_job_by_key_tx("request", key)
            if existing is not None:
                self._assert_job_payload(existing, payload)
                return existing
            provenance: dict[str, Any] = {
                "Source": sender,
                "Agent": recipient_name,
                "Via": "queue",
            }
            task_id = request_metadata.get("task_id")
            if isinstance(task_id, str) and task_id.strip():
                provenance["task_id"] = task_id.strip()[:160]
            message = self._publish_message_tx(
                sender=sender,
                body=body,
                channel=channel,
                targets=(target,),
                kind="request",
                provenance=provenance,
                deadline_at=deadline_at,
                idempotency_key=f"request-message:{key}",
                deliver=False,
            )
            return self._enqueue_job_tx(
                job_kind="request",
                subject_id=message.message_id,
                payload=payload,
                deadline_at=deadline_at,
                idempotency_key=key,
            )

    def enqueue_discussion(
        self,
        *,
        channel: str,
        prompt: str,
        participants: Sequence[str],
        rounds: int,
        task_digest: str,
        correlation_id: str,
        deadline_at: str,
        token_budget: int = 8_000,
        content_budget_bytes: int = 24_000,
        source: str = "authority-discussion",
        task_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> AuthorityJob:
        """Queue a bounded (one-to-three round) discussion without invoking it.

        A Kimi participant is refused before any write: Kimi seats never join
        discussions.
        """
        targets = _admit_recipients(participants, mode=ACP_MODE)
        key = idempotency_key or new_id("authority-discussion-key")
        channel_name = _nonempty(channel, field="channel")
        participants_norm = _normalize_recipients(target.recipient for target in targets)
        if not participants_norm:
            raise AuthorityServiceError("discussion_participants_required")
        if rounds not in {1, 2, 3}:
            raise AuthorityServiceError("discussion_rounds_must_be_between_1_and_3")
        if token_budget < 0 or content_budget_bytes < 0:
            raise AuthorityServiceError("discussion_budget_must_be_nonnegative")
        digest = _nonempty(task_digest, field="task_digest")
        correlation = _nonempty(correlation_id, field="correlation_id")
        source_name = _nonempty(source, field="source")
        task_name = _nonempty(task_id, field="task_id") if task_id is not None else None
        deadline = self._normalize_deadline(deadline_at)
        self._ensure_channel(channel_name)
        payload = {
            "participants": list(participants_norm),
            "rounds": rounds,
            "task_digest": digest,
            "correlation_id": correlation,
            "prompt_sha256": _sha256_bytes(prompt.encode("utf-8")),
            "token_budget": token_budget,
            "content_budget_bytes": content_budget_bytes,
            "source": source_name,
            "task_id": task_name,
        }
        with self._write_transaction():
            existing = self._find_job_by_key_tx("discussion", key)
            if existing is not None:
                self._assert_job_payload(existing, payload)
                return existing
            conversation_id = new_id("conversation")
            now = _iso()
            self._conn.execute(
                """INSERT INTO conversations(conversation_id, created_at, source, title)
                   VALUES (?, ?, ?, ?)""",
                (conversation_id, now, source_name, channel_name),
            )
            self._conn.execute(
                """INSERT INTO acp_conversations(
                    conversation_id, task_digest, correlation_digest, idempotency_digest,
                    rounds_requested, participants_json, created_at, deadline_at,
                    token_budget, content_budget_bytes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    conversation_id,
                    digest,
                    _sha256_bytes(correlation.encode("utf-8")),
                    _sha256_bytes(key.encode("utf-8")),
                    rounds,
                    _canonical_json(list(participants_norm)),
                    now,
                    deadline,
                    token_budget,
                    content_budget_bytes,
                ),
            )
            self._conn.execute(
                """INSERT INTO acp_conversation_events(
                    event_id, conversation_id, sequence, event_type, state,
                    metadata_json, created_at
                ) VALUES (?, ?, 1, 'CREATED', 'CREATED', ?, ?)""",
                (
                    new_id("acp-event"),
                    conversation_id,
                    _canonical_json({"authority_reserved": True}),
                    now,
                ),
            )
            provenance: dict[str, Any] = {
                "Source": source_name,
                "Agent": "acp-controller",
                "Via": "queue",
            }
            if task_name is not None:
                provenance["task_id"] = task_name[:160]
            self._publish_message_tx(
                sender="authority-service",
                body=prompt,
                channel=channel_name,
                targets=targets,
                kind="discussion",
                conversation_id=conversation_id,
                correlation_id=correlation,
                provenance=provenance,
                deadline_at=deadline,
                idempotency_key=f"discussion-message:{key}",
                deliver=False,
            )
            return self._enqueue_job_tx(
                job_kind="discussion",
                subject_id=conversation_id,
                payload=payload,
                deadline_at=deadline,
                idempotency_key=key,
            )

    def get_job(self, job_id: str) -> AuthorityJob:
        jid = _nonempty(job_id, field="job_id")
        row = self._conn.execute(
            "SELECT * FROM authority_jobs WHERE job_id = ?", (jid,)
        ).fetchone()
        if row is None:
            raise AuthorityServiceError("job_not_found")
        return self._job_from_row(row)

    def claim_next_job(
        self,
        worker_id: str,
        *,
        job_kinds: Iterable[AuthorityJobKind] | None = None,
        lease_seconds: int = 300,
        now: str | None = None,
    ) -> AuthorityJobLease | None:
        """Claim exactly one eligible durable job under a fenced lease.

        A stored Kimi job (legacy) is passed over unwritten (``_stored_kimi_job``).
        """
        worker = _nonempty(worker_id, field="worker_id")
        if lease_seconds <= 0:
            raise AuthorityServiceError("lease_seconds_must_be_positive")
        kinds = self._normalize_job_kinds(job_kinds)
        now_value = self._now_string(now)
        lease_until = _iso(_parse_iso(now_value, field="now") + timedelta(seconds=lease_seconds))
        with self._write_transaction():
            self._reclaim_expired_jobs_tx(now_value)
            clauses = ["state = 'queued'", "(deadline_at IS NULL OR deadline_at > ?)"]
            params: list[Any] = [now_value]
            if kinds:
                clauses.append("job_kind IN (" + ", ".join("?" for _ in kinds) + ")")
                params.extend(sorted(kinds))
            candidates = self._conn.execute(
                """SELECT * FROM authority_jobs WHERE """
                + " AND ".join(clauses)
                + " ORDER BY created_at ASC, job_id ASC",
                params,
            )
            row = next((job for job in candidates if not self._stored_kimi_job(job)), None)
            if row is None:
                return None
            token = int(row["fence_token"]) + 1
            cursor = self._conn.execute(
                """UPDATE authority_jobs
                   SET state = 'running', lease_owner = ?, lease_expires_at = ?,
                       fence_token = ?, attempt_count = attempt_count + 1, updated_at = ?
                   WHERE job_id = ? AND state = 'queued' AND fence_token = ?""",
                (worker, lease_until, token, now_value, str(row["job_id"]), int(row["fence_token"])),
            )
            if cursor.rowcount != 1:  # BEGIN IMMEDIATE makes this defensive only.
                raise AuthorityStaleLeaseError("job_claim_raced")
            self._append_job_event_tx(
                str(row["job_id"]), token, "claimed", "running", {"worker_id": worker}
            )
            claimed = self._conn.execute(
                "SELECT * FROM authority_jobs WHERE job_id = ?", (str(row["job_id"]),)
            ).fetchone()
            if claimed is None:  # pragma: no cover - same transaction
                raise AuthorityServiceError("job_claim_lost")
            return AuthorityJobLease(self._job_from_row(claimed))

    def claim_job(
        self,
        job_id: str,
        worker_id: str,
        *,
        lease_seconds: int = 300,
        now: str | None = None,
    ) -> AuthorityJobLease:
        """Claim one named job without accidentally taking older queue work.

        A retry by the same still-live worker returns its original fenced lease
        unchanged.  That lets a synchronous caller replay a receipt rather
        than duplicate provider work after a response was lost locally.
        A stored Kimi job (legacy) is refused unwritten, and the reclaim
        this runs first leaves other stored Kimi jobs as they are.
        """
        jid = _nonempty(job_id, field="job_id")
        worker = _nonempty(worker_id, field="worker_id")
        if lease_seconds <= 0:
            raise AuthorityServiceError("lease_seconds_must_be_positive")
        now_value = self._now_string(now)
        lease_until = _iso(_parse_iso(now_value, field="now") + timedelta(seconds=lease_seconds))
        with self._write_transaction():
            row = self._require_job_tx(jid)
            if self._stored_kimi_job(row):
                raise AuthorityServiceError("kimi_job_not_claimable")
            self._reclaim_expired_jobs_tx(now_value)
            row = self._require_job_tx(jid)
            state = str(row["state"])
            if state == "running":
                if (
                    str(row["lease_owner"] or "") == worker
                    and row["lease_expires_at"] is not None
                    and str(row["lease_expires_at"]) > now_value
                ):
                    return AuthorityJobLease(self._job_from_row(row))
                raise AuthorityStaleLeaseError("job_already_claimed")
            if state != "queued":
                raise AuthorityServiceError("job_not_claimable")
            if row["deadline_at"] is not None and str(row["deadline_at"]) <= now_value:
                # The reclaim above should have terminalized it. Keep this
                # defensive branch fail-closed if a malformed row survived.
                raise AuthorityServiceError("job_deadline_expired")
            token = int(row["fence_token"]) + 1
            cursor = self._conn.execute(
                """UPDATE authority_jobs
                   SET state = 'running', lease_owner = ?, lease_expires_at = ?,
                       fence_token = ?, attempt_count = attempt_count + 1, updated_at = ?
                   WHERE job_id = ? AND state = 'queued' AND fence_token = ?""",
                (worker, lease_until, token, now_value, jid, int(row["fence_token"])),
            )
            if cursor.rowcount != 1:
                raise AuthorityStaleLeaseError("job_claim_raced")
            self._append_job_event_tx(jid, token, "claimed", "running", {"worker_id": worker})
            claimed = self._require_job_tx(jid)
            return AuthorityJobLease(self._job_from_row(claimed))

    def read_job_result(self, job_id: str) -> bytes | None:
        """Return the integrity-checked stored terminal result, never re-run a job."""
        job = self.get_job(job_id)
        if job.state not in _JOB_TERMINAL:
            raise AuthorityServiceError("job_result_not_terminal")
        if job.result_artifact_id is None:
            return None
        try:
            return self.store.read_bytes(job.result_artifact_id)
        except Exception as exc:
            raise AuthorityServiceError("job_result_unreadable") from exc

    def retry_job(
        self,
        job_id: str,
        *,
        now: str | None = None,
        deadline_at: str | None = None,
    ) -> AuthorityJob:
        """Explicitly requeue a failed or expired job without losing attempt history."""
        return self._requeue_terminal_job(
            job_id,
            allowed_states=frozenset({"failed", "expired"}),
            event_type="retried",
            now=now,
            deadline_at=deadline_at,
        )

    def redrive_job(
        self,
        job_id: str,
        *,
        now: str | None = None,
        deadline_at: str | None = None,
    ) -> AuthorityJob:
        """Explicitly redrive a dead-lettered job while retaining its DLQ receipt."""
        return self._requeue_terminal_job(
            job_id,
            allowed_states=frozenset({"dead_lettered"}),
            event_type="redriven",
            now=now,
            deadline_at=deadline_at,
        )

    def finish_job(
        self,
        job_id: str,
        *,
        worker_id: str,
        fence_token: int,
        state: Literal["complete", "failed"],
        result: bytes | None = None,
        result_artifact_id: str | None = None,
        failure: Mapping[str, Any] | None = None,
        now: str | None = None,
    ) -> AuthorityJob:
        """Terminalize a lease once; exact replay is harmless, divergence fails."""
        if state not in {"complete", "failed"}:
            raise AuthorityServiceError("invalid_job_terminal_state")
        if (result is not None) and (result_artifact_id is not None):
            raise AuthorityServiceError("provide_result_or_result_artifact_id_not_both")
        failure_metadata = _safe_failure_metadata(failure)
        if state == "complete" and failure_metadata is not None:
            raise AuthorityServiceError("complete_job_cannot_have_failure_metadata")
        jid = _nonempty(job_id, field="job_id")
        worker = _nonempty(worker_id, field="worker_id")
        if fence_token < 1:
            raise AuthorityStaleLeaseError("invalid_fence_token")
        now_value = self._now_string(now)
        with self._write_transaction():
            row = self._require_job_tx(jid)
            artifact, terminal_sha = self._result_artifact_tx(
                result=result,
                result_artifact_id=result_artifact_id,
                producer=f"authority-job:{row['job_kind']!s}",
            )
            if str(row["state"]) in _JOB_TERMINAL:
                self._assert_terminal_replay(
                    actual_state=str(row["state"]),
                    actual_sha=row["terminal_sha256"],
                    expected_state=state,
                    expected_sha=terminal_sha,
                )
                return self._job_from_row(row)
            self._assert_current_job_lease(row, worker, fence_token, now_value)
            self._conn.execute(
                """UPDATE authority_jobs
                   SET state = ?, result_artifact_id = ?, terminal_sha256 = ?,
                       lease_owner = NULL, lease_expires_at = NULL,
                       updated_at = ?, completed_at = ?
                   WHERE job_id = ?""",
                (state, artifact.artifact_id if artifact else None, terminal_sha, now_value, now_value, jid),
            )
            event_metadata: dict[str, Any] = {"worker_id": worker}
            if failure_metadata is not None:
                event_metadata["failure"] = failure_metadata
            self._append_job_event_tx(
                jid, fence_token, "finished", state, event_metadata
            )
            return self.get_job(jid)

    def reclaim_expired_jobs(self, *, now: str | None = None) -> int:
        """Recover leases left by a crash and expire jobs only at their deadline."""
        now_value = self._now_string(now)
        with self._write_transaction():
            return self._reclaim_expired_jobs_tx(now_value)

    def _requeue_terminal_job(
        self,
        job_id: str,
        *,
        allowed_states: frozenset[str],
        event_type: str,
        now: str | None,
        deadline_at: str | None,
    ) -> AuthorityJob:
        jid = _nonempty(job_id, field="job_id")
        now_value = self._now_string(now)
        with self._write_transaction():
            row = self._require_job_tx(jid)
            if self._stored_kimi_job(row):
                # Requeueing would address Kimi again; the legacy job stays as it is.
                raise AuthorityServiceError("kimi_job_not_retryable")
            previous_state = str(row["state"])
            if previous_state not in allowed_states:
                raise AuthorityServiceError("job_not_retryable")
            deadline = (
                self._normalize_deadline(deadline_at)
                if deadline_at is not None
                else row["deadline_at"]
            )
            if deadline is not None and str(deadline) <= now_value:
                raise AuthorityServiceError("job_retry_deadline_expired")
            self._conn.execute(
                """UPDATE authority_jobs
                   SET state = 'queued', deadline_at = ?, result_artifact_id = NULL,
                       terminal_sha256 = NULL, lease_owner = NULL,
                       lease_expires_at = NULL, updated_at = ?, completed_at = NULL
                   WHERE job_id = ?""",
                (deadline, now_value, jid),
            )
            self._append_job_event_tx(
                jid,
                int(row["fence_token"]),
                event_type,
                "queued",
                {
                    "previous_state": previous_state,
                    "previous_result_artifact_id": row["result_artifact_id"],
                    "previous_terminal_sha256": row["terminal_sha256"],
                },
            )
            return self.get_job(jid)

    # ------------------------------------------------------------------
    # Delivery leases, acknowledgements, and wake receipts

    def get_delivery(self, delivery_id: str) -> AuthorityDelivery:
        did = _nonempty(delivery_id, field="delivery_id")
        row = self._conn.execute(
            "SELECT * FROM authority_deliveries WHERE delivery_id = ?", (did,)
        ).fetchone()
        if row is None:
            raise AuthorityServiceError("delivery_not_found")
        return self._delivery_from_row(row)

    def claim_next_delivery(
        self,
        recipient: str,
        worker_id: str,
        *,
        lease_seconds: int = 300,
        max_attempts: int = 3,
        resume_owned: bool = False,
        now: str | None = None,
    ) -> AuthorityDeliveryLease | None:
        """Claim one recipient delivery; a stale worker cannot later acknowledge it.

        A Kimi recipient is refused before any write: Kimi is not a bridge
        recipient. A stored delivery of a request pinned to a Kimi model
        (legacy) is passed over unwritten (``_stored_kimi_delivery``).
        Legacy supervisory requests on ordinary inboxes wait for their deadline.
        """
        (target,) = _admit_recipients((recipient,), mode=BRIDGE_MODE, claiming=True)
        recipient_name = _nonempty(target.recipient, field="recipient")
        supervisory_inbox = bool(_SUPERVISORY_RECIPIENT_RE.fullmatch(recipient_name))
        worker = _nonempty(worker_id, field="worker_id")
        if lease_seconds <= 0 or max_attempts <= 0:
            raise AuthorityServiceError("lease_and_max_attempts_must_be_positive")
        now_value = self._now_string(now)
        lease_until = _iso(_parse_iso(now_value, field="now") + timedelta(seconds=lease_seconds))
        with self._write_transaction():
            self._reclaim_expired_deliveries_tx(now_value, max_attempts=max_attempts)
            if resume_owned:
                held = self._conn.execute(
                    f"""SELECT d.* FROM authority_deliveries d JOIN comms_messages m USING (message_id)
                       WHERE d.recipient = ? AND d.state = 'running' AND d.lease_owner = ?
                         AND {_SUPERVISORY_CLAIM_PREDICATE}
                       ORDER BY d.created_at ASC, d.delivery_id ASC""",
                    (recipient_name, worker, supervisory_inbox, _SUPERVISORY_REQUEST_KIND),
                ).fetchall()
                for current in held:
                    if not self._stored_kimi_delivery(current):
                        message = self.get_message(str(current["message_id"]))
                        return AuthorityDeliveryLease(
                            delivery=self._delivery_from_row(current),
                            body_artifact_id=message.body_artifact_id,
                            content_sha256=message.content_sha256,
                        )
            candidates = self._conn.execute(
                f"""SELECT d.* FROM authority_deliveries d JOIN comms_messages m USING (message_id)
                   WHERE d.recipient = ? AND d.state = 'queued'
                     AND d.attempt_count < ?
                     AND (d.deadline_at IS NULL OR d.deadline_at > ?)
                     AND {_SUPERVISORY_CLAIM_PREDICATE}
                   ORDER BY d.created_at ASC, d.delivery_id ASC""",
                (recipient_name, max_attempts, now_value, supervisory_inbox, _SUPERVISORY_REQUEST_KIND),
            )
            row = next((item for item in candidates if not self._stored_kimi_delivery(item)), None)
            if row is None:
                return None
            token = int(row["fence_token"]) + 1
            cursor = self._conn.execute(
                """UPDATE authority_deliveries
                   SET state = 'running', lease_owner = ?, lease_expires_at = ?,
                       fence_token = ?, attempt_count = attempt_count + 1, updated_at = ?
                   WHERE delivery_id = ? AND state = 'queued' AND fence_token = ?""",
                (worker, lease_until, token, now_value, str(row["delivery_id"]), int(row["fence_token"])),
            )
            if cursor.rowcount != 1:
                raise AuthorityStaleLeaseError("delivery_claim_raced")
            self._conn.execute(
                """INSERT INTO authority_delivery_attempts(
                    attempt_id, delivery_id, fence_token, state, started_at
                ) VALUES (?, ?, ?, 'running', ?)""",
                (new_id("authority-delivery-attempt"), str(row["delivery_id"]), token, now_value),
            )
            self._record_wake_receipt_tx(
                str(row["delivery_id"]), recipient_name, token, state="received", now=now_value
            )
            claimed = self._conn.execute(
                """SELECT d.*, m.body_artifact_id, m.content_sha256
                   FROM authority_deliveries d
                   JOIN comms_messages m ON m.message_id = d.message_id
                   WHERE d.delivery_id = ?""",
                (str(row["delivery_id"]),),
            ).fetchone()
            if claimed is None or claimed["body_artifact_id"] is None:
                raise AuthorityServiceError("delivery_message_missing")
            return AuthorityDeliveryLease(
                delivery=self._delivery_from_row(claimed),
                body_artifact_id=str(claimed["body_artifact_id"]),
                content_sha256=str(claimed["content_sha256"]),
            )

    def read_delivery_body(self, delivery_id: str, *, worker_id: str, fence_token: int) -> str:
        """Read only under a current lease; report unreadable coordinator replies to both parties."""
        row = self._require_delivery_tx(delivery_id)
        self._assert_current_delivery_lease(row, worker_id, fence_token, _iso())
        try:
            return self.read_message_body(str(row["message_id"]))
        except (AuthorityServiceError, ArtifactStoreError, OSError) as exc:
            with self._write_transaction():
                reported = self._notify_coordinator_reply_failure_tx(
                    delivery_id, reason_code="read_failed",
                )
            if reported:
                raise AuthorityServiceError("coordinator_reply_read_failed") from exc
            raise

    def record_supervisory_consumption(
        self,
        delivery_id: str,
        *,
        worker_id: str,
        fence_token: int,
        driver_generation: str,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Bind consumption to a leased generation before performing a side effect.

        A replay or reconciliation must not blindly repeat the side effect. An
        earlier consumption proves intent, not completion: the successor must
        inspect the external outcome before terminalizing the delivery.
        """
        did = _nonempty(delivery_id, field="delivery_id")
        worker = _nonempty(worker_id, field="worker_id")
        generation = _nonempty(driver_generation, field="driver_generation")
        now_value = self._now_string(now)
        namespace = f"supervisory-consumption:{did}"
        digest = _sha256_json({"driver_generation": generation})
        with self._write_transaction():
            row = self._require_supervisory_delivery_tx(did)
            self._assert_current_delivery_lease(row, worker, fence_token, now_value)
            existing = self._check_idempotency_tx(namespace, str(fence_token), digest)
            previous = self._conn.execute(
                """SELECT subject_id FROM authority_idempotency
                   WHERE namespace = ? AND CAST(idempotency_key AS INTEGER) < ?
                   ORDER BY CAST(idempotency_key AS INTEGER) ASC LIMIT 1""",
                (namespace, fence_token),
            ).fetchone()
            if existing is None:
                self._insert_idempotency_tx(namespace, str(fence_token), digest, generation)
            self._record_wake_receipt_tx(
                did, str(row["recipient"]), fence_token, state="consumed", now=now_value
            )
            return {
                "delivery_id": did,
                "fence_token": fence_token,
                "driver_generation": generation,
                "replay": existing is not None,
                "reconciliation": previous is not None,
                "reconciled_generation": str(previous["subject_id"]) if previous else None,
            }

    def act_on_supervisory_delivery(
        self,
        delivery_id: str,
        *,
        worker_id: str,
        fence_token: int,
        acknowledgment: bytes | None = None,
        now: str | None = None,
    ) -> AuthorityDelivery:
        """Record the verified action outcome after durable consumption."""
        self._require_supervisory_delivery_tx(delivery_id)
        return self.acknowledge_delivery(
            delivery_id, worker_id=worker_id, fence_token=fence_token,
            acknowledgment=acknowledgment, now=now,
        )

    def refuse_supervisory_delivery(
        self,
        delivery_id: str,
        *,
        worker_id: str,
        fence_token: int,
        result: bytes | None = None,
        now: str | None = None,
    ) -> AuthorityDelivery:
        """Record a driver's refusal after durable consumption."""
        self._require_supervisory_delivery_tx(delivery_id)
        return self.finish_delivery(
            delivery_id, worker_id=worker_id, fence_token=fence_token,
            state="failed", result=result, now=now,
        )

    def supervisory_delivery_status(self, delivery_id: str) -> str:
        """Project durable delivery and consumption evidence into supervisory state."""
        row = self._conn.execute(
            """SELECT d.*, m.kind, EXISTS(
                   SELECT 1 FROM authority_idempotency i
                   WHERE i.namespace = 'supervisory-consumption:' || d.delivery_id
               ) AS has_consumption
               FROM authority_deliveries d JOIN comms_messages m USING (message_id)
               WHERE d.delivery_id = ?""",
            (_nonempty(delivery_id, field="delivery_id"),),
        ).fetchone()
        if row is None:
            raise AuthorityServiceError("delivery_not_found")
        if row["kind"] != _SUPERVISORY_REQUEST_KIND:
            raise AuthorityServiceError("not_supervisory_delivery")
        state = str(row["state"])
        if state in _DELIVERY_TERMINAL:
            return {"acknowledged": "acted_on", "failed": "refused"}.get(state, state)
        if row["has_consumption"]:
            return "live_driver_consumed"
        return "delivered" if state == "running" else state

    def _require_supervisory_delivery_tx(self, delivery_id: str) -> sqlite3.Row:
        row = self._require_delivery_tx(_nonempty(delivery_id, field="delivery_id"))
        if self.get_message(str(row["message_id"])).kind != _SUPERVISORY_REQUEST_KIND:
            raise AuthorityServiceError("not_supervisory_delivery")
        if not _SUPERVISORY_RECIPIENT_RE.fullmatch(row["recipient"]):
            raise AuthorityServiceError("supervisory_recipient_required")
        return row

    def acknowledge_delivery(
        self,
        delivery_id: str,
        *,
        worker_id: str,
        fence_token: int,
        acknowledgment: bytes | None = None,
        acknowledgment_artifact_id: str | None = None,
        now: str | None = None,
    ) -> AuthorityDelivery:
        """Acknowledge one fenced delivery; duplicate terminal acks are idempotent."""
        return self.finish_delivery(
            delivery_id,
            worker_id=worker_id,
            fence_token=fence_token,
            state="acknowledged",
            result=acknowledgment,
            result_artifact_id=acknowledgment_artifact_id,
            now=now,
        )

    def finish_delivery(
        self,
        delivery_id: str,
        *,
        worker_id: str,
        fence_token: int,
        state: Literal["acknowledged", "failed"],
        result: bytes | None = None,
        result_artifact_id: str | None = None,
        now: str | None = None,
    ) -> AuthorityDelivery:
        """Finish a delivery exactly once, recording any failed terminal as a dead letter."""
        if state not in {"acknowledged", "failed"}:
            raise AuthorityServiceError("invalid_delivery_terminal_state")
        if result is not None and result_artifact_id is not None:
            raise AuthorityServiceError("provide_result_or_result_artifact_id_not_both")
        did = _nonempty(delivery_id, field="delivery_id")
        worker = _nonempty(worker_id, field="worker_id")
        if fence_token < 1:
            raise AuthorityStaleLeaseError("invalid_fence_token")
        now_value = self._now_string(now)
        with self._write_transaction():
            row = self._require_delivery_tx(did)
            artifact, terminal_sha = self._result_artifact_tx(
                result=result,
                result_artifact_id=result_artifact_id,
                producer="authority-delivery-result",
            )
            # The writer lock also covers retention's mark and unlink phases.
            # Refuse a marked artifact even while its bytes still exist (#12573).
            if artifact is not None and artifact.retention_class.startswith(RETENTION_EXPIRED_PREFIX):
                raise AuthorityServiceError("artifact_expired")
            if str(row["state"]) in _DELIVERY_TERMINAL:
                self._assert_terminal_replay(
                    actual_state=str(row["state"]),
                    actual_sha=row["terminal_sha256"],
                    expected_state=state,
                    expected_sha=terminal_sha,
                )
                return self._delivery_from_row(row)
            self._assert_current_delivery_lease(row, worker, fence_token, now_value)
            if self.get_message(str(row["message_id"])).kind == _SUPERVISORY_REQUEST_KIND:
                if not _SUPERVISORY_RECIPIENT_RE.fullmatch(row["recipient"]):
                    raise AuthorityServiceError("supervisory_recipient_required")
                consumed = self._conn.execute(
                    """SELECT 1 FROM authority_idempotency
                       WHERE namespace = ? AND idempotency_key = ?""",
                    (f"supervisory-consumption:{did}", str(fence_token)),
                ).fetchone()
                if consumed is None:
                    raise AuthorityServiceError("supervisory_consumption_required")
            self._conn.execute(
                """UPDATE authority_deliveries
                   SET state = ?, acknowledgment_artifact_id = ?, terminal_sha256 = ?,
                       lease_owner = NULL, lease_expires_at = NULL,
                       updated_at = ?, completed_at = ?
                   WHERE delivery_id = ?""",
                (state, artifact.artifact_id if artifact else None, terminal_sha, now_value, now_value, did),
            )
            self._conn.execute(
                """UPDATE authority_delivery_attempts
                   SET state = ?, finished_at = ?, outcome_sha256 = ?, artifact_id = ?
                   WHERE delivery_id = ? AND fence_token = ?""",
                (
                    state,
                    now_value,
                    terminal_sha,
                    artifact.artifact_id if artifact else None,
                    did,
                    fence_token,
                ),
            )
            self._record_wake_receipt_tx(
                did, str(row["recipient"]), fence_token, state="consumed", now=now_value
            )
            if state == "failed":
                self._dead_letter_delivery_tx(did, reason_code="worker_failed")
            return self.get_delivery(did)

    def reclaim_expired_deliveries(
        self,
        *,
        max_attempts: int = 3,
        now: str | None = None,
    ) -> int:
        """Recover abandoned delivery leases without dropping pending work."""
        if max_attempts <= 0:
            raise AuthorityServiceError("max_attempts_must_be_positive")
        now_value = self._now_string(now)
        with self._write_transaction():
            return self._reclaim_expired_deliveries_tx(now_value, max_attempts=max_attempts)

    def count_undelivered(self, *, older_than_days: int = 7, now: str | None = None) -> int:
        """Queued or running deliveries older than the cutoff. Reads only."""
        if older_than_days <= 0:
            raise AuthorityServiceError("older_than_days_must_be_positive")
        return len(self._stale_undelivered(older_than_days=older_than_days, now=now))

    def expire_undelivered(self, *, older_than_days: int = 7, now: str | None = None) -> int:
        """Expire old undelivered rows through the existing deadline path.

        A null deadline is set to the row's created_at, which is already past
        the cutoff, and reclaim then marks the row expired. The row and its
        message stay. An unread body stays ineligible for retention deletion.
        The batch sends one summary notification, not one per row. A stored
        Kimi delivery is left untouched.
        """
        if older_than_days <= 0:
            raise AuthorityServiceError("older_than_days_must_be_positive")
        now_value = self._now_string(now)
        with self._write_transaction():
            cleanup_ids: set[str] = set()
            for row in self._stale_undelivered(older_than_days=older_than_days, now=now_value):
                if self._stored_kimi_delivery(row):
                    continue
                cleanup_ids.add(str(row["delivery_id"]))
                self._conn.execute(
                    """UPDATE authority_deliveries
                       SET deadline_at = created_at, updated_at = ?
                       WHERE delivery_id = ?""",
                    (now_value, str(row["delivery_id"])),
                )
            expired = self._reclaim_expired_deliveries_tx(
                now_value, max_attempts=3, cleanup_ids=frozenset(cleanup_ids),
            )
        return expired

    def _stale_undelivered(self, *, older_than_days: int, now: str | None) -> list[sqlite3.Row]:
        """Queued or running rows past the age cutoff whose deadline is null or already due.

        A stored Kimi delivery is omitted. The caller writes one row at a time.
        """
        now_value = self._now_string(now)
        cutoff = _iso(_parse_iso(now_value, field="now") - timedelta(days=older_than_days))
        rows = self._conn.execute(
            """SELECT delivery_id, recipient, message_id FROM authority_deliveries
               WHERE state IN ('queued', 'running') AND created_at <= ?
                 AND (deadline_at IS NULL OR deadline_at <= ?)""",
            (cutoff, now_value),
        ).fetchall()
        return [row for row in rows if not self._stored_kimi_delivery(row)]

    def count_unreadable(self) -> dict[str, int]:
        """Open deliveries and subscriptions whose recipient no drain reads, by cause. Reads only (#594).

        Bare provider names and the retired Kimi seats count as not registered.
        Nothing is counted while the roster is incomplete (``roster_complete``
        is then false), and no name of unknown standing is counted.
        """
        roster = _load_recipient_roster()
        counts = {RECIPIENT_NOT_REGISTERED_REASON: 0, RECIPIENT_SETTLED_REASON: 0}
        for _row, reason in self._unreadable_deliveries_tx(roster, settled_only=False):
            counts[reason] += 1
        return {
            "deliveries": sum(counts.values()),
            **counts,
            "subscriptions": len(self._unreadable_subscriptions_tx(roster, settled_only=False)),
            "roster_complete": roster.complete,
        }

    def expire_unreadable(self, *, now: str | None = None) -> dict[str, int]:
        """Expire open deliveries no drain will read and end those recipients' subscriptions.

        The one-time hygiene of #594, run by ``deliveries expire-stale --apply``.
        Rows and messages stay; each expired row dead-letters with its cause
        (``recipient_not_registered`` or ``recipient_settled``) and each cause
        sends one summary notice to the CTO. This is the one writer that expires
        a stored Kimi delivery: Kimi is retired, so its queued rows are retired
        too, as a state change only (CTO decision on #594). No per-row notice is
        sent, so no write admits a Kimi recipient. With an incomplete roster
        nothing is expired or ended, and ``roster_complete`` reports false.
        """
        now_value = self._now_string(now)
        with self._write_transaction():
            roster = _load_recipient_roster()
            ended = len(self._unreadable_subscriptions_tx(roster, settled_only=False))
            expired: dict[str, list[str]] = {RECIPIENT_NOT_REGISTERED_REASON: [], RECIPIENT_SETTLED_REASON: []}
            for row, reason in self._unreadable_deliveries_tx(roster, settled_only=False):
                delivery_id = str(row["delivery_id"])
                self._conn.execute(
                    """UPDATE authority_deliveries
                       SET state = 'expired', lease_owner = NULL, lease_expires_at = NULL,
                           updated_at = ?, completed_at = ? WHERE delivery_id = ?""",
                    (now_value, now_value, delivery_id),
                )
                self._conn.execute(
                    """UPDATE authority_delivery_attempts
                       SET state = 'expired', finished_at = ?
                       WHERE delivery_id = ? AND fence_token = ? AND finished_at IS NULL""",
                    (now_value, delivery_id, int(row["fence_token"])),
                )
                self._dead_letter_delivery_tx(delivery_id, reason_code=reason, notify=False)
                expired[reason].append(delivery_id)
            self._end_unreadable_subscriptions_tx(roster, settled_only=False)
            for reason, ids in expired.items():
                if ids:
                    self._notify_cleanup_expiry_summary_tx(ids, reason_code=reason)
        counts = {reason: len(ids) for reason, ids in expired.items()}
        return {
            "deliveries": sum(counts.values()), **counts, "subscriptions": ended,
            "roster_complete": roster.complete,
        }

    def _unreadable_deliveries_tx(
        self, roster: _RecipientRoster, *, settled_only: bool,
    ) -> list[tuple[sqlite3.Row, str]]:
        """Queued or running deliveries whose recipient is a settled task (or, unless ``settled_only``, unregistered).

        Empty while the roster is incomplete: no removal on doubt (#594).
        """
        if not roster.complete:
            return []
        rows: list[tuple[sqlite3.Row, str]] = []
        recipients = self._conn.execute(
            """SELECT DISTINCT recipient FROM authority_deliveries
               WHERE state IN ('queued', 'running') ORDER BY recipient"""
        ).fetchall()
        for item in recipients:
            reason = self._unreadable_reason(str(item["recipient"]), roster, settled_only=settled_only)
            if reason is None:
                continue
            rows.extend(
                (row, reason)
                for row in self._conn.execute(
                    """SELECT delivery_id, fence_token, recipient, message_id FROM authority_deliveries
                       WHERE recipient = ? AND state IN ('queued', 'running')
                       ORDER BY created_at ASC, delivery_id ASC""",
                    (str(item["recipient"]),),
                ).fetchall()
            )
        return rows

    def _unreadable_subscriptions_tx(self, roster: _RecipientRoster, *, settled_only: bool) -> list[str]:
        if not roster.complete:
            return []
        names = self._conn.execute(
            "SELECT DISTINCT recipient FROM authority_channel_subscribers ORDER BY recipient"
        ).fetchall()
        return [
            str(item["recipient"])
            for item in names
            if self._unreadable_reason(str(item["recipient"]), roster, settled_only=settled_only) is not None
        ]

    def _end_unreadable_subscriptions_tx(self, roster: _RecipientRoster, *, settled_only: bool) -> int:
        return sum(
            self._end_subscriptions_tx(name)
            for name in self._unreadable_subscriptions_tx(roster, settled_only=settled_only)
        )

    @staticmethod
    def _unreadable_reason(name: str, roster: _RecipientRoster, *, settled_only: bool) -> str | None:
        standing = _recipient_standing(name, roster)
        if standing == "settled_task":
            return RECIPIENT_SETTLED_REASON
        if standing == "not_registered" and not settled_only:
            return RECIPIENT_NOT_REGISTERED_REASON
        return None

    def record_wake_receipt(
        self,
        delivery_id: str,
        *,
        fence_token: int,
        state: Literal["received", "consumed"] = "received",
        now: str | None = None,
    ) -> None:
        """Durably record receiver observation of an authority wake signal."""
        if state not in {"received", "consumed"}:
            raise AuthorityServiceError("invalid_wake_state")
        did = _nonempty(delivery_id, field="delivery_id")
        if fence_token < 0:
            raise AuthorityServiceError("invalid_fence_token")
        now_value = self._now_string(now)
        with self._write_transaction():
            delivery = self._require_delivery_tx(did)
            self._record_wake_receipt_tx(
                did,
                str(delivery["recipient"]),
                fence_token,
                state=state,
                now=now_value,
            )

    # ------------------------------------------------------------------
    # Historical migration seam (no live-state caller is invoked here)

    def import_records(
        self,
        records: Iterable[Mapping[str, Any]],
        *,
        source: str,
    ) -> HistoricalImportResult:
        """Import immutable legacy metadata/bodies once, preserving identity links.

        A repeat with the same source/external id/body hash returns the original
        message rather than creating another row.  A changed replay is refused;
        callers must choose a new source/external identity instead of silently
        overwriting history.
        """
        source_name = _nonempty(source, field="source")
        materialized = [self._normalize_import_record(item, source_name) for item in records]
        imported = 0
        replayed = 0
        message_ids: list[str] = []
        with self._write_transaction():
            self._conn.execute("PRAGMA defer_foreign_keys = ON")
            seen: dict[str, str] = {}
            for record in materialized:
                existing_digest = seen.get(record["external_id"])
                if existing_digest is not None and existing_digest != record["payload_sha256"]:
                    raise AuthorityServiceError("duplicate_import_external_id_conflict")
                seen[record["external_id"]] = record["payload_sha256"]
                receipt = self._conn.execute(
                    """SELECT payload_sha256, message_id FROM authority_import_receipts
                       WHERE source = ? AND external_id = ?""",
                    (source_name, record["external_id"]),
                ).fetchone()
                if receipt is not None:
                    if str(receipt["payload_sha256"]) != record["payload_sha256"]:
                        raise AuthorityServiceError("historical_import_conflict")
                    replayed += 1
                    message_ids.append(str(receipt["message_id"]))
                    continue
                if record["channel"] is not None:
                    self._ensure_channel_tx(str(record["channel"]))
                message = self._publish_message_tx(
                    sender=record["sender"],
                    body=record["body"],
                    channel=record["channel"],
                    targets=None,
                    imported_recipients=record["recipients"],
                    kind=record["kind"],
                    conversation_id=record["conversation_id"],
                    in_reply_to=record["in_reply_to"],
                    correlation_id=record["correlation_id"],
                    provenance=record["provenance"],
                    context_revisions=record["context_revisions"],
                    deadline_at=None,
                    created_at=record["created_at"],
                    idempotency_key=f"historical-message:{source_name}:{record['external_id']}",
                    thread_id=record["thread_id"],
                    message_id=record["message_id"],
                    imported_source=source_name,
                    emit_wakes=False,
                    imported_delivery_state="acknowledged",
                )
                self._conn.execute(
                    """INSERT INTO authority_import_receipts(
                        source, external_id, payload_sha256, message_id, imported_at
                    ) VALUES (?, ?, ?, ?, ?)""",
                    (
                        source_name,
                        record["external_id"],
                        record["payload_sha256"],
                        message.message_id,
                        _iso(),
                    ),
                )
                imported += 1
                message_ids.append(message.message_id)
        return HistoricalImportResult(imported, replayed, tuple(message_ids))

    def import_legacy_sqlite(self, source_path: Path, *, source: str = "legacy-broker") -> HistoricalImportResult:
        """Read a legacy bridge/channel SQLite database without modifying it."""
        path = Path(source_path).expanduser().resolve()
        if not path.is_file():
            raise AuthorityServiceError("legacy_source_db_not_found")
        source_name = _nonempty(source, field="source")
        conn = open_readonly(path)
        conn.row_factory = sqlite3.Row
        try:
            records: list[dict[str, Any]] = []
            if self._sqlite_table_exists(conn, "channels"):
                for row in conn.execute("SELECT * FROM channels ORDER BY name ASC"):
                    name = str(row["name"])
                    subscribers = self._split_legacy_csv(row["subscribers"])
                    self.create_channel(
                        name,
                        subscribers=subscribers,
                        legacy_import=True,
                        metadata={
                            "Source": source_name,
                            "Agent": "legacy-channel",
                            "Via": "sqlite-import",
                            "description": str(row["description"] or ""),
                            "include": str(row["include"] or ""),
                        },
                    )
            if self._sqlite_table_exists(conn, "messages"):
                for row in conn.execute("SELECT * FROM messages ORDER BY id ASC"):
                    records.append(self._legacy_bridge_record(row, source_name))
            if self._sqlite_table_exists(conn, "channel_messages"):
                delivery_map = self._legacy_delivery_map(conn)
                for row in conn.execute("SELECT * FROM channel_messages ORDER BY created_at ASC, message_id ASC"):
                    records.append(self._legacy_channel_record(row, source_name, delivery_map))
        finally:
            conn.close()
        return self.import_records(records, source=source_name)

    # ------------------------------------------------------------------
    # Internal transaction helpers

    def _publish_message_tx(
        self,
        *,
        sender: str,
        body: str,
        channel: str | None,
        targets: Iterable[AdmittedTarget] | None,
        kind: str,
        imported_recipients: Iterable[str] | None = None,
        conversation_id: str | None = None,
        in_reply_to: str | None = None,
        correlation_id: str | None = None,
        provenance: Mapping[str, Any] | None = None,
        context_revisions: Mapping[str, str] | None = None,
        deadline_at: str | None = None,
        created_at: str | None = None,
        idempotency_key: str,
        thread_id: str | None = None,
        message_id: str | None = None,
        imported_source: str | None = None,
        emit_wakes: bool = True,
        imported_delivery_state: str | None = None,
        deliver: bool = True,
    ) -> AuthorityMessage:
        """Write one immutable message and its deliveries.

        ``deliver=False`` records the message for a job, which is its own queue:
        no delivery is created. Explicit recipients of a delivered live message
        must be readable (``_require_readable_recipients``); stored channel
        subscribers no drain reads are skipped and their subscriptions end.
        """
        if not isinstance(body, str):
            raise AuthorityServiceError("message_body_must_be_text")
        if re.fullmatch(
            r"""(?:/|~/)\S*|(?:"(?:/|~/)[^"]*"|'(?:/|~/)[^']*'|`(?:/|~/)[^`]*`)"""
            r"(?::\d+(?::\d+)?)?",
            body.strip(),
        ):
            raise AuthorityServiceError("message_body_path_only: put the content in the message body")
        sender_name = _nonempty(sender, field="sender")
        kind_name = _nonempty(kind, field="kind")
        key = _nonempty(idempotency_key, field="idempotency_key")
        live_delivery = deliver and imported_source is None and imported_delivery_state is None
        channel_row = self._require_channel_tx(channel) if channel is not None else None
        subscriber_fanout = imported_recipients is None and targets is None and channel_row is not None
        explicit_recipients: tuple[str, ...] = ()
        if imported_recipients is not None:
            # Historical records keep the recipients they were written with.
            explicit_recipients = _normalize_recipients(imported_recipients)
        elif not subscriber_fanout:
            explicit_recipients = _normalize_recipients(
                require_admitted(target).recipient for target in targets or ()
            )
            _refuse_session_cto_recipients(explicit_recipients)
        requested_revisions = self._normalize_context_revisions(context_revisions, None)
        provenance_data = _safe_json_mapping(provenance)
        deadline = self._normalize_deadline(deadline_at)
        creation = self._normalize_created_at(created_at)
        # The idempotency payload keeps the caller's deadline so a retry matches.
        # A live publish that omits one still expires after 7 days. Imports keep
        # a null deadline so a historical replay is not expired on the next claim.
        stored_deadline = deadline
        if stored_deadline is None and imported_source is None and imported_delivery_state is None:
            stored_deadline = _iso(_parse_iso(creation, field="created_at") + timedelta(days=7))
        body_bytes = body.encode("utf-8")
        body_sha = _sha256_bytes(body_bytes)
        caller_fields = {
            "sender": sender_name,
            "body_sha256": body_sha,
            "channel_id": str(channel_row["channel_id"]) if channel_row is not None else None,
            "kind": kind_name,
            "conversation_id": conversation_id or "",
            "thread_id": thread_id or "",
            "in_reply_to": in_reply_to or "",
            "correlation_id": correlation_id or "",
            "provenance": provenance_data,
            "deadline_at": deadline or "",
            "imported_source": imported_source or "",
        }
        # The key binds what the caller asked for (#1620). The channel's current
        # context revision and subscribers, and a recipient's standing, are store
        # state that can change after a commit, so a retry after a crash finds its
        # receipt before any of them is read. The format tag keeps this digest
        # disjoint from the whole-payload digest that rows written earlier hold.
        # Omitted and empty reply ids behave differently (an empty parent is refused,
        # an omitted conversation is inherited), so the request digest keeps them apart.
        request = {
            **caller_fields,
            "conversation_id": conversation_id,
            "thread_id": thread_id,
            "in_reply_to": in_reply_to,
            "digest_format": "caller-request.v1",
            "recipients": None if subscriber_fanout else list(explicit_recipients),
            "context_revisions": requested_revisions,
            "deliver": deliver,
        }
        payload_sha = _sha256_json(request)
        for namespace in ("message", "message:reclassified-request"):
            row = self._conn.execute(
                """SELECT payload_sha256, subject_id FROM authority_idempotency
                   WHERE namespace = ? AND idempotency_key = ?""",
                (namespace, key),
            ).fetchone()
            if row is not None and str(row["payload_sha256"]) == payload_sha:
                return self.get_message(str(row["subject_id"]))

        recipients_norm = explicit_recipients
        if subscriber_fanout and channel_row is not None:
            subscriber_rows = self._conn.execute(
                """SELECT recipient FROM authority_channel_subscribers
                   WHERE channel_id = ? AND recipient != ? ORDER BY recipient ASC""",
                (str(channel_row["channel_id"]), sender_name),
            ).fetchall()
            durable = (_durable_subscriber(str(row["recipient"]), sender_name) for row in subscriber_rows)
            recipients_norm = _normalize_recipients(
                target.recipient for target in _admitted_subscribers(name for name in durable if name)
            )
            if deliver:
                recipients_norm = self._readable_subscribers_tx(channel_row, recipients_norm)
        supervisory_inbox = len(recipients_norm) == 1 and bool(_SUPERVISORY_RECIPIENT_RE.fullmatch(recipients_norm[0]))
        if live_delivery and kind_name == _SUPERVISORY_REQUEST_KIND and not supervisory_inbox:
            raise AuthorityServiceError("supervisory_recipient_required")
        if live_delivery and not subscriber_fanout:
            # Only an exact caller-request hit above skips admission; a new message
            # or a row in the earlier format still needs a readable recipient.
            _require_readable_recipients(recipients_norm)
        revisions = self._normalize_context_revisions(context_revisions, channel_row)
        # The whole-payload digest that rows written before #1620 hold.
        payload = {
            **caller_fields,
            "recipients": list(recipients_norm),
            "context_revisions": revisions,
        }
        namespace = "message"
        if live_delivery and kind_name == "request" and len(recipients_norm) == 1 and not supervisory_inbox:
            # Reissue the same misrouted request as mail. Both records keep their key
            # and payload binding, and the old delivery keeps its original deadline.
            misrouted = (
                _sha256_json({**request, "kind": _SUPERVISORY_REQUEST_KIND}),
                _sha256_json({**payload, "kind": _SUPERVISORY_REQUEST_KIND}),
            )
            if self._conn.execute(
                """SELECT 1 FROM authority_idempotency
                   WHERE namespace = 'message' AND idempotency_key = ? AND payload_sha256 IN (?, ?)""",
                (key, *misrouted),
            ).fetchone() is not None:
                namespace = "message:reclassified-request"
        existing_id = self._check_idempotency_tx(
            namespace, key, payload_sha, legacy_sha256=_sha256_json(payload)
        )
        if existing_id is not None:
            existing = self.get_message(existing_id)
            # The earlier format did not bind the delivery mode. A queue-only message
            # has no deliveries, a live one has one per recipient, explicit or fan-out.
            if (not deliver and existing.delivery_ids) or (
                live_delivery and len(existing.delivery_ids) != len(recipients_norm)
            ):
                raise AuthorityServiceError("idempotency_key_reused_with_different_payload")
            return existing

        mid = message_id or new_id("message")
        if self._conn.execute("SELECT 1 FROM comms_messages WHERE message_id = ?", (mid,)).fetchone():
            raise AuthorityServiceError("message_id_already_exists")
        conversation = conversation_id
        if in_reply_to is not None:
            parent = self._conn.execute(
                "SELECT conversation_id FROM comms_messages WHERE message_id = ?", (in_reply_to,)
            ).fetchone()
            if parent is None:
                # Historical batches may include a child before its parent.
                # The import transaction defers FK checks until every immutable
                # record is inserted, but live authority writes still fail fast.
                if imported_source is None or conversation is None:
                    raise AuthorityServiceError("reply_parent_not_found")
            else:
                parent_conversation = str(parent["conversation_id"])
                if conversation is None:
                    conversation = parent_conversation
                elif conversation != parent_conversation:
                    raise AuthorityServiceError("reply_thread_mismatch")
        conversation = conversation or new_id("conversation")
        thread = _nonempty(thread_id, field="thread_id") if thread_id is not None else conversation
        self._conn.execute(
            """INSERT OR IGNORE INTO conversations(conversation_id, created_at, source, title)
               VALUES (?, ?, ?, ?)""",
            (
                conversation,
                creation,
                "authority-import" if imported_source else "authority",
                str(channel_row["name"]) if channel_row is not None else None,
            ),
        )
        artifact = self.store.store_bytes(
            body_bytes,
            producer="authority-import" if imported_source else "authority-message",
            retention_class="historical-message" if imported_source else "message-body",
            mime_type="text/plain; charset=utf-8",
            logical_filename=f"{mid}.txt",
            commit=False,
        )
        recipient_value = recipients_norm[0] if len(recipients_norm) == 1 else None
        self._conn.execute(
            """INSERT INTO comms_messages(
                message_id, conversation_id, in_reply_to, kind, sender, recipient,
                body_inline, body_artifact_id, content_sha256, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                mid,
                conversation,
                in_reply_to,
                kind_name,
                sender_name,
                recipient_value,
                body[:500],
                artifact.artifact_id,
                body_sha,
                _canonical_json({"authority": True, "body_artifact_id": artifact.artifact_id}),
                creation,
            ),
        )
        self.store.reference(mid, artifact.artifact_id, relation="body", commit=False)
        self._conn.execute(
            """INSERT INTO authority_message_metadata(
                message_id, channel_id, thread_id, correlation_id, context_revisions_json,
                provenance_json, imported_source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                mid,
                str(channel_row["channel_id"]) if channel_row is not None else None,
                thread,
                correlation_id,
                _canonical_json(revisions),
                _canonical_json(provenance_data),
                imported_source,
                creation,
            ),
        )
        for recipient in recipients_norm if deliver else ():
            delivery_id = new_id("authority-delivery")
            delivery_state = imported_delivery_state or "queued"
            terminal_sha = _sha256_bytes(b"") if delivery_state == "acknowledged" else None
            completed_at = creation if delivery_state in _DELIVERY_TERMINAL else None
            self._conn.execute(
                """INSERT INTO authority_deliveries(
                    delivery_id, message_id, recipient, state, deadline_at, terminal_sha256,
                    created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    delivery_id,
                    mid,
                    recipient,
                    delivery_state,
                    stored_deadline,
                    terminal_sha,
                    creation,
                    creation,
                    completed_at,
                ),
            )
            if emit_wakes and delivery_state == "queued":
                self._record_wake_receipt_tx(
                    delivery_id, recipient, 0, state="emitted", now=creation
                )
        self._insert_idempotency_tx(namespace, key, payload_sha, mid)
        # One receipt shape for the first publish and every retry.
        return self.get_message(mid)

    def _readable_subscribers_tx(self, channel_row: sqlite3.Row, names: Sequence[str]) -> tuple[str, ...]:
        """Fan-out targets a drain reads; end the subscription of a settled task or unregistered name.

        A subscriber whose standing cannot be checked keeps its subscription and
        its delivery, and while the roster is incomplete no subscription is
        ended at all (fail closed: nothing is ended on doubt).
        """
        roster = _load_recipient_roster()
        if not roster.complete:
            return tuple(names)
        kept: list[str] = []
        for name in names:
            standing = _recipient_standing(name, roster)
            if standing in {"settled_task", "not_registered"}:
                self._end_subscriptions_tx(name, channel_id=str(channel_row["channel_id"]))
                continue
            kept.append(name)
        return tuple(kept)

    def _end_subscriptions_tx(self, recipient: str, *, channel_id: str | None = None) -> int:
        """Remove future fan-out to ``recipient``; past messages and deliveries are kept."""
        if channel_id is None:
            cursor = self._conn.execute(
                "DELETE FROM authority_channel_subscribers WHERE recipient = ?", (recipient,)
            )
        else:
            cursor = self._conn.execute(
                "DELETE FROM authority_channel_subscribers WHERE channel_id = ? AND recipient = ?",
                (channel_id, recipient),
            )
        return int(cursor.rowcount or 0)

    def _enqueue_job_tx(
        self,
        *,
        job_kind: AuthorityJobKind,
        subject_id: str,
        payload: Mapping[str, Any],
        deadline_at: str | None,
        idempotency_key: str,
    ) -> AuthorityJob:
        if job_kind not in _JOB_KINDS:
            raise AuthorityServiceError("invalid_job_kind")
        key = _nonempty(idempotency_key, field="idempotency_key")
        payload_data = _safe_json_mapping(payload)
        existing = self._find_job_by_key_tx(job_kind, key)
        if existing is not None:
            self._assert_job_payload(existing, payload_data)
            return existing
        subject_row = self._conn.execute(
            """SELECT * FROM authority_jobs
               WHERE job_kind = ? AND subject_id = ?""",
            (job_kind, subject_id),
        ).fetchone()
        if subject_row is not None:
            existing_subject = self._job_from_row(subject_row)
            self._assert_job_payload(existing_subject, payload_data)
            return existing_subject
        artifact = self.store.store_text(
            _canonical_json(payload_data),
            producer=f"authority-job:{job_kind}",
            retention_class="authority-job-payload",
            logical_filename=f"{job_kind}.json",
            mime_type="application/json; charset=utf-8",
            commit=False,
        )
        job_id = new_id("authority-job")
        now = _iso()
        deadline = self._normalize_deadline(deadline_at)
        self._conn.execute(
            """INSERT INTO authority_jobs(
                job_id, job_kind, subject_id, payload_artifact_id, state, deadline_at,
                idempotency_key, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)""",
            (job_id, job_kind, subject_id, artifact.artifact_id, deadline, key, now, now),
        )
        self._append_job_event_tx(job_id, 0, "enqueued", "queued", {})
        self._insert_idempotency_tx(f"job:{job_kind}", key, _sha256_json(payload_data), job_id)
        return self.get_job(job_id)

    def _find_job_by_key_tx(self, job_kind: str, idempotency_key: str) -> AuthorityJob | None:
        row = self._conn.execute(
            """SELECT * FROM authority_jobs
               WHERE job_kind = ? AND idempotency_key = ?""",
            (job_kind, idempotency_key),
        ).fetchone()
        return self._job_from_row(row) if row is not None else None

    def _assert_job_payload(
        self,
        job: AuthorityJob,
        expected_payload: Mapping[str, Any],
        *,
        subset: bool = False,
    ) -> None:
        try:
            actual = json_depth.loads(self.store.read_bytes(job.payload_artifact_id).decode("utf-8"), max_depth=512)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuthorityServiceError("job_payload_corrupt") from exc
        if not isinstance(actual, dict):
            raise AuthorityServiceError("job_payload_corrupt")
        expected = _safe_json_mapping(expected_payload)
        matches = (
            all(actual.get(key) == value for key, value in expected.items())
            if subset
            else actual == expected
        )
        if not matches:
            raise AuthorityServiceError("idempotency_key_reused_with_different_payload")

    def _reclaim_expired_jobs_tx(self, now: str) -> int:
        """Expire or requeue abandoned jobs; a stored Kimi job (legacy) is left as it is."""
        reclaimed = 0
        expired_rows = self._conn.execute(
            """SELECT job_id, fence_token, payload_artifact_id FROM authority_jobs
               WHERE state IN ('queued', 'running') AND deadline_at IS NOT NULL
                 AND deadline_at <= ?""",
            (now,),
        ).fetchall()
        for row in expired_rows:
            if self._stored_kimi_job(row):
                continue
            self._conn.execute(
                """UPDATE authority_jobs
                   SET state = 'expired', lease_owner = NULL, lease_expires_at = NULL,
                       updated_at = ?, completed_at = ? WHERE job_id = ?""",
                (now, now, str(row["job_id"])),
            )
            self._append_job_event_tx(
                str(row["job_id"]), int(row["fence_token"]), "expired", "expired", {}
            )
            self._dead_letter_job_tx(str(row["job_id"]), reason_code="deadline_expired")
            reclaimed += 1
        stale_rows = self._conn.execute(
            """SELECT job_id, fence_token, payload_artifact_id FROM authority_jobs
               WHERE state = 'running' AND lease_expires_at IS NOT NULL
                 AND lease_expires_at <= ?
                 AND (deadline_at IS NULL OR deadline_at > ?)""",
            (now, now),
        ).fetchall()
        for row in stale_rows:
            if self._stored_kimi_job(row):
                continue
            self._conn.execute(
                """UPDATE authority_jobs
                   SET state = 'queued', lease_owner = NULL, lease_expires_at = NULL,
                       updated_at = ? WHERE job_id = ?""",
                (now, str(row["job_id"])),
            )
            self._append_job_event_tx(
                str(row["job_id"]), int(row["fence_token"]), "reclaimed", "queued", {}
            )
            reclaimed += 1
        return reclaimed

    def _reclaim_expired_deliveries_tx(
        self, now: str, *, max_attempts: int, cleanup_ids: frozenset[str] | None = None,
    ) -> int:
        """Expire or requeue abandoned deliveries; a stored Kimi delivery (legacy) is left as it is.

        ``cleanup_ids`` is given only by the operator's stale-delivery cleanup.
        Its rows dead-letter as ``CLEANUP_EXPIRY_REASON`` and share one summary
        notice. Any other row past a real deadline stays ``deadline_expired``
        with its per-row notice; inside a cleanup batch a stored session-CTO
        recipient is notified through the durable CTO, so its refusal cannot
        roll back the batch.

        Every pass also expires the open deliveries to a task that has settled,
        which no drain will read (#594), as ``recipient_settled`` with a per-row
        notice, and ends that task's subscriptions. A stored Kimi delivery is
        left as it is here too.
        """
        reclaimed = 0
        cleanup_expired: list[str] = []
        expired = self._conn.execute(
            """SELECT delivery_id, fence_token, recipient, message_id FROM authority_deliveries
               WHERE state IN ('queued', 'running') AND deadline_at IS NOT NULL
                 AND deadline_at <= ?""",
            (now,),
        ).fetchall()
        for row in expired:
            if self._stored_kimi_delivery(row):
                continue
            delivery_id = str(row["delivery_id"])
            self._conn.execute(
                """UPDATE authority_deliveries
                   SET state = 'expired', lease_owner = NULL, lease_expires_at = NULL,
                       updated_at = ?, completed_at = ? WHERE delivery_id = ?""",
                (now, now, delivery_id),
            )
            self._conn.execute(
                """UPDATE authority_delivery_attempts
                   SET state = 'expired', finished_at = ?
                   WHERE delivery_id = ? AND fence_token = ? AND finished_at IS NULL""",
                (now, delivery_id, int(row["fence_token"])),
            )
            if cleanup_ids is None:
                self._dead_letter_delivery_tx(delivery_id, reason_code="deadline_expired")
            elif delivery_id in cleanup_ids:
                self._dead_letter_delivery_tx(delivery_id, reason_code=CLEANUP_EXPIRY_REASON, notify=False)
                cleanup_expired.append(delivery_id)
            else:
                self._dead_letter_delivery_tx(delivery_id, reason_code="deadline_expired", notify=False)
                self._notify_coordinator_reply_failure_tx(
                    delivery_id, reason_code="deadline_expired", session_via_durable_cto=True,
                )
            reclaimed += 1
        if cleanup_expired:
            self._notify_cleanup_expiry_summary_tx(cleanup_expired)
        if cleanup_ids is None:
            roster = _load_recipient_roster()
            for row, reason in self._unreadable_deliveries_tx(roster, settled_only=True):
                if self._stored_kimi_delivery(row):
                    continue
                delivery_id = str(row["delivery_id"])
                self._conn.execute(
                    """UPDATE authority_deliveries
                       SET state = 'expired', lease_owner = NULL, lease_expires_at = NULL,
                           updated_at = ?, completed_at = ? WHERE delivery_id = ?""",
                    (now, now, delivery_id),
                )
                self._conn.execute(
                    """UPDATE authority_delivery_attempts
                       SET state = 'expired', finished_at = ?
                       WHERE delivery_id = ? AND fence_token = ? AND finished_at IS NULL""",
                    (now, delivery_id, int(row["fence_token"])),
                )
                self._dead_letter_delivery_tx(delivery_id, reason_code=reason, notify=False)
                self._notify_coordinator_reply_failure_tx(
                    delivery_id, reason_code=reason, session_via_durable_cto=True,
                )
                reclaimed += 1
            self._end_unreadable_subscriptions_tx(roster, settled_only=True)
        stale = self._conn.execute(
            """SELECT d.delivery_id, d.fence_token, d.attempt_count, d.recipient, d.message_id, m.kind
               FROM authority_deliveries d JOIN comms_messages m USING (message_id)
               WHERE d.state = 'running' AND d.lease_expires_at IS NOT NULL
                 AND d.lease_expires_at <= ?
                 AND (d.deadline_at IS NULL OR d.deadline_at > ?)""",
            (now, now),
        ).fetchall()
        for row in stale:
            if row["kind"] == _SUPERVISORY_REQUEST_KIND and not _SUPERVISORY_RECIPIENT_RE.fullmatch(row["recipient"]):
                continue
            if self._stored_kimi_delivery(row):
                continue
            delivery_id = str(row["delivery_id"])
            terminal = int(row["attempt_count"]) >= max_attempts
            next_state = "dead_lettered" if terminal else "queued"
            self._conn.execute(
                """UPDATE authority_deliveries
                   SET state = ?, lease_owner = NULL, lease_expires_at = NULL,
                       updated_at = ?, completed_at = CASE WHEN ? THEN ? ELSE completed_at END
                   WHERE delivery_id = ?""",
                (next_state, now, terminal, now, delivery_id),
            )
            self._conn.execute(
                """UPDATE authority_delivery_attempts
                   SET state = ?, finished_at = ?
                   WHERE delivery_id = ? AND fence_token = ? AND finished_at IS NULL""",
                ("dead_lettered" if terminal else "reclaimed", now, delivery_id, int(row["fence_token"])),
            )
            if terminal:
                self._dead_letter_delivery_tx(delivery_id, reason_code="attempts_exhausted")
            reclaimed += 1
        return reclaimed

    def _stored_kimi_job(self, row: sqlite3.Row) -> bool:
        """True when a stored job is addressed to a Kimi seat or model (legacy: enqueue refuses Kimi).

        Every generic reclaim, expiry, requeue, dead-letter and claim path
        skips such a job before any write. The target is read, never written,
        from the job's immutable payload artifact: a request's ``recipient``
        and ``metadata`` (a ``requested_model``), a discussion's
        ``participants``; a formal review names none. A payload that cannot be
        read is not provably non-Kimi, so it is skipped too (fail closed).
        """
        artifact_id = str(row["payload_artifact_id"])
        cached = self._kimi_payloads.get(artifact_id)
        if cached is not None:
            return cached
        try:
            payload = json_depth.loads(self.store.read_bytes(artifact_id).decode("utf-8"), max_depth=512)
        except (ArtifactStoreError, UnicodeDecodeError, json.JSONDecodeError):
            return True
        if not isinstance(payload, dict):
            return True
        recipients = [payload.get("recipient"), *(payload.get("participants") or ())]
        addressed = stored_kimi_request(
            (name for name in recipients if name), attachments=(payload.get("metadata") or {},)
        )
        self._kimi_payloads[artifact_id] = addressed
        return addressed

    def _stored_kimi_delivery(self, row: sqlite3.Row) -> bool:
        """True when a stored delivery is to a Kimi seat, or delivers a request job pinned to a Kimi model (legacy)."""
        if stored_kimi_row(row["recipient"]):
            return True
        jobs = self._conn.execute(
            """SELECT payload_artifact_id FROM authority_jobs
               WHERE job_kind = 'request' AND subject_id = ?""",
            (str(row["message_id"]),),
        ).fetchall()
        return any(self._stored_kimi_job(job) for job in jobs)

    def _record_wake_receipt_tx(
        self,
        delivery_id: str,
        recipient: str,
        fence_token: int,
        *,
        state: str,
        now: str,
    ) -> None:
        requested_rank = _WAKE_STATES[state]
        row = self._conn.execute(
            """SELECT wake_id, state FROM authority_wake_receipts
               WHERE delivery_id = ? AND fence_token = ?""",
            (delivery_id, fence_token),
        ).fetchone()
        if row is None:
            self._conn.execute(
                """INSERT INTO authority_wake_receipts(
                    wake_id, delivery_id, recipient, fence_token, state, emitted_at, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_id("authority-wake"),
                    delivery_id,
                    recipient,
                    fence_token,
                    state,
                    now,
                    now if state in {"received", "consumed"} else None,
                ),
            )
            return
        current_rank = _WAKE_STATES[str(row["state"])]
        if requested_rank < current_rank:
            raise AuthorityServiceError("wake_receipt_state_regression")
        if requested_rank == current_rank:
            return
        self._conn.execute(
            """UPDATE authority_wake_receipts
               SET state = ?, received_at = COALESCE(received_at, ?)
               WHERE wake_id = ?""",
            (state, now, str(row["wake_id"])),
        )

    def _dead_letter_delivery_tx(self, delivery_id: str, *, reason_code: str, notify: bool = True) -> None:
        self._conn.execute(
            """INSERT OR IGNORE INTO authority_dead_letters(
                dead_letter_id, delivery_id, reason_code, created_at
            ) VALUES (?, ?, ?, ?)""",
            (new_id("authority-dead-letter"), delivery_id, reason_code, _iso()),
        )
        if notify:
            self._notify_coordinator_reply_failure_tx(delivery_id, reason_code=reason_code)

    def _notify_cleanup_expiry_summary_tx(
        self, delivery_ids: list[str], *, reason_code: str = CLEANUP_EXPIRY_REASON,
    ) -> bool:
        """One delivery-error for a batch of cleanup expiries, addressed only to the durable CTO.

        Per-row notices would flood the coordinator and a legacy session recipient
        would abort the whole batch. Rows whose sender is not the coordinator are
        omitted, matching the per-row rule.
        """
        noticed: list[str] = []
        for start in range(0, len(delivery_ids), 100):
            chunk = delivery_ids[start:start + 100]
            placeholders = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                f"""SELECT d.delivery_id, d.recipient, m.sender
                    FROM authority_deliveries d JOIN comms_messages m USING (message_id)
                    WHERE d.delivery_id IN ({placeholders})""",
                chunk,
            ).fetchall()
            for row in rows:
                if row["sender"] == DURABLE_CTO_RECIPIENT and row["recipient"] != DURABLE_CTO_RECIPIENT:
                    noticed.append(str(row["delivery_id"]))
        if not noticed:
            return False
        ids = sorted(noticed)
        digest = _sha256_bytes(",".join(ids).encode())[:32]
        self._publish_message_tx(
            sender="fleet-comms", channel=None, kind="delivery-error",
            body=_canonical_json({
                "error": "coordinator_reply_delivery_failed",
                "reason_code": reason_code,
                "expired_count": len(ids),
            }),
            targets=_admit_recipients((DURABLE_CTO_RECIPIENT,), mode=BRIDGE_MODE),
            idempotency_key=(
                f"coordinator-reply-error:summary:{digest}" if reason_code == CLEANUP_EXPIRY_REASON
                else f"coordinator-reply-error:summary:{reason_code}:{digest}"
            ),
        )
        return True

    @staticmethod
    def _session_only_launcher_inbox(name: str) -> bool:
        """True when ``name`` is a launcher slot or a driver-lane sender.

        Those inboxes are read only while a session is up. A bare static
        identity that is not itself a slot stays with the standing check.
        An unreadable slot or lane registry fails closed: the notice is not
        queued back to ``name``.
        """
        from lu_machinery import agent_identity
        from lu_machinery.ops import driver_lanes

        try:
            slots = set(agent_identity._load_registry_slots(strict=True))
        except Exception as exc:
            _LOG.warning("launcher inbox check unreadable: %s", type(exc).__name__)
            return True
        if name in slots:
            return True
        if name in agent_identity.STATIC_VALID_AGENTS:
            return False
        try:
            senders = {lane.sender for lane in driver_lanes.load_registry().lanes}
        except Exception as exc:
            _LOG.warning("launcher inbox check unreadable: %s", type(exc).__name__)
            return True
        return name in senders

    def _notify_coordinator_reply_failure_tx(
        self, delivery_id: str, *, reason_code: str, session_via_durable_cto: bool = False,
    ) -> bool:
        """Notify the durable CTO, and the original recipient when a drain still reads it.

        With ``session_via_durable_cto`` a stored session-specific CTO recipient,
        which admission refuses as a new target, is notified only through the
        durable CTO, as ``_durable_subscriber`` maps stored subscribers.
        """
        row = self._conn.execute(
            """SELECT d.message_id, d.recipient, m.sender
               FROM authority_deliveries d JOIN comms_messages m USING (message_id)
               WHERE d.delivery_id = ?""",
            (delivery_id,),
        ).fetchone()
        if row is None or row["sender"] != DURABLE_CTO_RECIPIENT or row["recipient"] == DURABLE_CTO_RECIPIENT:
            return False
        key = f"coordinator-reply-error:{delivery_id}"
        if self._conn.execute(
            "SELECT 1 FROM authority_idempotency WHERE namespace = 'message' AND idempotency_key = ?",
            (key,),
        ).fetchone():
            return True
        # Notifications have a different sender, so their own expiry cannot recurse.
        # The original recipient is told only if a drain still reads it (#594).
        # A launcher slot is registered, but nothing reads it between sessions, so
        # a copy queued there is the next week's dead letter (machinery-infra#1628).
        recipient = str(row["recipient"])
        standing = _recipient_standing(recipient, _load_recipient_roster())
        if session_via_durable_cto and is_session_cto_recipient(recipient):
            parties: tuple[str, ...] = (DURABLE_CTO_RECIPIENT,)
        elif standing == "live_task" or (standing == "registered" and not self._session_only_launcher_inbox(recipient)):
            parties = (recipient, DURABLE_CTO_RECIPIENT)
        else:
            parties = (DURABLE_CTO_RECIPIENT,)
        self._publish_message_tx(
            sender="fleet-comms", channel=None, kind="delivery-error",
            body=_canonical_json({
                "error": "coordinator_reply_delivery_failed",
                "reason_code": reason_code,
                "delivery_id": delivery_id,
                "message_id": str(row["message_id"]),
                "recipient": str(row["recipient"]),
            }),
            targets=_admit_recipients(parties, mode=BRIDGE_MODE),
            in_reply_to=str(row["message_id"]),
            idempotency_key=key,
        )
        return True

    def _dead_letter_job_tx(self, job_id: str, *, reason_code: str) -> None:
        self._conn.execute(
            """INSERT OR IGNORE INTO authority_dead_letters(
                dead_letter_id, job_id, reason_code, created_at
            ) VALUES (?, ?, ?, ?)""",
            (new_id("authority-dead-letter"), job_id, reason_code, _iso()),
        )

    def _append_job_event_tx(
        self,
        job_id: str,
        fence_token: int,
        event_type: str,
        state: str,
        metadata: Mapping[str, Any],
    ) -> None:
        self._conn.execute(
            """INSERT INTO authority_job_events(
                event_id, job_id, fence_token, event_type, state, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id("authority-job-event"),
                job_id,
                fence_token,
                event_type,
                state,
                _canonical_json(_safe_json_mapping(metadata)),
                _iso(),
            ),
        )

    # ------------------------------------------------------------------
    # Historical import helpers

    def _normalize_import_record(self, raw: Mapping[str, Any], source: str) -> dict[str, Any]:
        data = dict(raw)
        external_id = _nonempty(
            str(data.get("external_id") or data.get("id") or data.get("message_id") or ""),
            field="external_id",
        )
        body_raw = data.get("body", data.get("content", ""))
        if not isinstance(body_raw, str):
            raise AuthorityServiceError("historical_body_must_be_text")
        body_sha = _sha256_bytes(body_raw.encode("utf-8"))
        declared_hash = data.get("body_sha256") or data.get("content_sha256")
        if declared_hash is not None and str(declared_hash).lower() != body_sha:
            raise AuthorityServiceError("historical_body_hash_mismatch")
        channel = data.get("channel")
        if channel is not None:
            channel = _nonempty(str(channel), field="channel")
        sender = _nonempty(
            str(data.get("sender") or data.get("from_agent") or data.get("from_llm") or "legacy"),
            field="sender",
        )
        recipients_raw = data.get("recipients")
        if recipients_raw is None:
            singular = data.get("recipient") or data.get("to_agent") or data.get("to_llm")
            recipients_raw = () if singular is None else (str(singular),)
        if isinstance(recipients_raw, str):
            recipients_raw = (recipients_raw,)
        recipients = _normalize_recipients(recipients_raw)
        conversation_external = str(
            data.get("conversation_id") or data.get("thread_id") or data.get("task_id") or external_id
        )
        conversation_id = "legacy-conversation-" + _sha256_bytes(
            f"{source}:{conversation_external}".encode()
        )[:32]
        reply_raw = data.get("in_reply_to") or data.get("parent_id")
        in_reply_to = (
            "legacy-message-" + _sha256_bytes(f"{source}:{reply_raw}".encode())[:32]
            if reply_raw
            else None
        )
        message_id = "legacy-message-" + _sha256_bytes(f"{source}:{external_id}".encode())[:32]
        provenance = _safe_json_mapping(data.get("provenance") if isinstance(data.get("provenance"), Mapping) else None)
        provenance.setdefault("Source", str(data.get("Source") or source))
        provenance.setdefault("Agent", str(data.get("Agent") or sender))
        provenance.setdefault("Via", str(data.get("Via") or "historical-import"))
        provenance.setdefault("LegacyThreadId", conversation_external)
        if reply_raw:
            provenance.setdefault("LegacyReplyTo", str(reply_raw))
        metadata = data.get("metadata")
        if isinstance(metadata, Mapping):
            provenance.setdefault("metadata", _safe_json_mapping(metadata))
        context_raw = data.get("context_revisions")
        context_revisions: dict[str, str] = {}
        if isinstance(context_raw, Mapping):
            for key, value in context_raw.items():
                revision = str(value).strip()
                if revision:
                    context_revisions[_nonempty(str(key), field="context_revision_key")] = revision
        original_timestamp = str(data.get("created_at") or data.get("timestamp") or "")
        created_at = self._normalize_created_at(original_timestamp)
        if original_timestamp:
            provenance.setdefault("OriginalTimestamp", original_timestamp)
        payload = {
            "external_id": external_id,
            "message_id": message_id,
            "conversation_id": conversation_id,
            "thread_id": conversation_external,
            "in_reply_to": in_reply_to,
            "channel": channel,
            "sender": sender,
            "recipients": list(recipients),
            "kind": str(data.get("kind") or data.get("message_type") or "message"),
            "body_sha256": body_sha,
            "correlation_id": data.get("correlation_id"),
            "provenance": provenance,
            "context_revisions": context_revisions,
            "created_at": created_at,
        }
        return {
            **payload,
            "body": body_raw,
            "payload_sha256": _sha256_json(payload),
        }

    @staticmethod
    def _sqlite_table_exists(conn: SQLiteConnection, name: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone() is not None

    @staticmethod
    def _split_legacy_csv(value: Any) -> tuple[str, ...]:
        return tuple(item.strip() for item in str(value or "").split(",") if item.strip())

    def _legacy_delivery_map(self, conn: SQLiteConnection) -> dict[str, tuple[str, ...]]:
        if not self._sqlite_table_exists(conn, "deliveries"):
            return {}
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(deliveries)")}
        if not {"message_id", "to_agent"}.issubset(columns):
            return {}
        mapped: dict[str, list[str]] = {}
        for row in conn.execute("SELECT message_id, to_agent FROM deliveries"):
            mapped.setdefault(str(row["message_id"]), []).append(str(row["to_agent"]))
        return {key: tuple(sorted(set(value))) for key, value in mapped.items()}

    def _legacy_bridge_record(self, row: sqlite3.Row, source: str) -> dict[str, Any]:
        data_value = self._sqlite_row_optional(row, "data")
        metadata: dict[str, Any] = {}
        if data_value:
            try:
                parsed = json_depth.loads(str(data_value), max_depth=512)
                if isinstance(parsed, dict):
                    metadata = parsed
            except json.JSONDecodeError:
                metadata = {"legacy_data_sha256": _sha256_bytes(str(data_value).encode("utf-8"))}
        return {
            "external_id": f"bridge:{row['id']}",
            "body": str(row["content"] or ""),
            "sender": str(row["from_llm"]),
            "recipient": str(row["to_llm"]),
            "kind": str(row["message_type"] or "message"),
            "conversation_id": str(row["task_id"] or row["id"]),
            "created_at": str(row["timestamp"]),
            "provenance": {
                "Source": source,
                "Agent": str(row["from_llm"]),
                "Via": "legacy-bridge",
                "metadata": metadata,
            },
        }

    def _legacy_channel_record(
        self,
        row: sqlite3.Row,
        source: str,
        delivery_map: Mapping[str, tuple[str, ...]],
    ) -> dict[str, Any]:
        message_id = str(row["message_id"])
        parent_id = row["parent_id"]
        return {
            "external_id": f"channel:{message_id}",
            "body": str(row["body"] or ""),
            "channel": str(row["channel"]),
            "sender": str(row["from_agent"]),
            "recipients": delivery_map.get(message_id, ()),
            "kind": str(row["kind"] or "post"),
            "conversation_id": str(row["thread_id"]),
            # Channel records use a namespaced external identity. Apply the
            # same namespace to their parent link so normalization resolves
            # both sides to the same immutable authority message ID.
            "in_reply_to": f"channel:{parent_id}" if parent_id else None,
            "correlation_id": row["correlation_id"],
            "context_revisions": {
                "shared": str(row["context_rev_shared"] or ""),
                "channel": str(row["context_rev_channel"] or ""),
            },
            "created_at": str(row["created_at"]),
            "provenance": {
                "Source": source,
                "Agent": str(row["from_agent"]),
                "Via": "legacy-channel",
            },
        }

    @staticmethod
    def _sqlite_row_optional(row: sqlite3.Row, key: str) -> Any:
        """Read an optional legacy column without assuming one schema version."""
        return row[key] if key in set(row.keys()) else None

    # ------------------------------------------------------------------
    # Small validation / row conversion helpers

    def _require_authority_schema(self) -> None:
        row = self._conn.execute(
            "SELECT MAX(version) AS version FROM comms_schema_migrations"
        ).fetchone()
        applied = int(row["version"] or 0) if row is not None else 0
        known = MIGRATIONS[-1].version if MIGRATIONS else 0
        if known < _AUTHORITY_SCHEMA_VERSION or applied < _AUTHORITY_SCHEMA_VERSION:
            raise AuthorityServiceError("authority_schema_migration_required")

    def _ensure_channel(self, channel: str) -> None:
        try:
            self.get_channel(channel)
        except AuthorityServiceError as exc:
            if str(exc) != "channel_not_found":
                raise
            self.create_channel(channel)

    def _ensure_channel_tx(self, channel: str) -> sqlite3.Row:
        channel_name = _nonempty(channel, field="channel")
        row = self._conn.execute(
            "SELECT * FROM authority_channels WHERE name = ?", (channel_name,)
        ).fetchone()
        if row is not None:
            return row
        channel_id = new_id("authority-channel")
        now = _iso()
        self._conn.execute(
            """INSERT INTO authority_channels(channel_id, name, metadata_json, created_at)
               VALUES (?, ?, '{}', ?)""",
            (channel_id, channel_name, now),
        )
        created = self._conn.execute(
            "SELECT * FROM authority_channels WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        if created is None:  # pragma: no cover - same transaction
            raise AuthorityServiceError("channel_create_lost")
        return created

    def _require_channel_tx(self, channel: str | None) -> sqlite3.Row:
        channel_name = _nonempty(channel, field="channel")
        row = self._conn.execute(
            "SELECT * FROM authority_channels WHERE name = ?", (channel_name,)
        ).fetchone()
        if row is None:
            raise AuthorityServiceError("channel_not_found")
        return row

    def _normalize_context_revisions(
        self,
        values: Mapping[str, str] | None,
        channel_row: sqlite3.Row | None,
    ) -> dict[str, str]:
        normalized: dict[str, str] = {}
        if values is not None:
            for key, value in values.items():
                key_name = _nonempty(str(key), field="context_revision_key")
                value_name = str(value).strip()
                if value_name:
                    normalized[key_name] = value_name
        if channel_row is not None and channel_row["current_context_revision_id"] is not None:
            normalized.setdefault("channel", str(channel_row["current_context_revision_id"]))
        return normalized

    def _normalize_deadline(self, value: str | None) -> str | None:
        if value is None:
            return None
        return _iso(_parse_iso(value, field="deadline_at"))

    def _normalize_created_at(self, value: Any) -> str:
        if value is None or not str(value).strip():
            return _iso()
        return _iso(_parse_iso(str(value), field="created_at"))

    def _now_string(self, value: str | None) -> str:
        return self._normalize_created_at(value)

    @staticmethod
    def _normalize_job_kinds(values: Iterable[AuthorityJobKind] | None) -> frozenset[str]:
        if values is None:
            return frozenset()
        result = frozenset(str(value) for value in values)
        if not result.issubset(_JOB_KINDS):
            raise AuthorityServiceError("invalid_job_kind")
        return result

    def _check_idempotency_tx(
        self, namespace: str, key: str, payload_sha256: str, *, legacy_sha256: str | None = None
    ) -> str | None:
        """Return the subject a key already names, or refuse a different payload under it.

        ``legacy_sha256`` is the digest an earlier record format stored for the
        same request; it is accepted as well, never written.
        """
        row = self._conn.execute(
            """SELECT payload_sha256, subject_id FROM authority_idempotency
               WHERE namespace = ? AND idempotency_key = ?""",
            (namespace, key),
        ).fetchone()
        if row is None:
            return None
        if str(row["payload_sha256"]) not in {payload_sha256, legacy_sha256}:
            raise AuthorityServiceError("idempotency_key_reused_with_different_payload")
        return str(row["subject_id"])

    def _insert_idempotency_tx(
        self, namespace: str, key: str, payload_sha256: str, subject_id: str
    ) -> None:
        self._conn.execute(
            """INSERT INTO authority_idempotency(
                namespace, idempotency_key, payload_sha256, subject_id, created_at
            ) VALUES (?, ?, ?, ?, ?)""",
            (namespace, key, payload_sha256, subject_id, _iso()),
        )

    def _result_artifact_tx(
        self,
        *,
        result: bytes | None,
        result_artifact_id: str | None,
        producer: str,
    ) -> tuple[ArtifactRecord | None, str]:
        if result_artifact_id is not None:
            artifact = self._require_artifact_tx(result_artifact_id)
            return artifact, artifact.sha256
        if result is not None:
            artifact = self.store.store_bytes(
                result,
                producer=producer,
                retention_class="authority-result",
                mime_type="application/octet-stream",
                logical_filename="result.bin",
                commit=False,
            )
            return artifact, artifact.sha256
        return None, _sha256_bytes(b"")

    def _require_artifact_tx(self, artifact_id: str) -> ArtifactRecord:
        try:
            return self.store.get(artifact_id)
        except Exception as exc:
            raise AuthorityServiceError("artifact_not_found") from exc

    def _require_job_tx(self, job_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM authority_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise AuthorityServiceError("job_not_found")
        return row

    def _require_delivery_tx(self, delivery_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM authority_deliveries WHERE delivery_id = ?", (delivery_id,)
        ).fetchone()
        if row is None:
            raise AuthorityServiceError("delivery_not_found")
        return row

    @staticmethod
    def _assert_terminal_replay(
        *,
        actual_state: str,
        actual_sha: Any,
        expected_state: str,
        expected_sha: str,
    ) -> None:
        if actual_state != expected_state or str(actual_sha or "") != expected_sha:
            raise AuthorityStaleLeaseError("terminalization_conflict")

    @staticmethod
    def _assert_current_job_lease(
        row: sqlite3.Row, worker: str, token: int, now: str
    ) -> None:
        if (
            str(row["state"]) != "running"
            or str(row["lease_owner"] or "") != worker
            or int(row["fence_token"]) != token
            or row["lease_expires_at"] is None
            or str(row["lease_expires_at"]) <= now
        ):
            raise AuthorityStaleLeaseError("stale_job_lease")

    @staticmethod
    def _assert_current_delivery_lease(
        row: sqlite3.Row, worker: str, token: int, now: str
    ) -> None:
        if (
            str(row["state"]) != "running"
            or str(row["lease_owner"] or "") != worker
            or int(row["fence_token"]) != token
            or row["lease_expires_at"] is None
            or str(row["lease_expires_at"]) <= now
        ):
            raise AuthorityStaleLeaseError("stale_delivery_lease")

    @staticmethod
    def _decode_mapping(raw: object, *, field: str) -> dict[str, Any]:
        if not isinstance(raw, str):
            raise AuthorityServiceError(f"{field}_corrupt")
        try:
            decoded = loads_strict_json(raw, max_depth=512)
        except (ValueError, RecursionError) as exc:
            raise AuthorityServiceError(f"{field}_corrupt") from exc
        if not isinstance(decoded, dict):
            raise AuthorityServiceError(f"{field}_corrupt")
        return decoded

    def _decode_string_mapping(self, raw: object) -> dict[str, str]:
        decoded = self._decode_mapping(raw, field="context_revisions")
        if any(not isinstance(value, str) for value in decoded.values()):
            raise AuthorityServiceError("context_revisions_corrupt")
        return decoded

    @staticmethod
    def _context_revision_from_row(row: sqlite3.Row) -> ContextRevision:
        return ContextRevision(
            context_revision_id=str(row["context_revision_id"]),
            channel_id=str(row["channel_id"]),
            sha256=str(row["sha256"]),
            artifact_id=str(row["artifact_id"]),
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _delivery_from_row(row: sqlite3.Row) -> AuthorityDelivery:
        return AuthorityDelivery(
            delivery_id=str(row["delivery_id"]),
            message_id=str(row["message_id"]),
            recipient=str(row["recipient"]),
            state=str(row["state"]),
            deadline_at=str(row["deadline_at"]) if row["deadline_at"] is not None else None,
            lease_owner=str(row["lease_owner"]) if row["lease_owner"] is not None else None,
            lease_expires_at=(
                str(row["lease_expires_at"]) if row["lease_expires_at"] is not None else None
            ),
            fence_token=int(row["fence_token"]),
            attempt_count=int(row["attempt_count"]),
            acknowledgment_artifact_id=(
                str(row["acknowledgment_artifact_id"])
                if row["acknowledgment_artifact_id"] is not None
                else None
            ),
            terminal_sha256=(
                str(row["terminal_sha256"]) if row["terminal_sha256"] is not None else None
            ),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            completed_at=str(row["completed_at"]) if row["completed_at"] is not None else None,
        )

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> AuthorityJob:
        return AuthorityJob(
            job_id=str(row["job_id"]),
            job_kind=str(row["job_kind"]),
            subject_id=str(row["subject_id"]),
            payload_artifact_id=str(row["payload_artifact_id"]),
            state=str(row["state"]),
            deadline_at=str(row["deadline_at"]) if row["deadline_at"] is not None else None,
            lease_owner=str(row["lease_owner"]) if row["lease_owner"] is not None else None,
            lease_expires_at=(
                str(row["lease_expires_at"]) if row["lease_expires_at"] is not None else None
            ),
            fence_token=int(row["fence_token"]),
            attempt_count=int(row["attempt_count"]),
            result_artifact_id=(
                str(row["result_artifact_id"]) if row["result_artifact_id"] is not None else None
            ),
            terminal_sha256=(
                str(row["terminal_sha256"]) if row["terminal_sha256"] is not None else None
            ),
            idempotency_key=str(row["idempotency_key"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            completed_at=str(row["completed_at"]) if row["completed_at"] is not None else None,
        )

    class _WriteTransaction:
        def __init__(self, service: AuthorityService) -> None:
            self.service = service

        def __enter__(self) -> None:
            self.service._conn.execute("BEGIN IMMEDIATE")

        def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
            if exc_type is None:
                try:
                    self.service._conn.commit()
                except Exception:
                    self.service._conn.rollback()
                    raise
            else:
                self.service._conn.rollback()
            return False

    def _write_transaction(self) -> _WriteTransaction:
        return self._WriteTransaction(self)


def open_authority_service(root: Path | None = None) -> AuthorityService:
    """Factory used by authority-mode adapters and tests."""
    return AuthorityService(root=root)


__all__ = [
    "AuthorityChannel",
    "AuthorityDelivery",
    "AuthorityDeliveryLease",
    "AuthorityJob",
    "AuthorityJobLease",
    "AuthorityMessage",
    "AuthorityService",
    "AuthorityServiceError",
    "AuthorityStaleLeaseError",
    "ContextRevision",
    "HistoricalImportResult",
    "open_authority_service",
]

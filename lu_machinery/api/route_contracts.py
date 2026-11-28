"""Machine-readable Monitor API and dashboard contracts.

The registry is intentionally family-based: most Monitor endpoints belong to
router families with the same source and freshness behavior. Tests verify that
every public FastAPI route and literal dashboard HTML page matches a contract.

The declarations here cover the machinery routes and pages. The product
contribution adds its own (``scripts.project_api.route_contracts``), and the
lookups read both: machinery first, then product
(Monitor split, C-4687).
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from .product_contribution import ProductContribution, has_suffix, installed_product

ContractKind = Literal["http", "websocket"]
MatchType = Literal["exact", "prefix"]
RouteLocality = Literal["host_affine", "cluster_authoritative", "mixed"]


def _top() -> str:
    """Unresolved name of this file's top package directory."""
    return Path(os.path.abspath(__file__)).parents[1].name


def _rel(*parts: str) -> str:
    return "/".join((_top(), *parts))


def _mod(*parts: str) -> str:
    return ".".join((_top(), *parts))


@dataclass(frozen=True)
class RouteContract:
    pattern: str
    match: MatchType
    kind: ContractKind
    purpose: str
    source_of_truth: str
    freshness: str
    consumers: tuple[str, ...]
    overlap: str
    stale_risk: str
    recommendation: str
    locality: RouteLocality = "host_affine"
    mutates: bool = False
    replacement: str | None = None
    response_schema_version: str | None = None

    def matches(self, path: str, kind: ContractKind) -> bool:
        if self.kind != kind:
            return False
        if self.match == "exact":
            return path == self.pattern
        prefix = self.pattern.rstrip("/")
        return path == prefix or path.startswith(f"{prefix}/")

    @property
    def specificity(self) -> tuple[int, int]:
        return (1 if self.match == "exact" else 0, len(self.pattern))

    def to_dict(self) -> dict:
        data = asdict(self)
        data["consumers"] = list(self.consumers)
        return data


# Characters that show nothing: controls, format (bidi), unassigned, private-use and surrogate
# code points, line and paragraph separators, and the letters and symbols drawn as blanks.
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Cn", "Co", "Cs", "Zl", "Zp"})
_BLANK_GLYPHS = frozenset("\u115f\u1160\u2800\u3164\uffa0")


def _visible_text(text: str) -> str:
    """``text`` as one line of visible text, or ``""`` when a reader would see nothing.

    Invisible characters are dropped and runs of whitespace collapse to one space. What is
    left counts only when it holds a letter, number, punctuation mark or symbol.
    """
    kept = "".join(
        ch for ch in text if unicodedata.category(ch) not in _INVISIBLE_CATEGORIES and ch not in _BLANK_GLYPHS
    )
    line = " ".join(kept.split())
    return line if any(unicodedata.category(ch)[0] in "LNPS" for ch in line) else ""


@dataclass(frozen=True)
class PageContract:
    file: str
    url: str
    purpose: str
    source_of_truth: str
    freshness: str
    consumers: tuple[str, ...]
    overlap: str
    stale_risk: str
    recommendation: str
    label: str = ""

    @property
    def nav_label(self) -> str:
        """The name the Monitor navigation shows, never empty.

        The declared label, else one derived from the file name, else from the URL path
        (``/`` is "Home"), else the URL itself. Each candidate is cleaned to one line of
        visible text (``_visible_text``) and used only if something visible is left.
        """
        if label := _visible_text(self.label):
            return label
        if self.url == "/":
            return "Home"
        file_name = Path(self.file).name
        # The file name without its suffix, by the rule has_suffix states (``PurePath.stem`` changed in 3.14).
        stem = file_name.rpartition(".")[0] if has_suffix(file_name) else file_name
        for source in (stem, self.url.strip("/")):
            if name := _visible_text(source.replace("-", " ").replace("_", " ")):
                return name.capitalize()
        return _visible_text(self.url) or "Untitled page"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["consumers"] = list(self.consumers)
        return data


ROUTE_CONTRACTS: tuple[RouteContract, ...] = (
    RouteContract(
        "/api/epics/v1",
        "prefix",
        "http",
        "Remote TTL-fenced epic registry, lease claim, heartbeat, handoff, release, and bounded digest projection.",
        "API-host SessionStreamStore seeded at process start from the release snapshot's "
        f"{_rel('config', 'issue_streams.yaml')}; no PID probe and no GitHub enrichment.",
        "Registry identity is fresh at the last successful API restart; lease state and bounded digest are read per request.",
        ("remote orchestrators", "launchers", "Monitor"),
        "Complements local session-supervisor hooks; it is the remote authority for v1 epic leases and a snapshot projection for registry identity.",
        "high if callers treat lease: null as never claimed, or treat registry status as live parity rather than last-restart snapshot state",
        "keep as the M2 registry-seeded remote lifecycle contract",
        locality="cluster_authoritative",
        mutates=True,
    ),
    RouteContract(
        "/api/session-streams",
        "prefix",
        "http",
        "Read-only session-stream health, status, digest and plane-continuity surfaces.",
        "SessionStreamStore.",
        "Health is generated per request; store schema versions are read from the configured database.",
        ("agents", "Monitor", "session-stream consumers"),
        "Complements the remote /api/epics/v1 lifecycle projection; it never exposes filesystem paths.",
        "medium if callers treat local inventory as remote lease authority",
        "keep as the path-free session-stream observability contract",
        locality="mixed",
        response_schema_version="session-streams.v3",
    ),
    RouteContract(
        "/api/ops/v1/test-slots",
        "exact",
        "http",
        "Host test-slot pool (#10662): slots in use, holders by task, the FIFO queue with positions, and wait times.",
        "This host's private test-slot pool directory: flock-live holder records and queue tickets "
        f"({_rel('common', 'test_slots.py')}).",
        "Read live per request; flock decides liveness, so a crashed holder disappears at once. No cache.",
        ("operators", "dispatch", "agents"),
        f"Same data as `python -m {_mod('common', 'test_slots')} status --json`; dispatch admission reads the queue depth.",
        "low; read-only and path-free",
        "keep",
        response_schema_version="test-slots.status.v1",
    ),
    RouteContract(
        "/api/ops/v1/load-samples",
        "exact",
        "http",
        "Bounded high-load attribution: CPU command names and test-slot holders (#10715).",
        "Ignored batch_state load-samples ring on this Monitor host.",
        "Existing Monitor periodic collector; above LU_LOAD_SAMPLE_THRESHOLD, at most once per 60 seconds; last 500 samples.",
        ("operators", "agents"),
        "Complements the live test-slot status; CPU shares use one CPU as 100%.",
        "low; read-only, command names only, no process arguments or paths",
        "keep",
        response_schema_version="load-samples.v1",
    ),
    RouteContract(
        "/api/ops/v1/alerts",
        "exact",
        "http",
        "Alerts firing now in the host Alertmanager, in plain English. Each row has seven keys: alertname (a listed name, else UnrecognizedAlert), and title, meaning, next_step and subject. The title, meaning and next_step come from a fixed table keyed by alert name and are null for UnrecognizedAlert. The subject is a fixed-set pool or tool name for the alerts that have one, else null. The severity (critical, warning, info, none or unknown) and starts_at (a normalised UTC time, or null) are read per instance and sanitised. The dead-man heartbeat is a flag, not a row.",
        "The loopback Alertmanager v2 alerts API (active, not silenced, not inhibited).",
        "15-second cache; its cheap loopback read refreshes on expiry before returning. A 60-second display bound allows cache-hit age, the collector, page and HTTP budgets even with concurrent readers. Observation age is exposed in snapshot headers. An unreachable Alertmanager is unknown, never an empty list.",
        ("index.html", "operators"),
        "Complements Prometheus and the alert relay; annotation text and every label other than the sanitised alert name, severity and the allow-listed subject (instance, job and the generator URL among them) are never returned.",
        "low; read-only and allow-listed",
        "keep as the machinery home's alerts reading",
    ),
    RouteContract(
        "/artifacts",
        "prefix",
        "http",
        "Human artifact browser and approved documentation artifact serving.",
        "docs, audit, and other approved artifact roots.",
        "No route cache; serves artifacts.html for browser requests and JSON/file listings on demand.",
        ("artifacts.html", "humans"),
        "Shares artifact metadata with /api/artifacts/html.",
        "low",
        "keep",
    ),
    RouteContract(
        "/files",
        "prefix",
        "http",
        "Approved documentation artifact serving alias.",
        "docs, audit, and other approved artifact roots.",
        "No route cache; live filesystem serving with extension/root allowlists.",
        ("humans", "agents"),
        "Alias of /artifacts file serving without the browser page default.",
        "low",
        "keep",
    ),
    RouteContract(
        "/api/fleet/operations",
        "exact",
        "http",
        "Read-only curriculum batch-progress projection for Fleet Observer operations panels.",
        "The product's bounded batch/log read models, sanitized into aggregate metadata.",
        "Generated per request; item lists are capped at 50.",
        ("operators", "agents"),
        "Canonical visible home for batch operations; the legacy broker and zombie sections are removed (#515).",
        "medium if missing stores are mistaken for zero activity or sanitized aggregates are treated as control-plane authority",
        "keep as the read-only canonical operations projection; do not use it to mutate, clean, expire, or retire routes",
        locality="mixed",
    ),
    RouteContract(
        "/api/fleet/agents",
        "exact",
        "http",
        "Configured and durable fleet agent endpoint inventory with configuration secrets omitted.",
        f"Endpoint registry ({_rel('config')}) plus durable agent_endpoints rows from the fleet-comms v1 SQLite read model.",
        "Generated per request; every fleet route opens the durable plane read-only. No response cache.",
        ("operators", "agents"),
        "Specific member of the /api/fleet observer family; /api/fleet/endpoints serves the same handler as an alias.",
        "low — read-only inventory; agents missing from the durable plane are reported via durable_registration=false",
        "keep as the exact agent-endpoint contract beside the /api/fleet prefix family",
        locality="cluster_authoritative",
        mutates=False,
    ),
    RouteContract(
        "/api/fleet/seats",
        "exact",
        "http",
        "Configured Fleet Comms seats with queued delivery counts and last activity.",
        "Driver and external recipient registries, SQLite Fleet Comms deliveries, and live session-stream leases.",
        "Per-request read-only snapshot. Snapshot headers report unavailable sources.",
        ("fleet.html", "comms.html", "operators", "agents"),
        "Seat ids also filter the Fleet delivery projection.",
        "medium if unavailable sources are mistaken for no queued mail or no live lease",
        "keep as the read-only seat projection",
        locality="cluster_authoritative",
        mutates=False,
    ),
    RouteContract(
        "/api/fleet/deliveries",
        "exact",
        "http",
        "Paged Fleet Comms delivery records for exact roster seats.",
        "SQLite authority deliveries joined to allowlisted sender and kind labels.",
        "Per-request read-only snapshot. Limit and offset page one consistent SQLite read.",
        ("comms.html", "operators", "agents"),
        "The queued filter shares its recipient set with the seat backlog count.",
        "high if message content or labels outside the approved vocabulary escape the projection",
        "keep as the body-free delivery projection",
        locality="cluster_authoritative",
        mutates=False,
    ),
    RouteContract(
        "/api/fleet/messages/{message_id}/body",
        "exact",
        "http",
        "Read one visible Fleet message body. Returns typed expired, oversized, withheld, corrupt and unavailable states.",
        "Canonical Fleet Comms SQLite authority, configured delivery roster and the message's verified CAS body reference.",
        "Loopback-only, on demand, read-only and no-store. Bounds raw bytes, serialized response, SQLite work and concurrency.",
        ("comms.html", "operators"),
        "Uses the same local access guard and secrets-and-paths redaction as the ACP transcript route.",
        "high if body content crosses the operator boundary",
        "keep as the on-demand Fleet message reader",
        locality="cluster_authoritative",
    ),
    RouteContract(
        "/api/fleet/messages/{message_id}/thread",
        "exact",
        "http",
        "Page the visible metadata (message_id, allowlisted sender label, kind, created_at) in the selected message's thread, channel and conversation.",
        "Canonical Fleet Comms SQLite authority and configured delivery roster.",
        "Loopback-only, on demand, read-only and no-store. An offset page contains no bodies.",
        ("comms.html", "operators"),
        "Uses the message body visibility rule and the deliveries allowlisted sender projection. Supplies IDs and sender labels for thread navigation.",
        "high if unrelated or hidden message records or non-allowlisted sender labels appear in a thread",
        "keep as the metadata-only Fleet thread reader",
        locality="cluster_authoritative",
    ),
    RouteContract(
        "/api/fleet",
        "prefix",
        "http",
        "Unified read-only observer for durable fleet-comms requests, authority queue jobs, messages, discussions, ACP rounds, formal reviews, endpoints, migrations, dead letters, provenance, and operations health.",
        "Fleet-comms v1 SQLite read models, endpoint registry metadata, sanitized runtime/ACP projections, and sanitized batch operations read models.",
        "No response cache except the default acp/conversations page, served stale-while-revalidate (state_helpers.swr_route): the last value at once with its true age in X-Snapshot-Age-S for 15 s; every other route opens the durable plane in SQLite read-only mode. Lists have deterministic order and bounded limit/offset pages; inline bodies are preview-only, redacted, and never fetch artifact blobs.",
        ("operators", "agents"),
        "Consolidates observation previously spread across comms, runtime, ACP, and session-stream pages; legacy pages remain during the pre-flip soak.",
        "medium if a consumer mistakes the observer for plane authority or relies on an absent optional table as evidence of no historical work",
        "keep as the pre-flip consolidated observer; file handoffs remain authoritative and all fleet routes are read-only",
        locality="cluster_authoritative",
        response_schema_version="comms.v2",
    ),
    RouteContract(
        "/api/comms/live-activity",
        "exact",
        "http",
        "Deprecated live module activity summary; its broker dispatch feed is removed (#515) and stays empty.",
        "Build state files.",
        "No route cache; live state-file reads.",
        ("legacy docs",),
        "Replaced by channel activity and delegate/build event endpoints.",
        "medium",
        "deprecated legacy live-activity summary; delegate/active is the live task view",
        replacement="/api/delegate/active",
    ),
    RouteContract(
        "/api/comms",
        "prefix",
        "http",
        "Fleet Comms metrics, plane status, body-free inbox counts and batch progress; the legacy channel, module-trail and mutation routes answer 410 with their replacement (#515).",
        "Fleet Comms authority plane (read-only) and build state.",
        "Metrics has a startup-warmed 30-second cache with background refresh and a 120-second display bound allowing two page cycles and collection margin. Other routes read live. No route mutates state.",
        ("progress.html", "comms.html", "agents"),
        "Fleet Comms is the only message store; the legacy broker and channel stores are removed (#515).",
        "low/medium",
        "keep",
        locality="mixed",
        response_schema_version="comms.v2",
    ),
    RouteContract(
        "/api/delegate/active",
        "exact",
        "http",
        "Active delegated task summaries without a request-time history scan.",
        "Context-owned snapshot of task JSON and process liveness, warmed during startup.",
        "One background writer waits 2 seconds between collections. Snapshot headers expose age including collection time, a 60-second bound and fresh/stale state. Before the first successful collection: 503, unknown age, Retry-After 2. Requests retain the last good snapshot.",
        ("fleet.html", "agents"),
        "Active view of /api/delegate/tasks.",
        "Observation can lag by the refresh delay plus collection time; inspect the age header.",
        "keep",
    ),
    RouteContract(
        "/api/delegate",
        "prefix",
        "http",
        "Delegated task list, active task count, and task result detail.",
        "batch_state/tasks/*.json, process liveness, and task result files.",
        "Recent tasks uses one 30-second background snapshot with a 90-second bound and observation age in headers; a read that wakes it waits at most 0.5 s. /api/delegate/tasks is served stale-while-revalidate (state_helpers.swr_route): the last value at once with its true age in X-Snapshot-Age-S, per status and limit, refreshed after 2 s. Other reads use the task cache and live filesystem/process checks.",
        ("fleet.html", "agents"),
        "Complements /api/orient.delegate.",
        "low",
        "keep",
    ),
    RouteContract(
        "/api/coordination",
        "prefix",
        "http",
        "Parallel-agent coordination ledger task detail.",
        f"orchestration/agent-ledger/tasks/*.json written by {_rel('orchestration', 'agent_ledger.py')}.",
        "No route cache; live filesystem reads from the local coordination ledger.",
        ("agents", "orchestrator", "future dashboards"),
        "Complements /api/delegate process state with explicit task ownership and review metadata.",
        "low while JSON ledger is healthy; invalid task files surface as errors instead of silent stale state",
        "keep",
    ),
    RouteContract(
        "/api/worktrees",
        "exact",
        "http",
        "Active git worktree registry.",
        "git worktree list --porcelain, per-worktree git status, and git log -1.",
        "No route cache; each git subprocess has a bounded timeout.",
        ("agents", "cold-start tooling", "worktree hygiene"),
        "Complements /api/git/cleanup.",
        "low; failures degrade to empty/error payload",
        "keep",
    ),
    RouteContract(
        "/api/runtime/routing",
        "exact",
        "http",
        "How work was routed, for the Models & budget page: one row per dispatch and review seat in the last 24 hours (what "
        "was asked, the model and pool chosen, who chose each where the record proves it, the admission outcome, any "
        "recorded substitution) with per-lane counts, and the subscription pools in page order.",
        "Hot task records (batch_state/tasks) with the admission snapshot each dispatch records; the start-refusal "
        f"and pid-less liveness rules of {_rel('orchestration', 'dispatch_admission.py')}.",
        "recent is one scan of the task records, served stale-while-revalidate (state_helpers.cache_read_swr): the "
        "last value at once, refreshed after 30 s, stale after 90 s; a read waits up to 0.5 s for a first scan, else "
        "answers pending and the page reads again after 3 s. Its age_s and X-Snapshot-Age-S count from the scan's "
        "as_of. The task store is listed on every request: a listing fault is typed unknown with its reason, never "
        "zero, and unreadable records make the scan partial.",
        ("models.html", "operators", "agents"),
        "Replaces /api/runtime/routing-assignments, whose routing-reservation ledger lost its only writer when the "
        "sealed formal review path was retired; reads the task records dispatch already writes instead of keeping a "
        "second record.",
        "medium if a review row's model is read as the resolver's pick (the task record keeps the seat that ran, "
        "not who chose it)",
        "keep as the routing observer; routing stays with the brief, the account choice, admission and the resolver",
        response_schema_version="routing.v1",
    ),
    RouteContract(
        "/api/runtime/subscriptions",
        "exact",
        "http",
        "Every subscription account's weekly usage, pace (headroom or deficit, run-out time), 5-hour and scoped windows, credits, banked resets and plan renewal or end, one row per account grouped by provider. Also lists non-retired catalog model IDs.",
        "The Monitor's validated per-account Claude snapshots, the host usage cache (Codex, AGY, Grok+Cursor), the operator's plan-facts file and the fleet model catalog.",
        "Generated per request from those caches and files only, never a provider call; each account carries its read time and is stale after 15 minutes; missing usage is typed unknown and an unreadable catalog is null.",
        ("index.html", "agents", "operators"),
        "The routing-budget route shares this subscription projection for the Models & budget page. The console command python -m lu_machinery.api.subscriptions prints the underlying usage snapshot.",
        "medium if a stale or unknown reading is read as current headroom",
        "keep as the subscriptions view",
    ),
    RouteContract(
        "/api/runtime/acp/conversations/{conversation_id}/transcript",
        "exact",
        "http",
        "Loopback-only read-only ACP discussion transcript with explicit per-turn and transcript states "
        "(ok, body_expired, withheld, missing_or_corrupt, oversized_or_partial, backend_unavailable). "
        "Only allowlisted producers (acpx-discuss:<lane> turns and authority-message ACP turns) and kinds "
        "(request, reply, synthesis) are served; anything off-allowlist is withheld with a typed reason. "
        "Every allowed body and label passes through redact_text then sanitize_text server-side before caps "
        "and serialization; redaction is best-effort for secrets and filesystem paths only (hostnames and "
        "IP addresses are not redacted). "
        "Redaction covers plain text and JSON (including JSON nested inside JSON strings up to the depth "
        "cap); other encodings (base64, URL-encoding, HTML entities, compression) are out of scope. "
        "Only a body (or nested string) that is one complete JSON object, array or string is redacted "
        "structurally; any other text, whatever it starts with, is plain text and its JSON escapes are not "
        "decoded. A standalone finite number stays plain text; a non-finite number, or an integer too long "
        "to convert anywhere inside structured JSON, is missing_or_corrupt (body_redaction_failed). "
        "Structured JSON deeper than 64 containers or 4 nested JSON strings, and text nested more than 512 "
        "levels before its first syntax error, is withheld (redaction_depth_exceeded). "
        "Turns are every stored message of the conversation in persistence order, whether or not an event "
        "links it (round shown where an event links it), then any event-linked message id with no stored "
        "row as missing_or_corrupt (message_row_missing). "
        "A malformed conversation id is HTTP 400 (invalid_conversation_id); 404 means only a valid id "
        "that a completed lookup did not find. "
        "Bounded by raw-body bytes, whole serialized response bytes, SQL "
        "row count checked before materialization, SQLite busy timeout, and a concurrency semaphore.",
        "Fleet Comms SQLite store (read-only) and content-addressed body artifacts under blobs/sha256.",
        "Read live per request; no route cache (Cache-Control: no-store); GET never mutates storage.",
        ("comms.html", "operators"),
        "Uses the shared local body-read access guard (loopback listener, direct loopback peer, exact Host "
        "and Origin, Sec-Fetch-Site in {same-origin, none}, no Forwarded or X-Forwarded-* headers). "
        "Body-free timelines stay on /api/runtime/acp/conversations/{conversation_id}.",
        "medium; redaction covers secrets and filesystem paths only, not hostnames or IP addresses",
        "keep as the loopback-only ACP transcript reader contract",
        locality="cluster_authoritative",
        mutates=False,
    ),
    RouteContract(
        "/api/runtime",
        "prefix",
        "http",
        "Agent runtime inventory, usage, ACPX shadow evidence, ACP conversation observability, default-model quota headroom, recent calls, auth mode, and cached transport health.",
        "Runtime config, usage JSONL, the fleet-comms ACP SQLite store, sanitized probe receipt, environment presence booleans, and OAuth credential presence.",
        "Logins uses one request-triggered background snapshot, a 600-second refresh interval and a 900-second display bound allowing probe and page timing. The ACP conversation list is served stale-while-revalidate (state_helpers.swr_route): the last value at once with its true age in X-Snapshot-Age-S, per limit, refreshed after 15 s. Other routes read live files/env/SQLite. ACPX is aggregate evidence only and never launches a probe; ACP transcripts use the exact transcript contract (loopback-only, no-store, secrets-and-paths redaction only); transport health carries receipt expiry; auth reports booleans/source names only.",
        ("index.html", "comms.html", "models.html", "agents"),
        "Usage complements /api/state/routing-budget and cost endpoints.",
        "medium; transcript bodies are restricted to the local conversation reader with secrets-and-paths redaction only",
        "keep",
        locality="mixed",
    ),
    RouteContract(
        "/api/state/routing-budget",
        "exact",
        "http",
        "Per-agent soft-cap burn, routing recommendation, and Cursor Auto and API windows. "
        "The Models & budget page requests the per-account snapshot with ?include_subscriptions=true.",
        f"{_rel('config', 'agent_budgets.yaml')}, cost records, delegate in-flight state, process-local CodexBar, and the usage and plan-facts inputs shared with /api/runtime/subscriptions.",
        "15-second routing cache with background refresh of expired snapshots; age, state and the 900-second source bound are exposed in snapshot headers. Subscription usage has its own background scheduler. HTTP ?fresh_codexbar=true starts a refresh and returns cache/LKG without waiting for a probe.",
        ("models.html", "agents"),
        "Overlaps runtime usage and cost summaries but adds routing recommendation.",
        "low",
        "keep canonical for routing guidance",
    ),
    RouteContract(
        "/api/issues/map",
        "exact",
        "http",
        "Open issue map grouped by category.",
        "gh issue list output.",
        "The map is served stale-while-revalidate (state_helpers.swr_route): the last value at once with its true age in X-Snapshot-Age-S, per limit, with one gh issue list per 120 s refresh; /api/orient collects its issues section cache-first in a detached worker.",
        ("agents", "issue hygiene"),
        "Overlaps /api/orient.issues.",
        "medium if GitHub is slow or unavailable",
        "keep best-effort",
    ),
    RouteContract(
        "/api/issues/streams",
        "exact",
        "http",
        "Issue-stream hygiene report (#4708): orphans without a stream epic, multi-homed, pending native links.",
        "batch_state/issue_stream_audit.json auditor cache plus a runtime-only refresh state sidecar; ?fresh=true schedules one detached worker.",
        "Cache TTL 3600s with a 7-day stale fallback; every response reports privacy-safe scheduled/running/terminal refresh state.",
        ("agents", "issue hygiene", "orchestrators"),
        "Complements /api/issues/map (label categories) with stream-epic membership.",
        "low — nonblocking single-flight refresh records allowlisted failure state without raw errors",
        "keep best-effort",
    ),
    RouteContract(
        "/api/rules",
        "exact",
        "http",
        "Condensed agent rule set for cold start.",
        "Deployed rule/source markdown files.",
        "Hash/ETag semantics when telemetry footer is disabled; otherwise generated per request.",
        ("agents", "docs"),
        "Replaces direct rule-file reads at cold start.",
        "low",
        "keep cold-start source of truth",
    ),
    RouteContract(
        "/api/session/current",
        "exact",
        "http",
        "Agent-specific current session handoff.",
        "docs/session-state/current.md router and current.<agent>.md handoff files.",
        "Hash/ETag semantics when telemetry footer is disabled; generated per request otherwise.",
        ("agents", "docs"),
        "Replaces direct session-state file spelunking.",
        "low",
        "keep cold-start source of truth",
    ),
    RouteContract(
        "/api/orient",
        "exact",
        "http",
        "One-shot agent and dashboard orientation snapshot.",
        "Git, GitHub issues, pipeline state, runtime files, delegate state, broker, rollover registry, wiki, governance, health, and session hints.",
        "Per-section TTLs in main.py; ?fresh=true invalidates orient cache entries.",
        ("orient.html", "index.html", "agents"),
        "Aggregates many other API sections.",
        "low/medium when upstream collectors degrade",
        "keep cold-start source of truth",
        locality="mixed",
        response_schema_version="orient.v2",
    ),
    RouteContract(
        "/api/knowledge",
        "prefix",
        "http",
        "ADR-011 bounded Project Research Registry discovery: task-scoped pointer "
        "manifest and per-record compact digest bodies.",
        "docs/references/research-registry.yaml + docs/references/research-digests/** "
        "via the P1 validator primitives (scripts/audit/check_research_registry.py).",
        "No route cache; per-request deterministic registry load. Strong ETags: a "
        "context-scoped ETag on /manifest and the P1 content_hash on /record/{id}. "
        "Registry-serving endpoints obey the research_registry kill switch; /find and "
        "consumption metadata remain ungated.",
        ("agents", "dispatch", "cold-start tooling"),
        "Pointer index for /api/state/manifest's research component; bodies never enter cold start.",
        "low; disabled or a failed load degrades to an empty/disabled projection",
        "keep as the research discovery surface",
    ),
    RouteContract(
        "/api/state/manifest",
        "exact",
        "http",
        "Tiny cold-start manifest with hashes and URLs.",
        "Rules/session hash helpers plus static endpoint metadata.",
        "Generated per request; rules/session hashes let agents skip unchanged payloads.",
        ("agents", "monitor client"),
        "Index for /api/rules, /api/session/current, /api/orient, and the comms inbox.",
        "low",
        "keep cold-start source of truth",
    ),
    RouteContract(
        "/api/milestones",
        "exact",
        "http",
        "Open milestone summaries and issue progress for every registered fleet repository.",
        "Fleet repository registry and conditional GitHub REST collection reads through the shared cache.",
        "Single-flight refresh every 300s while requested; healthy page data at most 420s old. "
        "True age includes collection time. Expired data is marked stale; failures are unknown.",
        ("milestones.html", "operators"),
        "Private repository milestones return counts and percent only, without text, dates or links.",
        "medium if a stale or partial collection is mistaken for complete live progress",
        "keep as the milestones page's progress reading",
        response_schema_version="milestones.v1",
    ),
    RouteContract(
        "/api/journal",
        "exact",
        "http",
        "Loopback-only project journal: newest-first operator-facing decisions, merges, and operational events per project.",
        "Read-only subprocess seam to the driver-state CLI configured by LU_DRIVER_STATE_CLI (`journal list --json`); never imports control_plane_pg.",
        "Read live per request with bounded timeout; no route cache (Cache-Control: no-store). Unavailable CLI, DB, or invalid CLI payload returns typed availability with entries [] and total null.",
        ("journal.html", "operators"),
        "Complements Fleet Comms deliveries and driver-state handoffs with an append-only plain-English project log.",
        "low; uses the shared local body-read guard (loopback listener and peer, exact Host/Origin, Sec-Fetch-Site in {same-origin, none}, no Forwarded/X-Forwarded-* headers), read-only, no-store",
        "keep as the local journal read contract",
        locality="cluster_authoritative",
        mutates=False,
        response_schema_version="journal.v1",
    ),
    RouteContract(
        "/api/fleet/model-stats",
        "exact",
        "http",
        "Per concrete model id, effective effort, role (write, review) and task class over a 24h, 7d, 30d, 90d or "
        "all window (view=full keeps by_class, tokens and weeks; view=page omits them and keeps model/effort totals "
        "plus charts): tasks, terminal status mix, landed tasks, median and p90 wall time, tokens, strict "
        "wall_hours_per_landed (null when any duration is missing) and wall_hours_observed (recorded hours per landed "
        "task when at most 1% or 1 of 10+ tasks is untimed), review verdicts and blocking findings of parsed reviews "
        "of record, reviewer levels, author-side review rounds, first-round approval per author, first reviewer and "
        "review class, what followed a blocked first round, routed catalog models idle in the window, cluster-grouped "
        "weekly allowance spent per landed task and ISO-week counts, each with its sample size (#581). Both views "
        "include token use and waste. view=tokens returns just that report and its window, without reply text or "
        "task ids. The report has normalized token totals, usage coverage, tokens per observed merged PR, work "
        "and waste breakdowns, cache hit ratios and five highest-volume runs. Unsupported attributions are null. "
        "The per-merge denominator needs readable mainline history for every registered repository. "
        "/metrics exports "
        "selected gauges from the same cached snapshot for 7d, 30d, 90d, all and ISO weeks (omitting 24h, observed "
        "wall hours and chart series). Token gauges cover 24h and 7d: lu_tokens_fresh_input, lu_tokens_cached_input, "
        "lu_tokens_output and lu_tokens_per_merged_pr by model and work type, lu_tokens_waste by model and waste "
        "class, and lu_token_data_age_seconds. Figures without readings emit no sample. Comparisons list the "
        "observed effort levels. The unknown effort group is "
        "reported separately and excluded from model totals.",
        "The durable model-stats summary rows under batch_state/model-stats/, written at settlement and "
        "backfilled from hot and archived task records, plus whole-line X-Agent trailers on each registered "
        "repository's local origin/main (never fetched), the merged delivery each task recorded, and exact-head "
        "review verdict publications.",
        "One single-flight snapshot of all five windows, refreshed every 300s while the route or /metrics is read; "
        "healthy data at most 420s old; a request with no snapshot waits up to 15s for that collect, then answers "
        "pending with Retry-After 1, or 503 unavailable if the collect fails.",
        ("models.html", "operators"),
        "Joins the agent-runtime usage log (ACP runs only) and /api/fleet/workers/v1 (live workers); "
        "agent success counts runs without landing or rounds.",
        "medium if a low-sample rate or a confounded comparison is read as a routing verdict",
        "keep as the model value reading; routing stays a CTO decision",
        response_schema_version="model-stats.v2",
    ),
    RouteContract(
        "/api/fleet/model-stats/planning",
        "exact",
        "http",
        "Open-issue blocker ranking, ready cards with non-overlapping write paths, and pool allowance at next reset.",
        "Registered GitHub issue inventories and dependency states, the internal active delegate snapshot, "
        "the existing Prometheus pool series and cached subscription meters, and settled task allowance readings.",
        "Issue snapshots refresh on demand every 300 seconds and are stale after 420 seconds. "
        "The live-worker snapshot must be at most 10 seconds old to show ready-now entries. "
        "Meter reads refresh every 30 seconds. Unmeasured forecasts are unknown.",
        ("milestones.html", "models.html", "operators"),
        "Private issue identities, issue text and worker paths stay internal. Private blockers have anonymous ranks. "
        "Ready-now requires an explicit path list in the card's scope. Files may use an extension and directories "
        "may use a trailing slash. The ./ prefix explicitly marks a repository-relative path.",
        "medium if incomplete inputs are mistaken for complete planning data",
        "keep as a read-only planning view",
        response_schema_version="model-planning.v1",
    ),
    RouteContract(
        "/api/git/pull-requests",
        "exact",
        "http",
        "Open pull requests and project status for every repository in the fleet registry: number, draft, age, CI rollup, cross-family review status and merge-queue position, plus per project the default-branch CI result, the lu-machinery version in the uv.lock at the default branch head (self for machinery; unknown when the lock is missing, cut short, malformed or has no single lu-machinery entry), and the count of live workers.",
        "One GitHub GraphQL read through the shared GitHub client over every registry repository, and the local live-worker snapshot for the worker counts.",
        "Startup warmup and a shared 120-second cache with one background refresh of expired snapshots. The 210-second display bound allows two 30-second page cycles plus collection/HTTP margin without extra GitHub reads. Age, bound and state are exposed in snapshot headers. A failed read is unknown, never an empty list: an unreadable project stays listed with status unknown, a reason and unknown_since (the first consecutive failed reading since this Monitor started), and one unreadable project never hides the others.",
        ("index.html", "pull-requests.html", "fleet.html", "operators"),
        "A private repository returns counts and states only: PR numbers, links and creation times, titles, branch names and the lu-machinery pin are withheld.",
        "medium if a two-minute-old queue position is read as live",
        "keep as the pull-requests and overview merge-queue reading",
    ),
    RouteContract(
        "/api/git",
        "prefix",
        "http",
        "Git cleanup dry-run and open pull-request reporting.",
        "Local git branches, worktrees, and batch_state task metadata.",
        "/api/git/cleanup is served stale-while-revalidate (state_helpers.swr_route): the last value at once with its true age in X-Snapshot-Age-S, refreshed after 60 s (computed_at names the report time) and reusing git cherry answers for unchanged branch and main tips; other routes read git and the filesystem live.",
        ("agents", "docs"),
        "Complements /api/worktrees.",
        "low/medium",
        "keep read-only cleanup planning",
    ),
    RouteContract(
        "/api/telemetry",
        "prefix",
        "http",
        "Tool-timing ingest and readback. Module-build token routes are product routes under the same prefix.",
        "Telemetry SQLite state used by Monitor instrumentation and module-build PR reporting.",
        "No route cache; POST records tool timings and GET reads them.",
        ("agents", "runtime tooling"),
        "Complements optional telemetry footer, runtime usage logs, analytics cost summaries, and #6106 legacy comms retirement evidence.",
        "low",
        "keep",
    ),
    RouteContract(
        "/api/health",
        "exact",
        "http",
        "API process health, uptime, resilience counters, machinery_version (installed lu-machinery release, or null), and loopback instance identity (opaque host id, checkout_sha/git_sha alias, serving_sha, serving_mode).",
        "API process state, LIVE_REPO_ROOT git HEAD, PROJECT_ROOT release layout, and resilience middleware counters.",
        "Generated per request; no route cache.",
        ("health checks", "tests", "agents"),
        "Related to /api/orient.health.",
        "low",
        "keep",
        locality="host_affine",
    ),
    RouteContract(
        "/api/cluster/readiness",
        "exact",
        "http",
        "Cluster readiness: reports storage-seam authority and ability to open authority stores for cluster-authoritative reads without claiming multi-host HA.",
        f"{_rel('control_plane', 'storage.py')} authority resolution and authority store connectivity.",
        "Evaluated per request; no cache.",
        ("agents", "orchestrators", "monitor client", "tests"),
        "Complements host-affine /api/health; distinct cluster-authoritative readiness contract.",
        "low",
        "keep as canonical cluster readiness endpoint",
        locality="cluster_authoritative",
    ),
    RouteContract(
        "/api/work",
        "prefix",
        "http",
        "Read-only Work control-plane projection: normalized public attention list, source envelopes, open PR rows plus bounded merged history qualified only by GitHub merged_at, and optional private-source capability seam.",
        "GitHub open issue/PR list enumerations (cap 1000), public /api/issues/streams, and frozen class-4 summaries (/api/delegate/active, /api/delegate/tasks, and in-process formal-review rows).",
        "Warm projection cache ~30s with cache_age_s; warm target <=2s; typed degradation within 5s; never proxies private adapters.",
        ("operators", "agents"),
        "Joins existing GH list + streams + class-4 summaries; does not replace GitHub or those sources as authority.",
        "medium if GitHub is slow or a class-4 section degrades independently",
        "keep as the Work foundation surface; FOUNDATION_COMPLETE only",
    ),
    RouteContract(
        "/api/occupancy",
        "exact",
        "http",
        "Opaque host occupancy: load fields, sanitized occupants {kind, agent, task_id, epic, optional instance_id}, burn_state in {active, idle, unknown}, top-level attention items empty_host_underused:<host_id> and empty_host_unknown_capacity:<host_id>; burn_sources has exactly atlas_job, driver, foundry, service, and observer, each with state in {active, clear, unknown} and non-negative observation_age_s. Default glance always includes host-teacher and observer-only mac-operator; host-job is a retired batch-host role and never appears, even if a MONITOR_OCCUPANCY_HOST_IDS mapping still points a canonical alias at it; empty-map Linux API fills host-teacher in-process; live observer heartbeats merge into that Mac row, and legacy cloud-observer stays presence-gated with honest presence age.",
        "In-process atlas-jobs load cache plus local atlas-jobs registry, local session-stream driver leases, optional occupancy markers, and the in-process observer presence store keyed by (host, agent, instance); host keys from MONITOR_OCCUPANCY_HOST_IDS (or empty-map API self-fill).",
        "Stale-while-revalidate via the existing HOST_LOAD_FRESH_S and HOST_LOAD_MAX_STALE_S load-cache constants; local marker TTL is DEFAULT_MARKER_TTL_S (15 minutes); never probes SSH on the request path.",
        ("operators", "dispatch", "tests", "qa-engineer", "grok-bot"),
        "Reuses GET /api/atlas-jobs/load cache; does not add a second probe board. Observer presence is not a RAM lease.",
        "low",
        "keep",
        mutates=False,
    ),
    RouteContract(
        "/api/observer/presence",
        "exact",
        "http",
        "Loopback-only observer heartbeat: grok-bot/QA/Cursor/Codex/Claude report current task or notebook session without a RAM lease; optional opaque host, instance, and bounded context counters are validated.",
        "In-process TTL store keyed by (opaque host, observer agent, instance); occupancy partitions live rows and the fresh matching instance can supply notebook-presence telemetry.",
        "Upsert immediately; row expires after 15 minutes without a heartbeat; Monitor restart drops the store.",
        ("qa-engineer", "grok-bot", "cursor", "codex", "tests"),
        "Not a fleet seat and never writes fleet-comms.",
        "low with loopback-only mutation and a short TTL",
        "keep",
        mutates=True,
    ),
    RouteContract(
        "/api/fleet/projects/v1",
        "exact",
        "http",
        "Per opaque host: primary checkout vs origin/main, worktree count, service serving SHA drift (fresh|stale|unknown), and attention items.",
        "In-process live collection on the API host plus loopback-pushed TTL reports from remote reporters keyed by MONITOR_OCCUPANCY_HOST_IDS and mac-operator.",
        "Remote reports expire after 15 minutes without a heartbeat; origin_main_age_s > 3600 degrades drift to unknown; API host is always fresh in-process.",
        ("operators", "dispatch", "tests"),
        "Complements GET /api/occupancy; does not probe SSH on the request path.",
        "low",
        "keep",
        locality="mixed",
        mutates=False,
    ),
    RouteContract(
        "/api/fleet/projects/v1/report",
        "exact",
        "http",
        "Loopback-only ingest for sanitized per-host project-state documents from remote reporters; schema v2 adds required workers block semantics (missing workers => unreported, [] => verified zero) and optional allowlisted lane_usage weekly rows {lane, window, used_pct, resets_at}.",
        "In-process TTL store keyed by opaque host_id; reporter must be allowlisted via MONITOR_OCCUPANCY_HOST_IDS or mac-operator.",
        "Upsert immediately when collected_at is newer than the stored report; 409 stale_report when older or equal; collected_at bounds return 400; row expires after 15 minutes without a report.",
        ("operators", "dispatch", "tests"),
        "Push complement to GET /api/fleet/projects/v1 and GET /api/fleet/workers/v1; API host self-reports in-process without POST.",
        "low with loopback-only mutation and a short TTL",
        "keep",
        mutates=True,
    ),
    RouteContract(
        "/api/fleet/workers/v1",
        "exact",
        "http",
        "Per opaque host AI worker map: one row per source identity (driver, delegate, observer, job, marker, project_state), host freshness plus its own report age_s, workers_status reported|unreported, unattributed host bucket for hostless leases, related links only with proof.",
        "Cache-only adapters: session-stream leases, local batch_state delegate tasks, observer presence, atlas-job registry, occupancy markers, and pushed project-state workers; never calls occupancy load refresh or SSH probes.",
        "Context-owned snapshot warmed during startup; one background writer waits 2 seconds between collections. A read that wakes it after two unread minutes waits at most 0.5 s for that collection. X-Snapshot-Age-S includes collection time and grows after refresh failures. Before the first successful collection: 503, unknown age, Retry-After 2. Requests retain the last good snapshot. Fresh hosts count toward counts.live; expired project-state reports yield freshness unknown and workers unreported; stale upstream does not synthesize worker rows.",
        ("operators", "dispatch", "tests"),
        "Complements GET /api/occupancy burn board without merging identities across sources.",
        "low",
        "keep",
        locality="mixed",
        mutates=False,
    ),
    RouteContract(
        "/api",
        "exact",
        "http",
        "API root: 307 redirect to the interactive /docs explorer so the bare path never 404s (#7090).",
        "FastAPI app routes; redirect target is the built-in Swagger UI page.",
        "Static redirect; generated per request, no cache.",
        ("humans", "agents"),
        "Complements the in-process route-contract registry.",
        "low",
        "keep; do not grow this into a new docs site",
    ),
    RouteContract(
        "/",
        "exact",
        "http",
        "Dashboard root static page.",
        "The declared product home, otherwise the machinery dashboard index.html.",
        "Static FileResponse; page fetches live API data client-side.",
        ("humans",),
        "Catch-all static route also serves other dashboard files.",
        "low",
        "keep",
    ),
    RouteContract(
        "/{path:path}",
        "exact",
        "http",
        "Static dashboard file serving catch-all.",
        "lu_machinery/api/dashboards/ (the machinery home at /machinery, and at / when the product declares no home), then the files the product entry declares (scripts/project_api/static/).",
        "Static files; machinery pages are served with the shared navigation rendered in; dashboard freshness comes from client-side API calls.",
        ("humans",),
        "Can hide missing page contracts unless dashboard files are scanned separately.",
        "medium",
        "keep; require explicit page contracts for the machinery pages and the product's declared pages",
    ),
)


PAGE_CONTRACTS: tuple[PageContract, ...] = (
    PageContract(
        "index.html",
        "/machinery",
        "Machinery overview: firing alerts and Alertmanager heartbeat, subscription capacity summary, a Projects line per registry project (open PRs, pinned lu-machinery version, default-branch CI, live workers), and merge queue status across fleet repositories.",
        "GET /api/ops/v1/alerts, /api/runtime/subscriptions, /api/runtime/logins and /api/git/pull-requests.",
        "HTTP reads every 30 seconds; a local one-second clock advances each source's age and labels it STALE past its stated bound, or unavailable when the source cannot be read. No model calls or agent polling.",
        ("humans", "operators"),
        "The machinery overview; area pages carry the full per-area tables and product pages belong to the product.",
        "medium if a stale reading is read as current",
        "keep as the machinery overview home",
        label="Overview",
    ),
    PageContract(
        "fleet.html",
        "/fleet",
        "Fleet: live agent workers with optional open-PR status, recently finished dispatches and unfinished-record "
        "attention list, and driver seats with lease liveness and links to queued mail on Comms.",
        "GET /api/delegate/active, /api/delegate/recent, /api/fleet/seats, and optional /api/git/pull-requests.",
        "HTTP reads every 30 seconds for live panels.",
        ("humans", "operators"),
        "Live agent fleet and seat liveness; messages, discussions and delivery health live on Comms.",
        "medium if a stale reading is read as current",
        "keep",
        label="Fleet",
    ),
    PageContract(
        "comms.html",
        "/comms",
        "Comms: Fleet Comms deliveries in a mail-style list with on-demand loopback-only message body and thread "
        "reader, on-demand loopback-only ACP discussions transcript reader with best-effort secrets-and-paths "
        "redaction, and delivery health store/queue/failure metrics.",
        "GET /api/fleet/deliveries, /api/fleet/seats, /api/comms/v1/metrics, and on-demand "
        "/api/fleet/messages/{message_id}/body, /api/fleet/messages/{message_id}/thread, "
        "/api/fleet/acp/conversations and /api/runtime/acp/conversations/{conversation_id}/transcript.",
        "HTTP reads every 30 seconds for live panels. Load and polling make zero body or transcript requests. "
        "Selecting a message opens its body and paged thread metadata. Opening Discussions loads the ACP list, "
        "and selecting a conversation opens its transcript. Bodies and transcripts use no-store and textContent rendering.",
        ("humans", "operators"),
        "Dedicated area page for Fleet Comms messages, ACP discussions, and delivery health.",
        "medium if a stale reading is read as current; transcript redaction covers secrets and filesystem paths only",
        "keep",
        label="Comms",
    ),
    PageContract(
        "pull-requests.html",
        "/pull-requests",
        "Pull requests and merge queue: open PRs for every fleet registry repository with draft/ready state, CI rollup, cross-family review status, queue position and age; an unreadable repository says unknown since its first failed reading.",
        "GET /api/git/pull-requests.",
        "HTTP reads every 30 seconds against the 120-second shared GitHub GraphQL cache (210-second display bound).",
        ("humans", "operators"),
        "Full per-repository tables backing the Overview merge queue summary.",
        "medium if a two-minute-old queue position is read as live",
        "keep",
        label="Pull requests",
    ),
    PageContract(
        "milestones.html",
        "/milestones",
        "Milestones: at-a-glance blocked and overdue counts, due state, plain-text open/closed counts, blocked issues with blockers first, overdue, ready-now, and top blockers across every registered fleet repository, with private repositories showing counts only and titles, due dates, and blockers withheld.",
        "GET /api/milestones and GET /api/fleet/model-stats/planning.",
        "HTTP reads every 30 seconds against the 300-second shared GitHub milestones cache (420-second display bound) and the 300-second planning issue snapshot (420-second bound); unknown, stale, or errored planning shows unknown, never zero.",
        ("humans", "operators"),
        "Dedicated area page for fleet milestones and issue planning.",
        "medium if a stale or partial collection is mistaken for complete live progress",
        "keep",
        label="Milestones",
    ),
    PageContract(
        "models.html",
        "/models",
        "Models & budget: per-account subscription usage, pace, resets, sign-in, Cursor windows, API account "
        "balances and capacity forecast; last-24-hour routing four-stage flow and recent decisions; and model "
        "value, token use and waste, job picks, value frontier, weekly trends, reviewer strictness, reviewer "
        "accuracy, and detailed model statistics.",
        "GET /api/state/routing-budget?include_subscriptions=true, /api/runtime/logins, "
        "/api/fleet/model-stats/planning, /api/runtime/routing, and "
        "/api/fleet/model-stats?window=24h|7d|30d|90d|all&view=page|samples.",
        "HTTP reads every 30 seconds for budget, capacity forecast, and routing panels; model value reads on load "
        "and on window change. Stale or unverified readings hide actionable quota, pace, reset and balance figures.",
        ("humans", "operators"),
        "Consolidates subscription budget, routing decisions, and model value statistics on one area page.",
        "medium if a stale reading is read as current or a low-sample rate is read as a verdict",
        "keep",
        label="Models & budget",
    ),
    PageContract(
        "journal.html",
        "/journal",
        "Journal: newest-first per-project log of decisions, landed changes, and operational events, with filters, paging, and since-you-last-looked markers.",
        "GET /api/journal.",
        "HTTP reads every 30 seconds (Cache-Control: no-store); auto-refresh never advances the local last-seen marker. Unavailable CLI or DB shows a clear message, never an empty log.",
        ("humans", "operators"),
        "Dedicated area page for the append-only project journal.",
        "low; loopback-only API, read-only display with plain-text bodies",
        "keep",
        label="Journal",
    ),
)


def all_route_contracts(product: ProductContribution | None = None) -> tuple[RouteContract, ...]:
    """Machinery route contracts, then the product's (the installed product when none is given)."""
    return (*ROUTE_CONTRACTS, *(installed_product() if product is None else product).route_contracts)


def all_page_contracts(product: ProductContribution | None = None) -> tuple[PageContract, ...]:
    """Product declarations replace machinery contracts for the same URL.

    The machinery home lives at ``/machinery``, so a product home at ``/`` that
    reuses the file name ``index.html`` does not hide its contract.
    """
    pages = (installed_product() if product is None else product).page_contracts
    return (*(page for page in PAGE_CONTRACTS if not any(p.url == page.url for p in pages)), *pages)


def contracts_for_route(path: str, kind: ContractKind = "http") -> list[RouteContract]:
    """Return matching contracts, most specific first."""
    return sorted(
        (contract for contract in all_route_contracts() if contract.matches(path, kind)),
        key=lambda item: item.specificity,
        reverse=True,
    )


def contract_for_route(path: str, kind: ContractKind = "http") -> RouteContract | None:
    matches = contracts_for_route(path, kind)
    return matches[0] if matches else None


def contract_for_page(filename: str) -> PageContract | None:
    for contract in all_page_contracts():
        if contract.file == filename:
            return contract
    return None

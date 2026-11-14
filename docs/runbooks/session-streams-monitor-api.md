# Session streams Monitor API (Sol PR-K)

Read-only Monitor surfaces for epic session streams. **No silent cutover.**

## Fleet Observer routes (pre-flip)

The `/api/fleet/*` routes are the read-only observer for durable fleet-comms
evidence during soak. They do not start work, apply migrations, change process
state, retrieve artifact bodies, or alter cutover authority. File handoffs
remain authoritative in every currently implemented plane mode.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/fleet/health` | Sanitized mode, health, schema, and pre-flip authority posture |
| GET | `/api/fleet/overview` | Durable requests/messages/reviews/dead-letter/ACP counts |
| GET | `/api/fleet/agents` or `/api/fleet/endpoints` | Configured and durable endpoint metadata; configuration JSON is omitted |
| GET | `/api/fleet/requests`, `/messages`, `/discussions`, `/reviews`, `/dead-letters` | Deterministic, paginated metadata projections with applicable filters |
| GET | `/api/fleet/authority/jobs` | Authority queue metadata with canonical `Source` / `Agent` / `Via`; payload and result artifacts are omitted |
| GET | `/api/fleet/acp/conversations` | Existing body-free ACP conversation and round read model |
| GET | `/api/fleet/activity` | Recent runtime `Source` / `Agent` / `Via` provenance projection |
| GET | `/api/fleet/migrations` | Applied migration metadata only; never applies a migration |

Collection routes accept bounded `limit`/`offset`; applicable routes support
`kind`, `state`, `agent`, `source`, `pr`, `conversation`, `since`, and `until`
filters. Message detail is an explicitly read-only, no-store redacted inline
preview; artifact content is deliberately omitted. Missing databases and
optional tables return deterministic empty availability states, not a write or
bootstrap attempt.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/session-streams/v1/health` | DB reachability / repo identity |
| GET | `/api/session-streams/v1/status/{stream_id}` | Lease/handoff diagnosis (`epic:N`) |
| GET | `/api/session-streams/v1/digest/{stream_id}?limit=20` | Pinned + recent entries |
| GET | `/api/session-streams/v1/dual-write-status` | Handoff file inventory existence |
| GET | `/api/session-streams/v1/drift?dry_run=true` | Projection receipts snapshot (default) |
| GET | `/api/session-streams/v1/plane-continuity` | Bundle board (streams + dual-write + plane pointer) |

Message plane status remains `/api/comms/v1/plane-status`; query it rather than
hard-coding a mode. The default and any authority change remain operator/advisor
gated, and this observer does not perform either action.

Parent: #5512 · stream #4707.

## Relationship Map data

`GET /api/epics/graph/v1` serves the read-only relationship map of issue-stream
areas, epic lease and status nodes, and cached issue membership. Missing,
incomplete or unknown membership is reported as such, never as a zero count.
For design context, see [monitor-epics-graph-map.md](../design/monitor-epics-graph-map.md).

## Milestones and issue progress

The machinery home at `/machinery` includes a progress section backed by
`GET /api/milestones`. It covers every repository in the fleet repository registry.
Each public milestone includes its title, description's first line, due date,
GitHub open/closed counts, percent complete, and linked open issues with labels.
Private repositories return counts and percent only; milestone text, dates,
identifiers, issue details and links are withheld regardless of the request peer.
The registry's `public-` role prefix admits public text; all other roles are private.

The existing dashboard polls every 30 seconds. The existing single-flight TTL
cache refreshes this collection off-loop every five minutes while requested.
No new service or periodic job is installed. The first request returns `pending`
while the collector runs. Healthy displayed data is at most seven minutes old,
including collection and page polling; older data is marked stale. The response
and page show the source age, including collection time. Upstream failures are
unknown, never a verified empty list. This bound assumes the page remains open
and GitHub responds within the collector's 20-second per-repository deadline.

GitHub REST exposes summaries and issues separately. Each collection refresh
uses a conditional milestones-list read and, for public repositories with open
milestones, one repository-wide open-issues-list read, plus pagination. Private
repositories need only the milestones list. The shared cached GitHub reader
reuses ETags. Warm page polls within the five-minute TTL make no GitHub calls.
The JSON `rest_reads` field reports collection read attempts per repository;
the shared reader may also initialize its authenticated identity once.
The 20-page safety ceiling is explicit partial data, never complete progress.

GitHub's milestone counts include pull requests, matching GitHub's progress.
The progress list includes issues only. Percent is closed / (open + closed) × 100,
rounded to one decimal; an empty milestone is 0%.
The module ships under the existing `graft lu_machinery` packaging rule. Agent
extension deployment inventory does not enumerate API modules or tests.

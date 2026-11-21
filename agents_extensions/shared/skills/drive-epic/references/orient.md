# Orient, inbox count, topology (drive-epic §0, §0b, §1)

Read at cold start and whenever you lose track of fleet state. The §0a inbox drain itself is
stated once in the core `SKILL.md` loop.

## §0. Orient

Read the pinned provider facts in `.claude/<lane>-epic/DRIVER-STATE.md`, then run
`lu-driver-state render --lane <lane>` for open work and `lu-driver-state check --lane <lane>`
(your assigned lane key). `lu-driver-state` is a wrapper the launcher environment puts on `PATH`,
not a command of the installed release; if `command -v lu-driver-state` is empty, report the
missing tool to the CTO and do not hand-edit tracker state. Treat `UNOWNED`, `STALE` and `OVERDUE` findings as a worklist. Read the
program and plan first (the `operator_order` item and its child `goal` items; `lu-driver-state
show <id>` gives full history), then standing orders, the latest `handoff_note`, in-flight work,
blockers, decisions and next actions. The database is the lane's handoff; the markdown file is
only a pointer ([handoff](handoff.md)).

```bash
.venv/bin/python -m lu_machinery.fleet_comms cold-start-board   # cold-start first action
curl -sS --max-time 2 "http://127.0.0.1:8765/api/orient?lean=true" || true
curl -sS --max-time 2 "http://127.0.0.1:8765/api/work/v1/projection?stream=<your-stream>" || true  # best-effort: degraded or absent sources are normal
curl -sS --max-time 2 "http://127.0.0.1:8765/api/work/v1/next?stream=<your-stream>" || true  # stream-scoped pick list
.venv/bin/python -m lu_machinery.fleet_comms plane-status        # message-plane mode/parity
```

`cold-start-board` probes fleet, plane and stream state so you orient from live data. Query the
plane mode fresh; never hard-code it. The projection returns your lane's issues, PRs, dispatch
tasks and reviews, each with a rule-derived `health`, an `attention_rank` and a
`safe_next_action`; `next` is a compact pick list (a cold cache answers `503` `building`, an
unknown stream `400` with `valid_streams`). Both are queue inputs alongside your stream, issue
and handoff sources, never replacements for them.

Know your `SESSION_EPIC`, stream and handoff slot; the launcher already claimed the lease, so
never open or resume it. Session health by seat: **gemini** has a canary lane
(`.venv/bin/python -m lu_machinery.session_canary.gemini_lane mint --epic <epic>`); **Claude** and
**Sonnet** use the native SessionStart and PostCompact hooks plus `thread-rollover`.

## §0b. Fleet Comms inbox count — cold start

The prompt hook `check-agent-inbox.sh` (`agents_extensions/shared/hooks/`) reports queued
deliveries, and this body-free count is the one-shot equivalent:

```bash
.venv/bin/python -I -m lu_machinery.fleet_comms deliveries pending --recipient "$SESSION_HANDOFF_AGENT"
```

It never reads a body and never claims a delivery; the §0a / §4a / §5a / §8a drains in the core
loop still read, apply and acknowledge everything it points at.

## §1. Read topology and metrics (query it, never hold it)

```bash
.venv/bin/python -m lu_machinery.fleet_comms metrics        # efficiency metrics (no content)
.venv/bin/python -m lu_machinery.fleet_comms backlog        # pending/dispatched delivery
.venv/bin/python -m lu_machinery.fleet_comms dead-letters   # stuck deliveries
```

Per-lane budget health and the pace check before dispatch are in
[routing-and-dispatch](routing-and-dispatch.md#3-route-by-model--harness-fit).

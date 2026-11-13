# Fleet Comms failure metrics

The authority-mode `metrics` CLI and `/api/comms/v1/metrics` expose
`failure_windows.24h` and `failure_windows.7d`. Each contains `dead_letters`,
`failed_deliveries` and `failed_jobs`, with a `total` and `by_recipient`.
`failure_lifetime` contains separate lifetime totals. The older `dead_letters`,
`deliveries` and `jobs` fields retain their lifetime meanings for compatibility.
The Comms panel and summary chip warn from recent values only. Missing recent
measurements display `?`; they are not inferred from lifetime counts.

Windows include both endpoints and exclude future events. Dead letters use
`created_at`; failed deliveries and jobs use `completed_at`, falling back to
`updated_at` for historical rows. An old job that fails today counts today.
A maintenance expiry creates dead letters today and therefore counts today too.
Only state `failed` counts in the two failure measures; expired/dead-lettered
rows remain represented by the separate dead-letter measure.

A request job inherits its message's recipient only when there is exactly one.
Other jobs use `authority-job`, so fan-out never multiplies the event count.
Older PostgreSQL job schemas without subject metadata also use `authority-job`;
when they lack `completed_at`, the reader uses `updated_at`.
The CLI preserves exact recipients. Public JSON and Prometheus retain fixed
agent names, `claude-cto`, `kimi` and `authority-job`, combining other identities
in `other`. They do not expose private session identities or unbounded labels.
Legacy mode retains its older metrics; recent authority measurements are
unavailable there.

## Alert integration

The existing Monitor `/metrics` scrape collects independently of page visits:

- `lu_fleet_comms_dead_letters_recent{window="24h"}` and `{window="7d"}`
- `lu_fleet_comms_failed_deliveries_recent{window="24h"}` and `{window="7d"}`
- `lu_fleet_comms_failed_jobs_recent{window="24h"}` and `{window="7d"}`
- Each measure also has a `_recent_by_recipient{window,recipient}` gauge.
- `lu_fleet_comms_collection_success` is 1 on collection success and 0 when
  unavailable. Unavailable measurements are omitted rather than emitted as zero.

These are rolling gauges, not monotonic counters. Do not apply `rate()`.
Suggested initial alert: `lu_fleet_comms_dead_letters_recent{window="24h"} > 0`
for 5 minutes. Alert separately on collection success 0 or absent scrape data.
The private infrastructure repository owns alert rules and routing to the CTO;
this change supplies measurements and does not install those rules.

## Remove a stale subscription

```bash
.venv/bin/python -m lu_machinery.fleet_comms channel unsubscribe CHANNEL RECIPIENT \
  --idempotency-key UNIQUE-REMOVAL-KEY
```

The command removes future implicit fan-out only. Existing messages, deliveries,
explicit recipients and acknowledgments are unchanged. All subscribers must
exist or the transaction fails. Retired identities can be removed without
re-admitting them. An unknown channel also fails.

A durable `authority_idempotency` receipt records the operation's channel,
payload hash, key and timestamp atomically with removal. Reuse the key for a
retry. A retry after re-subscription leaves the new subscription intact; use a
new key to remove that new subscription. Reusing a key with a different channel
or recipient set fails.

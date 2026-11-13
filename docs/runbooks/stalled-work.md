# Stalled dispatch work

Issue #11212 returns committed work to its dispatching lane until it lands or
the owner explicitly abandons it. The report scans hot task records younger
than 14 days and open PRs in the public repository. It imports the existing
stale-record classifiers without settling, archiving or editing any record.

```bash
.venv/bin/python -m lu_machinery.orchestration.stalled_work stalled
.venv/bin/python -m lu_machinery.orchestration.stalled_work stalled --route
```

The JSON rows name the task, dispatch `initiator` lane, commit or PR, stall
kind, starting timestamp and age in hours. Current lanes come from the slots
in `area_assignments.yaml`. Retired initiators resolve through
`terminal_abandon_succession.yaml` and the fleet taxonomy to a current slot.
The resolver preserves the provider when possible, otherwise using the first
registered slot for that stream. Unrecognized initiators and PRs
without a matching recent task go to `claude-cto`. Worker provider and branch
prefix do not establish ownership.

Unpushed work requires a local branch or worktree commit absent from every
freshly fetched origin branch. Pushed branches without an open PR get a
two-hour grace period measured from terminal dispatch completion. Git does
not retain the remote push timestamp. Branches already contained in origin's
`main` or tied to a merged PR by exact commit identity are excluded. This also
excludes squash merges. Rejected PRs require a trusted recorder comment for the
current head, no live matching task, and two hours since both the rejection
and the latest terminal repair task. Author records take precedence over
read-only or `review_branch` records when selecting the owner and repair time.
New commits supersede older verdicts.
Explicit `terminal_abandon` records and `terminal-abandon.v1` ABANDON receipts
are excluded. This observer does not authorize abandonment or cleanup.

## Keeper entry point

The packaged keeper service calls this command after its queue pass:

```bash
.venv/bin/python -m lu_machinery.orchestration.stalled_work stalled --tick
```

The same command can be called by an existing five-minute lane-wake tick.
Do not install a second timer. The entry point serializes concurrent calls
using its cadence checkpoint in shared `batch_state/stalled-work/cadence.json`.
It writes no task records. A failed scan or partial routing attempt leaves
the cycle retryable. Fleet deduplicates messages already accepted on retry.

| Environment value | Default | Effect |
| --- | --- | --- |
| `LU_STALLED_WORK_CADENCE_MINUTES` | `30` | Minimum scheduling bucket size in minutes |
| `LU_STALLED_WORK_ROUTE_ENABLED` | `0` | `1` enables routing from `--tick` |

Report-only is the default. Direct `--route` explicitly sends requests.
Routing emits one supervisory message per owning lane and a separate CTO
summary through `AuthorityService(root=None)` when rows exist. Empty reports
send no messages. Keys include cycle, lane and
the digest of the sorted row set, excluding changing age values. Retries in
one cycle deduplicate; the next cycle repeats unresolved work. No tmux input,
push, rebase, deletion or task-record write occurs. Missing Git or GitHub
evidence produces errors, exit 1 and no routing from that report. An unknown
PR verdict instead produces a `verdict_unknown` row for `claude-cto`, with the
reason and unknown age, while other rows continue routing. The keeper's
post-step ignores the observer's exit status so a scan failure does not mark
the keeper itself as failed.

After merge, the host configuration owner must deploy the updated keeper
service or add the entry point to its existing private lane-wake service,
then set `LU_STALLED_WORK_ROUTE_ENABLED=1` in that service's environment.
Host/private configuration and live enablement are outside this public change.
Verify a report first, then inspect the tick's Fleet message receipts.

## Old unfinished records

For `needs_finalize` records at least seven days old, use the existing settlement
command before deciding whether surviving work should be kept or closed:

```bash
.venv/bin/python -m lu_machinery.orchestration.stale_task_records settle-stale --json
.venv/bin/python -m lu_machinery.orchestration.stale_task_records settle-stale --apply --json
```

The first command is a dry run. Remote heads come from `git ls-remote`; neither
mode fetches, refreshes, or prunes local refs. Its `classes`, `outcomes`, and `needs_decision`
fields show the proposed settlements and remaining decisions. `keep_close`
contains one row per remaining record, with task id, repository, branch,
recovery locations, open PRs, and age. The text report prints the same rows
with a `KEEP/CLOSE` prefix. A missing or incomplete PR listing is reported as
unknown, never as proof that no PR exists.

An old A, B, or D record becomes eligible for settlement when it has no matching
open PR and no existing worktree or runtime path. A surviving ref preserves
work without keeping the task live. A merged PR tied to the task settles the
record `done` with its link; otherwise it settles `failed` with
`abandoned; work preserved at <ref or location>`. Rescue refs, scan refs, local
branches, and original refs remain in place. An existing worktree directory
keeps the record reported even if its Git metadata is missing.

The age floor still applies. Incomplete repository, filesystem, or PR evidence
leaves a record unresolved. Settlement holds the existing record/worktree lock,
re-reads its mtime and status, and checks that its checkout has not reappeared
before writing. The new rule also lists open PRs and worktree directories again
under that lock. The older unrecoverable-work rule also rechecks local refs and
commit reachability under that lock. Settlement deletes no branch, ref, or
worktree. Those require a separate decision.

The machinery page counts current unresolved statuses. Settled records move
to recent outcomes and leave the keep/close warning, even if an old boolean
flag still says `needs_finalize`. The page does not run a Git census or write
records during a request; the settlement command performs that work.

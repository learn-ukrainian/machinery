# Watching a PR through review and CI

Used by `full-send` and `full-stop` between opening the PR and the gate. One supervisor per PR:
the accountable driver's cycle.

`"$PY"` is the primary checkout's interpreter ([delivery](delivery.md#interpreter)).

## Waiting

- Do not write a watcher, polling loop or `sleep` loop, and do not run a second watcher on the
  same PR: every watcher shares one GitHub rate limit.
- Arm waits on the original tasks (`lu_machinery/dispatch.py wait <task> --run-nonce <nonce>`, see
  `review-loop`). Check CI and the queue on each driver cycle:

  ```bash
  gh pr view "$PR" --json state,isDraft,headRefOid,mergeable,mergeStateStatus,autoMergeRequest
  gh pr checks "$PR"
  "$PY" -m lu_machinery.publish read checks --sha "$HEAD_SHA"   # REST, when GraphQL is spent
  "$PY" -m lu_machinery.gh_merge_queue_status "$PR" --json
  ```

- A review round does not wait for the PR's `CI Gate`: it runs in parallel with CI on the head
  under review (`full-send` steps 4 and 5). Pending CI is not successful CI, so the gate (not the
  review) waits for green before enqueue.
- While CI or a review runs, dispatch the next ready item instead of idling (drive-epic §2c).
- An event is news, not a decision: re-check the gate yourself.

## Findings from reviewers and bots

- Act only on checks and comments newer than the latest push.
- Verify each finding against the source before changing code. A real finding goes to the
  authoring lane as a fix; prove the fix before it is pushed.
- Answer a false positive with a written reason through `pr-comments`. Bot or AI review is
  advisory unless it is the reviewer of record.
- Every fix push moves the head: run the `review-loop` delta for it in parallel with the new
  head's CI; enqueue needs both on that head.
- Keep the PR at the size its outcome needs. Decline extras (unneeded guards, rare edges,
  extra compatibility) with a reason and send them as follow-ups.

## Failing CI

- Diagnose from the failed job, or from the pytest JUnit artifact when the run has one
  (drive-epic §7).
- Classify every failed job before acting. It is either a failure of the change's own
  checks, an evidenced infrastructure failure, or unknown.
- A failure of the change's own checks gets a fix through the authoring lane. A second such
  failure on this PR means stop and report, whatever the head: fix commits and owner changes
  do not reset the count. These are the only failures counted as "CI fails a second time".
- An **evidenced infrastructure failure** names its cause from the job log: a runner lost or
  shut down, a checkout or network failure before the change's code ran, or a provider
  capacity or outage error. A cancelled job alone is not evidence. Treat an evidenced
  infrastructure failure as a service problem. Write the evidence on the PR first (rules
  core P4), then use the head's one evidenced rerun, `"$PY" -m lu_machinery.publish run-rerun`,
  or one re-enqueue after an infrastructure ejection.
- The allowance is one rerun per unchanged approved head. It is a shared record, so it
  carries across owner changes: a successor never gets a fresh rerun on the same head
  (`pr_ownership accept` prints whether it is spent). A further infrastructure failure on
  that head escalates to the CTO with the evidence, without a second rerun. A failure with
  an unknown cause still means stop and report. Required checks must pass; nothing waives
  them.
- After a base-branch fix, refresh with
  `"$PY" -m lu_machinery.publish pr-update-branch --number "$PR"`, never `run-rerun`.

## Owner changes

An open PR always has one live owner. If its owner stops (`BLOCKED`, a timeout, a crash),
the merge-queue keeper alerts the CTO inbox after 15 minutes without a live owning task. The
alert stays outstanding until a named successor accepts. The successor runs this first, from
its own dispatch task:

```bash
"$PY" -m lu_machinery.orchestration.pr_ownership accept --pr "$PR"
```

Acceptance is refused while another task may still own the PR (one owner at a time). The
successor then carries the review ledger and budget from the PR and the head's rerun allowance
from the receipt (delivery.md, hand-off record); it never restarts them.

## The base moves

- Re-check BEHIND and DIRTY before calling the PR ready. Rebase through the authoring lane and
  push with `--force-with-lease=<branch>:<observed head>`; the new head needs a delta review
  and fresh CI.
- If another PR makes this one obsolete, stop and report it with evidence. Closing the PR is
  a driver act; closing an issue is the CTO's.

## Merge-queue kicks

After enqueue, a CI Gate comment on the PR with a run URL means the queue ejected it. Read the
failed jobs, fix or rebase, re-run the exact-head review if the head moved, and re-queue in the
same hour (drive-epic §7).

## Queue custody

When the PR's only remaining step is waiting in the merge queue, the owner may hand the rest
to the integration owner (or a successor) instead of waiting until the dispatcher's hard
timeout ends it. The custodian, from its own live dispatch task, accepts custody of ejection,
stale approval, merge confirmation and closeout, including disposing of the linked issues:

```bash
"$PY" -m lu_machinery.orchestration.pr_ownership accept --pr "$PR" --custody --from "$OWNER_TASK"
```

The waiting owner then checks:

```bash
"$PY" -m lu_machinery.orchestration.pr_ownership queue-exit --pr "$PR"
```

Exit 0 means the owner may end its run cleanly. It reports `QUEUED_UNDER_CUSTODY` with the
custodian and the queued head, not `MERGED`. Any other exit means keep waiting. The check
refuses unless the keeper's ownerless-PR alert ran successfully in the last 20 minutes, the
custody names this owner, the custodian is live, the head has not moved and the PR is in the
queue. There is no timeout exemption: without custody, the owner waits within its deadline.

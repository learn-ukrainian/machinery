---
name: drive-epic
description: Drive one explicitly assigned epic or track through Fleet Comms. For the core fresh lesson-based build (#7994) see §0d. Use track-completion for a single V7 module on a track that still uses V7.
effort: high
---

# Drive an epic lane

You drive **one epic or track lane** (`SESSION_EPIC` is set), not the whole fleet. You own the
lane's judgment: what is wrong, what is next, which model and harness should do it, whether the
artifact really worked, and what residual remains. A worker you brief owns its own chain to
`MERGED` and closeout (rules core P1, `full-send`); you sequence the stream, keep its milestones,
brief and route, check outcomes, escalate and hand off. Judgment is not implementation: seat
no-solo rules still bind. You are not a designated approver and not the reviewer of record for
work you drove.

This skill teaches the method, never the roster. Lanes, caps, models and review seats are live
data: `GET http://127.0.0.1:8765/api/rules`, `lu_machinery/config/model_catalog.yaml` and
`docs/best-practices/agent-activity-matrix.md` §2/§2b. Every claim you make (a lane, a cap, a
gate status, a count, a worker's state, a Ukrainian form) comes from tool output in this turn.
Timestamps come from `date -u`.

The CTO opens every GitHub issue and approves every new test. You report findings through Fleet
Comms and never brief a worker for a reproducer, a regression test or repeated runs.

## Delivery and done

A dispatch, a branch, an open PR, green CI or "Next:" is **not** done. Deliver every change with
the delivery skills: `full-send` (to MERGED and closeout, run by the briefed worker or by you),
`full-stop` (ready-to-merge), `review-loop` (rounds and budget), entered through `takeover` or
`modernize` for inherited or old work. A worker starts follow-ups only within its brief's cap.
You land a PR yourself only when no brief owns it, and only when the gates of
`workflow.md` § Merge policy hold: the recorded exact-head review the PR's tier requires (none for
a Trivial PR) and a green same-head CI Gate; never ask the operator to merge.

At each turn end and at session end:

- [ ] `.venv/bin/python -m lu_machinery.hygiene.branch_sweep --json` reviewed and its
      proven-safe deletions applied (`--apply`), then `git fetch --prune`. Every `merge_closeout`
      residual and every SKIPPED row gets a decision (remove it safely, or record a verified
      reason to keep it); never force removal.
- [ ] A settled task's worktree is gone (`worktree_disposition`); remove a `retained` or `error`
      one only after its content is published:
      `.venv/bin/python -m lu_machinery.orchestration.worktree_claims remove <path> --owner-task-id <task> --reason <why>`.
- [ ] No lane issue has a merged PR and no disposition; none is kept open as a running log.
- [ ] Every live worker has an armed wait, and no finished worker waits on the operator.
- [ ] The residual count is quoted from a tool; above zero it has a next dispatch this session
      (§2a).

Git hygiene is rules core P4: the primary checkout stays on `main` and read-only; work happens in
`.worktrees/dispatch/<agent>/<task>/`; every commit carries `X-Agent`; never push to `main`.

## The loop — run it every cycle

**Inbox drain** (§0a, §4a, §5a, §8a). The live loop itself, never a detached worker, reads and
applies every message marked `unread` or `read-but-not-live-consumed`, then acks it:

```bash
.venv/bin/python -I -m lu_machinery.fleet_comms deliveries receive --recipient "$SESSION_HANDOFF_AGENT" --worker-id "$SESSION_HANDOFF_AGENT"
.venv/bin/python -I -m lu_machinery.fleet_comms deliveries ack <delivery-id> --worker-id "$SESSION_HANDOFF_AGENT" --fence-token <n>
```

Record every order, in-flight task (with its full run nonce), blocker, decision and next action
as a lane tracker item (`lu-driver-state add|note|evidence|status`; never delete items; done
needs evidence, dropped needs an operator reference): [handoff](references/handoff.md).

1. **§0 Orient.** Run the cold-start board, lean orient, Work API projection, stream `next` list and
   plane status commands in [orient](references/orient.md); read your lane with `lu-driver-state render --lane <lane>` and
   `lu-driver-state check --lane <lane>` (a launcher-provisioned wrapper, not part of the
   release). The launcher holds your lease; never claim it.
2. **§0a Inbox drain.** A CTO demand (an URGENT order or blocker-report request) outranks all
   lane work. Answer within its deadline (default 10 minutes), one blocker line per named
   deliverable ([lane-report](references/lane-report.md)). Then work the named deliverables and
   their blockers first, until they land or the CTO releases them: fix any blocker you can, name
   the owner of any you cannot, and let other ready work fill only the worker slots the demanded
   deliverables cannot use. Report again on every change.
3. **§0c / §0d Epic-specific rules.** Hramatka (#4542) and the core fresh build (#7994) add
   binding rules: [epic-specific](references/epic-specific.md).
4. **§1 Topology and metrics.** Query the metrics, backlog and dead-letter commands in
   [orient](references/orient.md); never hold fleet state in context.
5. **§2 Pick the next action and dispose every open issue**: in flight, dispatched now, or on a
   named §2c hold. Before dispatching, look for a sibling PR
   (`gh pr list --state all --search "<issue>"`). While review or CI runs, dispatch the next
   ready item or record a §2c code in the same turn:
   [queue-and-capacity](references/queue-and-capacity.md).
6. **§3 Route** by model and harness fit from live capacity; write a ROUTING_CARD_V1 before every
   implement dispatch (no card, no dispatch); pass §3a outcome adequacy for substantive prompts:
   [routing-and-dispatch](references/routing-and-dispatch.md).
7. **§4a Inbox drain** immediately before dispatch.
8. **§4 Dispatch** only with a green task card and preflight, explicit model, research metadata
   and `--owned-path`.
9. **§5 Settle.** Arm `.venv/bin/python -m lu_machinery.dispatch wait <task-id> --run-nonce <nonce>`
   (or Monitor) in the dispatch turn; state a worker's status only after
   `.venv/bin/python -m lu_machinery.dispatch status <task-id>` this turn, and record its task ID,
   full nonce, status and head in its tracker item.
10. **§5a Inbox drain** after settle.
11. **§6 Review, §7 landing, §7a closeout, §7-rollout** run through the delivery skills above;
    the mechanics a driver still needs (the review launch recipe, red CI and queue kicks, the
    rollout authority table) are in [review-merge-cleanup](references/review-merge-cleanup.md).
12. **§8a Inbox drain** before handoff.
13. **§8 Handoff.** Reconcile the tracker, add one `handoff_note`, keep `DRIVER-STATE.md` a
    pointer under 4 KB, preserve the Fleet receipt: [handoff](references/handoff.md).

A new multi-PR outcome is briefed as a [decision report](references/decision-report.md). Report
to the CTO hourly, and every 30 minutes while an operator-priority outcome is open, in the
[lane-report](references/lane-report.md) format. Seat-specific adjustments:
[model-deltas](references/model-deltas.md).

## Escalate — do NOT decide these solo

Ask Opus 5.5 / Sol 6.2 for review, critique and design input. Never resolve these from the loop;
rules core P6 sets who decides each:

1. Any architecture, layout or process change (designated approval, or the operator).
2. A contested review verdict (author and reviewer disagree, or two reviewers split).
3. A fragile fix: root-cause it, then escalate the design if the right layer is unclear.
4. A high-risk route that would trip `risk_quality_floor` in `model_catalog.yaml`.
5. A repo-wide safety interruption of another lane (generated artifacts, linter or Python-version
   bumps, cross-track architecture conflict).
6. **Operator only:** a production, Pages or public cutover (needs a present-tense GO), HA or a
   new server, host access and security configuration, accounts and credentials (§7-rollout).

Apply the risk floor to yourself too. Passing gates is necessary, not sufficient: verify the real
artifact renders or runs before "ready".

## This skill is NOT

- A restatement of the served rules (`/api/rules`) or the live roster.
- The CTO seat (`cto` skill), a single-module writer (`$track-completion`; core fresh build: §0d)
  or a second curriculum orchestrator.
- Authority to flip the fleet-comms plane, self-merge a fleet-wide process change or self-review
  work you dispatched.

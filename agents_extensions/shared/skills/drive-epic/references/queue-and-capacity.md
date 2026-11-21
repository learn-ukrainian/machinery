# Queue, issue ownership, no idle lanes (drive-epic §2, §2-epic, §2a, §2b, §2c)

Read when picking the next action, disposing the epic's open issues, or deciding whether a free
lane may stay idle.

## §2. Pick the next unblocked action

Reconcile the latest operator directive with the issue, the frozen packet and the handoff before
choosing work, and apply it over older conflicting stop text. An operator pause needs an explicit
operator release; a newer unrelated directive is not one. Record superseded instructions and
unresolved conflicts.

Sources of next work: your stream's tail and handoff, the epic's open issues, the build and review
queue, the Work API projection's ranked attention list, and
`GET /api/work/v1/next?stream=<your-stream>`. Step 0 of any dispatch:
`gh pr list --state all --search "<issue-nr>"`, because an open issue is not an unfixed one. If
nothing fits a free lane, record the §2c `no_ready_work` disposition and leave it idle; never
manufacture busywork.

### Friction report

After checking the lane's actual work, send the CTO one friction report through Fleet Comms.
Messages are immutable: send a new report that names and supersedes the earlier one each cycle.
Include the affected outcome, sanitized evidence, cause (known or unknown), measured waste only
when measured, the working path, the smallest correction, the accountable owner and a falsifiable
DoD. Write NONE only after that check, and never create a report just to say NONE. Keep sensitive
evidence in private operational evidence.

## §2-epic. Epic issue ownership cycle

Every cycle advances the epic's open issue set:

1. **Inventory** the epic's open issues (plus Work API `/next`); quote the count.
2. **Disposition each one**: **in_flight** (task id and head), **dispatch now** (ROUTING_CARD,
   the capacity pick, explicit model) or a **named hold** with one §2c code.
3. An open epic issue with no disposition is a driver failure. While review or CI runs on unit
   N, dispatch the next ready child or emit a §2c code in the same turn; re-read checks and
   finish merge hygiene on the next live turn for any clean approved PR.

## §2a. No fabricated done

Never invent acceptance thresholds the operator, the issue or the epic goal did not set. Never
declare a goal done while a measured residual remains in the same mandate, unless tools prove it
impossible or the operator accepted it on the issue. Never end on "when you want" or an "optional
next" for in-scope residual; dispatch it. Before "done" or handback, quote the tool residual
count; `residual > 0` needs a next dispatch in the same session.

## §2b. Eggbot QA seat

UI QA, design review, UX review and retests are tasked through the CTO's bot-gateway role, not
through the dispatcher; ask the CTO over Fleet Comms. Eggbot is not a review of record and never
merges; same-family Grok must not review an eggbot-authored PR. Contract:
`docs/runbooks/grok-bot-qa-observer.md`.

## §2c. No idle lanes — subscription min-max

Idle paid lanes are direct financial loss (operator 2026-08-17). It binds every driver seat.

**Terms.** *Free lane*: a healthy, budget-eligible seat with no live assignment. *Ready item*:
queued work that is valuable, unblocked and has an integration path. *Settle event*: any
dispatch, review or CI terminal or decision point.

**Precedence:** correctness and quality, then safety and resource bounds, then dependency and
critical path, then utilization. A later item never overrides an earlier one.

1. **Waits are dispatch windows.** After any dispatch or review ask, fill every free lane with a
   compatible ready item (unblocked work, banked follow-ups, prep for the next child whose
   dependency allows it) before holding.
2. **Authorized idle names one code:** `dependency_blocked | authoring_wip_cap | review_wip_cap |
   ci_capacity | worktree_wip_cap | disk_capacity | integration_wip_cap | human_decision |
   no_ready_work`. Silence is not a disposition; `dependency_blocked` names a real blocker.
3. **Pipeline with a depth limit.** While review or CI runs on unit N, author N+1 only up to the
   WIP and resource cap; unit N regains priority the moment feedback returns.
4. **Ready-work forecast.** An unfinished epic needs a current forecast; an empty ready queue or
   an empty stream `/next` is a driver defect unless the epic is done or a disposition applies.
   Report banked follow-ups to the CTO through Fleet Comms.
5. **Anti-gaming.** No placeholder agents, artificial splitting, premature PRs or speculative
   work without an integration path. Disk wins every conflict (`df` and `du` of `.worktrees`
   before fan-out; clear retained worktrees whose content is published first).

At every dispatch or review settle, evaluate eligible ready items against the admission WIP
limits; the reminder fires only when something is eligible, then dispatch or pass a structured
code (unknown codes are rejected). `driver_breadth_report --enforce`
([routing-and-dispatch](routing-and-dispatch.md#3-routing-mandatory-routing_card_v1--breadth-operator-go-2026-08-06))
fails missing or dishonest idle dispositions.

```bash
.venv/bin/python -m lu_machinery.orchestration.dispatch_settle task --task-id <id> \
  --idle-snapshot-json <snap.json> [--dispatched | --disposition <code>]
.venv/bin/python -m lu_machinery.fleet.idle_settle evaluate \
  --snapshot-json <snap.json> --kind dispatch --task-id <id> \
  [--dispatched | --disposition <code>]
.venv/bin/python -m lu_machinery.fleet.idle_settle report
.venv/bin/python -m lu_machinery.fleet.idle_settle admission --snapshot-json <snap.json>
```

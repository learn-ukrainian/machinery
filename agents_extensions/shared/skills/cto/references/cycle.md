# Cycle checklist

Run every cycle while work is in flight, and before any routing, budget or status report.
Every line of a cycle report is tool output from this cycle; anything not read is unknown,
never fine.

0. **Monitoring first.** Read the firing alerts and the CTO scorecard metrics from the
   monitoring stack: Prometheus `ALERTS{alertstate="firing"}` and the `lu_cto_*` series (the
   private operations doc names the endpoints). A source reported down
   (`lu_cto_source_ok == 0`) is an attention item. Use hand-run commands (`gh`, usage
   meters) only for what no metric covers; a missing or wrong metric is a monitoring task,
   not a reason to keep measuring by hand.
1. **Inbox.** Inspect the launcher slot and drain the three driver recipients
   ([startup](startup.md#4-identity-and-inbox)) at cycle start, after workers settle and
   before handoff. Claim and read each delivery, then dispatch if the delivery permits it, then apply and ack the outcome even if no work launches.
   `claude-cto` is the durable CTO
   recipient: claim it with your session identity as `--worker-id`, and a worker escalation
   waits there at most one cycle. Any backlog still queued at a `claude-cto-<id>` name is a
   leftover to drain once, then report it as a producer defect.
   Owner replies come through the Jean channel and Fleet Comms.
2. **Every pool's usage**, from the scorecard metrics when present, including unused ones:
   each Claude pool, Codex, Grok and Cursor, AGY and any other catalog pool. A pool with no
   metric gets a hand-read meter, and the gap is logged as a monitoring task. Grok and Cursor
   share one meter, never two pools of headroom. Keep readings in a scratch log for trends.
   Read which pools report by counting the series per pool (for example
   `count by (pool) (lu_cto_pool_left_percent)`), never from a head-limited dump or a view
   that lists only known pools: the scorecard file writes each pool's block in turn, so a
   truncated listing showed one pool and was misread as only one pool being live.
   Read each pool's CodexBar pace line: used minus expected and whether it will last to reset.
   When the [pace rule](decisions.md#tactical-layer-per-cycle) shows a deficit, shift ready work
   to an eligible pool with reserve. Unknown pace is not scarcity.
   Read Opus task-hours per lane over the last 12 hours from task records. Flag a lane
   when the Claude pool's pace line shows it running out before reset, or for unauthorized use.
   Before recording an account or credential blocker from a refusal code, check whether
   the account's access token has merely expired while its refresh token remains. Then the
   blocker is a token refresh, done through the supported refresh path in the private
   operations doc, not an operator re-login.
3. **Disk and load.** `df -h .`, `uptime`, `git worktree list | wc -l`. When disk climbs,
   run the retention tools and clear retained worktrees (published content only) before admitting write workers; never delete bulk
   corpus or data without a separate operator-authorized task (core.md P6).
   **Lane machinery.** Read the latest tick result of the host driver keeper and its lane
   wake. They are host-local code, not in this repository; their units and log location
   belong in the private operations doc. A tick must report no errors and must list
   every running lane pane. An error, or no lane panes, means no lane gets nudged; fix it
   this cycle. A critical keeper alert is probed to its cause before it is called stale or
   noise: read the lane pane's process tree and state, and confirm the keeper can restart
   the lane (its launcher runs as a child of the pane shell); a lane that cannot be
   restarted stalls at its next handoff. After a keeper config edit, verify one tick
   before nudging lanes.
   Read the keeper's per-CLI context flow: Claude waits for the keeper's tokened handoff
   prompt. Codex compacts natively, but a Codex driver hands off to a fresh session after
   its 4th compaction and before its 8th (operator 2026-10-25). Count each live Codex
   driver thread's compactions from its rollout file under `$CODEX_HOME/sessions`
   (`"type":"compacted"` events). At 4 or more, order the handoff that cycle; at 7, restart
   the lane yourself once it shows HANDOFF-DONE. When you record a new lane rule, measure
   every lane against it in the same cycle and name the control that will enforce it.
4. **Open PRs**, from the scorecard metrics when present, in every repository the fleet
   lands in, with `number`, `headRefOid`, `mergeable`, `mergeStateStatus`, `isDraft`,
   `reviewDecision`. Per PR: a review of record at the current `headRefOid` (verdict,
   reviewer model), a CI Gate run for that head and green CI; an older-head verdict is stale.
   A conflicting PR or missing current-head CI Gate run is an attention item. A background
   watcher with a deadline checks both as well as failing checks, replacing polling.
   Check main-branch CI in both public and private repositories. Give each open PR one
   blocker line with its age, next action and owner; flag ages over the one-hour target.
   Whether a merge-group candidate contains a fix is read from Git, never inferred from
   timestamps: `gh api repos/<owner>/<repo>/compare/<fix-sha>...<candidate-sha>` reports
   `ahead` or `identical` only when it does.
   Read keeper reasons for approved green PRs not enqueued. List every `hold` with its
   owning tracker item and removal condition; clear it when that condition is met.
   Measure stage time across dispatch, review, PR admission CI, queue CI, merge and closeout
   in both repositories. Act on the slowest stage this cycle, with an owner and next check.
   Run `"$LEARN_UKRAINIAN_PYTHON" -I -m lu_machinery.orchestration.pr_ownership check` (exit 1 lists ownerless
   PRs, 2 is unknown). It is the replacement-owner check `lu_machinery.fleet.pr_owner_check` with
   accepted handoffs counted, and an owner whose supervisor cannot be observed is unknown, not
   ownerless. Dispatch an owner for every ownerless PR, replacing any owner that ended failed,
   timed out or was cancelled. Ownership is the live task on the PR branch, the running task
   named by the X-Agent trailer on the PR head, an accepted handoff, or recorded custody, never
   the author or branch prefix. The merge-queue keeper runs the same check every 5 minutes. It
   alerts the CTO inbox when a PR has had no live owning task for 15 minutes, and repeats the
   alert hourly. Re-seat each alerted PR this cycle with one successor (the integration owner
   for queue work in CTO-only mode). Start the successor's brief with
   `lu_machinery.orchestration.pr_ownership accept --pr <N> --repo <owner/name>`.
   Before choosing a custody command, run `pr_ownership status`. `custody.evidence` is `known` or
   `unknown`. Unknown means no keeper tick has been saved, or the last tick could not observe
   (`last_tick.error` is set): do not treat the PR as untouched. A tick that observed and failed
   to deliver stays `known`; `last_tick.ok` is still false.
   `waiting_for_grace` means the keeper has not acted. `recorded_custody` means a decision owner is
   already written; `by: keeper` means the keeper wrote it after grace, `by: coordinator` means this
   seat did. Do not record custody again when that row is already there. `unknown_evidence` is a PR
   this file cannot classify. `alert_outstanding` is an alert the keeper raised and did not turn
   into recorded custody. `check` prints one JSON row per PR it cannot treat as owned.
   Exit 1 means at least one of those rows is ownerless, even when another row is an
   unknown owner. Exit 2 means the check could not observe (stderr starts with `UNKNOWN`)
   or every printed row is an unknown owner. Read the rows. Status does not repeat that scan.
   Two accept forms, and they do not combine. A mixed call prints the usage of each form and writes
   nothing. Neither form changes review, CI, enqueue or merge.
   Queue custody is only for a predecessor that is still live and waiting in the merge queue. The
   successor runs `accept --pr <N> --repo <owner/name> --custody --from <predecessor-task>`. That
   hands ejection, stale approval, merge confirmation and closeout to the successor
   ([pr-watch](../../full-send/references/pr-watch.md#queue-custody)).
   Recorded custody is for an open PR with no live task, including a worker that crashed after
   approval, or a PR held for a named decision. This seat records it with no dispatch task:
   `accept --pr <N> --repo <owner/name> --reason "<why>" --decision-owner "<who>"`. Do not pass
   `--custody` on that command. The keeper records the same custody itself after grace when the PR
   is open, the head is readable, and the owner is ownerless rather than unknown. The alert clears
   on acceptance or custody, or when the PR closes. `--release` alone ends recorded custody and
   starts grace over. The successor inherits the PR's review budget and the head's rerun allowance
   (infrastructure failures only, never a failing test node).
5. **Live workers.** Active dispatches from the Monitor API, then
   `"$LEARN_UKRAINIAN_PYTHON" -I -m lu_machinery.dispatch status <task-id> --run-nonce <nonce>` for each task
   the handoff or the view names; task files outrank a partial view. Every running worker
   has an armed background `dispatch.py wait`. Any terminal status but `done` is an
   attention item; before calling a dispatch dead check open PRs and its worktree for
   unpushed work; check the worktree disposition after terminal status ([recipes](recipes.md#worktree-disposition-before-re-dispatch)).
   List every task result or lane message awaiting a CTO grant or approval, with its age.
   A dispatch hold names its reason and expiry; re-check it every cycle and lift it the moment the reason no longer holds.
   Never hold work back to save allowance. A window that needs an idle system starts its
   drain only when its gate PR is ready to land; until then every worker slot stays full. Each cycle, rank pools by required burn,
   the percent left divided by hours until the reset or an earlier drain deadline.
   Fill free worker slots from the top operator order's critical path first; when that path is serialized behind one change, put a worker on removing the serialization before starting other work.
   Fill the most urgent pool first with eligible, ready work. Before going quiet, queue long-running
   work under the same conditions on every pool. Name any blocker.
   Report any dispatcher cap that leaves a pool idle to the owning lane as a defect to fix.
   Sent orders are not starts.
6. **Residuals.** Quote open-issue and residual counts from `gh`. A free slot with a ready
   item gets a dispatch; a residual above zero gets a next dispatch this session.
7. **Record tracker changes.** Use `lu-driver-state add --lane cto` for new orders,
   commitments and in-flight work, with the matching kind, owner and done condition.
   Record goals under their program's `operator_order` with `--parent <id>`; record
   blockers as items too. Attach references with `link`, progress with `note`, and proof
   with `evidence`. Use `status <id> done --reason <reason>` only after evidence supports
   the done condition. Cite item IDs in each cycle report so unfinished work stays visible.
8. **Decide.** Rank PRs by leverage, audit drift, look ahead, choose by the binding
   constraint ([decisions](decisions.md#tactical-layer-per-cycle)); log any mistake found
   in the ledger ([learning](learning.md)).
   Before restarting a lane or ordering an item, check its latest state and linked PR or
   issue state. Honor stops; send stale-open items with merged PRs to the tracker owner.
9. **Chase delivery.** Lanes deliver; the CTO does not take over their work. For each
   priority deliverable, the owning lane reports one blocker line every cycle, and every
   lane answers the hourly check-in; format and CTO duties are in
   [lane-report](../../drive-epic/references/lane-report.md). Treat any of these as
   foot-dragging: no report by the deadline, a passed ETA, the same blocker in two
   cycles, unexplained inactivity, or other work displacing the priority deliverable or
   its blockers. An authorized hold (drive-epic §2c), an operator stop, and other ready
   work in worker slots the deliverable cannot use are not foot-dragging.
   Escalate one step per cycle until the result lands:
   1. Demand: a Fleet order marked URGENT with a 10-minute report deadline, plus a
      keeper nudge if the pane is idle.
   2. Focus order: name the missed deadline or ETA, and order the lane to work only the
      deliverable and its blockers until it lands. Other ready work may fill only the
      worker slots the deliverable cannot use.
   3. Unblock: the CTO removes the blocker itself (a decision, a test grant, a missing
      review seat, or rerouting the blocker to another lane). It then reports the facts
      to the operator, including the lane's missed deadlines.
   Record each demand and each report in the deliverable's tracker item. A blocker the
   lane can fix itself is not a wait: order the fix.

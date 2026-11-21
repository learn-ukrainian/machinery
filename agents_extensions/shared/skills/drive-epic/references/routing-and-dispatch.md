# Route, dispatch, settle (drive-epic §3, §3-routing, §3a, §4, §5)

Read before every implement dispatch and while a worker runs. The §4a and §5a inbox drains are
stated once in the core `SKILL.md` loop.

## §3. Route by model × harness fit

Decide the lane from `/api/rules` and `model_catalog.yaml`, **never** from the provider name.
Respect the live caps (in-flight ceilings), the language-lane restriction (Ukrainian authoring,
linguistic and content review route only to the sanctioned language lanes), folk carve-outs
(cross-family only) and the judge-seat rules. At a limit, note the substitution and reroute per
the fallback table; never block on one lane.

Every agent that commits to a branch adds its model family to the branch's author families, and
above the review policy's Low tier the reviewer must be outside all of them (`check_branch_author_families` in
`lu_machinery/dispatcher/checks.py`). The Ukrainian review profile
(`agents_extensions/shared/skills/review-loop/references/ukrainian.md`) seats only Gemini and Sol,
so a takeover successor or helper on a Ukrainian content branch stays with the family already
authoring it, or the branch restarts from main with one author family.

### Live capacity (binding every implement dispatch and review settle)

Before picking a seat, read fresh tool output from all of these:

```bash
curl -sS --max-time 3 "http://127.0.0.1:8765/api/state/routing-budget"
.venv/bin/python -m lu_machinery.fleet.capacity_pick
.venv/bin/python -m lu_machinery.fleet.usage show     # pace (also the capacity_pick pace column)
```

Prefer cooler, higher-headroom seats; do not habit-route to a hot, `near_cap` or in-flight-saturated
lane while a cooler eligible seat exists. A `credit_balance_present` lane is usable after the
plan-backed seats, only with the models in its `credit` row, and only while no `rate_limited`
outcome in the last 60 minutes contradicts it (`credit_use_unconfirmed`). Dispatch admission is a
separate gate: it refuses other models whenever the plan window is at or below the threshold and a
fresh positive balance exists, even when the router no longer recommends the lane. A **deficit**
lane (projected to run out before reset, more than 2 points ahead of pace) is not a target while a
cool lane has reserve. If the budget API and usage are both empty or stale, probe, record
`NOTE: routing_budget_empty` in the ROUTING_CARD, and use the `/api/rules` fallback tables. Never
invent burn percentages from memory.

## §3-routing. Mandatory ROUTING_CARD_V1 + breadth (operator GO 2026-08-06)

Binding text: `agents_extensions/shared/rules/fleet-driver-routing.md` (served at `/api/rules`).

Before **every** implement `dispatch`:

1. Emit a **ROUTING_CARD_V1** (handoff, issue or `batch_state/` receipt) with `tier`
   (authority|practical|heap) · `model_x_harness` · `why_this_tier` · `advisor_packet` (required if
   tier=heap) · `owned_paths` · `acceptance_cmd` · at least two `alternatives_considered` ·
   `parallel_free_seats` · quoted `routing_budget_primary` and `capacity_pick_order` (tool
   evidence).
2. **No card, no dispatch.** Do not launch the worker and write the card later; dispatch v2 does
   not run the retired DoR preflight, so the driver checks the card before launch.
3. Default eligible code worker, advisory path, Luna and Flash admission (including the bounded
   read-only and bounded-implementation exceptions and the advisory envelope bound through
   `--advisory-task`), and Cursor and Gemini overflow: rules core P2, `fleet-driver-routing.md`
   §2 and `model-assignment.md`. Do not spend an advisory seat on lockfile, pointer or smoke jobs.
4. After three or more implement dispatches this session, use at least two agents and two tiers,
   or write `NOTE: fleet_breadth` with tool-backed blockers.
5. Before handoff, run and attach:
   ```bash
   .venv/bin/python -m lu_machinery.fleet.driver_breadth_report --initiator "$SESSION_HANDOFF_AGENT" --since-hours 24
   # optional hard check: add --enforce
   ```

Fixation on one seat while free lanes sit idle is a utilization failure (§2c).

## §3a. Pre-dispatch outcome adequacy (before substantive phase or epic kickoff)

Freeze the exact prompt before presenting or routing it. Record its SHA-256, the user-visible
outcome, the real-world or source denominator, non-goals, role map, independent held-out
evaluation, stop and residual policy, and the completion vocabulary. A non-goal that shrinks the
actual mission needs designated approval or the operator (rules core P6). Discovery, seeds, prototypes, schemas,
transport checks and self-authored canaries prove research or engine readiness, never product
completion. On handback, name the verified user-visible outcome, denominator, held-out proof and
residual gap. For normative language evidence, establish the source's pedagogical or evidential
role before consuming an occurrence.

## §4. Dispatch

Dispatch only when the task card and the dispatch preflight are both green (DoR); chat "ready" is
not DoR (`docs/best-practices/task-quality.md`, `operator-expectations.md` §3b). Every brief names
**one outcome for one PR**; apply the review-size signal in
[workflow.md § Merge policy](../../../rules/workflow.md#merge-policy--ready-prs-must-not-sit-4703-landing-order-7450).

```bash
.venv/bin/python -m lu_machinery.dispatch dispatch --model <catalog-model> --agent <lane> --worktree ...
```

A takeover, or any worker pushing to an existing PR branch, is dispatched with
`--branch <PR head branch>`, `--base origin/main` and the PR's files as `--owned-path`. A `--base`
that settlement cannot verify (including a commit SHA) or that equals `--branch` is refused at
dispatch; read-only dispatches still accept a SHA base. If frozen inputs require a tree before
dispatch, create it with `create_worktree` from `lu_machinery.dispatcher.git` (it writes the
creator receipt; tasks launched with `--cwd` borrow it) and remove it afterwards as the
[helper cleanup runbook](../../../../../docs/runbooks/worktree-cleanup.md#remove-helper-created-trees-12602)
says: `.venv/bin/python -m lu_machinery.orchestration.worktree_claims remove PATH`, preceded for
trees that predate receipts by
`.venv/bin/python -m lu_machinery.orchestration.worktree_claims attribute-helper PATH --actor ACTOR --reason REASON`.

Dispatch v2 has no budget substitution guard: choose the lane from live capacity evidence, record
substitutions, and verify the task record's actual agent and model before attributing work. Before
launch, verify the brief's worktree path and owned paths against the dispatch target, and
reconcile queued instructions through the §4a inbox drain; unknown queue state stays unresolved,
and a packet superseded by a queued instruction is never launched.

Dispatch with a numbered brief (worktree → work → tests → ruff → conventional commit → push → PR;
the worker runs `full-send` to MERGED and never arms auto-merge) and the evidence preamble (each
claim, its deterministic tool and quoted raw evidence). Pass the research flags
(`--research-role`, `--research-task-family`, `--research-track`) and repeatable `--owned-path`
for write dispatches. Workers commit and push their own work; dispatch v2 has no auto-finalize.
Stagger same-lane spawns by about 10 seconds.

**Security-code briefs (#10665).** An author brief for security code (hooks, launchers,
credentials, admission, sandbox, guards) states the trust boundary, the allowed inputs, the
refusal behaviour and the attack classes in scope. The default design is fail-closed: an
allowlist of known-good forms, with everything else refused; a design that is not an
allowlist says why. The author ships the entry-point matrix, one concrete case per class and the
untested boundaries that the review brief freezes
([review-merge-cleanup](review-merge-cleanup.md) §6). Where compatibility with real Bash or a
real CLI is intended, evidence for each attack class uses the real tool as the oracle, with
its version pinned.
Reviewers keep their own independent adversarial cases; the author's matrix does not replace
review. After a rejected review round the author continues only within the
[review policy](../../review-loop/references/policy.md#4-convergence). A contested decision
or a fragile or high-risk fix escalates per the rules core P6.

Write every brief so pytest basetemp, probe copies and report copies go to the managed
scratch directory outside the worktree (`$TMPDIR`). A write-capable round ends with an empty
`git status --porcelain`; the pre-push `untracked_inputs` refusal covers untracked or ignored
Python and config overlays, and tracked dirt is `dirty_tree`.

The brief's test step names only the existing test files that cover the changed files (including
tests of code that imports a changed shared helper), and adds no test file or case without the
CTO's approval. Never collect the whole `tests/` tree or use `-n auto` or `-n` above 2; run those
tests in the foreground. PR CI runs the affected tests and the merge-group CI Gate runs the full
suite on the real candidate (rules core r2-root); those runs are the proof, so trigger no extra
full runs. Use `.venv/bin/python -m lu_machinery.publish workflow-run --workflow ci.yml --ref <branch>`
only when the brief explicitly asks for it.

## §5. Settle loop (never poll by hand)

Timers, keeper nudges and reminders are wakeups, not stop conditions: continue authorized work
until its terminal goal, a real blocker, an explicit operator pause or the seat's handoff signal.
A dispatch returning is not a settle. Arm
`.venv/bin/python -m lu_machinery.dispatch wait <task-id> --run-nonce <nonce>` in the same turn (or watch
the task's `batch_state/tasks/<id>.json` `status` with the Monitor tool); ending a turn with a
live worker and no wait is a driver defect. When it returns, read the result and act. The wait
is a §2c fill window, not an idle period.

Dispatch v2 terminal statuses: **`done` = success**; `failed | timeout | rate_limited | cancelled |
crashed | no_deliverable` are not. `--dry-run` creates no task record. Emit on any status not in
`{spawning, running, ""}`. The task file is truth (`/api/delegate/active` can omit live tasks).
**Before declaring a dispatch dead:** `gh pr list --state open`, then check the worktree for
finished-but-unpushed work. After a terminal status, read `worktree_disposition` and its reason;
resolve a `retained` or `error` tree as the [SKILL.md](../SKILL.md#delivery-and-done) checklist says.

**Wait-loop hygiene.** Before arming a wait on a tool's output, grep the tool's source or one real
output for the exact string; a loop on text the tool never prints waits forever. Never
`pkill -f` a pattern that also appears in your own command line.

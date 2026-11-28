# Delivery: hand-off record, proof and the gate

Shared by `full-send` (terminal `merge`) and `full-stop` (terminal `ready`). The landing order
itself stays in `workflow.md` § Merge policy; this file defines what the delivery skills carry
and check.

## Interpreter

Linked dispatch worktrees have no `.venv` of their own. Resolve the primary checkout's
interpreter once per shell, as drive-epic §6 does
([review-merge-cleanup](../../drive-epic/references/review-merge-cleanup.md)), and run every
recipe in the delivery skills with `"$PY"`:

```bash
PRIMARY_REPO="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"
PY="$PRIMARY_REPO/.venv/bin/python"
```

## Hand-off record

An entry skill (`takeover`, `modernize`) or a plain brief ends by writing one hand-off record
as a YAML block in its task result. It is the Definition of Ready written down (rules core P3);
it is not a new state store. If a field is missing, the delivery skill fills it from the task
record, issue or PR, or sends the work back to the entry stage. It never guesses a field.

```yaml
handoff:
  target: {kind: pr|branch|issue|task|area, id: "<id>"}
  repository: <owner/name>
  issue_or_brief: "<#N or ordered brief id>"
  authority: "<order or issue that authorizes the work>"
  driver: {name: "<accountable driver>", stream: "epic:<N>"}
  authoring: {task_id: "<id>", run_nonce: "<nonce>"}
  reviewers: [{task_id: "<id>", run_nonce: "<nonce>"}]
  terminal: merge|ready
  base: {ref: main, merge_base: "<sha>"}
  head: {ref: "<branch>", observed: "<sha>"}
  worktree: "<dispatch worktree>"
  scope:
    outcome: "<user-visible outcome>"
    open_prs: [] # List open PRs for the same issue (empty after checking for them).
    owned_paths: ["<path>"]
    non_goals: ["<non-goal>"]
    denominator: "<what counts as all of it>"
    acceptance: ["<AC-ID>"]
    verify_commands: ["<command>"]
    held_out_proof: "<independent proof the author did not produce>"
  stop_conditions: ["<condition>"]
  residual_owner: "<owner or none>"
  authors: {models: ["<model>"], credit: ["<contributor>"]}
  review:
    catalog_risk: low|medium|high|critical
    profile: code|infra
    rounds_used: {full: 0, delta: 0}
    ledger: []
    cap_disposition: "<CTO decision reference, if replacing a capped PR>"
  landing_state: {hold: false, queued: false, auto_merge: false}
```

The acceptance ids, typed evidence and behaviour proof live in the lifecycle ledger
(`workflow.md` § Mandatory task workflow, steps 3 to 5); the record names them, it does not
replace them.

`task_closeout init --reuse` refuses a changed effective AC snapshot or policy
with `AC_SNAPSHOT_MISMATCH` and exit 2. For wrongly frozen required evidence
types, use the supported append-only correction before the criterion is checked
or the issue is closed as completed:

```bash
"$PY" -m lu_machinery.orchestration.task_closeout correct-evidence-types \
  --state-file ledger.json --ac-id AC-02 \
  --required-evidence document --required-evidence review \
  --actor codex/task-id --reason 'This criterion verifies documentation'
```

The correction retains old/new snapshots and types with actor, reason and time,
and re-hashes the snapshot. It creates no proof and relaxes no evidence, review
or CI gate. Reuse then requires the corrected policy. See the
[lifecycle contract](../../../contracts/task-lifecycle-closeout.md#correcting-frozen-required-evidence-types)
for typed refusals, replay and retained-history checks.

**Public-safe subset.** Only these fields go into the PR body: target, acceptance ids and their
proof, catalog risk and rounds used, the finding ledger (including any optional DeepSeek
pre-check dispositions), public predecessor PRs and heads, and credit. Worktree paths, host
details and private task metadata stay in the task result (rules core P8).

On the same PR, each hand-off preserves the ledger, the rounds used and the head's CI rerun
allowance (infrastructure failures only, never a failing test node). A new reviewer, session or
accepted successor (`pr_ownership accept`) inherits those limits.

For an authorized replacement after the cap (at most one successor per card;
[policy §4](../../review-loop/references/policy.md#4-convergence)), bind the replacement delta
scope to the verified `handoff.review.cap_disposition` parent decision record and `review.ledger`
frozen before dispatch — never PR-body `Supersedes #N` alone. Carry every unresolved finding in
`review.ledger`, identified by its source PR and finding ID, with its owner and disposition.
Keep findings assigned elsewhere open in the parent record. Missing, edited, or mismatched
predecessor evidence fails closed. The replacement reviewer runs a delta review of inherited
open findings on the exact new head plus fix-diff regressions, P0/P1, and security findings
(settled round-1 mediums stay closed). Use source PR and finding ID together in the review brief
and reply (for example, `PR1055-F1`) to distinguish inherited findings from new ones. Record
review and CI evidence for the replacement's own head.

If the predecessor has a lifecycle ledger bound to its PR, keep it intact. `bind-successor`
keeps that PR binding. The CTO uses the existing
[remaining-scope transfer](../../../contracts/task-lifecycle-closeout.md#remaining-scope)
to a linked issue with a fresh ledger and names it in the replacement brief. If that transfer
is unavailable, record the lifecycle blocker in the cap disposition and wait for the CTO.

## Proof

Proof is evidence observed in this session: command output, a reproduction, a run id.

- Reproduce the original failure, apply the fix, then watch the same check pass.
- Run the checks CI runs for the touched area (Ruff, the existing test files that cover the
  changed files). Add no test unless the CTO approved that specific test.
- A user-visible API or UI change needs source-blind behaviour proof on the running artifact
  (`local-code-review` checklist Step 7, drive-epic §7-rollout). Green mocked tests are not
  behaviour proof.
- If the change cannot be proven, it is not ready. Say what is unverified.

## The gate

Every item must hold on the **current** head. A moved head voids the verdict and the CI.

1. Proof covers every change in the diff.
2. A recorded exact-head APPROVE from the reviewer of record (`review-loop`), with no open
   blocker applicable to that head ([policy §3](../../review-loop/references/policy.md#3-findings),
   [§4](../../review-loop/references/policy.md#4-convergence)). For an inherited blocker assigned
   elsewhere, approval requires exact-head evidence and the reviewer's confirmation under
   [policy §4](../../review-loop/references/policy.md#4-convergence). A missing or unverified
   disposition still blocks. The optional DeepSeek pre-check (DS-PRECHECK-1101) is never a review
   of record and never satisfies or waives this gate.
3. Same-head PR CI green, the same with or without a merge queue: no blocking check on the live
   head pending, red or cancelled, including the PR `CI Gate` (affected tests and Frontend, not
   the full suite) where the repository runs one. It may have finished before or after the
   review round. `pr-merge`'s merge guard enforces this before it enqueues or merges. Later CI
   evidence that materially undermines the recorded verdict needs reviewer disposition before
   landing. Where the base branch has a merge queue (`isMergeQueueEnabled`, `full-send` step 7),
   PR CI never replaces the merge-group CI Gate on the real queue candidate (`workflow.md` §
   Merge policy); without one, these same-head checks are the last CI gate.
4. Carried findings and follow-ups: on an APPROVE carrying eligible P3s or final-round
   non-security P2s ([policy §4](../../review-loop/references/policy.md#4-convergence),
   [Recording the verdict](../../review-loop/references/policy.md#recording-the-verdict)), keep
   the landing hold (`hold` label / `landing_state.hold`) in place until `claude-cto` files each
   carried ID as an owned `known-issue` ticket and every ticket link is verified in the PR ledger
   on the approved head. Once verified, a non-blocking carried follow-up does not block merge.
5. The PR is open, not a draft, mergeable and not BEHIND or DIRTY against its base.

After the gate, `full-send` lands through `pr-merge` (`full-send` step 8), which enqueues where
the base branch has a merge queue and merges directly where it has none. `--expect-queue` pins
the kind the gate was checked for: `pr-merge` refuses before any write when the base differs.
`full-stop` reports READY_TO_MERGE at the gate; in a merge-queue repository the future
merge-group CI is not yet proven at that point.

## Terminal states

- `full-send`: `MERGED` with a clean closeout receipt, `BLOCKED <reason>`, or
  `QUEUED_UNDER_CUSTODY` after `pr_ownership queue-exit` released the owner. In that case the
  custodian owns `MERGED` and the closeout.
- `full-stop`: exactly one of `READY_TO_MERGE`, `WAITING_ON <named approval>`,
  `BLOCKED <reason>`, `ALREADY_MERGED_EXTERNALLY`, naming the receiving driver.
- Neither skill ends on "Next:". Ready is never reported as delivered.

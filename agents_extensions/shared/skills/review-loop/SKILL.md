---
name: review-loop
description: Run the bounded independent review of one change, from its pushed head to a recorded exact-head verdict, inside full-send or full-stop. Use when the worker that owns a brief, or a driver, needs the review of record for a code, infra or Ukrainian change.
argument-hint: "<branch> [pr <number>]"
---

# Review loop

Take one pushed change to an exact-head verdict at the tier the review policy sets, within its
rounds, and record that verdict on the PR. This skill owns rounds, the budget and the finding ledger.
The reviewer's own procedure is `local-code-review`; learner-content review procedure stays
with `track-completion` and `post-build-review`, seated by the `ukrainian` profile below.
ACP discussion is never review.

**Who runs it.** The worker that owns the brief runs it itself (rules core P1), as do the
accountable driver, the integration owner and the CTO. The resolver picks the reviewer; the
runner never picks one, and the resolver never returns a family that authored a commit on the
branch. No eligible reviewer is handled in step 2; nobody substitutes a reviewer of their own
choosing.

**When it starts.** Right after the PR is opened or updated on the pushed head: review runs in
parallel with PR CI on that immutable head and never waits for it. Pending CI is not
successful CI, so the verdict never rests on a result that has not arrived. Landing still needs
this recorded exact-head verdict and green PR CI on the same head (the `CI Gate` where the
repository runs one), with or without a merge queue
([delivery](../full-send/references/delivery.md#the-gate)).

**Inputs.** The hand-off record ([delivery](../full-send/references/delivery.md#hand-off-record)):
branch, exact pushed head, base and merge-base, author model of every commit, catalog risk,
the finding ledger and rounds already used.

**Tiers, findings, rounds and recording:** [policy](references/policy.md) is the single source. Read it
before round 1 and before every further round.

**Read the tier first**, at the pushed head, with the classifier the merge gates use:

```bash
"$PY" -m lu_machinery.review.tiers --repository "$REPOSITORY" --pr "$PR"
```

`trivial` needs no review: stop here, seat no reviewer, and land on required CI. `low` gets the
policy's quick red-team pass, with `--risk low` and break-it findings only. Other tiers continue
below.

Recipes run with the primary checkout's interpreter, resolved once
([delivery](../full-send/references/delivery.md#interpreter)):

```bash
PRIMARY_REPO="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"
PY="$PRIMARY_REPO/.venv/bin/python"
```

Invoke the recorder by its absolute primary-checkout path below. This applies current
recorder rules even when the caller's branch predates a recorder fix. Resolving only the
interpreter does not select the primary checkout's Python modules.

## Steps

0. **Hand the reviewer an updated head, before round 1 and before every delta round.** Answer the
   PR's outstanding review comments with evidenced dispositions, fix every accepted
   `in_scope_blocker`, and run the relevant checks. Keep tracked P3 follow-ups as they are, and
   escalate a dispute or a scope breaker. A budgeted round is never spent on a defect the author
   already knows. If the change is being discarded for a new approach, skip repairs to the code
   being replaced; the code that stays and its replacement still get checks and exact-head review.
1. **Freeze the round-1 brief** ([brief](references/brief.md)): outcome, acceptance criteria,
   non-goals, the input contract (what executes, on which data, and who controls the input),
   dispositions. If an optional DeepSeek author pre-check
   (DS-PRECHECK-1101) was run, list its findings and their dispositions as author context only.
   Later rounds reuse the frozen sections unchanged: scope is frozen at round 1, and new code
   goes in a new PR ([policy](references/policy.md#4-convergence)). **A read-only Codex reviewer cannot read CI logs**, so every
   brief for one inlines the CI evidence of the exact head that exists when the brief is frozen:
   the head SHA, the run and job ids, their conclusions or `pending`, and the test output or
   log excerpts the review needs, never conclusions alone where the logs matter. A reviewer
   still blocked by it replies BLOCKED with `missing_capabilities: ["ci-logs"]`. A brief for a
   head whose CI is still running says so and tells the reviewer to withhold APPROVE when evidence
   needed to resolve a material finding is not yet available ([brief](references/brief.md)).
2. **Resolve the target and the reviewer live.** Never pick a fixed model. Derive the review
   profile from the target and record it in the hand-off record (`review.profile`) and the
   frozen brief: `infra` when the change touches the build pipeline, gates, CI, deploy, the
   agent runtime and harness, dispatch, review or routing tooling, or the rules and config that
   govern them; `code` for any other code change. A change that spans both is `infra`.
   A change to learner-facing Ukrainian is `ukrainian`: seat it from
   [ukrainian](references/ukrainian.md), not the resolver.
   `infra` walks its own profile suitability order in `lu_machinery/config/model_catalog.yaml`
   (including native AGY Gemini 4 Neon High, which holds `review_profiles: [code, infra]`), so the same value goes to
   the resolver (`--review-profile` and `--domain`) and to the dispatch below.

   ```bash
   REVIEW_PROFILE=infra   # or code, as derived above
   "$PY" -m lu_machinery.review.closeout_cli --state-file "$STATE" \
     target --mode branch --branch "$BRANCH" --base "origin/$BASE"
   "$PY" -m lu_machinery.review.closeout_cli --state-file "$STATE" \
     resolve-reviewer --author-model "$AUTHOR_MODEL" --review-profile "$REVIEW_PROFILE" \
     --domain "$REVIEW_PROFILE" --risk "$RISK"
   ```

   Add `--owned-path`, `--subject-seat` or `--subject-family` when the change governs a seat's
   adapter or reviewer hooks (`local-code-review` checklist). `$RISK` follows the
   [policy](references/policy.md#2-risk-tier-is-set-by-the-change); the resolver raises it to
   `critical` on security paths, a floor nobody lowers. Use the returned reviewer, model
   and `requires_silence_timeout`. Exit 1 is fail-closed: no eligible reviewer means escalate
   to the CTO, or use the red-team exception (`workflow.md` § Merge policy) exactly as written
   there. Never review your own work.
3. **Check the round budget, then dispatch the reviewer read-only at the exact pushed head**, passing the same
   `$REVIEW_PROFILE`. The dispatcher removes the review worktree when the task settles.
   Before every round, including round 1, run the recorder's `budget-check` command in step 6.
   Read its JSON and exit status: admission permits dispatch, and a refusal stops the round.

   For bound Contract 1 plan or Contract 2 lesson attempts using `--review-id`, `--attempt-id`
   and `--review-attempt`, follow [Running a bound attempt](../../../../docs/runbooks/formal-review-attempt-isolation.md#running-a-bound-attempt)
   exactly. Do not add `--require-review-verdict` to these bound attempts.

   ```bash
   # FLEET_REPO_KEY is the target's key in fleet_repos.yaml.
   "$PY" -m lu_machinery.dispatch dispatch \
     --agent "$REVIEW_AGENT" --model "$REVIEW_MODEL" --effort high \
     --mode read-only --worktree --repo "$FLEET_REPO_KEY" --task-id "$REVIEW_TASK" \
     --prompt-file "$REVIEW_BRIEF" --branch "$BRANCH" --pinned-head "$HEAD_SHA" --pr "$PR" \
     --review-profile "$REVIEW_PROFILE" --review-author-model "$AUTHOR_MODEL" --risk "$RISK" \
     --review-mode cross_family --require-review-verdict
   ```

   The dispatcher applies the PR's budget to charged and reserved review heads
   ([policy](references/policy.md#4-convergence)).
   For a code or infra review, `--require-review-verdict` binds
   `lu_machinery/schemas/code-review-findings.v1.schema.json` on a provider that accepts a schema. The
   brief asks for that object ([brief](references/brief.md#reply-format)). A provider that
   cannot take a schema gets one format correction in the same task before it settles; the
   correction cannot change the verdict, the findings, or their meaning. A Ukrainian review
   does not bind that schema. Stdout is two lines: the task id, then the run nonce. Record both.

   **Supplemental review (measurement only).** Run none by default: the review of record is
   the only review. This step is the only definition of the trigger; every other rule points
   here. Run one only when the brief carries the complete line
   `MEASURE: <model id> until <n> samples` (an operator-ordered model measurement), with a
   model id and a sample count. A line missing either, or no line, means no supplemental
   review. The dispatcher who wrote the line counts samples and removes it at `<n>`. With a
   complete line, dispatch exactly the model id it names, read-only, on the first code or
   infra review head, with that model's harness and flags from
   [model-assignment](../../rules/model-assignment.md). Never let a selector (for Neon, the
   High/Medium parity rule in its advisory routing) replace the named id.
   Use `--research-role advisory` and `--pinned-head "$HEAD_SHA"`. Put the PR number in the
   prompt. Omit `--pr` and formal review flags so the dispatch stays supplemental.
   Record an unavailable seat as such.

   Check the measured model's findings against the pinned head and compare them with the review of
   record. Apply the [council score rubric](../drive-epic/references/decision-report.md#council-optional-independent-panel)
   to the confirmed findings. Record the model, effort, verdict, score and finding dispositions
   in the PR body. Model stats reads structured results from settled tasks and joins them to
   the PR by exact head. The supplemental score and verdict carry no landing authority.
   Fix any independently confirmed blocker through the review loop.

4. **Wait on that task and nonce.** Never re-dispatch.

   ```bash
   "$PY" -m lu_machinery.dispatch wait "$REVIEW_TASK" --run-nonce "$REVIEW_NONCE" --timeout 1800
   ```

   Exit 0: `done`. Exit 124: client expiry while the task still runs; run the same wait again.
   Exit 1: a terminal failure, never approval. Exit 2: a refused request, such as
   `run nonce mismatch`; stop and report it.
5. **Settle.** Read the task JSON: `done`, attested reviewer model and family, and a pinned head
   equal to the current pushed tip. Read the findings themselves, not only the verdict line.
   Give each finding a stable id and a disposition in the ledger ([policy](references/policy.md)).
   Fixes go to the authoring lane (the worker itself when it runs the loop); a driver does not
   patch them in. Once a `CHANGES_REQUESTED` verdict is recorded (step 7), the author labels each
   of its P0-P2 findings before the next round: `fixed` (a real error, changed), `wrong` (the
   code was already correct), `invented` (it cites a file, line, symbol or behaviour that does
   not exist), `out_of_scope` or `duplicate`. Every label except `fixed` needs one line of
   evidence, which also goes in the delta brief. Model stats counts the labels per reviewer.

   ```bash
   "$PY" -m lu_machinery.review.record_cf_verdict finding-outcome --task-id "$REVIEW_TASK" --pr "$PR" \
     --finding F2 --label wrong --evidence '<one line that shows it>'
   ```
6. **Next round or stop.** The budget is three charged rounds per lineage, and nobody adds
   one ([policy](references/policy.md#4-convergence)). A dispatch reserves its head. A recognized
   verdict or a completed review with a nonempty reply charges that head before publication.
   A settled attempt releases its reservation when the record shows it yielded neither.
   Start a delta round only if an open blocker, a
   `not_applicable` claim the reviewer must confirm, a changed behaviour or a moved head needs
   it, and only after the recorder admits it. Ask the recorder itself before dispatching; it
   applies the clean-merge exemption and the lineage's one repair-verification round:

   ```bash
   "$PY" -m lu_machinery.review.record_cf_verdict budget-check --repository "$REPOSITORY" --pr "$PR"
   ```

   Exit 0 admits the round; carry any `repair_round` receipt (its approved and candidate heads,
   class, failed runs or conflicted paths, and `delta_sha256`) into the delta brief, and ask the
   reviewer to APPROVE only with the line `REPAIR_VERIFIED: <delta_sha256>` once it has verified
   that the complete delta is only that repair. Exit 1 is the stop below. A recorded rejection is cleared only at a new
   head ([policy](references/policy.md#recording-the-verdict)). The delta brief carries
   the ledger and the fix diff ([brief](references/brief.md#delta-rounds-round-2-and-later)).
   A BLOCKED with no findings that only a missing capability caused charges no round, once per
   lineage, and admits one same-seat replacement whose brief carries the missing evidence
   inline: the CTO records a capability failure for the unpublished run
   ([policy](references/policy.md#recording-the-verdict)). A second such reply is charged, and a published
   one stays charged. Any other failed execution (timeout, error, malformed reply) is rerun
   once at the same head; a second failure escalates.
7. **Record the verdict on the open PR** (it is open at the reviewed head, `full-send` step 4; its CI may still be running).
   Do not record a `BLOCKED` reply whose only reason is a check still pending on the head: wait
   for that check to finish, then rerun once at the same head with its result inlined
   ([policy](references/policy.md#recording-the-verdict)).

   ```bash
   "$PY" -m lu_machinery.review.record_cf_verdict --task-id "$REVIEW_TASK" --pr "$PR"
   ```

   Exit 0 means recorded with a JSON receipt; exit 1 means refused, so nothing is recorded.
   If an accepted P0 or P1 finding lands on a PR that already has a recorded APPROVE, add the
   `hold` label at once: `"$PY" -m lu_machinery.publish pr-edit --number "$PR" --add-labels hold`.
   On an APPROVE carrying eligible P3s or final-round non-security P2s
   ([policy](references/policy.md#recording-the-verdict)), keep the `hold` label in place until
   every carried ID is filed as an owned `known-issue` ticket and every ticket link verifies in
   the PR ledger on the approved head.

## Always true, whatever the round

- `SECURITY_VERDICT FAIL` blocks. INCONCLUSIVE, `BLOCKED`, timeout, error or no reply is never
  approval. A moved head voids the verdict.
- The `local-code-review` Step 6 breakers apply in every round.
- A decline needs file:line or test evidence and never clears a blocking verdict.

## Optional DeepSeek author pre-check (DS-PRECHECK-1101)

The optional DeepSeek author pre-check (`full-send`) is never a review of record and never
satisfies or waives the cross-family gate. It opens no review round and does not alter
reviewer choice, risk floors, or round budgets. If the author ran the pre-check, its usable
findings and their dispositions must appear in the PR body (fixed, or rejected with a reason)
and are listed in the review-of-record brief as author context only.

## Review profiles

- `code` and `infra`: this skill as written.
- `ukrainian`: the independent cross-family seats named in
  [ukrainian](references/ukrainian.md), with tool-verified findings, seated from that page instead of the resolver; the rest of this skill applies.

## Stop and report

Stop when the current head has a recorded APPROVE meeting the carry, stop, and convergence rules
in [policy §4](references/policy.md#4-convergence) and
[Recording the verdict](references/policy.md#recording-the-verdict), when `budget-check` refuses
the next round, when a dispute is unresolved after two replies, or when a breaker trips. A head
you only need to merge with the base, a CI fix after approval and a conflict resolution after
approval are not stops: `budget-check` admits them while the exemption or the lineage's repair
round applies. A refusal and a tripped breaker escalate to the CTO with the refusal text; a
dispute goes to the operator ([policy](references/policy.md)). None of them is approval.

Report: exact SHA reviewed, reviewer model, family and harness, verdict, rounds used against
the budget, the ledger with each disposition, diff size before and after review, who owns
anything still open, and the review and repair tokens, tier and task family that feed the
weekly [measures](references/policy.md#6-measures-weekly-cto).

Learned from tstack's `sol-loop` skill (MIT, Theo Browne); the text is our own.

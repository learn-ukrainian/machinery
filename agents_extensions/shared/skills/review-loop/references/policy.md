# Review policy v2.1 (single source)

Designated decision C-4836 (C-4829, C-4831). Rules and skills that mention
review tiers, findings or rounds point here; do not restate them.

## 1. Decide whether to build it

Before a brief: would the operator miss this if it never landed? If not, do not dispatch. A
simpler manual procedure beats new tooling.

## 2. Risk tier is set by the change

| Tier | Changes | Review before merge |
| --- | --- | --- |
| Low | Deletions of unused code, docs, data, test-only, comments | Required CI green + one exact-head APPROVE from a qualified non-author reviewer (any family); no cross-family seat required |
| Medium | Ordinary code changes | Required CI + one cross-family review |
| High | CI workflows, launchers, publishing/merge gates, secrets/credential handling, security tooling, installers | Required CI + cross-family review at critical depth; a second reviewer only here |
| Ukrainian content | Learner-facing Ukrainian text | Gemini via AGY with Sources, seated by the [Ukrainian profile](ukrainian.md); deletions of learner-facing content keep this tier |

The tier comes from the paths changed, never from the author. The path-to-tier table lives in
one place, `lu_machinery/config/review_tiers.yaml`. A PR spanning tiers takes the highest tier it
touches. Deleting a High-tier path or learner-facing content keeps that path's tier; only
deletions of other unused code are Low.

## 3. Findings

- Only real defects block: wrong behaviour, data loss, security, broken contract. Style,
  naming, cleanup, diagnostics and "consider" items never block and are not posted as
  findings. A factual language error in learner-facing Ukrainian is a real defect.
- Tag each finding New or Pre-existing. A pre-existing defect blocks only when the PR exposes
  or worsens it; otherwise it becomes an issue. An unresolved security finding always blocks.
- One finding per bug class, listing every instance; never repeat a posted finding.
- Every blocking finding is fixed, or dismissed with evidence the reviewer confirms.

## 4. Convergence

At most 3 rounds. Round 1 is the full review; rounds 2-3 review only the diff since the last
reviewed head. After round 3 the author steps back and reshapes or closes the PR; no grants.

## 5. Recovery over gates

After a mistake: revert or fix forward and fix the cause at the source (code, test, lint rule or
one line in a skill). Do not add a new refusal check, wrapper or ritual for a single slip. The
mistake ledger's "escalate the control" ladder is retired.

## 6. Measures (weekly, CTO)

Rounds to pass (mean, share at 1 round), cost per landed PR by pool, revert rate. If reverts
rise, tighten the tier for that path; otherwise loosen with evidence.

## Recording the verdict

The recorder implements the rounds above. Resolve `PRIMARY_REPO` and `PY` as in the
[review-loop setup](../SKILL.md#review-loop) and use the primary recorder.

- **The verdict of record** is the reviewer's reply at the exact pushed head, recorded on the
  PR with `record_cf_verdict.py --task-id <task> --pr <N>`. The live resolver picks the
  reviewer; a moved head voids the verdict. For a branch authored by both Anthropic and OpenAI
  (#10712) the resolver returns one seat per family, both must APPROVE the same head, and the
  pair counts as one round.
- **Ask before each round:** `record_cf_verdict.py budget-check --repository <owner/name>
  --pr <N>` admits or refuses it and writes nothing. The dispatcher and the recorder refuse a
  round past the budget. A head that only merges the base cleanly costs no round, and one CI
  fix or conflict resolution after approval gets one repair round, whose APPROVE carries
  `REPAIR_VERIFIED: <delta_sha256>`. A repository with its own budget declares `review_rounds`
  in `lu_machinery/config/review_budget_policy.yaml`.
- **Findings** use the `code-review-findings.v1` object. Each keeps a stable id (`F1`, `F2`, …)
  across rounds; the PR body ledger lists id, New or Pre-existing, blocking or not, and state.
  P0-P2 are blocking defects; a reply whose open findings are all P3 is recorded as an approval.
- **A recorded rejection** (`CHANGES_REQUESTED` or `BLOCKED`) is cleared only by an APPROVE at
  a new head.
- **A failed execution** (timeout, error, malformed reply, `BLOCKED`) is never approval. Rerun
  it once at the same head; a second failure escalates to the CTO. When a `BLOCKED` reply has
  zero findings and only a missing capability caused it, the CTO may record a capability
  failure instead of a verdict, which charges no round once per lineage:

  ```bash
  "$PY" "$PRIMARY_REPO/lu_machinery/review/record_cf_verdict.py" capability-failure --task-id "$REVIEW_TASK" --pr "$PR" \
    --capability <sources-mcp|test-execution|shell|browser|checkout-data|ci-logs> --authority claude-cto
  ```

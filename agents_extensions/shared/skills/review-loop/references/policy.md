# Review policy v2.2 (single source)

Approved plan REVIEW-GO-1128 (superseding C-4836 / v2.1). Rules and skills that mention
review tiers, findings or rounds point here; do not restate them.

## 1. Decide whether to build it

Before a brief: would the operator miss this if it never landed? If not, do not dispatch. A
simpler manual procedure beats new tooling.

## 2. Risk tier is set by the change

| Tier | Changes | Review before merge |
| --- | --- | --- |
| Trivial | Root documentation for people (README, CHANGELOG, LICENSE, SECURITY); the product's lock bump adopting a new lu-machinery release | Required CI green only; no review of record, no review-loop |
| Low | Deletions of unused code, test-only, data, `docs/**` | Required CI green + one quick red-team pass: one exact-head APPROVE from a qualified non-author reviewer (any family), `--risk low` |
| Medium | Ordinary code changes | Required CI + one cross-family review |
| High | CI workflows, launchers, publishing/merge gates, secrets/credential handling, security tooling, installers, dependency files | Required CI + one exact-head cross-family review at `--risk high` (Sol, Opus, Neon or Grok; the resolver picks); a security path is raised to `critical` (Sol, Opus or Neon) |
| Ukrainian content | Learner-facing Ukrainian text | Gemini via AGY with Sources, seated by the [Ukrainian profile](ukrainian.md); deletions of learner-facing content keep this tier |

The tier comes from the files changed, never from the author. The path-to-tier table lives in
one place, `lu_machinery/config/review_tiers.yaml`, and `lu_machinery/review/tiers.py` applies
it for the publisher's merge gate, the merge-queue keeper, the integration sweep, the queue's CI
review gate and the recorder's Low rule (`python -m lu_machinery.review.tiers` prints it). A PR spanning tiers takes the highest tier it touches, so one non-Trivial file makes the
whole PR non-Trivial. Deleting a High-tier path, learner-facing content or a Trivial doc keeps
that path's tier; only deletions of other unused code are Low.

**Trivial, exactly.** Every file is one of:

- a root `README`, `CHANGELOG.md`, `LICENSE` or `SECURITY.md` (the table's `trivial` list).
  Everything under `docs/` and every other text keeps its tier: code and agents read it as input
  or procedure (the review protocol, session handoffs, epics, the release procedure, the
  workstream registry, rules, skills and prompts), so no list of model-facing docs is complete;
- `requirements-machinery.lock` whose patch is one hunk from line 1 that replaces only the
  `lu-machinery==` line and its hashes, with a raised version. Any other line, a lowered version
  or a missing patch keeps the path's tier. CI installing the hash-pinned wheel proves the
  release exists. Machinery's post-release version bump (`pyproject.toml`, `package*.json`) is
  not Trivial: a patch cannot prove TOML or JSON scope.

Code comments are not Trivial; they keep their file's tier. A line that reads as a comment can
sit inside a string or a prompt, and only a full-file tokenizer could tell them apart. A recorded
rejection still blocks a Trivial head. A security-sensitive path, or anything the product deploy
policy calls learner content, is never Trivial.

**Low is one quick red-team pass.** The reviewer reports only break-it findings that meet
[§3](#3-findings), never style or "consider". When no blocking finding or blocking
P0/P1/security uncertainty remains, the reviewer gives APPROVE. Landing requires a recorded
APPROVE and the other merge gates. The Low budget remains 2 rounds.

The reviewer's risk is the `--risk` passed to the resolver: `high` or `critical` for a High-tier
change. Seat order and eligibility follow the review rotation in
[model-assignment](../../../rules/model-assignment.md) (REVIEW-ROTATION-GROK-1127): pinned Cursor
Grok 4.7 High joins Sol, Opus and Gemini 4 Neon High at `high`, `medium` and `low` only, and
`critical` seats Sol, Opus or Gemini 4 Neon High: critical and security sign-off stay unchanged
pending planted-defect data (operator decides). For `code` and `infra` reviews the
security-path classifier (`lu_machinery/review/security_paths.py`) is a floor: a change touching
one of its paths, either name of a rename or a deletion included, is reviewed at `critical`
whatever risk was passed. Nothing lowers that floor.

One exact-head cross-family review per change (owner decision C-5345, 2026-11-12).
High risk takes Sol, Opus, Gemini 4 Neon High or Grok 4.7 High; critical risk takes Sol, Opus or Gemini 4 Neon High
(REVIEW-ROTATION-GROK-1127). There is no second review.

## 3. Findings

- Only real defects block: wrong behaviour, data loss, security, broken contract. Style,
  naming, cleanup, diagnostics and "consider" items never block and are not posted as
  findings. A factual language error in learner-facing Ukrainian is a real defect.
- Tag each finding New or Pre-existing. A pre-existing defect outside the frozen obligation
  blocks only when the PR exposes or worsens it; otherwise it becomes an issue. An unresolved security finding always blocks.
- Report one finding per root cause and violated invariant; never repeat a posted finding.
- Code and infra reviews: Before returning a round that reports a new finding or leaves a
  finding open, name the violated invariant and the affected input or state family. In that
  round, sweep the review diff and the relevant code it reaches, including unchanged callees,
  applicable callers, fallbacks and affected tests. Record the search boundary and every
  evidenced member found, with its trigger, location and impact. Use one stable finding ID per
  root cause and violated invariant. Keep surviving siblings under the same ID and give distinct
  root causes their own IDs. Put the class and its members in the existing finding body. In
  delta rounds, verify the reported members and sweep analogous paths affected by the fix.
- A surviving sibling of an open blocker stays on its original ID. Real out-of-scope defects
  route to owned follow-up tickets. Blocking, reachability, security exceptions, and
  approval-with-carry follow the Threat model below and [§4](#4-convergence).
- Every blocking finding is fixed, or dismissed with evidence the reviewer confirms, subject to
  eligible final-round P2 carry in [§4](#4-convergence).

**Threat model.** Every code or infra brief identifies supported entry points and environments, input sources, who can influence each source or relevant write path directly or indirectly, and the controls that enforce any exclusion. The reviewer verifies those controls at the exact head. Low and Medium cover supported use, foreseeable operator error, and malformed or partial state, concurrency and recovery where applicable. High and security-sensitive reviews also assume hostile values, ordering and volume wherever an actor can influence them. A P2 blocks only when the finding names the actor or automation role, supported entry point, and an evidenced normal-use trigger; ordinary retries, interrupted writes, concurrency, and recovery are normal use and still block when evidenced, while hypothetical or unproven P2 triggers become owned P3 follow-ups. Untrusted input is data, ordering, or state a less-trusted actor can influence, directly or indirectly, before a consumer relies on it across a trust boundary; security findings on untrusted inputs block at any priority (P0–P3), citing source, consumer, control, and impact. Core r2-lease grants no blanket exemption for internal inputs or unknown authority. Public product reviews include reachable user and contributor inputs. Machinery and private infra reviews include the actual PR, CI, artifact, operator and automation boundaries, including lower-trust inputs consumed by privileged steps. A one-shot run may use the bytes at its pinned SHA as the input ceiling when that is what it executes and no lower-trust actor can substitute those bytes before use. Reusable tools cover future reachable inputs. A size or shape exclusion requires evidence. Material uncertainty about a plausible P0, P1, or security path blocks pending evidence naming the actor, entry point, control in question and impact; unproven P2 uncertainty does not block. The critical review floor and independent review of material threat-model changes still apply. Unresolved security findings always block. Code and infra reviews: A reviewer may carry a confirmed defect as an owned P3 follow-up only after verifying on the exact head that it is outside the frozen obligation and that the PR neither introduces, exposes nor worsens it. For an input exclusion, cite the applicable contract and the control that rules out the actor-to-input path, with evidence from the reviewed head. A scope label, an owned-path limit or a brief assertion cannot establish exclusion. Demonstrated material regressions; reachable P0–P1 failures within the supported contract and P2 failures that meet the evidenced normal-use test above, subject to §4; unresolved security findings; and material uncertainty about a plausible P0, P1, or security path still block. Material threat-model changes require independent review. APPROVE requires every acceptance criterion to be met, with no unresolved blocking P0, P1, or security uncertainty and only eligible follow-ups or final-round carried P2s (§4) remaining.

## 4. Convergence

At most 3 charged rounds; Low uses at most 2 (the red-team pass and one fix confirmation), and
Trivial none. A dispatch reserves a round at its head. A recognized verdict charges that head
before publication. A completed review with a nonempty reply also charges its head, including
when its verdict cannot be parsed. A settled attempt releases its reservation when the record
shows it yielded no verdict or completed review. Active attempts and unverifiable results keep
their reservation. The clean base merge, the repair round and the capability failure under
[Recording the verdict](#recording-the-verdict) do not count against the 3.
Scope closes after round 1. Later rounds check only open finding IDs, `not_applicable` evidence,
and fix-diff regressions against the frozen contract; newly noticed pre-existing P2s that the PR
and fix neither introduce, expose, nor worsen become owned follow-ups, while fix-exposed or
worsened evidenced P2 regressions (subject to the final-round carry rule below) and P0, P1, and
security findings still block. Budgets follow admission (default 3 charged rounds, 2 at Low,
repo overrides in `review_budget_policy.yaml`). On the final charged round, if every acceptance
criterion is verified and no P0, P1, security finding (current or inherited), or blocking
P0/P1/security uncertainty ([§3](#3-findings)) remains open, the sealed reviewer issues APPROVE
and names each carried P2 ID; on `security_paths.py` paths, carrying any P2 also requires an
explicit exact-head `SECURITY_VERDICT: PASS` line from the qualified critical reviewer. Escalate
to `claude-cto` if any P0, P1, security finding, blocking uncertainty, or unmet acceptance
criterion remains at the cap.
When the cap prevents the next required round, stop and send the findings ledger and exact PR
head to `claude-cto`. The CTO records one current disposition per work item on its parent
issue or ordered brief. Record the capped PR and full head SHA, rounds used, every unresolved
P0-P2 finding with its owner, and the smallest viable continuation. If none is authorized,
record what the work is waiting for or why it ends. Update that record if a later continuation
also caps. Keep the earlier decisions and ledgers linked.

Continuing requires explicit CTO authorization, a fresh branch and a replacement PR. The
replacement brief carries the ledger, predecessor rounds used and cap decision forward. The
authorized replacement starts at round 1 of its own configured PR budget. Record earlier
rounds as history. The capped PR remains capped.

An authorized replacement PR (at most one successor per card, bound by the authoritative CTO
cap-disposition decision record on the parent issue or ordered brief verified before dispatch and
frozen into the review brief—never PR-body `Supersedes #N` alone) gets a delta review of inherited
open findings on the exact new head plus fix-diff regressions, P0/P1, and security findings;
settled round-1 mediums stay closed. Missing, edited, or mismatched predecessor evidence fails
closed. Keep blockers assigned to other named work open
with their owners in the parent record. They still block the replacement until the reviewer
confirms on its exact head that the defect is fixed or that the replacement neither carries
the defective change nor exposes or worsens the defect. The rule for unresolved security
findings in [Findings](#3-findings) still applies.
The replacement needs its own CI under the [delivery gate](../../full-send/references/delivery.md#the-gate).

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
  Blocking thresholds follow [§3](#3-findings). The recorder preserves the sealed reviewer's
  verdict verbatim and never promotes rejection or uncertainty to approval; any open P0, P1, or
  security finding (any priority, current or inherited) blocks publication of an approval
  (rejections are still recorded under the existing disclosure controls).
- **Carried findings.** On a final-round APPROVE carrying eligible P3s or non-security P2s
  ([§4](#4-convergence)) — or any round's APPROVE carrying eligible P3s — the existing landing
  hold stays in place until `claude-cto` files each carried ID as an owned `known-issue` ticket
  and every ticket link is verified in the PR ledger on the approved head. The release notes list
  the known issues fixed by the release and those still open.
- **A recorded rejection** (`CHANGES_REQUESTED` or `BLOCKED`) is cleared only by an APPROVE at
  a new head.
- **Pending CI is not a verdict.** Review runs in parallel with PR CI. A `BLOCKED` reply with no
  findings whose only reason is a named check still pending on the reviewed head is never
  recorded: a recorded `BLOCKED` could not be cleared at that head. Wait for those checks to
  finish on the head (`gh pr checks`), then use the one same-head rerun below, with the finished
  CI evidence inlined in the brief. The `BLOCKED` verdict still counts against the round budget.
  A second such reply escalates to the CTO.
- **A failed execution** (timeout, error, malformed reply, `BLOCKED`) is never approval. Rerun
  it once at the same head; a second failure escalates to the CTO. When a `BLOCKED` reply has
  zero findings and only a missing capability caused it, the CTO may record a capability
  failure instead of a verdict, which charges no round once per lineage:

  ```bash
  "$PY" -m lu_machinery.review.record_cf_verdict capability-failure --task-id "$REVIEW_TASK" --pr "$PR" \
    --capability <sources-mcp|test-execution|shell|browser|checkout-data|ci-logs> --authority claude-cto
  ```

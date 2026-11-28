# Decision report template

Use it when the CTO publishes a proposal that needs designated or panel approval (as a
published page), and when a driver writes the brief for a new multi-PR outcome. Operator
template, C-2834. Keep each section to what changes the reader's decision; every claim cites
a command, file:line, run ID, SHA or tracker item, or says NOT CHECKED with its command.

## How it was made

Who drafted independently (model, family, harness), what each draft proposed, and how the
panel compared them. Name the drafts that lost and why.

## What it is and how it composes

The outcome in one paragraph, then the parts and how they fit the existing skills, rules,
tools and data. Name the owner of each part and the paths it touches.

## What keeps it safe

Pre-mortem: assume it failed and list why. Each credible cause becomes a check, a stop
condition or a sequencing change; name which.

## One source per procedure

Each procedure it defines lives in exactly one file. List the files it supersedes and the
pointers that replace their copies, so no stale duplicate survives.

## Rollout, timing and proof of use

The order of PRs, what lands first and why, the deploy step, and the proof that agents use
it (an attributed fetch, a run, a receipt), not only that it merged.

## Panel decisions

Each decision with who approved it. Designated approval is Opus 5.5 and Sol 6.2, the
non-author completing an advisor's proposal (operator-expectations.md item 12); panel voices
advise and never replace it, and the author's family never approves its own
proposal. A disagreement goes to the operator. The operator is informed; approved work does
not wait on the operator. Operator-only items (core.md P6) wait for the operator while
independent work continues; list each as a residual with a workaround and the command output
that shows it is operator-only.

## Council: optional independent panel

**When to use it.** Every PR above the Trivial tier gets normal review: the reviewer or reviewers of record that the
applicable tier and the branch's author families require (the resolver for code and infra, the Ukrainian profile's Seats for Ukrainian content), run through
`review-loop`, (the review policy's Low-tier exception and the Ukrainian
profile's Sources-backed seats stand). Add a council only for:

1. a consequential, unresolved choice between materially different approaches (a decision
   that needs designated approval does not need a council for that reason alone);
2. a new critical-risk design, or materially changed safety assumptions (deletion engines,
   privilege, release or deploy tooling, security boundaries, data migrations), not routine
   implementation of an approved design;
3. a named, materially unresolved critical risk, found at any stage, including the first
   review;
4. an unresolved blocking defect class, or a material disagreement between the author and the
   reviewer of record, after two review rounds (not merely more work in the same area);
5. a Ukrainian language, culture or heritage question where the council interprets
   conflicting evidence or makes a pedagogical judgment; missing tools or missing attestation
   stay unresolved, and model agreement never establishes a language norm;
6. an operator request ("council"), which overrides the exclusion below.

Never for routine low or medium risk PRs or Dependabot bumps. Each council records its
actionable findings by member, what the reviewer of record missed, model cost and rework
avoided; after about 20 councils this rule is tightened or loosened from that data.

- **Order:** the council comes first: each member answers the frozen question on its own,
  read-only and scored (below). Then at most one ACP `discuss` round with 2 to 4 seats
  (the ask and `discuss` rules in `fleet-comms-coordination.md`) takes up only the points where the
  members disagree. A discussion never counts as review or approval.
- **Members:** live eligible routes only, as `model-assignment.md` seats them for the question
  (see that file). Record each
  model's exact ID with its findings and score. On Ukrainian language, culture and heritage
  questions, Grok also advises (`model-assignment.md`, LANGUAGE-LANES RULE, 2026-11-16 amendment);
  its answers are input in the council and the discussion only, never review of record or
  designated approval. An unavailable member is reported, never substituted, and the panel is then
  not called complete. A member from the author's model family stays on the panel (operator order
  2026-11-16): it reviews and scores like any member, while the exact-head review of record
  follows the applicable tier and designated approval stays outside the author's family.
- **Read-only** comes from the harness (the dispatch's `--mode read-only`), not from prompt wording.
- **Freeze before dispatch:** the question, context, roles and stop condition. Panel rounds
  count against the same `review-loop` budget as any review
  ([policy](../../review-loop/references/policy.md)); never "until all members are satisfied".
- **Act on each result as it arrives:** check its findings against the code at once and
  decide fixes while the other members still run. Edit no file while a member still reviews
  the same checkout. A late non-blocking result from a member the decision did not wait for is
  feedback only; any late blocking finding under
  [policy §3](../../review-loop/references/policy.md#3-findings) is checked and dispositioned
  like any finding.
- **Findings and score:** every panelist returns written findings plus `SCORE n/10` with a
  one-line rationale, against this rubric: 10 = no findings; 7-9 = only P3; 4-6 = an open
  P1 or P2; 1-3 = a P0 or the wrong approach. Blocking thresholds, reachability, and carry rules
  follow [review policy §3](../../review-loop/references/policy.md#3-findings) and
  [§4](../../review-loop/references/policy.md#4-convergence).
  A score never gates a merge: the verdict and the findings do.
- **Output:** evidence, dissent, scores, dispositions and owners go in this report and the
  durable task record. For a PR panel, record each round in a rounds table in the PR body
  (round, member, verdict, score, key finding).
- **On a PR** the panel shares the `review-loop` budget and ledger, counted by reviewed
  head: several members on one head are one round. After round 2, run the council on that
  second head, settle the design, and keep the third head for fixes and the reviewer of
  record's verdict. A council at a new third head uses the last round and authorizes no
  fourth head, and council consensus never clears a recorded rejection. `review-loop` keeps the
  exact-head review of record the tier requires, so the panel adds no second review path. Designated
  approval and `full-send` are unchanged.
  Before a panel round, bring the head up to date as `review-loop` step 0 says, except for
  the council after round 2: it reads the unchanged second head and the open ledger before
  any fix, and step 0 then applies to the third head's review of record.

Learned from t3dotgg/tstack's `council` skill (MIT, Theo Browne), including its 2026-11-11
rules on acting on each delegate's result (commit 11e37a74); the text is our own.

## Sources

Every source cited above, with its link or path.

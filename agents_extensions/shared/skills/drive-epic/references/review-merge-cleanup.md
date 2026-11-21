# Review, merge, rollout, cleanup (drive-epic §6, §7, §7-rollout, §7a)

Read before launching a review, on red CI or a merge-queue kick, and when a rollout needs
authority. The ordered steps are the `full-send` and `full-stop` skills, `review-loop` owns review
rounds, the budget and the ledger ([policy](../../review-loop/references/policy.md)), and the
dispatcher, the merge-queue keeper and `merge_closeout` own settlement, enqueue cleanup and
post-merge cleanup. This file keeps only what a driver still needs and no other skill states.

## §6. Review of record

Every PR above the review policy's Trivial tier needs a review of record; a Trivial PR needs none and
lands on required CI alone, but a recorded rejection on its head still blocks landing. A review of record is independent, outside the author's model family (at the review policy's
[Low tier](../../review-loop/references/policy.md#2-risk-tier-is-set-by-the-change) any qualified
non-author reviewer), toolful and **exact-head**: attested `resolved_model`, a verdict, and an
attested SHA equal to the current PR head. A moved head makes it stale. Discussion on a thread,
ACP asks and council panels are not review; never record an APPROVE over a pipeline
`CHANGES_REQUESTED`. A review `ask-<lane> --review` or `--type review` runs a toolful native CLI
with synchronous dispatch and wait; its legacy `--background` flag is rejected. About 1000 changed
lines excluding tests is a review-size signal, not a finding
(`agents_extensions/shared/rules/workflow.md` § Merge policy).

Resolve the reviewer with the recipe in `review-loop` step 2, which first initializes the target
and then resolves with the exact author model, review profile and risk; pass the same profile to
the dispatch. `requires_silence_timeout` is boolean: when true, pass `--silence-timeout <seconds>` explicitly
(default 3600) and confirm `silence_timeout` in the task record. Dispatch v2 resolves the locally
available branch or the explicit `--pinned-head` SHA and does not fetch or freeze the remote
branch: verify the pushed tip before launch and before publishing the verdict. After settlement,
the recorder uses the task's `worktree_base_sha`. Publishing the verdict is `review-loop` step 7.
The launch and wait recipe, which the offline tests execute verbatim:

```bash
set -euo pipefail
PRIMARY_REPO="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"
PY="$PRIMARY_REPO/.venv/bin/python"

# If requires_silence_timeout is true, set SILENCE_TIMEOUT_SECONDS from current
# seat/runtime guidance and add --silence-timeout "$SILENCE_TIMEOUT_SECONDS".
dispatch_result="$("$PY" -m lu_machinery.dispatch dispatch \
  --agent "$REVIEW_AGENT" --model "$REVIEW_MODEL" --effort high \
  --mode read-only --worktree --task-id "$REVIEW_TASK" \
  --prompt-file "$REVIEW_BRIEF" --branch "$AUTHOR_BRANCH" \
  --pinned-head "$HEAD_SHA" --require-review-verdict \
  --review-profile "$REVIEW_PROFILE" --review-author-model "$AUTHOR_MODEL" \
  --risk "$REVIEW_RISK")"
mapfile -t dispatch_lines < <(printf '%s\n' "$dispatch_result")
REVIEW_TASK="${dispatch_lines[0]}"
REVIEW_NONCE="${dispatch_lines[1]}"
# Arm in a yielding tool session. Re-arm ONLY wait on client expiry.
while true; do
  wait_rc=0
  wait_result="$("$PY" -m lu_machinery.dispatch wait "$REVIEW_TASK" \
    --run-nonce "$REVIEW_NONCE" --timeout 1800)" || wait_rc=$?
  if (( wait_rc == 0 )); then printf '%s\n' "$wait_result"; break; fi
  if (( wait_rc == 124 )); then
    state="$("$PY" -m lu_machinery.dispatch status "$REVIEW_TASK" --run-nonce "$REVIEW_NONCE")"
    if "$PY" -c 'import json,sys; sys.exit(json.load(sys.stdin).get("status") not in ("running", "spawning", "done"))' < <(printf '%s\n' "$state"); then continue; fi
    # A terminal state raced expiry: read its same-nonce settlement/exit code.
    "$PY" -m lu_machinery.dispatch wait "$REVIEW_TASK" --run-nonce "$REVIEW_NONCE" --timeout 1
    exit $?
  fi
  printf '%s\n' "$wait_result"
  exit "$wait_rc"
done
```

**Synchronous ask expiry:** its wait omits `--run-nonce`; wait rc 124 returns the active task record
and becomes wrapper `ok=false`, empty `response`, and `ask-<lane> review dispatch did not complete: status='running'`
or `status='spawning'`.
The outer subprocess timeout instead reports `dispatch.py wait timed out at process level`
while the original review may still be live. Either signal requires the same lookup and
original-task wait recovery below; neither is a terminal task-record `timeout`.
Ask itself does not expose the dispatch nonce (or rc 124 for client expiry). Set `REVIEW_TASK` to the original ask's task ID
and `PRIMARY_REPO`/`PY` as above; query the live task record via `.venv/bin/python -m lu_machinery.dispatch status`:

```bash
set -euo pipefail
state="$("$PY" -m lu_machinery.dispatch status "$REVIEW_TASK")"
REVIEW_NONCE="$("$PY" -c 'import json,sys; s=json.load(sys.stdin); n=s.get("run_nonce"); (s.get("task_id")==sys.argv[1] and isinstance(n,str) and n.strip()) or sys.exit("invalid task identity/nonce"); print(n)' "$REVIEW_TASK" <<<"$state")"
```

Resume **only** the `while true` wait loop above, even for `done`; never rerun dispatch.
Missing/invalid lookup or nonce drift refuses continuation, not a new review.

Keep task ID and nonce. Wait rc 124 with a `running`/`spawning` stdout record means client expiry: re-arm only wait.
Status rejects nonce drift; `done` racing expiry gets one settlement read.
Only terminal task-record `timeout` is settled failure, with exit code 1. Settlement requires `done`, matching
identity, attested model/family, unchanged branch/SHA, and complete reply; failure, missing/malformed
evidence, unknown identity, or moved head is not approval.


### Review stopping rule

Review tiers, findings, rounds and the ledger live only in the
[`review-loop` policy](../../review-loop/references/policy.md); every reviewer and driver follows it.

### Review brief template (#10665)

Every review brief, code or Ukrainian, uses these sections. Round 1 freezes sections 2 to 4;
later rounds reuse them unchanged. From round 2 on, section 1 names both the last
reviewed head and the new exact head, the fix diff, and each earlier blocker's
resolution evidence. The reviewer brief states DELTA scope under the
[review budget](../../review-loop/references/policy.md). A review-typed
dispatch whose brief has no heading naming the dispositions is still dispatched, with the
typed warning `review_brief_no_dispositions` on stderr and in the task record. Phrase
reviewer asks as questions or statements: a line that starts with a write verb refuses a
read-only dispatch (#8703; [accepted wording](../../review-loop/references/brief.md#wording-that-read-only-admission-accepts)).

1. **Target** — branch, exact head SHA, issue if one exists, review profile and risk.
2. **Outcome** — the frozen intended behaviour, acceptance criteria and non-goals.
3. **Input contract** — what executes, on which data, and who controls the input: a one-shot
   run on the frozen tree at a named SHA (its invocation included), or a reusable tool and the
   inputs its contract admits. Add the input classes in scope with one concrete case per
   class, the non-goals, the residuals deliberately not handled, and the checks already run
   with their CI run ids. Name classes; never a "try to break it" list of individual spellings.
4. **Dispositions** (a `## Dispositions` heading; `local-code-review` checklist Step 5):
   - `in_scope_blocker` — a P0–P2 finding whose trigger is reachable under the input
     contract ([findings](../../review-loop/references/policy.md#3-findings)), or a demonstrated
     P0–P2 regression against main;
   - `follow_up` — a pre-existing defect the change does not expose or worsen, filed as an
     issue with an owner;
   - `stop_and_escalate` — material contract ambiguity, or a fix that would trip a breaker.
5. **Reviewer duties** — one class-level finding per input class, with a concrete example and every
   sibling found within this round's scope. Every P0–P2 finding cites `sources` that show its
   trigger is reachable under the input contract; a reviewer who cannot establish reachability
   says so. Later rounds use the review budget to decide blockers and follow-ups. Each blocker states the demonstrated failure, its impact and the required
   corrected behaviour; a code fix is a suggestion. APPROVE with tracked follow-ups is allowed.
   A delta-review brief asks the reviewer to probe every fallback or fail-open path changed
   by the fix diff and check listed fixes, interactions and weakened tests at the new exact
   head. A missing required capability is INCONCLUSIVE: the verdict line uses `BLOCKED` and
   names the capability; it still blocks.
6. **Breakers** — the review budget and `local-code-review` checklist
   Step 6 apply, even when the outstanding-finding count shrinks.
7. **Reply format** — the complete section from
   [`review-loop`'s Reply format](../../review-loop/references/brief.md#reply-format),
   included verbatim in the brief. That section alone defines the literal reply lines and
   how a structured findings request refers to the canonical schema.
8. **Reliability checklist** — the `brief_section` printed by
   `.venv/bin/python -P -m lu_machinery.review.closeout_cli --state-file <file> checklist --base origin/<base> --head <sha> --repo-root .`
   for this round's exact head, verbatim: the checklist items the diff triggers, the owner of
   every area it touches, and the rule that judge inputs are read from the merge-base
   ([`review-loop` brief](../../review-loop/references/brief.md#judge-inputs-come-from-the-merge-base)).

Every review brief requires the verdict and supporting evidence in the result text.
Workers write additional artifacts under their own worktrees. Dispatch v2 does not
preserve these artifacts automatically. Explicitly retrieve and verify required artifacts
before cleanup or reuse, and cite their verified durable locations in the result.

**Security-code variant** (hooks, launchers, credentials, admission, sandbox, guards):
section 3 also states the trust boundary, the allowed inputs, the refusal behaviour, the
attack classes in scope and the design: a fail-closed allowlist, or why not. The author brief
and the round-2 stop are in [routing-and-dispatch](routing-and-dispatch.md) §4.

**Ukrainian content variant** (lessons, modules, plans, practice decks, word cards, dataset
rows): the contract is the unit's plan or acceptance (learning goals, CEFR level, scope,
sources). Blocking, class reporting and evidence follow
`agents_extensions/shared/rules/ukrainian-linguistics.md` §8: every Ukrainian error in added
or edited text blocks, one finding per error class lists every occurrence with a
source-verified correction, and an unsourced blocker is INCONCLUSIVE. Seats and finding
verification: the `review-loop` [ukrainian profile](../../review-loop/references/ukrainian.md).

Reviewers do not re-run test suites that the PR's CI runs: review the diff, run at
most the specific existing tests that cover a finding you are checking, and cite CI run
ids for suite results. Add no test and repeat no run.

## §7. Landing

The gates are `workflow.md` § Merge policy; the delivery steps are `full-send` and `full-stop`,
and the briefed worker enqueues its own PR once the review its tier requires is recorded at the
exact head (none for a Trivial PR, where a recorded rejection still blocks) and the same-head CI Gate is green. You land only a PR no brief owns, and flag another
lane's PR with `needs=merge` instead of merging it. Run the core inbox drain immediately before an
enqueue and reconcile its messages with the approved head.

- **Queue kicks, failing CI and a moved base** follow `full-send`'s
  [pr-watch](../../full-send/references/pr-watch.md) (Failing CI, Merge-queue kicks, The base
  moves): the evidence requirement, the one-recovery limit and the stop on an uncertain diagnosis
  are stated there. The driver's part is timing and escalation: a queue kick is same-hour work,
  never left overnight, and a second failure or an uncertain cause is reported to the CTO.
- **Diagnose pytest failures from the JUnit artifact first** (`pr-watch` points here for the
  command). The live log truncates and stalls, which has read as a silent shard death. Download the
  artifacts to a scratch directory (never the primary checkout) and parse `<failure>`; a failed test
  there is a test failure, never infrastructure. Jobs without that artifact use
  `gh run view <run> --log-failed`.

  ```bash
  gh run download <run> --pattern 'pytest-junit-*' -D "$(mktemp -d)"
  ```
- **Main red: fix main first.** Find the breaking commit and fix main, or delete the test with the
  reason, before re-enqueueing anything (rules core P4).
- **Launchers and hooks.** Before opening a PR that touches them, run the launcher tests that
  cover the change (`tests/lib/test_launcher_contract.py` and its siblings). Most call the shared
  `run_launcher` helper rather than `subprocess`, so select on launcher names and that helper.

## §7-rollout. Local and production proof (when the epic requires it)

Epic drivers are not standing release owners. Gate rollout by charter:

| Kind | Driver owns? |
| --- | --- |
| **Local or service proof** after a change (restart Monitor API, smoke `/api/…`, UI check) | **Yes**, part of verifying the artifact |
| **Routine host maintenance** (pull merged `main`; restart an updated or broken service after checking no active dispatch depends on it; install or enable a reviewed repo systemd user unit or timer; clean agent-generated caches, logs and worktrees; install OS packages a reviewed repo change needs), including sudo where the host needs it | **Yes**: do it, then report. Never ask the operator |
| **Production, Pages or public cutover** | **Only on a present-tense operator GO**; listing it in the epic is scope, not a green light |
| **HA or a new server** (Patroni, new VPS, fenced cutover) | **Operator only**: needs the operator's present-tense GO; drive the checklist, never solo mutate |
| **Host access and security configuration** (sshd, sudoers, user accounts, SSH keys and other credentials, firewall changes that could cut off operator access) | **Operator only** (lock-out risk) |

Missing local proof on a user-visible API or UI change is incomplete closeout. Issue or PR wording
never authorizes a cutover.

## §7a. Closeout

The publisher's enqueue, the dispatcher's settlement removal of each task's own worktree
(`worktree_disposition`) and the post-merge `merge_closeout` own cleanup and its receipts:
`workflow.md` § Post-merge cleanup is the single recipe, and `full-send` runs it. Queue success
proves no cleanup. Before the next large dispatch you need the worker's exit, `MERGED` with the
merge SHA, and `merge_closeout` exit 0 with a residue-free receipt; a non-zero exit or any
residual blocks closeout, and nothing is forced. You remove a retained worktree only after its
content is published (checklist in [SKILL.md](../SKILL.md#delivery-and-done)). Stream leases are
launcher-owned: never open, resume, claim, renew, release or replace one.

**After a suspected secret leak:** from the product checkout, run
`.venv/bin/python scripts/audit/secret_scan_local.py --repo <checkout> tree` and `history`
(offline; the script is absent from this repository); triage only through its `show-keys` and `count` subcommands (never `jq`, `cat` or
`grep` over the report); tell the operator privately that a report needs inspection and where it
is on the host, with counts only; delete the report directory when triage ends; never rotate,
revoke or rewrite history yourself. Runbook: `docs/runbooks/secret-scanning.md`.

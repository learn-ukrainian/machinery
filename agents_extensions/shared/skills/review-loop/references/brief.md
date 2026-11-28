# Review brief for a review-loop round

The section layout of every review brief (target, outcome, input contract,
dispositions, reviewer duties, breakers) is the
[review brief template](../../drive-epic/references/review-merge-cleanup.md#review-brief-template-10665).
Use it unchanged. This file lists only what review-loop adds to it.

## Required brief contents (round 1)

The template's **Outcome** section carries what the CTO froze: the acceptance-criterion IDs
and expected behaviour, the non-goals, the owner boundary, the risk and, for a replacement,
its authorized scope.

The template's **Target** and **Input contract** sections carry what the author supplies:

- the review round (`round 1 of 3`, or of the budget the repository declares);
- the author model of every commit on the branch, the catalog risk and the review profile;
- the exact head, the base ref and the merge-base SHA, so the reviewer reviews
  `merge-base..exact head`;
- the real invocation, and one concrete case per input class;
- the threat model: the actor-to-input paths (entry points and environments, input sources,
  who can influence each source or relevant write path, and the controls that enforce any
  exclusion) with evidence for every exclusion, as the
  [policy's Threat model paragraph](policy.md#3-findings) requires;
- targeted same-head output or the relevant CI excerpts with their run ids (reviewers do not
  re-run suites), and the checks still pending on that head, named as pending. Review starts
  without waiting for the PR's `CI Gate`; pending CI is not successful CI.

The reviewer brings its own independent adversarial cases; the author's cases narrow nothing.

## Findings rules in every code or infra brief

Every code or infra brief, round 1 and each delta, tells the reviewer to apply
[policy section 3](policy.md#3-findings), read as the
[merge-base holds it](#judge-inputs-come-from-the-merge-base). It names two of its rules: the
class sweep before returning a round that reports a new finding or leaves one open, delta rounds
included, and the conditions for carrying a confirmed defect as a follow-up or excluding an
input.

## Judge inputs come from the merge-base

The reviewer's checkout is the change under review, so a PR can edit the reply schema, this
skill, the review policy or the rules that judge it. Every brief therefore names the judge
inputs as the merge-base holds them, never as the head does. In a repository that holds a
judge file, the reviewer reads it with `git show <merge-base>:<path>`; a head copy the diff
changes is the change under review, not the standard. A consumer repository (one without
the file at its merge-base) uses the installed release copy, which the PR cannot change.

Every round's brief, round 1 and each delta, carries the section this prints, verbatim:

```bash
"$PY" -P -m lu_machinery.review.closeout_cli --state-file "$STATE" \
  checklist --base "origin/$BASE" --head "$HEAD_SHA" --repo-root .
```

It reads `lu_machinery/config/reliability_checklist.yaml` and `lu_machinery/config/code_owners.yaml`
from the merge-base, matches every checklist item against `merge-base..head` (changed paths,
change kind, added and removed lines), and prints the matching items, each with its incident and
owning area, the owner of every area the diff touches, and the `judge-change` item when the diff
touches a judge file (the checklist, the ownership map, the reply schema, the review skills, the
brief template or the rules). Its JSON output's `brief_section` is the text the brief carries.
The reviewer answers each item with file:line or test evidence. In a delta round, prior
evidence may answer an unchanged checklist item when the reviewer confirms its premises still
hold; items affected by the delta or an open blocker require current-head evidence. It runs the installed release:
`-P` keeps the working directory off `sys.path`, so a machinery checkout's own `lu_machinery`
is not imported. It refuses to run from, or read a judge file from, a file the checkout under
review tracks (in the work tree's top level, whichever subdirectory `--repo-root` names) or a
file in a submodule, linked worktree or clone nested in that work tree. An installed release in
an untracked virtual environment such as `<top>/.venv` is not checkout source and runs. Any
other exit 1 (a malformed checklist or map at the merge-base, an unreadable diff) is
fail-closed: escalate to the CTO, and never hand-write the section.

For the `ukrainian` profile, identify the contracts in
[`ukrainian-linguistics.md` §8](../../../rules/ukrainian-linguistics.md#ukrainian-linguistic-principles),
as the merge-base holds it:
Every Ukrainian-profile brief identifies these contracts and their applicability, including level, track, workflow, learner state where relevant, and explicit exceptions.

## Reply format

For a Ukrainian review, copy exactly one reply section from the profile into the brief.
Use [the frozen-item reply](ukrainian.md#ukrainian-review-with-a-frozen-item-packet) when
the brief includes a frozen item packet. Otherwise use
[the generic reply](ukrainian.md#ukrainian-review-without-a-frozen-item-packet).
The selected section supplies every reply instruction, including where to put the verdict.

### Code or infra

Every code or infra brief includes this **Reply format** section. It is the reply contract
`verify_review` and the verdict recorder both accept. The reply is one JSON object and
nothing else. The object matches
[`lu_machinery/schemas/code-review-findings.v1.schema.json`](../../../../../lu_machinery/schemas/code-review-findings.v1.schema.json)
as the merge-base holds it ([judge inputs](#judge-inputs-come-from-the-merge-base)); `verify_review`
and the recorder validate against the installed release's copy. The reviewer uses that file's field names, required fields,
allowed values and nested definitions, and adds none. `overall` includes `correctness`,
`explanation` and `confidence`. Each finding includes `confidence` and `verbatim` with the
schema's other required fields. `overall.correctness` is `correct` to approve, `incorrect`
to request changes, and `uncertain` for `BLOCKED`. A finding with category `security`
blocks at every priority, so a reply that approves never carries one.

**Security sign-off.** A critical review (a security path, critical risk or the `security`
profile) approves only with a sign-off, and so does any approval after another review of the
PR raised a security finding. The sign-off is the first line of `overall.explanation`,
exactly `SECURITY_VERDICT: PASS` on its own line; the rest of the explanation follows it.
Never write the words security verdict anywhere else in the reply, the rest of the
explanation and the findings included; call it the sign-off line. Only a `critical_review`
seat signs off; the recorder refuses any other form of the approval.

The brief carries this repository-relative schema path, or an exact shape generated from
that file. It never carries a hand-written schema copy or asks for the format by name
alone. If the reviewer cannot read the schema, the reply is a schema-valid object with
`overall.correctness` of `uncertain`, no findings, and `missing_capabilities` naming
`checkout-data`. A prose approval or a verdict line does not replace the object. An
improvised or malformed findings block is refused even beside a valid verdict line.

A reviewer withholds APPROVE when evidence necessary to resolve a material finding remains
unavailable, such as a pending check that finding depends on: it replies `uncertain`
(`BLOCKED`), names the missing evidence and the pending check, and lists no other finding as
a reason. A pending check does not change a verdict that the diff and the available evidence
already settle. The driver or worker does not record that reply
([policy](policy.md#recording-the-verdict)). The capability-only `BLOCKED` below covers a
missing reviewer capability, never pending CI.

A capability-only `BLOCKED` uses `uncertain`, no findings, and `missing_capabilities`.
The recorder's extension and the permitted capabilities are defined by
[the capability-failure policy](policy.md#recording-the-verdict) and
`lu_machinery/review/verdict_parser.py`. `missing_capabilities` is allowed only where the schema
already allows it.

Ids are stable: a later round reuses `F1` rather than renaming it. Paths are
repository-relative with line suffixes. A `SCORE` line is not part of this reply. The
council's anchored rubric
([decision report](../../drive-epic/references/decision-report.md#council-optional-independent-panel),
"Findings and score") never gates a merge. The recorder stores a score when a reply still
carries a well-formed one, or records it as absent.

## Delta rounds (round 2 and later)

Freeze sections 2 to 4 of the template from round 1; do not edit them. Section 1 names:

- the last reviewed head and the new exact head;
- the fix diff between them (`git diff <last reviewed head>..<new head>`);
- the acceptance-criterion IDs the fix diff affects;
- the ledger: every finding id with its disposition and proof, that is its fix evidence, the
  author's decline with file:line or test evidence, or the author's `not_applicable` claim
  with its proof, and the outcome label recorded for each P0-P2 finding with its evidence;
- targeted same-head output or the relevant CI excerpts, and the checks still pending;
- the budget state (`round 2 of 3`, or of the budget the repository declares).

The reviewer marks every earlier id `resolved` or `still open: <reason>`, confirms or contests
each `not_applicable` claim on its evidence ([findings](policy.md#3-findings)), contests an
outcome label its evidence does not support with a line `CONTEST <finding-id>: <reason>` in
`overall.explanation` (the verdict recorder stores it against the previous blocking round, and
the label then counts as contested), reviews the fix
diff and traces its effects on the frozen acceptance criteria and existing contracts, including
changed fallbacks, fail-open paths, downstream callers and weakened tests; unchanged code is
context for those checks. A new blocker names the causal change or unresolved finding id, the
reachable trigger and the material impact; a demonstrated P0–P2 regression caused by the PR or
a concrete unresolved security finding still blocks when discovered late. Once those checks are
answered, the review is complete ([policy](policy.md#4-convergence)). The reply uses the round 1
reply format.

## Commands a read-only reviewer can run

The dispatcher checks out the exact head and provides the base ref. Use
`git show <merge-base>:<path>` to read judge files and `git diff <merge-base>..HEAD` to inspect
the change. The read-only checkout and Git metadata prevent fetches and other Git writes.
Use one command per call so a denied command does not obscure the result of another.

| Read-only dispatch route | Execution available to the brief |
| --- | --- |
| Native Claude Code | Bash inside the whole-process read-only boundary. Claude's tool policy also denies file edits and selected Git and GitHub writes. |
| Native Codex | Shell inside the whole-process read-only boundary, which provides the filesystem sandbox. |
| Cursor (including Grok) | Shell and file reads inside the whole-process read-only boundary. Cursor denies file writes, selected Git and GitHub write commands, and Sources tools that persist data. |
| Native AGY | Shell inside AGY's own bubblewrap boundary, with a read-only checkout and writable scratch. |
| OpenCode (DeepSeek direct or OpenRouter) | Bash and `.venv/bin/python` inside the whole-process read-only boundary. Sources reader tools use the boundary relay, and native `webfetch` uses the public HTTPS proxy. OpenCode denies edit and task tools. |

For Claude, Codex, Cursor, AGY, and OpenCode, name the probes, scripts, or test files the reviewer needs
and ask for their actual output. The boundary makes the project interpreter available at
`.venv/bin/python`. When a test needs the shared pool, check the boundary receipt if pytest
refuses to start. A targeted test command can use
`.venv/bin/python -m pytest -q -p no:cacheprovider tests/x.py`, with temporary output in scratch.

Shell HTTPS goes through the boundary's public egress proxy. The proxy blocks direct GitHub,
host, loopback, and private access. Use admitted GitHub reads through the relay, such as
`gh pr view`, `gh pr diff`, and `gh pr checks`. Inline exact-head CI log excerpts when the
reviewer's relay cannot retrieve them. After a denial, retry once with a literal permitted form
if one exists. If required evidence is still unavailable, name the missing capability.

## Wording that read-only admission accepts

A read-only dispatch refuses a brief in which a sentence or list item starts with a write verb
(#8703). Phrase asks as questions or statements, and quote any brief under review inside a
fenced or `>`-quoted block. Include the verdict and supporting evidence in the result text,
as the template says.

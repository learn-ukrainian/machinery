# Model value statistics

`GET /api/fleet/model-stats?window=24h|7d|30d|90d|all&view=full|page|samples` and the page at `/model-stats` show, per
concrete model id, effective effort, role and task class, how much work a model does, how much it wastes and how
slow it is (#581). `view=full` (default) includes per-class breakdowns (`by_class`), token counts and token rates,
ISO-week buckets (`weeks`) and the reply samples below. `view=page` (the figures `/model-stats` reads) omits those
fields, including the reply text and sample effort, and keeps model and effort totals plus `charts` series. Its string
values are closed-vocabulary labels, timestamps or ISO weeks, and the fixed sentences in `window.cohort`,
`charts.recommendation` and `charts.limits`. `view=samples` returns the reply samples and their recorded effort.
`/model-stats` fetches it on its own and leaves the failing-reply section empty when that fetch is refused
or when the samples window name or `until` does not match the page snapshot.
The page groups model, author, reviewer, effort and class labels before calculating
the figures. It admits the metrics model and effort vocabularies, the page's fixed model labels `auto` and
`unrecognized`, the defined work and review classes, and configured repository keys in landing coverage.
It groups other labels under `other`. The Monitor's `/metrics`
exposes the four longer windows (`7d`, `30d`, `90d`, `all`)
and ISO weeks as gauges for Grafana. The collector is `lu_machinery/api/model_stats_router.py`; it reads only, and
`view=page` and `/metrics` report counts and model, effort, class, pool and repository labels, never prompts,
bodies, paths, accounts, task ids or reply text.

## History store

Every settled dispatch task has one summary row in `batch_state/model-stats/rows.jsonl`
(`lu_machinery/api/model_stats_rows.py`, schema `model-stats-row.v1`): task id, harness, concrete model, effective
effort, role, class, terminal status, any typed pre-start refusal, start and finish times, wall seconds, normalised tokens, allowance readings,
the review verdict and finding priorities, the reviewed PR and head, and the merged delivery the task recorded.
A `code-review-findings.v1` reply also keeps `blocking_paths`: a 16-hex SHA-256 prefix of each P0-P1 finding's
file path, never the path itself (see [Reviewer accuracy](#reviewer-accuracy)). Rows written before this field
existed get it from the next backfill while the review's `.result` file is retained. A release without the field
reads such a row as malformed and its next backfill rewrites it in the older shape.
The dispatcher store appends a row after each write of a settled record in a `tasks/` directory; the last row of a
task id wins, so the record's final update decides. Each row carries its record version (`source_mtime_ns`, the
modification time of the record file it came from). A failed row write is logged and never changes the task's
status; settlement waits at most 5 s for the rows lock. An append closes any torn line an earlier failed append
left, so a fragment costs only itself. A record whose task id is not a dispatcher task id gets no row. The reader
skips any line whose values do not have the shape the writer gives them (for example a token count that is not a
whole number, a number above 2^63 - 1, a timestamp that does not convert to UTC, or a landing without its PR number
and merge SHA) and counts it in `coverage.rows.malformed_lines`. The bound keeps every sum and ratio finite,
so a damaged line never stops the snapshot or turns into a figure; the next backfill rewrites that task's row
from its record while the record exists.

The file sits beside `batch_state/tasks/`, not in it: archiving or deleting task records never removes a row,
so every window, including `all`, keeps working after retention. It is one append-only JSON-lines file under an
advisory lock, the same kind of store as the Monitor's `batch_state/load-samples/`. At the 2026-11 rate (about
400 tasks a day, about 520 bytes a row) it grows by roughly 75 MB a year. Past about 500,000 rows, revisit the
store (an indexed table or weekly roll-ups).

### Recover summary rows

```
.venv/bin/python -m lu_machinery.api.model_stats_rows backfill --dry-run
.venv/bin/python -m lu_machinery.api.model_stats_rows backfill
```

Run from the product checkout. Start with `--dry-run` to see how many rows the command would add or refresh.
The command reads hot and archived task records, with the hot copy taking precedence. It appends missing rows,
refreshes older rows, and recovers review evidence from retained results. It also refreshes a row when its task
record proves a pre-start refusal. Rerunning it with the same evidence
makes no changes. The command leaves task records intact. A row shows only what its record and that record's
current `.result` file prove: nothing is carried over from a stored row. A result that is gone, replaced or
unreadable when the backfill runs, or when the dispatcher rewrites the record, leaves the row with the record's
own verdict (no findings count), else `missing_result` and no sample; a later run restores the row once the
file reads cleanly again. Every review row is computed from a reading made under `rows.lock` (the dispatcher's,
and the backfill's for each review row it writes), so an older reading is never stored after a newer one. While
a backfill holds the lock, a settlement that waits longer than 5 s skips its row; the next backfill adds it.

## Speed

One refresh reads the rows once and adds each task to its finest group (model, role, effort, class) once per
window; every coarser figure, and the `/metrics` copy with bounded labels, is a merge of those groups. Measured on
2026-11-16 on the backfilled store (27,572 rows), in one process outside the Monitor: reading 0.6 s, the landing
scan of eight repositories 0.5 s, the aggregation 2.2 s (3.3 s before this layout). A warm request serves the
stored snapshot in about 10 ms (7d, about 650 KB) to 20 ms (`all`, about 1.5 MB).

The snapshot is refreshed off the event loop every 300 s while the route or `/metrics` is read. That refresh is
a TTL read of the shared single flight, never a forced one, so a refresh that runs after another caller stored a
snapshot finds it fresh and collects nothing. A request that finds no snapshot (a Monitor that just started)
starts or joins the flight itself, schedules no background refresh beside it, and is answered as soon as the
collect finishes, waiting at most 15 s; only when that wait expires does it answer `pending` with
`Retry-After: 1`, and the page asks again after that many seconds. Measured end to end on the same store, a cold request got its data after 2.5 s (5.2 s
before, when it answered `pending` with `Retry-After: 5` and the page polled every 5 s). A failed collect, whatever
it raised (a timeout inside the collect included), answers 503 and the page shows it; it never polls a failure.

## Definitions

- **Window**: `24h`, `7d`, `30d`, `90d` or `all`. A task counts when its `finished_at` (else `started_at`) falls in
  `[since, until)`; `all` starts at the first row. One refresh computes all five windows and the weekly buckets;
  `/metrics` omits `24h`.
- **Model**: the concrete model id: the model the runtime observed from the provider when it attested one
  (`resolved_model_known: true`), else the requested model (`resolved_model_known` false or absent). Before
  stored-label validation, `model_id` trims whitespace, strips any `[...]` context suffix (`claude-opus-5-5[1m]` is
  `claude-opus-5-5`, `claude-opus-5-6[1m]` is `claude-opus-5-6`), maps known Cursor display names via
  `canonical_cursor_model` (`Grok 4.7 256K High` is `grok-4.7`, `Composer 2.5` is `composer-2.5`), and tries
  catalog lookup across the spelling and each `:` or `/` suffix (`grok-4.7-high` is `grok-4.7`,
  `deepseek-direct/deepseek-v4-flash` is `deepseek-v4-flash`, `openrouter/deepseek/deepseek-v4.1-flash` is
  `deepseek-v4.1-flash`, `kimi-code/k3-256k` stays `kimi-code/k3-256k`). Validation then checks the resulting label
  against the stored-label grammar (at most one slash, no spaces or `..`): when `resolved_model_known` is true,
  `concrete_model` never falls back to the requested model, so an unmatched spelling that fits that grammar (e.g.
  `claude-opus-5-6` or AGY's `unrecognized`) stays as that normalized label and takes its `identity` from the
  identity list below, while an empty spelling or one still rejected after normalization and catalog lookup (e.g. an
  unmapped display name with spaces like `Grok 4.7 256K High Fast`, or an uncatalogued two-slash string) becomes
  `unknown`.
  Rows store a bare harness alias with no observed model (`opus`, `sonnet`, `haiku`, `fable`) as recorded. The
  statistics count it as the catalog id it ran only where that is unambiguous: the dispatcher resolves the alias
  to its family's newest catalog id (`model_pause._newest_claude`), and a row counts as that id when it is no
  older than the first row naming the id and no other model of the family appears in any row at or after it.
  Otherwise the version is unknown and the alias stays. Unknown versions (`claude-opus-5-6`) stay as recorded.
  Each model carries an `identity`: `catalog`, or for a label that names no concrete model `alias` (version not
  observed), `harness_choice` (Cursor `auto`), `unreported` (`unknown`, `unrecognized`: the harness reported no
  model), `joint` (a PR reviewed for two author models) or `not_in_catalog` (any other spelling). The page lists
  catalog models first and groups the rest under "Not a concrete model id". The full view retains the recorded
  model labels. GLM and Kimi runs stay in the history store. The API and dashboard omit those retired models.
- **Idle catalog models**: `idle_catalog_models` lists every catalog model the catalog still routes (lifecycle
  not `retired`) that ran no task in the window, so a routed model never silently drops off the page. It is
  `null` when the model catalog cannot be read, and the page then says the list is unknown.
- **Effort** comes from the observed runtime variant, the matching requested variant, or the recorded effort
  field. When `resolved_model_known` is true and the runtime attestation carries no variant, an unmapped or
  `unrecognized` attestation never inherits a requested effort, a suffixed request selector applies only when
  a mapped Cursor display name attests that variant word (such as `Grok 4.7 256K High` for `grok-4.7-high`),
  and the recorded request `effort` field applies only for harnesses that pass a separate effort flag
  (`claude`, `codex`, `muse`, or `opencode` on `subq`) whose attested model matches the requested model (or
  the current catalog target of a native Claude alias). A catalog ID ending in an effort word carries no
  implied effort. Missing values and legacy markers such as `unset` and `not-exposed` appear as `unknown` in
  the API and dashboard.
  Model totals (`effort: all`) and comparisons count known effort only and report the levels they include.
  The `by_effort` entries include the separate unknown group. In `view=full`, each group splits by task class
  in `by_class`.
- **Role**: a task is a review when its mode is `read-only` or its id contains `review`; otherwise a write.
- **Class**: a write's `research_task_family` (else `unclassified`); a review's `review_profile/review_risk`.
- **Never started** records a dispatch refusal before the provider model ran. The task retains its dispatcher
  terminal status and carries `never_started_reason`. Model stats projects `status.never_started` and reports
  `never_started_reasons` by model, effort, role and class. The dashboard lists counts by cause for the selected
  window and model. A refusal with known effort counts toward the total and not-done attempts. These rows
  have zero tokens and contribute no review verdict, unparsed reply, allowance spend or runtime measurement. A sandbox launcher can have a
  process group while the provider it guards has yet to start.
  Admission refusals without an attributed worktree have their own record ID. The requested task ID remains
  available for a later invocation. If preparation leaves a worktree, its original task record retains custody.
- **Legacy refusal evidence** comes from a retained task record with a known pre-start error and no provider
  start evidence, or a verified refusal receipt. For example, exit 125 and a matching `refused` boundary status
  identify the boundary refusal. A missing receipt or an `interrupted` boundary leaves the original status in
  model stats. Summary rows whose task records expired retain their original classification.
- **Landed**: a write task whose id appears in a whole-line `X-Agent: <harness>/<task>` trailer outside a code
  fence, in a commit message on a registered repository's local `origin/main` since the first row, or that
  recorded its own merged delivery into `main` (`delivery_merge_sha`, proven against GitHub at settlement).
  `landed_by` reports how many landed by trailer, by recorded merge, or both. The Monitor never fetches; trailer
  landing is as fresh as the last fetch of each checkout. `coverage.landing.trailer_task_ids_by_repository`
  counts trailer task ids per repository. See [Landing source](#landing-source) for why the trailer is read and
  what the line scan cannot see.
- **Wall hours per landed**: `wall_hours_per_landed` divides wall time of started write tasks of that model by its
  landed tasks, so failed runs are charged to the work that landed. It is unknown (`null`) when `landed` is 0
  or any started task has no recorded duration. `wall_seconds.n` counts started tasks with a duration.
  `wall_hours_observed` divides total recorded hours by `landed` and reports a `value` when `landed` > 0, at
  least one task is timed, and untimed tasks are at most 1% of started tasks or 1 task out of 10 or more.
  Otherwise `value` is `null`. It always reports `n`
  (`landed`), `timed_tasks` and `untimed_tasks`, and the page displays it with any excluded or missing count labelled.
  Tokens per landed task are computed from total tokens over `landed`. Fresh input includes cache writes; output
  includes reasoning tokens (Codex `output_tokens` already include them; the reasoning or thinking counts of Cursor,
  AGY and OpenCode are added).
- **Review figures** use structured findings, recorded verdicts and supported historical Markdown verdicts.
  Task and status totals include all read-only work. A task with neither review metadata nor a readable verdict
  contributes to Evidence absent, exposed by reason in `review_evidence_absent`. Backfill refreshes existing rows
  while their records are retained. The unparsed count includes identified review tasks without a readable verdict,
  split by `unparsed_reasons`: `missing_result`
  (no result file bound to the record: absent, not a regular file, unreadable, or not matching the record's
  `result_sha256`), `too_big` (over 4 MB, not read whole), `bad_format` (not UTF-8 or no review JSON),
  `bad_fields` (review JSON that breaks the review contract), `not_done` (the task did not finish `done`) and
  `unknown` (a row written before the split). `view=full` and `view=samples` also give, per model and reason, the
  first 200 escaped characters of the earliest failing reply (`unparsed_samples`): only for public repositories and
  data classes, only for a reply of at most 16 KiB, and only when no OPSEC or secret match in the whole reply
  starts inside those characters. `missing_result` and `too_big` have no sample. `view=page` and `/metrics` never
  carry one. The page reads samples from `view=samples` and shows no failing-reply section when that response is
  not ok, or when its window name or `until` differs from the page snapshot it is about to draw.
  The approval rate uses parsed verdicts,
  while findings rates use results with a recorded findings count. A verdict alone leaves the findings count
  unknown. The author figures and first-round matrix use **reviews of record**, which name the PR and the
  author's model.
- **Reviewer levels** use the runtime model selector where available. First-round approval uses the first
  parsed verdict for that level on the PR's first review head. A supplemental review without a PR number can join
  a PR through an exact head match in the same repository. The time per landed PR divides time across PR-linked
  reviews by linked PRs with recorded merges. Quota uses valid readings and counts landed PRs once per pool.
  Levels with fewer than 10 parsed verdicts carry a **Low sample** badge.
- **Author figures**: per PR whose first review of record finished in the window (else started), only parsed
  reviews of record count, read from the PR's whole history so a window that starts mid-PR never takes a later
  round for the first; an unparsed reply is no round. Rounds are the distinct pinned heads those reviews judged,
  ordered by the start of each head's first review; the first round is the earliest review of the earliest head.
  "Approved eventually" means any parsed review approved. The author rows report the effort of the reviewers.
  The PR cohort requires known effort on every counted review so the first round and eventual outcome retain their meaning.
  A PR reviewed for more than one author model is grouped under the joined ids (`a+b`), which `/metrics`
  exports as `other`. The page keeps the join with each member bounded like a model label (`gpt-6.1-sol+other`),
  and its model picker selects a joint author's PRs by any of its members.
- **After a blocked first round** (`after_blocked_first_round`, per author): of the PRs whose first round did not
  approve, the share where a later review approved a new head (the author changed the code), approved the same
  head (no change), or where nothing approved yet in the window.
- **First-round matrix** (`first_round_matrix`): per author, first reviewer and review class of that first
  review (`profile/risk`, and `all`), the PRs and how many the first round approved. See [Author or
  reviewer](#author-or-reviewer).
- **Author results per risk tier** (`author_risks[].by_risk`): PRs, first-round approval and P0-P1 and P0-P2
  findings per PR with a known count, from published reviews only (see [Publication ledger](#publication-ledger)).
  This cohort is built from the published reviews alone: a round is a head with a published review, each review
  counts by the verdict the recorder published (a P3-only reply published as an approval is an approval; a
  published review whose reply is not parsed counts with unknown findings), the PR's author is the published
  reviews' author, and its first published review sets both its risk tier (`critical`, `high`, `medium`, `low`,
  then any other recorded tier; a class with no tier is `other`) and the window it falls in. A PR the author
  totals count in the window that these tiers do not (no published review at all, or a first published review
  before the window) is counted per author in `author_risks[].prs_without_review_of_record`. The author totals
  above still read the parsed replies, so the tiers need not sum to them.
- **Reviewer accuracy** (`reviewer_accuracy`): per reviewer model, the file-inferred outcome of every P0-P1
  finding of its blocking reviews of record (`findings`), author-labelled finding outcomes (`labels`), and the
  planted-defect recall. See [Reviewer accuracy](#reviewer-accuracy).
- **Allowance**: the dispatcher records the task's pool (`claude:<subscription label>`, `codex:a|b`, `gemini`,
  `agy_claude`, `cursor`) and its remaining weekly allowance at start and finish from the Monitor
  routing-budget, or `unknown` when no plan meter funds the seat or no fresh reading exists. Each reading is one
  request of at most 2 s with no readiness wait, so it never delays a launch or a settlement; `agy_claude` records
  its weekly window, not the stricter five-hour one admission uses. Only tasks with both readings join allowance
  aggregation. Within each window separately, overlapping write tasks on one pool among that window's selected rows
  with valid readings are grouped into a cluster charged that pool's net drop once
  (`earliest.start_pct - latest.finish_pct`), so an overlapping task outside the window does not split a cluster
  inside it. A cluster stays unknown (`null`, never `0`) when a later reading is
  above an earlier one (a reset anywhere in the cluster, including a task's own finish above its start or a rise
  between tasks), when two readings share a timestamp and disagree, or when net drop is negative. Clusters are not
  split across subgroups: any reporting group (model, effort, class, or pool/model/effort row) uses the cluster's
  net drop when it contains all tasks in that cluster, and reports `null` when it only partly owns a cluster. Tasks
  with a finish above start stay in their group's selection and count in `reset_crossed`, so that group reports
  unknown spend rather than `0`.
- **Low sample**: every rate carries its denominator `n`; below 10 it is marked `low_sample`.

## Read the dashboard

Use the window summary to check how old the snapshot is and how many repositories it could read.
The landing counts are exclusive. A task confirmed by both a commit trailer and a recorded merge
appears under **Both sources**.

- **Best value in this window** ranks eligible catalog models by first-round approvals per recorded
  hour. The frontier plots first-round approval against recorded hours per merge. Hollow points
  have fewer than 10 reviewed PRs or fewer than 10 landed tasks. A green ring marks a model on the
  frontier for the whole window, even when a model filter hides other points.
- **Best model per job** uses a star for the highest positive landed share in each class among
  models with at least 10 tasks in the window. Models tied at that rate all get a star. Filtering
  by model hides other rows and keeps the original ranks.
- **Where the work went** counts dispatcher outcomes. **Done** measures completed runs. **Landed**
  in the write table measures merges. The status chart shows each model's total and Done share.
- **Week by week** uses UTC ISO weeks. An asterisk marks a week that the selected window only
  partly covers. A line joins consecutive full weeks for the same model when both have at least
  10 tasks and the chart has more than two weeks. Hollow points show smaller weekly samples.
- **Reviewer strictness** shows how often a reviewer approved and how many P0–P2 findings it
  reported per review with a findings count. Differences also reflect who reviewed which work.
- **Time to finish** orders up to 12 of the busiest timed write models by median. The box spans
  the 25th through 75th percentiles, and the right whisker ends at the 90th percentile. Positive
  times under a minute display as **<1**.

Open the values beneath a chart for counts, intervals, and exact displayed values with a mouse,
keyboard, or touch. On the two scatterplots, the legend identifies individual models and a plus
marks models with identical coordinates. Text below the chart names the models that share each
position. If a table shows **Scroll across for more columns**, scroll inside the table. The
first column stays visible.

## Metrics (Grafana's contract)

Prometheus keeps about 15 days, so long history comes from the `window="all"` and weekly gauges of the latest
scrape, not from range queries. A scrape never computes: it reads the cached snapshot and schedules a refresh
when the snapshot is older than 300 seconds. Label values come from finite sets: `window` is `7d`, `30d`, `90d` or
`all`; `week` is an ISO week (`2026-W46`) since the first row; `model` is a catalog model id, a bare native alias
(`opus`, `sonnet`, `haiku`, `fable`) whose version could not be determined, or `unknown`, and any other spelling
is `other`; `effort` is a catalog effort word
(`low`, `medium`, `high`, `xhigh`, `max`, `minimal`, `thinking`, `thinking-<level>`), `unknown` or `all`
(the total for known effort); `role` is `write`, `review` or
`author`; `status` is a terminal status other than `done` (`no_deliverable`, `failed`, `timeout`, `rate_limited`,
`cancelled`, `crashed`, `never_started`, `other`). The collector calculates each `other` bucket from its grouped inputs. Exact
model labels stay in the rows and `view=full`. No task ids, paths or accounts.

| Metric | Labels | Meaning |
| --- | --- | --- |
| `lu_model_stats_collection_success` | — | 1 when a snapshot exists |
| `lu_model_stats_snapshot_age_seconds` | — | age of the snapshot |
| `lu_model_stats_tasks` | window, model, effort, role | settled tasks |
| `lu_model_stats_landed` | window, model, effort, role=write | landed write tasks |
| `lu_model_stats_not_done` | window, model, effort, role, status | tasks that ended in that status |
| `lu_model_stats_wall_seconds_median` | window, model, effort, role | median wall seconds |
| `lu_model_stats_wall_seconds_p90` | window, model, effort, role | 90th percentile wall seconds |
| `lu_model_stats_output_tokens_per_landed` | window, model, effort, role=write | output tokens of all writes per landed task |
| `lu_model_stats_blocking_findings_per_review` | window, model, effort, role=review | P0–P1 findings per review with a recorded findings count |
| `lu_model_stats_review_rounds_per_pr` | window, model, effort=all, role=author | heads judged by a parsed review of record per authored PR |
| `lu_model_stats_allowance_pct_points_per_landed` | window, model, effort, role=write | weekly allowance points per landed task, tasks with both readings |
| `lu_model_stats_week_tasks` | week, model, effort, role | settled tasks per ISO week |
| `lu_model_stats_week_landed` | week, model, effort, role=write | landed write tasks per ISO week |
| `lu_model_stats_week_not_done` | week, model, effort, role, status | not-done tasks per ISO week and status |

A series with no denominator (no landed task, no read review) is absent rather than 0.

## Author or reviewer

One first-round approval per author mixes three things: how good the author's change was, how strict its
reviewer is, and how risky the work was. The matrix separates them as far as the records allow. Down one column
the reviewer is fixed, so differences between rows are on the authors' side; along one row the author is fixed,
so differences between columns are on the reviewers' side; choosing a review class fixes the risk. Routing still
confounds inside a class (some authors get the hardest or most security-sensitive work), and a cell below 10 PRs
is marked low sample.

Which reviewer findings were real comes from the author's outcome labels where they are recorded, and is
estimated from what the author changed otherwise (see [Reviewer accuracy](#reviewer-accuracy)).

On 2026-11-16 over all history (parsed reviews of record): with Sol 6.1 as first reviewer, Grok 4.7 PRs were
approved first time 78% (178 PRs), Sonnet 5.5 68% (164) and Opus 5.5 45% (362); inside infra/critical alone
Opus 5.5 41% (227) and Grok 4.7 63% (38). Opus 5.5 as first reviewer approved Sol 6.1 58% (211) and Grok 4.7
65% (62), lower than Sol 6.1 approved Grok 4.7, so Sol 6.1 is not the stricter reviewer of the two. After a
blocked first round, 81% of Opus 5.5 PRs were approved on a new head and 1.5% on the same head.

## Reviewer accuracy

Approval rate and findings per review say how strict a reviewer is, not whether it is right. `reviewer_accuracy`
adds the measures below.

**Finding precision** (`reviewer_accuracy.findings`). A blocking review of record is a review whose
`CHANGES_REQUESTED` verdict the verdict recorder published on the PR at the row's head, as the
[publication ledger](#publication-ledger) records it. Each of its P0-P1 findings waits for the same reviewer's
first published approval of that PR at another head that finished after the blocking review finished:

- `fixed`: the approved head differs from the blocked head in the finding's file (the author changed it);
- `dismissed`: the approved head left that file unchanged (the reviewer accepted the author's answer);
- `open`: no such approval yet. Another reviewer's approval does not settle the finding, nor does an unpublished
  approval, nor an approval of the blocked head itself: the verdict recorder refuses an APPROVE at a head with a recorded rejection
  (`require_no_same_head_rejection`), so that reply is never a verdict of record;
- `unknown`: the row keeps no finding paths (a reply in the older unversioned format, or a row not yet
  backfilled), or a head is missing from the row or from the repository's local checkout.

A parsed blocking reply (verdict `incorrect`) that the ledger does not name at its PR and head is of unknown
publication: it counts in `reviews_unknown_publication` (the page's "Blocking replies of unknown publication"
column) and in nothing else, so it never enters the denominator. Before the ledger, nothing local could tell the
two apart: published and unpublished formal runs carry the same task-record fields. Measured on 2026-11-19
against the PR comments of the machinery and public repositories over 30 days, 34 of 234 blocking reviews whose
findings a later approval settled had no published verdict (rounds whose head moved before recording, for
example). Findings settle on the reviewer ids as recorded; the page groups labels (`other`) only after that. A
review here must name the author's model; its PR is the one its ledger entry names, so a review dispatched by
branch alone counts once it is published (see [Publication ledger](#publication-ledger)).

Per reviewer, `reviews_approved_later` counts the blocking reviews with finding paths that the same reviewer later
approved, and `reviews_approved_later_read` those whose changed files were read; the page's note under the table
sums both over the reviewers it shows, in the selected window.

### Publication ledger

`batch_state/model-stats/publications.jsonl` (schema `review-publication.v1`) holds one line per verdict the
recorder published: task id, repository, PR, head, verdict and GitHub's comment time. Its one writer is
`lu_machinery/review/record_cf_verdict.py`, under the rows lock. Both of its paths use `publication_of` to read
the published marker's verdict, including legacy markers recorded as `APPROVED`. A voided chunk, a second review,
or a marker from a model ineligible to supply a review of record stores nothing. `record` stores the line after it posts
the verdict or finds it already posted, and reports `publication_marker`: `stored`, `not stored: <reason>`, or
`failed: <reason>; store it with: ... import-publications --repository <owner/name> --pr <N>`. A failure never
undoes the publication, and that one-PR import works even after the PR has moved on or closed.

The statistics read the ledger; the Monitor never reads GitHub. A line that does not have the writer's shape is
skipped and counted in `coverage.publications.malformed_lines`. A line is one publication of a task's verdict on
one repository, PR and head: a line for another repository, PR or head is stored beside it, never taken for a
duplicate. A row takes the newest entry by GitHub comment time (of two at the same time, the later line) whose
repository is the row's, whose head is the row's and, when the row names a PR, whose PR is the row's; so an import
never supersedes a newer publication, and an entry for another target never hides a matching one. A review
dispatched by branch alone keeps no PR in its row, and joins the PR the recorder resolved from the entry for the
publication figures only (reviewer accuracy and the risk tiers); the legacy author totals, matrix and review
levels still read only rows that name their PR. Each collection starts by dropping every catalog reading the
statistics, the model catalog loader and the fleet registry keep (`refresh_readings`), so the model and fleet
catalogs are read afresh and no reading of an earlier collection, failed or valid, is reused. Within a collection
every statistics consumer of the model catalog (labels, idle models, runtime levels, alias resolution, the public
data classes of reply samples) shares one reading, or all see none; the repository map and the changed-file read
share one reading of the fleet catalog, and an unreadable one matches no entry and reads no file in that
collection. A parsed rejection of
a formal review (one that names its author's model) with no matching entry counts as unknown publication, whether
or not its row names a PR.

History: verdicts published before the ledger existed are imported once, after the release installs, by the same
module from the PRs' `cf-verdict` comments. It keeps only the markers the recorder itself would trust (the
current identity, an owner, member or collaborator association, never edited, well-formed author fields) and
adds what the ledger lacks:

```
.venv/bin/python -m lu_machinery.review.record_cf_verdict import-publications --dry-run
.venv/bin/python -m lu_machinery.review.record_cf_verdict import-publications --days 30
```

It reads each PR of the review rows that finished in the last `--days` days once. A review dispatched by branch
alone names no PR in its row, so its verdict is imported only when another review row names the same PR; otherwise
it stays of unknown publication (on 2026-11-19, 18 parsed branch-only formal reviews in 30 days, none in the
ledger), and `--repository R --pr N` imports that PR by hand. Measured on 2026-11-19 against
a copy of the store: 1,976 PRs for 30 days in about 15 minutes, 3,303 verdicts. A rate limit stops the reads and
keeps what was read; rerunning it is safe, and it exits 1 when a PR could not be read or a rate limit stopped it.
Reviews older than the import stay of unknown publication.

`precision` is `fixed / (fixed + dismissed)` with its 95% Wilson interval (`precision_ci`); it is `low_sample`
below 10 settled findings. The collector reads the changed files with one `git cat-file --batch-check` and one
`git diff-tree --stdin --raw` per repository over every known-effort (blocked head, approved head) pair, from local objects only
(`GIT_NO_LAZY_FETCH=1`); `coverage.finding_heads` counts the distinct known-effort pairs of all history and how many were
read (the page's note uses the per-reviewer window counts instead). Measured on
2026-11-19 on a backfilled copy of the store (29,274 rows, 625 pairs, host load 10): the read took 0.7 s and the
whole collect 13.3 s against 11.9 s before.

Why the file, not the head: over the 30 days to 2026-11-19, of the blocked reviews of record a later review
approved, 2 of 942 were approved on the same head (the recorder refuses such an approval). Authors push a new head while answering, so
"approved on a new head" cannot separate a fix from an accepted rebuttal. The file test can, within limits:

- a file the author changed for another reason (another finding in the same file, a merge of `main`) counts the
  finding as fixed;
- a fix made in a different file than the finding's location (a `missing` claim placed at a caller) counts it as
  dismissed;
- the measure needs the reply's finding locations, so it covers `code-review-findings.v1` replies only.

On 2026-11-19 over 30 days (backfilled copy): GPT-6.1 Sol 0.949 (237 settled findings, 157 open), Opus 5.5 0.868
(91, 28 open), GPT-6.2 Sol 1.0 (3, 35 open, low sample), Grok 4.7 1.0 (7, low sample).

**Outcome labels** (`reviewer_accuracy.labels`). After it records a `CHANGES_REQUESTED` verdict,
the author labels each P0-P2 finding of that review (every finding that can block it) with
`record_cf_verdict finding-outcome --task-id <review> --pr <N> --finding <id> --label <label> [--evidence '<line>']`:
`fixed` (a real error, changed), `wrong` (the code was already correct), `invented` (it cites a file, line,
symbol or behaviour that does not exist), `no_live_trigger` (the cited code exists, but a guard, caller or
configuration keeps its trigger from occurring on the real system; the evidence names it), `out_of_scope` or
`duplicate`. Every label except `fixed` needs one line of evidence. The verb refuses a finding that is not a P0-P2
finding of the review's structured reply, a review with no published `CHANGES_REQUESTED` verdict on that PR at its reviewed head at or after its start in the
publication ledger, and a review task id that names more than one run record in the task store (a task id can be
dispatched again; a label line names the task id, not the run). The reply, the run census and the label's time
are read under the task-store lock that archive and restore moves take. Each label line stores the labelled
review's reviewed `head`, normalized `started_at`, and canonical `reviewer_model` at write time (attested from the
review task record), and each contest line stores the target run's validated `head` and normalized `started_at`. One label per finding:
the same label again is a no-op, and another one is refused. The next review of the PR may contest a label with a
line `CONTEST <finding-id>: <reason>` in its reply's `overall.explanation`. When `record` publishes that review,
it binds the PR's previous round from the PR's own verdict comments: the newest earlier head with any verdict
before this run's comment (a round reviews one head, so a comment at the contesting review's own head is a sibling
seat of its round, never the round it contests, and a contest never falls back past the previous round). That
round must hold exactly one `CHANGES_REQUESTED` review of another task, else
`not stored: unresolved: the previous round at <head> has <n> blocking review(s) of another task`. It reads that
review's reply only from the run its comment names (the task record's reviewed head and start must equal the
marker's, else `not stored: unresolved: ...`), stores each contested id that is a P0-P2 finding of that reply and
reports `outcome_contests` (`stored against <review>: <ids>; unmatched: <ids>`, or `not stored: unmatched: <ids>`
when no id matched, which writes nothing); a reply that contests nothing adds no field. A failed write reports
`failed: <reason>`; recording the same verdict again while the head stands retries it.

Per-reviewer label statistics count label lines directly by their stored `reviewer_model` (filtered to the window
by `recorded_at`), with no join to the rows or publication ledgers. A label contested for the same run (`task_id`,
repository identity, `pr`, `finding_id`, `head`, `started_at`) before the snapshot time counts as `contested`, not
as its label; a contest for another run of a reused task id never suppresses an earlier run's label. Legacy lines
written without `reviewer_model` or run binding (`head` and `started_at`) are accepted as valid lines (legacy
labels remaining conflict-protected on write), counted in `coverage.finding_outcomes.unattributed_labels` and
`coverage.finding_outcomes.unbound_contests`, and attributed to no reviewer. An unreadable fleet registry counts no label
(`coverage.finding_outcomes.registry_unreadable`). `real_precision` is
`fixed / (fixed + wrong + invented + no_live_trigger)` with its Wilson interval; a label is the author's word
unless a later review contested it. The schema stays `review-finding-outcome.v1`: a reader from before
`no_live_trigger` skips such a line as malformed (`coverage.finding_outcomes.malformed_lines`) and attributes it
to no reviewer.

Both line kinds go to `batch_state/model-stats/finding-outcomes.jsonl` (schema `review-finding-outcome.v1`)
under the rows lock; the recorder is its one writer. It keeps the evidence only as its SHA-256, never the text: the
text stays in the PR and the delta brief. A line without the writer's shape is skipped and counted in
`coverage.finding_outcomes.malformed_lines`. The file sits beside the publication ledger rather than inside it, so a
reader of an older release never counts these lines as damaged publications.

**Planted-defect recall** (`reviewer_accuracy.planted_defect_recall`) is `unknown` with `missing_input` named: the
summary rows carry no seeded or known-answer defect result. The frozen known-answer set (`docs/review-eval`) is
scored by `lu_machinery/bakeoff/review_eval.py` into its own output directory, which this route does not read,
and on 2026-11-19 no run output existed and the lesson findings database's `seed_results` table was empty.

## Landing source

The recorded merged delivery alone cannot answer "did this task land" over the route's windows. The dispatcher
records `delivery_pr` and `delivery_merge_sha` for every merged write task only since 2026-11-14 (#548); before
that it recorded them only when the task branch could not show the delivery, so the 30-day, 90-day and all-time
windows would read as almost nothing landed. A task inside the write boundary that asks for its own closeout
records the closeout's PR, not `delivery_pr`, and work a successor lands after the task settled is never
recorded on the original task. Every agent commit carries its `X-Agent` trailer, so the trailer on `main` is the
one source that covers all history; the recorded delivery is added to it.

The trailer is read by a line scan: a whole line `X-Agent: <harness>/<task>` counts unless it sits inside a
backtick or tilde code fence. Git's own trailer parser is not used because it reads only the last paragraph, and a
squash merge puts most task trailers in earlier paragraphs (on the product repository's `main` it finds 1,724
task ids where the line scan finds 8,672). The line scan is not a Markdown parser, so it has two known limits:

- a trailer-shaped line inside an inline code span that runs across lines (a backtick opened on the line before)
  still counts as landed;
- a fence opened in the message prose and never closed hides every trailer after it (one task id in the
  product repository's history at 2026-11-15).

Measured on 2026-11-15 over every commit of both repositories' local `origin/main` (11,538 and 442): the scan
finds 8,672 and 10 task ids, against 8,673 and 10 for a fence-blind whole-line scan (the one missed is the
unclosed-fence case above). A CommonMark parser found 8,671 and 10; the one id it rejected that this scan counts
is a real trailer written straight after a list item, so the parser would undercount, not correct, this scan.

## Reference comparison (2026-11-15)

Over 2026-11-08 to 2026-11-15T06:20Z the route (before the summary rows) and the CTO's reference script agree on
write tasks within 4 per model; landed is 3 to 6 higher per model (225 landed task ids against 208). This is a
definition difference, not a defect: the reference joined the product repository's `main`, while the route joins
every registered repository's `main`. Task ids are unique across the fleet and a whole-line trailer is added only
by the task's own commit, so a trailer on any registered `main` is real landing evidence. The route's rule is
otherwise stricter (whole lines only), so it cannot add landings on the product repository. To confirm on the
host, compare the extra landings with the non-product entries of `trailer_task_ids_by_repository`. Recorded
merged deliveries (`landed_by.delivery`) add further landings the reference cannot see, mostly squash merges that
dropped the trailer.

## Confounders

These numbers describe our routing, not the models alone. Harder and security work is routed to Opus and Sol,
so their per-task wall time and rounds are inflated. Author and reviewer families are paired by policy, so a
reviewer's blocking rate partly measures the authors it is paired with; read the first-round matrix, not one
author figure, before blaming either side. Allowance readings are pool-wide: within each window separately,
overlapping write tasks on one pool among that window's selected rows with valid readings are grouped and charged
the pool's net drop once, while a cluster with a reset, tied-timestamp conflict, or negative net drop stays unknown
(`null`) and any subgroup that only partly owns a shared cluster in that window reports `null`; the meter's
resolution is one reading per probe. Landing depends on the
trailer surviving the merge or on the task observing its own merge; work merged later by another seat, through
a squash merge that drops `X-Agent` lines, is counted as not landed. Treat a comparison as a question for the
CTO, not a routing verdict; routing changes stay a catalog decision.

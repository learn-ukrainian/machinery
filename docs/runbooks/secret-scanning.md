# Secret scanning (local TruffleHog)

TruffleHog is installed locally and is on every agent's `PATH` as
`trufflehog`. CI already scans every pushed range (see
[`ci-gate.md`](ci-gate.md)); this runbook covers the local scan an agent runs
itself, through one wrapper that keeps it offline. Scans and summary commands
print closed-set names and counts. `locate` is the explicit private-console
exception for validated current-tree locations. Issue #9416.

## When to run it

- Before opening a PR that changes credential, identity, transport or hook code
  (launchers, auth helpers, network clients, git or agent hooks): `tree`.
- After any suspected leak (a secret pasted into a file, commit, log, issue, PR or
  transcript): `tree`, then `history`.
- When a driver needs a baseline of the whole public history: `history`.

It does not replace CI or the pre-commit hook, and it is not needed for ordinary
changes.

## How to run it

```bash
.venv/bin/python scripts/audit/secret_scan_local.py tree
.venv/bin/python scripts/audit/secret_scan_local.py history
.venv/bin/python scripts/audit/secret_scan_local.py show-keys "$REPORT"
.venv/bin/python scripts/audit/secret_scan_local.py count "$REPORT" --path uv.lock --path tests/fixtures/
.venv/bin/python scripts/audit/secret_scan_local.py --repo "$REPO" locate "$REPORT"
.venv/bin/python scripts/audit/secret_scan_local.py --help
```

From a dispatch worktree, use the primary checkout's interpreter (see
[`worktree-cleanup.md`](worktree-cleanup.md) § Shared Python environment).

| Mode | Scans | How |
| --- | --- | --- |
| `tree` | Tracked and untracked-not-ignored files of the work tree | `trufflehog filesystem` over that file list (batched). Never `.git`, `.venv`, `node_modules`, `.worktrees` or database files under `data/`; symlinks and files beneath a symlinked directory are skipped. |
| `history` | Every commit on every ref of the configured public remote (default `origin`) | Clones a full bare mirror into a new temporary directory under the temp directory, scans it with `trufflehog git file://<mirror> --bare`, then removes the directory, also on SIGINT, SIGTERM or SIGHUP. A failed removal is exit 2. |
| `show-keys REPORT` | An existing report | Prints only the report's top-level key names and the count of findings per detector. Runs nothing. |
| `count REPORT [--path P]... [--detector NAME]` | An existing report | Prints only how many findings (of one detector, if given) lie in the given paths, outside them, and without a file location. A path ending in `/` is a directory. The paths and the name are never printed. Runs nothing. |
| `locate REPORT --repo REPO` | An existing tree report and an explicit repository boundary | Private console only. Validates the entire report before printing `path<TAB>line<TAB>detector` on stdout. Prints fixed summary counts on stderr. Uses the current tree, never commit lookup or history objects. Runs no scanner. |

Why a mirror: agent checkouts are blobless partial clones. `trufflehog git`
cannot fetch the missing objects from a partial clone, and a `file://` URL to a
bare repository only works with `--bare`. The mirror clone is the full public
history, so it needs disk room for a complete clone; point `TMPDIR` at a
directory on a larger disk (outside the repository) if needed. Only a plain
`https://` remote without credentials is mirrored; git may use no other
transport, and the clone runs with no credential helper and no terminal prompt.

## Threat model

The operator's agents run the wrapper as the operator's own user. Hostile data
comes from the scanned content, the fields of each finding, the configured
remote and the command-line options. Other processes of the same user racing to
move directories, hardlink files or bind-mount are out of scope: they could
write the files directly.

## Fixed flags and parity with CI

The wrapper passes these flags on every call and accepts no TruffleHog
pass-through options:

| Flag | Why |
| --- | --- |
| `--no-verification` | Offline. Nothing is sent to any provider. |
| `--no-update` | No update check over the network. |
| `--results=unverified` | Offline, every candidate is "unverified". CI runs verified mode and keeps `--results=verified,unknown`; offline that filter would hide everything, so the local scan reports the full unverified superset. Expect more false positives than CI. |
| `--exclude-detectors=Lob` | Same as CI (#6575). |
| `--exclude-paths=.trufflehogignore` | Same as CI's Secret scan job, when the file exists. |
| `--fail-on-scan-errors` | A scan error is a tool error (exit 2), not a clean result. |
| `--json` | The report format the wrapper parses. |

## Verified mode is not allowed

Verified mode sends each candidate secret to the provider's API to test it.
That discloses the candidate to a third party and contacts external services
from the host. The wrapper refuses any verification-style option (`--verify`,
`--verifier`, `--results`, `--no-no-verification`, and similar) with exit 2.
Running TruffleHog in verified mode by hand, by any route, needs an explicit
operator decision first.

## Output handling

- The full JSON Lines report contains the **raw secret values** (`Raw`,
  `RawV2`, `Redacted`, `ExtraData` and others). The wrapper writes it, and
  TruffleHog's own log beside it as `<report>.log`, as new owner-only (0600)
  files in a new owner-only (0700) `secret-scan-*` directory under the temp
  directory: the first set of `TMPDIR`, `TEMP` and `TMP`, else `/tmp`, with
  symlinks resolved. The modes are set explicitly, whatever the umask. There is
  no option to choose another location. The temp directory is decided from the
  path alone, so the run is refused before anything at all is written when it is
  inside the repository or its primary checkout.
- Scans and summary commands never show finding paths, commits, lines or
  values. They show the report's generated file name, the number of files
  scanned (`tree`), the total, and a count per detector. Detector names come
  from TruffleHog's own detector list compiled into the wrapper; any other name
  is counted as `other`. Every error is a fixed message.
- `locate` is the one explicit private-console location exception. It requires
  `--repo`, before or after `locate`, with no default. Set `REPO` to the tree
  that produced the report. Each output path must exactly match a canonical
  `tree_candidates` member of that tree. Paths are never normalized into a
  match. Absolute paths, traversal, symlinks, tabs, controls, format and bidi
  characters, and multiline injection are refused. Lines must be integers,
  excluding booleans, between 1 and the current file's line count. Detector
  names use the same closed set, with unknown strings mapped to `other`.
  Only `Filesystem` sources are supported. Every `Git` record is a counted
  history refusal, without reading commits. Other sources are counted source
  refusals. Duplicate JSON keys and ambiguous sources are malformed reports.
- For structurally valid reports, `locate` prints `findings`, `located`,
  `refused history`, `refused source`, `refused path`, and `refused line` counts
  on stderr. Located plus the refusal counts equals the `show-keys` total.
  A refusal withholds **all** location stdout and ends with
  `location view refused; no locations emitted`. Here `located` counts valid
  locations withheld from output too. Malformed or unreadable reports produce
  a fixed diagnostic without claiming a complete summary. Success ends with
  `location view complete`, meaning every finding has a validated location,
  not that a candidate is a credential, a false positive, or remediated.
- Never paste the report, the log, any field of a finding or a screenshot of
  them into a PR, issue, comment, commit, chat or transcript. Public text
  quotes only counts and outcomes.
- Delete the report directory when triage is finished.

Exit codes: `0` no findings (`show-keys`, `count`: report summarized;
`locate`: every finding located, including an empty report), `1`
findings, `2` refusal, missing binary, git or TruffleHog error, timeout,
unexpected report structure, any `locate` refusal, or mirror removal failure, `129`/`143` terminated
by SIGHUP/SIGTERM (any temporary mirror is removed first). A missing binary
prints a typed message pointing here.

## Triage

Agents read a report **only** through the wrapper's `show-keys`, `count` and
`locate` subcommands. `show-keys` and `count` print fixed text, closed-set names
and integers. `locate` adds only the private-console location exception above.
It does not authorize ad-hoc source-line or raw-report reads. Never run
`jq`, `cat`, `grep`, `head`, `less`, a Python one-liner or any other ad-hoc
command over a report or its log: a finding can carry a secret in any field,
including a key name or a field of an unexpected type, and an agent's console is
a transcript. If a triage question needs a view these subcommands lack, add a
closed-output subcommand to the wrapper, with tests, in a reviewed PR.

1. Locate the report without printing its directory, and see its shape:

   ```bash
   REPORT=$(find "${TMPDIR:-/tmp}" -maxdepth 2 -name 'secret-scan-tree-0123456789abcdef.jsonl')
   .venv/bin/python scripts/audit/secret_scan_local.py show-keys "$REPORT"
   ```

2. Test false-positive hypotheses by counting. Paths are repository-relative, as
   scanned (`tree`) or as committed (`history`); a path ending in `/` covers a
   directory:

   ```bash
   .venv/bin/python scripts/audit/secret_scan_local.py count "$REPORT" --path uv.lock
   .venv/bin/python scripts/audit/secret_scan_local.py count "$REPORT" --path tests/fixtures/ --path uv.lock
   .venv/bin/python scripts/audit/secret_scan_local.py count "$REPORT" --path tests/fixtures/ --detector PrivateKey
   ```

   "outside the given paths: 0" and "without a file location: 0" settle the
   hypothesis that every finding (of that detector) is in paths you believe are
   noise.

3. False positive in a file that should never be scanned (lockfiles, generated
   hashes): propose a `.trufflehogignore` entry in a reviewed PR. For a single
   line, an inline `trufflehog:ignore` comment is the narrower fix. Never widen
   the exclusions to silence a real credential.
   If counts do not identify the current-tree locations needed for triage,
   use the explicit private-console view:

   ```bash
   .venv/bin/python scripts/audit/secret_scan_local.py --repo "$REPO" locate "$REPORT"
   ```

   Expect only tab-separated path, line and detector fields on stdout. A
   refusal exits 2 with no location stdout. Return the fixed counts and refusal
   class for a reviewed correction. There is no ad-hoc reader fallback.
4. Anything the counts do not settle is a possible real credential: stop. Do
   not try it, do not verify it, do not remove it in a quick commit (history
   keeps it). Tell the operator through the private channel that a report needs
   inspection and where it is on the host; the operator opens it with their own
   tools and decides on rotation or revocation. Agents never rotate, revoke or
   rewrite history.
5. Public issue text records only the count and the outcome ("N candidates, all
   false positives" or "handed to the operator"), never the value, its location
   or where a credential is used.

## Relation to the other secret gates

| Gate | When | Scope |
| --- | --- | --- |
| gitleaks (`.pre-commit-config.yaml`, `.gitleaks.toml`) | Every commit, staged files | Stops a secret before it is committed. |
| TruffleHog in CI (`.github/workflows/ci.yml`, `lu_machinery/ci/secret_scan_scope.py`) | Every PR, merge-queue and scheduled run | The event's range. The nightly schedule is an ancestry window, not a commit-date window (below). Verified mode, `--results=verified,unknown`. Required. |
| OPSEC linter (`scripts/audit/lint_opsec_leaks.py`) | CI Secret scan job | Infrastructure identifiers (addresses, key headers) that are not credentials. |
| This wrapper | On demand, as above | Offline; whole work tree or whole public history. |

The nightly schedule scans `base..HEAD` for a fixed first-parent depth
(`SCHEDULE_FIRST_PARENT_STEPS` in `lu_machinery/ci/secret_scan_scope.py`, 400 as of
the 2026-10-16 measurement). That range includes every commit reachable from
HEAD and not from the base, merges and side branches included, with no commit
date involved. 400 is more than twice the measured peak of 156 first-parent
commits in any 48 hours (`git rev-list --count --first-parent --since=...` was
73 for the 48 hours ending 2026-10-16 18:05 UTC). History shorter than 400
steps scans the whole branch, including the root. A git failure fails the job
instead of narrowing the window.

After a successful scheduled scan or explicit default-branch full-history
audit, CI uploads the scanned head outside Git history as the 90-day artifact
`secret-scan-covered-head-v1`. The next schedule reads GitHub artifact
metadata for the latest eligible default-branch CI run. It uses that run's
immutable head SHA, without downloading or extracting any archive. Artifacts
from PRs, forks, other branches and other workflows are ignored. The lookup
checks at most the latest 100 matching artifacts and fails if no eligible
record remains. If that head predates the normal window, the scan widens to `last-scanned-head..HEAD`. A failed, cancelled
or timed-out scan never advances the record. Concurrent runs can read
an older record and repeat work safely; a record ahead of the checkout
fails visibly. PR, queue and bounded manual scans never advance coverage.

Missing, expired, corrupt, shallow or unrelated coverage fails the scheduled
job visibly. To initialize coverage, or recover after such a failure, run
CI on the default branch with `secret_scan_mode=full-history-audit`.
Only a successful audit establishes coverage; findings must be resolved
before retrying. The audit has a 60-minute budget. The normal scheduled
and bounded manual budgets stay at five minutes; a recovery timeout keeps
the previous record, so an explicit audit can recover the missed range.

Manual dispatch defaults to `bounded`: a branch scans from its merge-base
with the default branch to the dispatched SHA; a default-branch or already
merged head scans the ancestry window at that SHA. Resolution errors fail.
Only the exact `full-history-audit` input selects full history, including
the root. An audit on another branch does not establish scheduled coverage.

### Blobless CI checkout (FR-58)

The public pack is about 8 GB, nearly all of it old file contents, so CI no
longer downloads it. The Secret scan and Queue commit metadata scan jobs check
out full history without blobs (`filter: blob:none`); the queue job's checkout
is also sparse (`scripts/` and `.python-version`), since it reads no file
content. Only the `full-history-audit` dispatch, which reads every blob anyway,
keeps a full clone. Before TruffleHog runs,
`python3 -m lu_machinery.ci.secret_scan_scope materialize` takes the scan's base
and head (the scope's outputs, or the pull request's base and head that the
action uses on its own), then:

1. resolves every merge base of head and base, because TruffleHog scans
   `head ^merge-base` and go-git may pick any one of them;
2. fetches, in batches of 50,000, the blobs on both sides of every diff in
   that range, or every blob of the history when the scan has no base;
3. re-runs the `git log` TruffleHog runs (same options as
   `gitparse.prepGitArgs` in 3.99.0) with `GIT_NO_LAZY_FETCH=1` and fails on
   any missing object.

Step 3 is required. TruffleHog's default mode clones the checkout with
`git clone file://`, which fails on a partial clone, so both steps pass
`--trust-local-git-config` and scan the checkout in place. In place, a blob that
is missing and cannot be fetched does not fail TruffleHog: it exits 0 having
scanned 0 bytes (reproduced with 3.99.0 against an unreachable promisor). A
full clone skips both steps.

In place, go-git also reads the checkout's own git config. A setting it does not
support is a scan error that, by default, still exits 0 with nothing scanned
(for example `extensions.worktreeConfig`, which `git sparse-checkout set` turns
on; `actions/checkout` sets `core.sparseCheckout` directly and does not). Both
steps therefore also pass `--fail-on-scan-errors`, as the local wrapper does. No
scan errors appeared in the twelve Secret scan and queue jobs checked before this
change, including two 400-commit schedule windows.

Coverage is unchanged. The same base and head give the same chunk and byte
counts as the full clone did: 10 chunks and 21,939 bytes for queue run
40752784461, 45 and 13,905 for PR run 40752258232, and 15,503 and 37,957,893
for the 400-step schedule window of run 40667791948.

The action SHA pins the workflow script only. Both TruffleHog steps set the
action input `version` to one exact release (`3.99.0` as of #10056). Omitting
it pulls `:latest`. Bump both steps together to the newest
`trufflesecurity/trufflehog` release, never to an older tag to clear a finding,
then re-run `scripts/audit/secret_scan_local.py tree`.
`tests/ci/test_trufflehog_scanner_pin.py` rejects a missing pin and `latest`.

## CodeQL baseline alert triage

The first whole-repository Python analysis on `main` at `62f026eb`
reported 22 alerts. The table preserves their original locations. Each
classification follows the SARIF source-to-sink trace, rather than the rule's
severity alone. No alert is dismissed by this change. False positives remain
for the repository owner to adjudicate.

| Alert | Rule | Original location | Disposition and data-flow evidence |
| --- | --- | --- | --- |
| 1 | `py/clear-text-logging-sensitive-data` | `agents_extensions/shared/hooks/guard-secret-print.py:2085` | False positive. `_expanded_secret_var` returns the shell variable's **name**, obtained by `_VAR_REF_RE`, rather than its environment value. Indirect references come from `named`, populated only with identifier-shaped strings. `_echo_secret_reason` embeds that name in the blocking explanation; `_block_msg` writes the explanation. No environment lookup or shell expansion occurs on this flow. |
| 2 | `py/clear-text-logging-sensitive-data` | `lu_machinery/api/subscription_usage.py:2549` | Real, excluded from this change's owned files. Provider `error.message` reaches the warning's `%s` argument without redaction. The traced fixture in `tests/api/test_delegate_api.py` supplies a fake credential/path and asserts that it appears in `caplog`; production provider diagnostics follow the same path. The API response's fixed marker does not protect the log. The subscription-usage owner must remove or sanitize that argument. |
| 3 | `py/clear-text-storage-sensitive-data` | `lu_machinery/dispatcher/store.py:282` | False positive. The SARIF source is `trusted_core_context()` in `test_attempt_network.py:400`. Its public rules block and dependency hashes become the rendered fixture prompt, then `store.create(record, rendered)` writes those bytes. The trace contains no credential value. The production prompt file is created exclusively with mode `0600` under the task directory, created with mode `0700`. This conclusion applies to the reported trace, not to arbitrary future prompt contents. |
| 4 | `py/clear-text-storage-sensitive-data` | `tests/agent_runtime/test_runtime_rules_core_delivery.py:180` | False positive. `trusted_core_context()` returns public rules/dependency metadata. `_admitted` stores a synthetic task record with that metadata and a digest of the fixture prompt under the test task store. No real secret is supplied. The fixture intentionally exercises persisted admission evidence. |
| 5 | `py/clear-text-storage-sensitive-data` | `tests/agent_runtime/test_runtime_rules_core_delivery.py:240` | False positive. The same synthetic record from `_admitted` is deliberately given an all-zero provenance digest and rewritten for the tampering test. The stored bytes are fake admission metadata, not credentials. Removing this write would remove the tampering case. |
| 6 | `py/path-injection` | `lu_machinery/api/session_router.py:89` | False positive. The query agent is normalized against the agent-name grammar, then selects a handoff mapping. Before `is_file`, `_safe_project_path` resolves both root and candidate with `realpath` and requires equality or the root plus a directory separator as prefix. Traversal, absolute escape, sibling-prefix escape and symlink escape are refused. The analyzer does not carry that returned-path barrier to the sink. |
| 7 | `py/path-injection` | `lu_machinery/api/session_router.py:95` | False positive. The same checked path reaches `read_text`; no unchecked query value is opened. The containment barrier described for #6 runs before both sinks. |
| 8 | `py/incomplete-url-substring-sanitization` | `lu_machinery/publish/github.py:1049` | False positive. `gate.normalize_repository` strips only anchored transport prefixes, validates the ASCII hostname and owner/repository components, and returns exactly `host/owner/repository` or `unknown`. The subsequent `startswith("github.com/")` checks that canonical host at position zero with a slash boundary, not a substring in an arbitrary URL. Host suffixes, userinfo and embedded `github.com` paths cannot pass. |
| 9 | `py/incomplete-url-substring-sanitization` | `lu_machinery/publish/github.py:1703` | False positive. `repository(...)` supplies the same normalized identity to the broker-request check. `unknown` and any canonical identity without the exact `github.com/` prefix are refused before its owner/repository becomes the broker payload. |
| 10 | `py/stack-trace-exposure` | `lu_machinery/api/discussions_router.py:130` | Fixed. A caught SQLite error previously reached HTTP JSON through `str(exc)`, exposing such details as SQL or database paths. The response now contains only the stable `broker_query_failed` code. The existing module test covers a database error with a private fixture path. |
| 11 | `py/stack-trace-exposure` | `lu_machinery/ai_agent_bridge/openai_proxy.py:308` | Fixed. `OSError` text reached `_first_error_line`, then `_openai_error` and HTTP JSON. Subprocess failure and timeout stderr used the same disclosure pattern. Responses now retain their status, error type/code and trusted route family, with no exception or stderr text. The unused line-extraction helper is removed. Existing proxy tests cover all three exception classes. |
| 12 | `py/polynomial-redos` | `lu_machinery/session_streams/store.py:385` | False positive. The body is limited to 64 KiB before `_contains_hostname`. `_WINDOWS_NAMESPACE_RE` begins with `(?<![/\\])`, so a slash run can begin a match only at its first character. The separated slash quantifiers have punctuation or fixed namespace words between them, not overlapping repetitions. A failed slash run is consumed/backtracked once; interior starts fail the left boundary immediately. The reported maximum-size slash input completes in a bounded subprocess probe. |
| 13 | `py/polynomial-redos` | `lu_machinery/session_streams/store.py:405` | False positive. The reported percent repetition is in `EMAIL_RE`'s local-part class. Its negative lookbehind excludes that same class, including `%`, so the regex cannot retry the local-part scan at each interior percent. Domain labels cannot contain `%`. The body is also capped at 64 KiB before the search, and the maximum-size reported percent input completes in a bounded subprocess probe. |
| 14 | `py/overly-permissive-file` | `lu_machinery/dispatcher/agy_sandbox.py:523` | False positive. `_write_private` defaults to `0600`; the credential copy uses that default. Explicit `0644` applies to confirmed review-input copies, and `0444` to sandbox identity/plan files. `run` first creates a fresh `0700` scratch root; input copies have an additional `0700` parent. Only the intended reviewer sees the explicitly mounted files. Other host users cannot traverse the private root. Making mount-readable non-credential inputs private is unnecessary to protect the token. |
| 15 | `py/overly-permissive-file` | `lu_machinery/review/repair_round.py:464` | Fixed. Initial repair-admission receipts used `0644`; atomic replacement also created a temporary file with default permissions. Both paths now create exclusive `0600` files before writing and syncing, and replacement preserves that private mode. The replacement uses a unique temporary name and removes it on failure, so a stale PID-named file cannot block a retry. The existing recorder test checks admission, failed replacement, retry and consumption under umask zero. |
| 16 | `py/overly-permissive-file` | `tests/agent_runtime/test_acpx_adapter.py:2904` | False positive. `watching_open` is a pass-through spy on `os.open`, retaining the built-in default argument. The exercised launcher/lease creation supplies explicit private modes. The test verifies the shared lease already exists before launcher creation and asserts the resulting lease is `0600`; it does not create a production world-writable file. |
| 17 | `py/overly-permissive-file` | `tests/orchestration/test_dispatch_isolation.py:526` | False positive. The `0644` descriptor is a fake launcher's stderr fixture under `tmp_path`. This test proves one fallback to `Popen` after a fake systemd launch fails. No credential or production log is written. |
| 18 | `py/overly-permissive-file` | `tests/orchestration/test_dispatch_isolation.py:1000` | False positive. The test opens its temporary fake stderr log, prefilled with a public sentinel and repeated `x` bytes, to prove the fallback reads only a bounded new excerpt. No real secret or production log reaches the descriptor. |
| 19 | `py/log-injection` | `lu_machinery/research/registry.py:561` | False positive. The query role reaches a `%r` logging slot, which escapes CR/LF in a string representation. The `%s` slot contains only dropped IDs from semantically validated registry records. A hostile role cannot inject a physical log line through this flow. |
| 20 | `py/log-injection` | `lu_machinery/research/registry.py:661` | False positive. The query ID passes `_slug_ok` using `fullmatch`, then a validated-record lookup, before the over-budget warning. The slug grammar admits no CR/LF, and the warning also renders it with `%r`. Byte counts are integers. |
| 21 | `py/log-injection` | `lu_machinery/research/registry.py:676` | False positive. The same full slug validation and validated-record lookup precede the drift warning, whose ID uses `%r`. The query cannot supply a log separator. |
| 22 | `py/log-injection` | `lu_machinery/orchestration/task_record_store.py:57` | Fixed. `_report_foreign` previously interpolated a foreign filename with `%s`, allowing a local sidecar name with CR/LF to forge log lines. It now logs the string path with `%r`. The API task-ID trace already rejects such control characters, but the general filesystem scanner can encounter foreign filenames. The existing task-record tests cover that reachable scanner case. |

The runtime changes also have product copies at
`scripts/api/discussions_router.py`, `scripts/ai_agent_bridge/openai_proxy.py`,
`scripts/orchestration/task_record_store.py`, and
`scripts/review/repair_round.py`. Those copies are outside this change; their
owner must reconcile them separately.

The fixes follow the upstream guidance for
[log injection](https://codeql.github.com/codeql-query-help/python/py-log-injection/),
[exception exposure](https://codeql.github.com/codeql-query-help/python/py-stack-trace-exposure/),
and [file permissions](https://codeql.github.com/codeql-query-help/python/py-overly-permissive-file/).

### Scanned code-scanning dismissal

An interactive seat can use the closed publisher verb after a merged PR has
reviewed the alert. Workers and reviewers are refused before any transport,
including direct Python publisher calls. The worker broker, reviewer relay
and raw `gh api` write guard do not admit this verb.

```sh
.venv/bin/python -m lu_machinery.publish code-scanning-dismiss \
  --repo OWNER/REPO --number 1 --reason 'false positive' \
  --comment 'Alert #1 logs variable names rather than values. https://github.com/OWNER/REPO/pull/468'
```

The comment must name `Alert #N`, explain that alert, and include exactly one
canonical same-repository merged PR link. Its final text passes the common
OPSEC scanner and verdict-marker guard, then the same text is frozen into the
request. Supported reasons are `false positive`, `won't fix` and `used in tests`.
Issue-close reasons are separate.

The linked PR must change this runbook and contain a four-column triage row
like the table above. The first three cells identify the alert number, rule ID
and `path:line`. The final cell contains the written rationale, starting with
`False positive.` or `Won't fix.` as appropriate. `used in tests` requires a
false-positive row whose location is under `tests/`.

Before sending, the publisher retrieves that file at the PR's merge SHA,
checks the alert and every open branch instance, and compares their SARIF
rule, location, messages and flow evidence with the analysis at the triage
merge. Analysis-local indexes are resolved before comparison. Missing analysis,
unknown evidence or drift refuses the write. If a later change moves a location
or changes a flow, the alert needs fresh reviewed triage. A final preflight
re-read rejects evidence that changed during validation.

The only mutation is one PATCH of the numbered alert with `state=dismissed`,
its reason and the scanned comment. The publisher reports success only after
reading back all three values. A pending dismissal request does not pass.
Exit 3 means `UNKNOWN`: the write may have landed, or its read-back failed or
mismatched. Inspect GitHub before any further action; there is no automatic
retry. No alert is dismissed by adding this command.

The endpoint and read-back fields follow the
[GitHub REST code-scanning API](https://docs.github.com/en/rest/code-scanning/code-scanning#update-a-code-scanning-alert).
SARIF evidence comes from its documented `application/sarif+json` analysis
representation. The publisher does not request additional credential permissions.

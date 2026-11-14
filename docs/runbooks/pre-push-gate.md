# Pre-push gate

`.githooks/pre-push` is the tracked hook a push runs. `lu_machinery/install_git_hooks.sh` installs a delegator that execs the copy in the current worktree. The agent git shim (`lu_machinery/agent_runtime/shims/git`) pins `core.hooksPath` to `lu_machinery/opsec/push_hooks` for `git push`, so Git runs that directory's `pre-push`. That hook execs `lu_machinery.opsec.git_push`. After a clean scan, `lu_machinery/opsec/push_hooks/chain` runs the caller's own pre-push hook with the same arguments and input.

## What the hook runs

The hook saves Git's ref updates, then:

1. When Git passed the remote name and URL, it requires a readable `lu_machinery/opsec/git_push.py`. A missing file prints `OPSEC: push scanner unavailable; push refused.` and exits 1. Otherwise it runs `.githooks/pre_push_gate.py --push-messages`, which calls `git_push.main` and does not chain another hook. If Git's hooks directory is already the scanner's `push_hooks` directory, that entry returns 0 and does not scan a second time. This fixed metadata lookup uses a separate `git_push.Repository` instance with `pin_hooks=False` to observe the push's inherited configuration. Ordinary scanner reads retain the default `core.hooksPath=/dev/null` pin.
2. `check_owned_paths push` (`lu_machinery/dispatcher/ownership.py`) when `LEARN_UKRAINIAN_DISPATCH_TASK_ID` is set and that module is present. A dispatched write task whose pushed branch commits touch a path outside its owned paths exits 1. A read-only task, a task with no owned paths, or a check that cannot run exits 0. Settlement still checks the delivery.
3. `.githooks/check-pytest-stamp.py`. It does not run pytest. An update of `refs/heads/main` whose diff touches `tests/`, `lu_machinery/`, `scripts/`, `curriculum/`, `agents_extensions/shared/rules/` or `.dagger/` needs a pytest stamp for the local branch whose age is at most 600 seconds (`MARKER_MAX_AGE_SECONDS` in `.githooks/pytest_stamp.py`). `SKIP_PYTEST_HOOK=1` skips the check. A missing, mismatched or older stamp exits 1.
4. When Git passed the remote name and URL, `git-lfs` must be on `PATH` (exit 2 when it is not) and `git lfs pre-push` runs on the same updates.

With no arguments the hook skips the scanner and Git LFS, and still runs the owned-path check and the stamp check. No push path runs pytest. A green push is not evidence that the suite passed.

The scanner reads remote ref names, annotated tag names and messages (nested tags included) and the raw messages of commits reachable from the sent ids, including mergetag text. It does not read file contents. A deletion publishes no text. Commits reachable from the verified head of the destination's registered public default branch are left out of the scan. A destination is left unscanned only when the catalogue marks it private and Git reaches it on the trusted transport route; every other destination is scanned as public. A malformed update, a graft, a shallow boundary inside the scanned range, `GIT_SHALLOW_FILE` in the environment, a scanner error or the deadline refuses the push (exit 1). The deadline is 300 seconds. `LU_OPSEC_PUSH_SCAN_TIMEOUT` replaces it and must be a positive finite number; any other value refuses the push. `LU_OPSEC_REAL_GIT`, when it is an absolute executable path, is the git binary the scan and `chain` use. `LU_OPSEC_OVERRIDE`, set for that command only, is offered to the private override authority after a finding. `chain` unsets it before the caller's hook. A finding prints `OPSEC blocked:` with the rule, class, field and line, and does not print the matched text.

## What a push does not run

The hook does not run `pre-commit`, `lint_opsec_leaks.py --commit-range` or `check-no-internal-ids`. This repository has no `.pre-commit-config.yaml`. The hook does not call `.githooks/pre_push_gate.py` `main()`. That entry still holds admission, the stage and run budgets, the concurrent scope, receipts, measurements and the Entire-ref refusal, and its product stage always raises `validation_incomplete` with the detail `this repository has no product push stage; the publication scanner is the push scan`. The same uncalled entry still reads `LU_PRE_PUSH_GATE_ADMISSION_WAIT_S`, `LU_PRE_PUSH_GATE_STAGE_BUDGET_S`, `LU_PRE_PUSH_GATE_RUN_BUDGET_S`, `LU_PRE_PUSH_GATE_MAX_CONCURRENT_RUNS` and `LU_PRE_PUSH_GATE_RUN_TOKEN`. None of them apply to a push.

## Bypass

`git push --no-verify` tells Git to skip hooks. The agent git shim refuses that option, and any argument whose name is at least three characters and a prefix of `--no-verify`, before Git runs (`OPSEC: --no-verify would skip the push scan; push refused.`, exit 2). It also refuses a push when `GIT_CONFIG` is set, when `--shallow-file` is passed, or when the scanner hooks, `git_push.py` or the project interpreter cannot be used. Those refusals exit 2.

## Merge-main rule

Merge `origin/main` into a worker branch only when `git merge-tree` against a freshly fetched main SHA reports a conflict, when the driver disposes on base/head/combined-tree evidence that the branch is incompatible with the base, or on driver order. A merge-tree error is unknown, not a conflict. Every new head needs renewed approval and CI; merge-group CI stays the authority for semantic combinations.

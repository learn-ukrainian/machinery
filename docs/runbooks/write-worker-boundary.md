# Write-worker boundary (#423)

Every dispatched `workspace-write` worker runs inside an operating-system boundary in which the
primary checkout is read-only. The shell-command write guard stays as an accident tripwire
(decision GUARD-TRIPWIRE-1113); containment is the boundary's job (decision CONTAIN-OS-1113).

## Routes

`worker.supervise` launches every write route through `lu_machinery/dispatcher/write_boundary.py`:
`claude`, `codex`, `cursor`, `agy` and `opencode` (both its direct and OpenRouter upstreams). `muse`
refuses `workspace-write` before launch. There is no unsandboxed fallback: a host without bubblewrap,
a setup error or a failed canary refuses the run with exit 125 and the typed error
`write_boundary_refused: <reason>`.

## Read-only review dispatches

Read-only native Claude, Codex, Cursor and OpenCode also run through `write_boundary.launcher`. The worktree and Git metadata
are read-only mounts, with no branch import. Codex uses these mounts
as its filesystem sandbox: its inner sandbox cannot start under `--disable-userns`. Its dispatch keeps
the private scratch lease. Other native providers keep their read-only permission profiles.
Provider credential files retain writable binds so rotating provider logins persist; GitHub credentials remain masked.
Muse retains its existing self-contained filesystem sandbox.
Read-only AGY retains its existing filesystem boundary and uses
the same GitHub domain and address refusal rules, without nesting two boundaries.

Both paths withhold the host's GitHub login and refuse direct GitHub egress. The existing read-only
`gh` relay admits `pr view`, `pr diff`, `pr checks`, `issue view` and `run view`; arbitrary `gh api`
requests, including repository permission queries, are refused. Named reads also use `github-read`.
The host login reads only a registered repository with a `public-` role in `config/fleet_repos.yaml`
(decision A6): the relay resolves the repository on the host (`--repo`/`-R`, else the worktree's
`origin`) and refuses a private, unknown or unresolved one before `gh` runs; the argv it runs names that
repository first, so `gh` never chooses one itself. A bare `--` is refused.
The broker binds authority to the launch's mode and refuses every other verb for a read-only seat,
including `push`, `pr-create`, `pr-edit`, `pr-comment`, `pr-merge` and `record-verdict`.

Review-loop records the finished review from the owning worker through its existing `record-verdict`
path. The reviewer supplies its structured result and cannot publish a verdict or act for a child task.
The boundary receipt includes the launch mode and GitHub read-relay outcomes. Setup or canary failure
refuses the run, with no uncontained fallback.

The supervisor sets the existing broker marker in Claude's trusted launch environment before every
provider build, including correction launches. Read-only Claude strips any inherited per-shell
prefix on every build, marked or not, because that prefix is gone. Every such dispatch still goes
through the mandatory outer launcher; setup or canary failure prevents provider start. Interactive
sessions remain outside this boundary.

## What the write boundary is

One bubblewrap invocation per launch, built in full by the launcher:

- `--unshare-all --unshare-user --disable-userns --die-with-parent --new-session --cap-drop ALL`:
  private PID, IPC, UTS, cgroup, user and network namespaces, no capabilities, no nested user
  namespace, no controlling terminal.
- `--ro-bind / /`: the whole host file system is read-only. `/tmp`, `/run` and `/dev/shm` are private
  tmpfs, so host sockets there (user bus, systemd, SSH and GPG agents, tmux) do not exist inside.
  Declared shared resource directories under those roots are restored read-only; their siblings stay hidden.
  Every listening host Unix socket exposed by those mounts, or elsewhere, found in `/proc/net/unix` at launch is covered with
  `/dev/null`. `/dev` and `/proc` are fresh.
- HOME is an overlay whose lower layer is empty (decision A4): no file of the real HOME is visible
  unless it is restored below. What the CLIs write lands in a private upper directory that is deleted
  after the run. The lower layer holds only the directories of the restored paths, so the overlay
  refuses to rename them (`EXDEV`) and no worker moves a restored path's parent aside to plant files.
  Restored read-only: the toolchain (`~/.local/bin`, `~/.local/lib`, `~/.local/share/claude`,
  `~/.local/share/cursor-agent`, `~/.local/share/uv/python`, `~/.local/share/uv/tools`, every `PATH`
  directory under HOME), the launcher's own interpreter, virtual environment and package, and the
  public checkouts below. The selected provider's credential file (Claude `.credentials.json`, Codex
  `auth.json`, the AGY token, OpenCode `auth.json`) is a writable bind, so a token refresh is kept. The
  selected Claude login, wherever it lives, is a private directory of the run's scratch bound at its
  resolved path. It holds copies of the state its launch guard binds (#10619), which the CLI may
  rewrite there: `settings.json`, the legacy `.config.json`, the policy caches, and `.claude.json`
  reduced to its `env` map and account and organisation ids. The selected credential is bound on top.
  An existing Claude profile store refuses the run, because the CLI would prefer it to the login. An API-key login's key helper must be `cat <absolute file>`: that file is its
  selected credential and is restored read-only, and any other helper form refuses the run. Cursor's own
  login file is a writable bind like the others. Every selected path is bound where it resolves, and a
  HOME named through a link below an emptied root stays a link to the overlay. No other provider
  configuration is restored: Codex runs on its task-local
  `CODEX_HOME`, and no other account's or provider's login, no transcript, history or plugin and no
  private folder of HOME exists inside.
- The data roots outside HOME are empty read-only tmpfs mounts: `/home` (other users), `/root`,
  `/srv`, `/mnt`, `/media`, `/opt` and `/var/tmp`, which holds every task's scratch. Each
  `/opt/<package>` that an executable on the provider's `PATH` resolves into (a Node install, for
  example) is restored read-only.
- The primary checkout is a read-only bind. Its `.git` is an overlay with a private upper directory,
  so commits, fetches and pushes work, while the host's objects, refs and metadata never change. The
  primary's `HEAD`, `index`, `config`, `packed-refs`, `hooks` and its checked-out branch ref are also
  read-only binds inside, and so is the worktree's `.git` link file.
- Public checkouts: the primary and the product checkout are read-only binds, so a write there fails
  instead of vanishing with the overlay. Their private runtime state is emptied inside: `batch_state`,
  `.agent`, `.secrets`, `.runtime`, `.deploy-state`, `.pids`, `.codex`, `.gemini`, `.mcp`, `.env`,
  `.worktrees` (other tasks' worktrees), `logs`, `docs/session-state`, `data/telemetry` and the Fleet
  databases. A checkout's `.claude` settings stay: Claude Code loads them and its launch guard binds them. So are the task store (`LU_TASKS_DIR`), the Fleet
  Comms plane (`FLEET_COMMS_ROOT`, resolved as Fleet resolves it, `~` and links included) and the
  scratch root, wherever they resolve. A relative `FLEET_COMMS_ROOT` refuses the run, because its plane
  depends on each reader's working directory. Restored on top: the run's own
  worktree and scratch and the task's own records, `<task>.rules-core`, `<task>.launch-prompt`,
  `<task>.inputs` (inputs a driver stages for the worker) and Codex's `.codex-schemas/<task>.json`.
  Claude's and Cursor's file tools get the same grants (`--add-dir`): the shared skills and
  `<task>.inputs`.
- Writable: the task worktree, a private `TMPDIR` (also `LU_SCRATCH_ROOT` and `XDG_RUNTIME_DIR`) and
  the declared outputs (the final-message file and AGY's write log).
- Network: only the CONNECT-only egress proxy of the formal review attempts (`attempt_network`),
  reached through the in-namespace forwarder. Any public host on port 443 except GitHub: its names
  (`github.com`, every name below it, and `ghcr.io`) and every name that resolves into the address
  ranges GitHub publishes for its web, API, Git and package hosts (read with the host login at
  launch; the launch refuses when they cannot be read). Downloads served from those ranges are
  unavailable inside. Loopback, private and host addresses are refused, so no host service (Monitor
  API, Sources, SSH, databases) is reachable.
  `NODE_USE_ENV_PROXY=1` makes Node's own `fetch` use the proxy.
- No GitHub credential: the provider's environment carries no GitHub credential variable, and the
  host login's known credential stores read empty inside.

## Canary and receipt

The projected canary (`agy_canary.py`) is the sandbox's first process. It checks: writes to a tracked
primary file and to absent primary names fail with `EROFS`. The tracked file is the first one the host
user owns and may write, because the kernel checks file permissions before the read-only bind and a
read-only file would give `EACCES` whatever the mount; a primary with no such file refuses the run; each protected Git file is read-only and
cannot be renamed or unlinked (`EBUSY`); the worktree `.git` link is read-only; the worktree, the Git
overlay, HOME's overlay and `TMPDIR` take a create-write-rename-delete cycle; declared outputs open for
writing; loopback ports 22, 8765 and 8766 refuse; every masked socket refuses. For the view it checks
that the other providers' logins and every hidden entry found by walking HOME, each emptied private
directory and the data roots through the restored paths' parents are absent (`ENOENT`, at most 1000
paths, existence only), that each emptied private file reads nothing, that emptied directories refuse
writes, that the selected login and the task's own records are present and that the Sources
interpreter's top directory below HOME or an emptied root cannot be renamed. The launcher refuses a
Sources interpreter installed below `/tmp`, `/run` or `/dev/shm`, which stay writable inside. The
launcher compares every outcome before it lets the provider start.

The receipt `<task>.write-boundary.json` beside the task record holds the bwrap path, the argv digest,
every canary outcome, the egress destinations, the provider's exit status and the import result.
`write_boundary.verify` fails the run unless the receipt shows `ran`, a passed canary and an import
status of `unchanged` or `imported`.

## Branch import

After the sandbox exits (or on cancel and timeout, when the worker sends the launcher `SIGTERM`), the
launcher reads the dispatched branch's tip from the Git overlay's upper directory. When it moved, the
host fetches it from a private bare repository whose alternates are the upper objects and the primary
objects; `index-pack` hashes every object it accepts. A compare-and-swap `update-ref` then moves the
branch and the worktree index is reset to it. Host Git runs with no global or system configuration,
`core.hooksPath=/dev/null` and `core.fsmonitor=false`.

Everything else the worker wrote into Git metadata is dropped: other local branches, tags,
remote-tracking refs (fetch them again), stashes and per-worktree configuration. A worker that moved its
worktree off the dispatched branch is recorded as `head_moved`.

## Host Git and worker hooks

Host publication never runs hooks from a worker tree. Settlement and the broker use
`write_boundary._git`, which pins `core.hooksPath=/dev/null` and `core.fsmonitor=false`.
Before a push, that transport runs `opsec.git_push.main` explicitly on one immutable
object ID, destination ref and expected remote ID. The scanner's caller-hook chain is
disabled. The same IDs form the push refspec and lease; automatic tag following and
recursive submodule pushes are disabled. Scan failure or an unexpected push shape
refuses publication. Hook suppression also covers remote-tracking ref updates.

Settlement requires the named branch to be the worktree's current branch, one origin
push URL, and a fast-forward update. After publication it records the tracking ref and
upstream with hooks disabled. Cleanup deletions use the same transport and an exact
lease, with the scanner's native deletion record. No caller hook runs after a clean scan.
Registered GitHub SSH origins resolve through the broker's existing `_remote` resolver
to scoped HTTPS publication; the clone's configured origin is unchanged.
The worker shim's hook chaining remains inside the worker's credential-free boundary.

The host push/fetch inventory is reproducible with
`rg -n '"(push|fetch)"' lu_machinery --glob '*.py'` and inspection of each runner and cwd:

| Call sites | Execution contract |
| --- | --- |
| `orchestration/dispatch_settle.push_and_maybe_open_pr`; `dispatcher/write_boundary._Broker._push` | Shared hook-disabled transport and explicit exact-update scan. |
| `dispatcher/write_boundary._Broker._fetch_base`; `_Boundary.fetch_objects` | Existing `_git` transport disables hooks, including local imports. |
| `hygiene/branch_sweep._git`; `orchestration/scheduled_worktree_cleanup._run_git` | These accept worker checkouts. Fetches disable hooks; leased deletion pushes use the shared scanner and transport. |
| `ai_agent_bridge/_agy.list_commit_changed_paths` and `list_branch_changed_paths` | Both fetch invocations pin `core.hooksPath=/dev/null`. |
| `review/target_resolution._ensure_commit_available`; `review/record_cf_verdict.default_branch_review_base` | Their Git runners pin `core.hooksPath=/dev/null`. |
| `ci/check_large_files.prefetch_missing_blobs`; `ci/data_tier.prepare_test_worktree` | Fetches in the supplied or newly created worker checkout disable hooks. |
| `orchestration/merge_closeout._fetch_live_pr_head`; `orchestration/orchestrator_control`; `orchestration/task_family/git_safety` | Existing `execution_safe_git` runner disables hooks. Closeout also resolves the primary first. |
| `dispatcher/git._verification_git`, including salvage | Existing hook-disabled verification transport. Salvage explicitly scans the exact outgoing commit and file contents before publication. Verification fetches use private bare repositories. |
| `orchestration/execution_safe_git._isolated_remote`; `orchestration/safe_git_context.SafeGitContext` | Runner-owned Git configuration and bare repositories; execution-safe Git disables hooks. Source configuration and hooks are not imported. |
| `ai_agent_bridge/_review_worktree._fetch_exact_ref` | Provisioning fetches use a fresh neutral bare repository, separate from the source worktree. |
| `fleet/sibling_git.GitSession.fetch` | Network operations reject linked worktrees and use a runner-owned hooks directory. |
| `guardrails/assert_primary_on_main`; `wt.sh` | Fetch paths select the primary checkout. The guard returns before fetching when called in a linked worktree. |
| `ci/queue_review_gate`; `ci/check_agent_attribution`; `common/flake_quarantine`; `hygiene/remote_branch_cleanup` | Hosted CI paths, outside host worker settlement. Remote cleanup additionally requires the trusted current default-branch revision. |

Paths in the table are relative to `lu_machinery/`. CI-only paths must not be reused
as credentialed host operations in worker trees without the same hook suppression.
The settlement regression in `tests/opsec/test_git_push_scan.py` uses local bare remotes
and worker-authored `pre-push` and `reference-transaction` marker hooks. It verifies both
file and SSH-form origins, new and existing branch updates, and that a secret-bearing commit message is refused
without changing the remote or executing either hook.

## Host broker (decision CONTAIN-BROKER-1113)

The task store, the Fleet Comms plane and the primary's Git state stay read-only inside. The host steps
of a worker's chain go through one launcher-owned Unix socket, bound inside at
`/run/lu-write-boundary/broker.sock` and named by `LU_WRITE_BOUNDARY_BROKER`. The existing commands
forward to it on their own when that variable is set, so a worker runs the usual chain:

| Command inside | Verb | What the host does |
| --- | --- | --- |
| `dispatch.py dispatch --mode read-only ...` | `review-dispatch` | Starts the review in its own normal sandbox |
| `record_cf_verdict.py --task-id T --pr N` | `record-verdict` | Records the verdict under the host's PR lock |
| `fleet_comms channel publish ...` | `fleet-publish` | Appends the message with the task id as sender |
| `write_boundary.broker_call("closeout-request", {"pr": N})` | `closeout-request` | Runs `merge_closeout --apply` after exit |

Every verb has a fixed schema, builds its own argv (never a shell) and takes its identities from the
launch, never from the request:

- **Peer.** A connection is served only when the peer process descends from this run's bubblewrap.
  Another sandbox, or a host process, cannot act as this task even when it can see the socket file.
- **Task identity.** The review task id must be `<task>-review-<suffix>`. The Fleet sender is the task
  id, the source is `write-worker`, and the idempotency key is prefixed with the task id.
- **Exact head.** `review-dispatch` checks on GitHub that the PR is open in the primary's own
  repository and that its head branch is the dispatched branch. The pinned head must equal both the PR's
  live head and the worker's branch tip. The host then copies that commit from the private Git layer
  (`index-pack` hashes every object) and dispatches with `--mode read-only --worktree`, the launch's
  model as `--review-author-model` and `--review-mode cross_family`. A request field for the author is
  refused.
- **Replay.** Each request id is accepted once per run. The dispatcher refuses an existing task id.
- **Paths.** No request names a host path. The prompt arrives as text and the host writes it to the
  run's private scratch.
- **Not relayed.** Red-team reviews, write dispatches, other repositories, and every other host
  operation.
- **Closeout.** `closeout-request` checks that the PR is merged and that its head was the dispatched
  branch. The launcher runs `merge_closeout <pr> --repo <owner/name> --repo-root <primary> --apply`
  after the sandbox exits and after the branch import. Its exit status is in the receipt (`closeout`).

The receipt lists every broker request (`broker`: verb, request id, return code and refusal).

## GitHub publication (decision PUBLISH-CRED-1113, #434)

A write worker holds no GitHub credential and has no route to GitHub. It publishes through broker
verbs instead; the host's own login does the write. The commands the skills name forward to them on
their own (#443), with the same arguments:

| Command inside | Verb |
| --- | --- |
| `git push` to a github.com URL (`origin`, or the URL itself) | `push`, once per ref update |
| publisher `pr-create --title --body-file --base --head [--draft]` | `pr-create` |
| publisher `pr-edit --number --title/--body-file` | `pr-edit` |
| publisher `pr-comment --number --body-file` | `pr-comment` |
| publisher `pr-merge --number --match-head` | `pr-merge` |

The launcher sets `url.lu-write-boundary::https://github.com/.pushInsteadOf` for GitHub's push URLs
and defines the Git alias `remote-lu-write-boundary`, so Git hands the push to its documented
remote-helper protocol (gitremote-helpers(7)) in `write_boundary`; no file, `PATH` entry or other tool
is added. The helper reports the remote-tracking refs as the remote's refs, so Git's own
fast-forward and lease checks still run; the broker then checks the live remote branch. The
client-side push scan (`opsec.git_push`, Git's pre-push hook) cannot read the public history inside,
so for the helper URL alone it defers to the broker, whose `push` runs the same scanner on the exact
update on the host before anything is sent; every other URL is scanned as before. The
publisher (`lu_machinery.publish`) forwards the four PR verbs when `LU_WRITE_BOUNDARY_BROKER` is set,
reading `--body-file` (or `-` for stdin) itself. Each forwarded request names the target its caller
gave: the branch, the head and base names, and the repository from `--repo` or `GH_REPO` when given.
The broker refuses a named target that is not the launch's own; it never replaces it.

The verbs can also be sent directly with
`python -m lu_machinery.dispatcher.write_boundary call <verb>` and the payload as one JSON object on
stdin.

| Verb | Payload | What the host does |
| --- | --- | --- |
| `push` | `head`, optional `branch`, `repo`, `lease` | Pushes the dispatched branch at `head`, which must be the worker's tip |
| `fetch-base` | none | Fetches the default branch into `refs/remotes/origin/<base>` |
| `pr-create` | `title`, `body`, optional `draft`, `head`, `base`, `repo` | Opens a PR from the dispatched branch to the default branch |
| `pr-edit` | `pr`, `title` and/or `body`, optional `repo` | Edits the title or body of the worker's own open PR |
| `pr-comment` | `pr`, `body`, optional `repo` | Comments on the worker's own open or merged PR |
| `pr-merge` | `pr`, `head`, optional `repo` | Enqueues the worker's own PR at `head` |
| `github-read` | `name`, optional `number` or `sha` | One named read of the repository |

What every publication verb takes from the launch, never from the request: the repository (the
primary's registered GitHub origin), the branch, the base (the repository's default branch on
GitHub) and PR ownership (the PR's head branch is the dispatched branch, in the same repository).
No verb takes an argv, endpoint, refspec or host path. The optional `branch`, `head`, `base` and
`repo` fields only narrow a request: each must equal the launch's own value (a branch may be given as
`refs/heads/<name>`), or the request is refused. `lease` is the remote branch's expected commit
(empty for an absent branch), as `--force-with-lease` gives it; a different live value is refused.
Texts arrive as JSON strings, and the host writes them to the run's private scratch.

- **Push.** Only the dispatched branch, only at the worker's current tip, and only as a
  fast-forward of the remote branch. The host copies the commits from the private Git layer
  (`index-pack` hashes every object) and runs the push scan (`opsec.git_push`) on the exact update.
  It then pushes with `--force-with-lease` on the remote value it read. Host Git runs with no
  global or system configuration and no hooks; its only credential is the registry-scoped helper
  of `git_credentials`. It works at any time while the worker runs, so review can follow.
- **Texts.** `pr-create`, `pr-edit` and `pr-comment` call the scanned publisher
  (`lu_machinery.publish`), so the public-content scan checks the exact text published, and a
  review-verdict marker in a comment is refused.
- **Merge.** `pr-merge` requires the PR's live head, the worker's tip and `head` to be the same
  commit. It also requires a host-held approval of that commit in the task store: a review task
  `<task>-review-<suffix>` that this broker dispatched (its `initiator` is the task and its author
  model is the launch's), that is done, cross-family, with a sealed reply that approves exactly
  that commit. A worker cannot write the task store, and its comments cannot carry a marker. The
  publisher's own gates then run: the recorder's exact-head marker, the same-head green CI Gate,
  `--match-head`, and merge-queue admission. The merge group's CI decides the landing. A new head
  needs a new approval.
- **Reads.** `github-read` names one of the publisher's fixed GET templates (`pull`, `pr-files`,
  `commits`, `commit`, `comments`, `reviews`, `timeline`, `issue`, `checks`, `check-annotations`,
  `run`, `jobs`). It reads only the primary's own repository and never security alerts.

Every GitHub verb acts only on a primary whose origin is a registered repository with a `public-` role
(decision A6). A worker reaches the public internet, so the host login neither reads nor publishes for it
in a private, unknown or unresolved repository; such a primary's verbs are refused.

Sources goes through the read-only AGY boundary's `SourcesRelay` (read-only tools only). The launcher
binds its own directory read-only at `/run/lu-write-boundary`, so its byte proxy and the relay socket sit at
fixed paths, `/run/lu-write-boundary/sources-stdio.py` and `/run/lu-write-boundary/sources.sock`, and the
MCP stdio command (`write_boundary.sources_command()`) is a constant: the dispatcher's interpreter with `-I`
and those two paths. That directory is a mount point, so a worker cannot rename it away and put its own
proxy or socket in its place; when the interpreter's installation lies under HOME, it is bound read-only
too. The canary checks both before the provider starts. Claude names it in its explicit
`--mcp-config` with `--strict-mcp-config`; no product or worktree `.mcp.json` is read. Codex runs on a
task-local `CODEX_HOME` the launcher projects into the run's private scratch, beside its TMPDIR (Codex
creates no helper `PATH` aliases below its temporary directory), and removes with it. It holds a
`config.toml` with only that server and its startup policy, plus a link to the selected account's `auth.json`, so no layer of the
real Codex configuration reaches the run. The checkout's untrusted `.codex/` layer is disabled too.
Dispatched Codex therefore omits their hooks, agents settings and context-window overrides, as well as
MCP servers, connectors and loopback Sources URLs. Codex uses its model defaults. `mcp_optional_startup_grace_ms = 0` waits for the optional server to
finish startup or reach its existing timeout before the first model turn. The default one-second grace
can omit the still-starting relay's tools from that turn. Sources stays optional if startup fails. Its argv enables only the Sources readers and passes `--disable apps` in both modes. The tools that persist
fetched entries stay refused by the relay. The receipt's `sources` holds the relay's call counts, or why
it was unavailable, and `codex_home` the config digest and whether the login was linked. Sources is
optional: without the relay the server fails to start and the run goes on.

## Known limits

- The Monitor API is a host loopback service and stays unreachable. `closeout_cli resolve-reviewer`
  inside falls back to its no-snapshot route, and the host dispatcher's own admission checks the pick.
- Only native Claude and Codex name the Sources stdio command. Another provider whose `sources` server
  points at the loopback address cannot reach it inside.
- Markers posted with the shared GitHub login remain forgeable by a session outside a write boundary
  (an interactive session) that holds the login. Inside, the worker has neither the login nor a route
  to GitHub, and `pr-merge` also checks the host-held review record.
- Only `git push` to GitHub and the publisher's `pr-create`, `pr-edit`, `pr-comment` and `pr-merge`
  forward. `gh`, `git fetch` from GitHub (use `fetch-base`), other publisher verbs and reads (use
  `github-read`) fail inside. A forwarded `pr-edit` carries only the title and body (`--add-labels`
  and `--milestone` are refused before sending), `pr-merge` needs `--match-head`, and a push that
  deletes a branch or rewrites the published one is refused.
- The product's tracked `scripts.publish` copy forwards only once it runs this package's publisher.
- Fleet messages can be sent but not received or acknowledged inside.
- Decision A4 changes what a worker sees. It no longer reads the real provider configuration beyond the
  selected login's guard-bound state (no plugins, session history or other account), other tasks' records (stage a brief's inputs in
  `<task>.inputs`), private data roots such as `LU_SENSE_PROOF_DIR`, or driver files under `/var/tmp`.
  A worker in a private repository can still work in its checkout, but every GitHub verb refuses it
  (decision A6, with no per-task exception).
- The emptied checkout state is a named list, not every ignored path: an ignored private file a
  checkout keeps outside those names stays readable. Read-only AGY keeps its own boundary
  (`agy_sandbox`), which still binds the whole task store.
- A Unix socket that a host service first creates after launch, outside `/tmp`, `/run` and `/dev/shm`,
  is not masked. No such service is known on the fleet host.
- Unpushed commits of a run whose launcher is killed with `SIGKILL` (not `SIGTERM`) stay in the
  discarded overlay; the worktree files remain.

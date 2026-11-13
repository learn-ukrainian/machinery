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

## What the boundary is

One bubblewrap invocation per launch, built in full by the launcher:

- `--unshare-all --unshare-user --disable-userns --die-with-parent --new-session --cap-drop ALL`:
  private PID, IPC, UTS, cgroup, user and network namespaces, no capabilities, no nested user
  namespace, no controlling terminal.
- `--ro-bind / /`: the whole host file system is read-only. `/tmp`, `/run` and `/dev/shm` are private
  tmpfs, so host sockets there (user bus, systemd, SSH and GPG agents, tmux) do not exist inside.
  Every other listening host Unix socket path found in `/proc/net/unix` at launch is covered with
  `/dev/null`. `/dev` and `/proc` are fresh.
- HOME is an overlay. The CLIs see their configuration and state; what they write lands in a private
  upper directory that is deleted after the run. The provider's credential file (Claude
  `.credentials.json`, Codex `auth.json`, the AGY token, OpenCode `auth.json`) is a writable bind, so
  a token refresh is not lost.
- The primary checkout is a read-only bind. Its `.git` is an overlay with a private upper directory,
  so commits, fetches and pushes work, while the host's objects, refs and metadata never change. The
  primary's `HEAD`, `index`, `config`, `packed-refs`, `hooks` and its checked-out branch ref are also
  read-only binds inside, and so is the worktree's `.git` link file.
- Shared state under HOME is read-only rather than overlaid, so a write there fails instead of
  vanishing with the overlay: the product checkout, the task store (`LU_TASKS_DIR`) and the Fleet
  Comms plane (`FLEET_COMMS_ROOT`).
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
primary file and to absent primary names fail with `EROFS`; each protected Git file is read-only and
cannot be renamed or unlinked (`EBUSY`); the worktree `.git` link is read-only; the worktree, the Git
overlay, HOME's overlay and `TMPDIR` take a create-write-rename-delete cycle; declared outputs open for
writing; loopback ports 22, 8765 and 8766 refuse; every masked socket refuses. The launcher compares
every outcome before it lets the provider start.

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

A write worker holds no GitHub credential and has no route to GitHub, so `git push`, `gh` and the
publisher fail inside. It publishes through broker verbs instead; the host's own login does the
write. From inside, send one with
`python -m lu_machinery.dispatcher.write_boundary call <verb>` and the payload as one JSON object on
stdin.

| Verb | Payload | What the host does |
| --- | --- | --- |
| `push` | `head` | Pushes the dispatched branch at `head`, which must be the worker's tip |
| `fetch-base` | none | Fetches the default branch into `refs/remotes/origin/<base>` |
| `pr-create` | `title`, `body`, optional `draft` | Opens a PR from the dispatched branch to the default branch |
| `pr-edit` | `pr`, `title` and/or `body` | Edits the title or body of the worker's own open PR |
| `pr-comment` | `pr`, `body` | Comments on the worker's own open or merged PR |
| `pr-merge` | `pr`, `head` | Enqueues the worker's own PR at `head` |
| `github-read` | `name`, optional `number` or `sha` | One named read of the repository |

What every publication verb takes from the launch, never from the request: the repository (the
primary's registered GitHub origin), the branch, the base (the repository's default branch on
GitHub) and PR ownership (the PR's head branch is the dispatched branch, in the same repository).
No verb takes an argv, endpoint, refspec, repository or host path. Texts arrive as JSON strings,
and the host writes them to the run's private scratch.

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

Sources goes through the read-only AGY boundary's `SourcesRelay` (read-only tools only).
`LU_WRITE_BOUNDARY_SOURCES` holds the MCP stdio command, as a JSON argv, that reaches it. The receipt's
`sources` holds the relay's call counts, or why it was unavailable. Sources is optional; the boundary
starts without it.

## Known limits

- The Monitor API is a host loopback service and stays unreachable. `closeout_cli resolve-reviewer`
  inside falls back to its no-snapshot route, and the host dispatcher's own admission checks the pick.
- Provider MCP configurations are not yet rewritten to the Sources stdio command. A provider whose
  `sources` server points at the loopback address cannot reach it inside.
- Markers posted with the shared GitHub login remain forgeable by a session outside a write boundary
  (an interactive session) that holds the login. Inside, the worker has neither the login nor a route
  to GitHub, and `pr-merge` also checks the host-held review record.
- Existing commands (`git push`, `gh`, `scripts.publish`) do not forward to the broker; inside, a
  worker uses `write_boundary call`.
- Fleet messages can be sent but not received or acknowledged inside.
- A Unix socket that a host service first creates after launch, outside `/tmp`, `/run` and `/dev/shm`,
  is not masked. No such service is known on the fleet host.
- Unpushed commits of a run whose launcher is killed with `SIGKILL` (not `SIGTERM`) stay in the
  discarded overlay; the worktree files remain.

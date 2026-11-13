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
  reached through the in-namespace forwarder. Any public host on port 443; loopback, private and
  host addresses are refused, so no host service (Monitor API, Sources, SSH, databases) is reachable.
  `NODE_USE_ENV_PROXY=1` makes Node's own `fetch` use the proxy.

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
  operation. Enqueueing (`scripts.publish pr-merge`) needs no verb: it writes nothing local and
  reaches GitHub through the egress proxy.
- **Closeout.** `closeout-request` checks that the PR is merged and that its head was the dispatched
  branch. The launcher runs `merge_closeout <pr> --repo <owner/name> --repo-root <primary> --apply`
  after the sandbox exits and after the branch import. Its exit status is in the receipt (`closeout`).

The receipt lists every broker request (`broker`: verb, request id, return code and refusal).

Sources goes through the read-only AGY boundary's `SourcesRelay` (read-only tools only).
`LU_WRITE_BOUNDARY_SOURCES` holds the MCP stdio command, as a JSON argv, that reaches it. The receipt's
`sources` holds the relay's call counts, or why it was unavailable. Sources is optional; the boundary
starts without it.

## Known limits

- The Monitor API is a host loopback service and stays unreachable. `closeout_cli resolve-reviewer`
  inside falls back to its no-snapshot route, and the host dispatcher's own admission checks the pick.
- Provider MCP configurations are not yet rewritten to the Sources stdio command. A provider whose
  `sources` server points at the loopback address cannot reach it inside.
- Markers posted with the shared GitHub login remain forgeable by anyone holding that login, inside the
  boundary or not. Closing this needs the queue gate to check markers against host-held review
  receipts, or a separate reviewer identity.
- Fleet messages can be sent but not received or acknowledged inside.
- A Unix socket that a host service first creates after launch, outside `/tmp`, `/run` and `/dev/shm`,
  is not masked. No such service is known on the fleet host.
- Unpushed commits of a run whose launcher is killed with `SIGKILL` (not `SIGTERM`) stay in the
  discarded overlay; the worktree files remain.

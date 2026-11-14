"""OS and publication boundary for dispatched workers and reviewers (#423, #100).

The primary-checkout write guard parses shell commands and is only an accident tripwire. This boundary
makes the primary checkout read-only for the operating system instead, so no command spelling writes it.
``worker.supervise`` launches every ``workspace-write`` provider and native read-only providers other
than Claude through ``launcher``. Read-only Claude retains its per-shell boundary pending a separate
provider enrollment. Read-only AGY keeps its existing filesystem boundary and reuses the GitHub egress refusal.
The launcher runs the provider and all of its descendants under bubblewrap and
reuses the read-only AGY boundary's pieces (``agy_sandbox``): its bwrap resolver, its projected canary
with the report and go descriptors, the CONNECT-only egress proxy and its in-namespace forwarder.

Inside the boundary:

* the whole host file system is a read-only bind; ``/tmp``, ``/run`` and ``/dev/shm`` are private tmpfs,
  so host Unix sockets there (the user bus, systemd, agents, tmux) are absent, and every other listening
  host socket path is masked with ``/dev/null``;
* PID, IPC, UTS, cgroup, user and network namespaces are private, every capability is dropped, nested
  user namespaces are refused and the session is new. The only network route is the egress proxy:
  public hosts over HTTPS, loopback and private addresses refused, so no host service runs anything
  for the worker;
* the primary working tree is a read-only bind. Its Git directory is an overlay whose writes land in a
  private upper directory for write workers, so the worker can commit, fetch and push, but the host's HEAD, index,
  configuration, hooks, refs and objects never change; the primary's HEAD, index, configuration,
  ``packed-refs`` and hooks are also read-only binds inside, and so is the worktree's ``.git`` link;
* HOME is an overlay as well, so the CLIs keep their configuration and state while nothing they write
  outlives the run, except the provider credential files named by ``--credential``, which are
  writable binds so a token refresh is not lost;
* a write worker's worktree, a private TMPDIR and the declared outputs (``--output``) are the only other
  writable paths. A read-only provider's worktree and Git metadata are read-only mounts, with no branch import.

The task store, the Fleet Comms plane and the primary's Git state stay read-only inside, so the worker chain's
host steps (start its review, record the verdict, send a Fleet message, close out after merge) go through the
launcher's broker (``_Broker``, decision CONTAIN-BROKER-1113): one Unix socket with typed verbs whose
identities come from the launch. The worker holds no GitHub credential and the egress proxy refuses GitHub's
hosts, so it publishes (push, PR, comment, merge) only through the broker's publication verbs, which the host's
own login carries out behind the existing scanned publisher and push scan (decision PUBLISH-CRED-1113).
Read-only providers may use only ``github-read``; the owning worker records the review verdict after settlement.
Sources goes through the read-only AGY boundary's ``SourcesRelay``. Its proxy and socket sit at fixed paths
inside, so ``sources_command`` is the MCP stdio command a provider names without reading any MCP file.

The canary runs first and checks each boundary property; the provider starts only after the launcher
has checked every outcome. Any setup or canary failure refuses the run (exit ``REFUSED``) and nothing
ever falls back to an unsandboxed launch. After the sandbox exits, the launcher imports exactly one
thing: the dispatched branch's new tip, through a host-side ``git fetch`` from a private repository
whose alternates are the upper objects (index-pack hashes every object it accepts), then a
compare-and-swap ``update-ref``. Other refs, objects and Git metadata the worker wrote are dropped.
The receipt beside the task record is the run's sandbox evidence; ``verify`` checks it.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from lu_machinery.common.repo_root import machinery_path, main_checkout_root
from lu_machinery.common.scratch import ensure_scratch_root
from lu_machinery.dispatcher import agy_sandbox
from lu_machinery.dispatcher.agy_sandbox import REFUSED, Refused, _now, _remove, _write_private, _write_receipt

_PACKAGE = Path(__file__).absolute().parents[1].name
SCHEMA = 1
RECEIPT_SUFFIX = ".write-boundary.json"
_BOOTSTRAP = (
    "import sys; sys.path.insert(0, sys.argv[1]); "
    f"from {_PACKAGE}.dispatcher.write_boundary import main; raise SystemExit(main(sys.argv[2:]))"
)
_PROJECTED = {
    "canary.py": machinery_path("dispatcher/agy_canary.py"),
    "forwarder.py": machinery_path("agent_runtime/attempt_forwarder.py"),
    "sources-stdio.py": machinery_path("agent_runtime/attempt_proxy.py"),
}
_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# Private tmpfs inside: host sockets below these roots, including ones created after launch, are absent.
# /var/tmp stays a read-only view so driver-provided inputs remain readable; its sockets are masked.
_MASKED_ROOTS = (Path("/tmp"), Path("/run"), Path("/dev/shm"))
# Loopback services a worker must not reach (Monitor API, Sources, SSH); the namespace has no loopback route.
_LOOPBACK_PORTS = (22, 8765, 8766)
_CANARY_TIMEOUT = 60
_GIT_TIMEOUT = 300
# The primary's own Git state, kept read-only inside on top of the private overlay.
_PRIMARY_GIT_FILES = ("HEAD", "index", "config", "packed-refs", "hooks")


# The dispatcher's own configuration the launcher needs on the host (roots, scratch, task store, Fleet plane,
# interpreter, user bus). The provider's environment is minimal (``environment.child_env``), so the launcher gets
# these as ``--host-env NAME=VALUE`` and applies them to itself only; none is passed to the provider. Paths and
# addresses only: no credential is ever one of them.
_HOST_CONTEXT = frozenset({
    "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT", "LEARN_UKRAINIAN_GITHUB_SLUG", "LEARN_UKRAINIAN_STATE_ROOT",
    "LEARN_UKRAINIAN_PYTHON", "LU_PRODUCT_ROOT", "LU_SCRATCH_ROOT", "LU_RUNTIME_TMP_BASE_ROOT", "LU_TASKS_DIR",
    "FLEET_COMMS_ROOT", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "XDG_CONFIG_HOME", "GH_CONFIG_DIR",
    "DELEGATE_MONITOR_API", "LU_DISPATCH_ISOLATION",
})

# The host broker (decision CONTAIN-BROKER-1113). Inside the boundary the task store, the Fleet plane and the
# primary's Git state are read-only, so the worker chain's host steps go through one launcher-owned socket with
# typed verbs. Each verb has a fixed schema, and every identity it acts under comes from the launch, never the
# request.
BROKER_ENV = "LU_WRITE_BOUNDARY_BROKER"
_BROKER_VERBS = ("review-dispatch", "record-verdict", "fleet-publish", "closeout-request", "push", "fetch-base",
                 "pr-create", "pr-edit", "pr-comment", "pr-merge", "github-read")
# Named reads of the worker's own repository (the publisher's fixed GET templates); no security-alert reads.
_GITHUB_READS = frozenset({"pull", "pr-files", "commits", "commit", "comments", "reviews", "timeline", "issue",
                           "checks", "check-annotations", "run", "jobs"})
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
# GitHub's write-capable hosts. The egress proxy refuses them inside the boundary, by name and by the address ranges
# GitHub publishes for them, so no credential the worker could read anywhere is usable from inside; every
# publication goes through the broker (PUBLISH-CRED-1113).
GITHUB_DOMAINS = ("github.com", "ghcr.io")
_GITHUB_META_KEYS = ("web", "api", "git", "packages")
_REQUEST_ID = re.compile(r"[0-9a-f]{32}")
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}")  # a model, agent, profile or label: never a flag
_CHANNEL = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_IDEMPOTENCY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_GITHUB_REMOTE = re.compile(
    r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}?)(?:\.git)?/?")
_GITHUB_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
_MAX_REQUEST = 1 << 20
_MAX_BODY = 64 << 10
# The launcher's own directory appears inside at this short fixed path on the private /run (below the
# sockaddr_un length limit), read-only and as a mount point, so no worker can rename, remove or replace it or
# anything below it: the broker socket, the Sources relay socket and the projected Sources byte proxy. The MCP
# stdio command a provider names (``sources_command``) is therefore a constant, and no MCP file is read.
_INSIDE = "/run/lu-write-boundary"
_BROKER_INSIDE = "/run/lu-write-boundary/broker.sock"
_SOURCES_INSIDE = "/run/lu-write-boundary/sources.sock"
_SOURCES_STDIO_INSIDE = "/run/lu-write-boundary/sources-stdio.py"
_BROKER_TIMEOUTS = {"review-dispatch": 300, "record-verdict": 900, "fleet-publish": 120, "gh": 120,
                    "closeout-request": 900, "publish": 300, "pr-merge": 900}


def sources_command() -> list[str]:
    """The MCP stdio argv that reaches the launcher's read-only Sources relay from inside the boundary.

    The launcher runs the provider with this interpreter and binds its proxy and the relay socket at these
    fixed paths. Without a relay the socket is absent: the server fails to start and the run goes on.
    """
    return [str(Path(sys.executable).resolve(strict=True)), "-I", _SOURCES_STDIO_INSIDE, _SOURCES_INSIDE]


def _interpreter_root() -> Path:
    """The installation ``sources_command``'s interpreter runs from (binary and standard library)."""
    interpreter = Path(sources_command()[0])
    prefix = Path(sys.base_prefix).resolve()
    return prefix if interpreter.is_relative_to(prefix) else interpreter.parent


def broker_path() -> str | None:
    """The broker socket when this process runs inside a write boundary, else None."""
    return os.environ.get(BROKER_ENV) or None


def broker_call(verb: str, payload: dict) -> dict:
    """Send one request to the launcher's broker; the reply holds ``returncode``, ``stdout`` and ``stderr``."""
    import socket

    path = broker_path()
    if path is None:
        raise RuntimeError("not inside a write boundary")
    request = json.dumps({"verb": verb, "request_id": uuid.uuid4().hex, "payload": payload}).encode()
    if len(request) > _MAX_REQUEST:
        return {"returncode": 2, "stdout": "", "stderr": "write boundary broker: the request is too large\n"}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(path)
        connection.sendall(request)
        connection.shutdown(socket.SHUT_WR)
        chunks = []
        while chunk := connection.recv(65536):
            chunks.append(chunk)
    try:
        reply = json.loads(b"".join(chunks))
    except ValueError:
        reply = None
    if not isinstance(reply, dict):
        return {"returncode": 1, "stdout": "", "stderr": "write boundary broker: no reply\n"}
    return reply


def unavailable() -> str | None:
    """Why this host cannot start the write boundary, or None (bubblewrap on Linux, as for AGY)."""
    return agy_sandbox.unavailable()


def receipt_file(task_file: Path) -> Path:
    return task_file.with_suffix(RECEIPT_SUFFIX)


def scratch(task_id: str) -> Path:
    if not _TASK_ID.fullmatch(task_id):
        raise ValueError("write boundary: the task id is unsafe as a scratch name")
    return ensure_scratch_root() / f"write-boundary-{task_id}"


def discard(task_id: str) -> str | None:
    """Remove the scratch of a launcher that was killed before its own cleanup; never raises."""
    try:
        _remove(scratch(task_id))
    except (OSError, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}"[:300]
    return None


def credential_files(agent: str, env: dict[str, str]) -> list[Path]:
    """Provider credential files a token refresh rewrites; they stay writable so a refresh is not lost."""
    home = Path(env.get("HOME") or Path.home())
    if agent == "claude":
        candidates = [Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude") / ".credentials.json"]
    elif agent == "codex":
        candidates = [Path(env.get("CODEX_HOME") or home / ".codex") / "auth.json"]
    elif agent == "agy":
        from lu_machinery.agent_runtime.review_mcp import _real_agy_token

        candidates = [_real_agy_token()]
    elif agent == "opencode":
        candidates = [home / ".local" / "share" / "opencode" / "auth.json"]
    else:
        candidates = []
    return [path for path in candidates if path.is_file() and not path.is_symlink()]


def declared_outputs(spec, invocation) -> list[Path]:
    """Files outside the worktree the provider writes: its final message and AGY's write log."""
    outputs = [invocation.final_file] if invocation.final_file else []
    if spec.agent == "agy":
        outputs.append(spec.final_file.with_suffix(".log"))
    return [Path(path) for path in outputs]


def launcher(spec, invocation, task_file: Path) -> list[str]:
    """The argv that runs ``invocation.argv`` inside the write boundary."""
    found = shutil.which(invocation.argv[0], path=invocation.env.get("PATH")) if invocation.argv else None
    if not found:
        raise ValueError(f"write_boundary_refused: the {spec.agent} executable was not found")
    argv = [
        sys.executable, "-I", "-c", _BOOTSTRAP, str(machinery_path("dispatcher/write_boundary.py").parents[2]),
        "--task-id", spec.task_id, "--worktree", str(spec.worktree), "--scratch", str(scratch(spec.task_id)),
        "--receipt", str(receipt_file(task_file)), "--author-model", spec.model, "--mode", spec.mode,
    ]
    for path in declared_outputs(spec, invocation):
        argv += ["--output", str(path)]
    for path in credential_files(spec.agent, invocation.env):
        argv += ["--credential", str(path)]
    if spec.agent == "codex" and spec.mode == "read-only":
        from lu_machinery.agent_runtime.read_only_tmp import dispatch_lease

        argv += ["--runtime", str(dispatch_lease(spec.task_id))]
    for name in sorted(_HOST_CONTEXT):
        if os.environ.get(name) and name not in invocation.env:
            argv += ["--host-env", f"{name}={os.environ[name]}"]
    return [*argv, "--", str(Path(found).absolute()), *invocation.argv[1:]]


def verify(task_file: Path, task_id: str, mode: str = "workspace-write") -> tuple[dict, str | None]:
    """The boundary evidence and the refusal that fails the run, if any."""
    path = receipt_file(task_file)
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "missing"}, "write_boundary_unproven: no boundary receipt"
    summary = {key: receipt.get(key) for key in (
        "status", "reason", "mode", "argv_sha256", "bwrap", "returncode", "import", "cleanup", "broker", "closeout_request")}
    summary["canary_passed"] = bool(receipt.get("canary", {}).get("passed"))
    if receipt.get("schema") != SCHEMA or receipt.get("task_id") != task_id:
        return summary, "write_boundary_unproven: the receipt names another run"
    if receipt.get("mode") != mode:
        return summary, "write_boundary_unproven: the receipt names another mode"
    if receipt.get("status") != "ran" or not summary["canary_passed"]:
        return summary, f"write_boundary_refused: {receipt.get('reason') or receipt.get('status')}"
    if (receipt.get("import") or {}).get("status") not in ("unchanged", "imported"):
        return summary, f"write_boundary_import_failed: {(receipt.get('import') or {}).get('status')}"
    return summary, None


def run_closeout(request: dict) -> dict:
    """``merge_closeout --apply`` for a worker's closeout request, run by the supervisor after settlement.

    The request comes from the launcher's receipt, which only the host writes; it is still checked here.
    """
    pr, repo, root = request.get("pr"), request.get("repo"), request.get("repo_root")
    if not (type(pr) is int and pr > 0 and isinstance(repo, str) and _GITHUB_SLUG.fullmatch(repo)
            and isinstance(root, str) and Path(root).is_absolute() and Path(root).is_dir()):
        return {"pr": pr, "returncode": None, "reason": "the closeout request is malformed"}
    code = (f"import sys; sys.path.insert(0, sys.argv[1]); from {_PACKAGE}.orchestration.merge_closeout "
            "import main; raise SystemExit(main(sys.argv[2:]))")
    argv = [sys.executable, "-I", "-c", code, str(machinery_path("dispatcher/write_boundary.py").parents[2]),
            str(pr), "--repo", repo, "--repo-root", root, "--apply", "--json"]
    try:
        # A fixed argv: this interpreter, the package's own closeout module and checked request fields.
        result = subprocess.run(argv, cwd=root, capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL,  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
                                timeout=_BROKER_TIMEOUTS["closeout-request"])
    except (OSError, subprocess.SubprocessError) as exc:
        return {"pr": pr, "returncode": None, "reason": f"{type(exc).__name__}"}
    return {"pr": pr, "returncode": result.returncode, "stdout": result.stdout[-4000:], "stderr": result.stderr[-2000:]}


def _git(cwd: Path, *args: str, git_dir: Path | None = None, network: bool = False) -> str:
    """Host Git with no user or system configuration, hooks or fsmonitor: nothing the worker wrote runs.

    ``network`` admits HTTPS for the broker's publication verbs; their credential is the scoped helper of
    ``git_credentials.verification_credentials``, the only credential configuration Git then has.
    """
    env = {"PATH": os.defpath, "HOME": os.environ.get("HOME", "/"), "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_OPTIONAL_LOCKS": "0"}
    if network:  # gh, behind the scoped helper, finds the host login through its own configuration
        env.update({name: os.environ[name] for name in ("GH_CONFIG_DIR", "XDG_CONFIG_HOME") if os.environ.get(name)})
    command = list(args)
    while command[:1] == ["-c"] and len(command) >= 2:
        command = command[2:]
    # Clone transport is not publication authority. The broker's existing resolver verifies the
    # registry and binds credentials to HTTPS, including when the protected origin uses SSH.
    if (network and command[:1] in (["push"], ["ls-remote"]) and len(command) >= 3
            and (match := _GITHUB_REMOTE.fullmatch(command[-2]))):
        try:
            command[-2], credentials = _Broker._remote(match.group(1))
        except _BrokerRefusal as exc:
            raise Refused(str(exc)) from None
        args = (*credentials, *command)
    if command[:1] == ["push"]:
        # Broker and settlement share this exact-update scan. No worker hook is trusted, including
        # reference-transaction hooks, so the transport below always has hooks disabled.
        if (len(command) != 7 or command[:4] != ["push", "--quiet", "--no-verify", "--porcelain"]
                or not command[4].startswith("--force-with-lease=")):
            raise Refused("host push requires one exact update and lease")
        head, separator, branch = command[6].partition(":")
        target, expected_separator, remote = command[4].removeprefix("--force-with-lease=").partition(":")
        if (not separator or (head and not _OBJECT_ID.fullmatch(head)) or not branch.startswith("refs/heads/")
                or not expected_separator or target != branch or (remote and not _OBJECT_ID.fullmatch(remote))
                or (not head and not remote)):
            raise Refused("host push requires one exact update and lease")
        common = git_dir or Path(_git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
        update = (f"{branch if head else '(delete)'} {head or '0' * len(remote)} "
                  f"{branch} {remote or '0' * len(head)}\n").encode()
        code = ("import sys; sys.path.insert(0, sys.argv[1]); "
                f"from {_PACKAGE}.opsec.git_push import main; "
                "raise SystemExit(main(sys.argv[2:4], stdin=sys.stdin.buffer.read(), chain=lambda *_: 0))")
        package_parent = str(machinery_path("dispatcher/write_boundary.py").parents[2])
        # Fixed isolated interpreter and scanner code; input is the validated update above, never a shell command.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        scanned = subprocess.run(
            [sys.executable, "-I", "-c", code, package_parent, "origin", command[5]],
            cwd=cwd, input=update, env={**os.environ, "GIT_DIR": str(common)},
            capture_output=True, timeout=_BROKER_TIMEOUTS["publish"], check=False,
        )
        if scanned.returncode:
            raise Refused(scanned.stderr.decode("utf-8", "replace") or "host push scan refused")
    result = subprocess.run(
        ["git", "--no-replace-objects", "-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false",
         "-c", "push.followTags=false", "-c", "push.recurseSubmodules=no",
         "-c", "protocol.allow=never", "-c", "protocol.file.allow=always",
         *(["-c", "protocol.https.allow=always"] if network else []),
         *([f"--git-dir={git_dir}"] if git_dir else []), "-C", str(cwd), *args],
        capture_output=True, text=True, timeout=_GIT_TIMEOUT, check=False, env=env, stdin=subprocess.DEVNULL,
    )
    if result.returncode:
        if command[:1] == ["push"] and "[rejected]" in result.stdout and "(stale info)" in result.stdout:
            raise Refused("host push [rejected] (stale info)")
        raise Refused(f"git {args[0]} failed")
    return result.stdout


def _writable_tracked_file(repository: Path) -> Path:
    """The first tracked regular file the host user owns and may write, for the canary's read-only bind probe.

    The kernel checks file permissions before the mount's read-only flag, so a tracked file that is read-only
    on disk gives EACCES under any mount and would test its mode, not the bind. The sandbox maps only the host
    user, so a file writable only through its group or another owner gives EACCES inside it as well.
    """
    for name in _git(repository, "ls-files", "-z").split("\0"):
        if not name:
            continue
        path = repository / name
        try:
            status = path.lstat()
        except OSError:
            continue
        if stat.S_ISREG(status.st_mode) and status.st_uid == os.geteuid() and os.access(path, os.W_OK):
            return path
    raise Refused("the primary has no tracked regular file the host user may write, so the canary cannot "
                  "tell its read-only bind from file permissions")


def _listening_socket_paths() -> list[Path]:
    """Every listening path-named Unix socket of the host, from the kernel's table."""
    paths = set()
    with contextlib.suppress(OSError):
        for line in Path("/proc/net/unix").read_text(errors="replace").splitlines()[1:]:
            fields = line.split(None, 7)  # the path is the rest of the line and may hold spaces
            if len(fields) == 8 and fields[5] == "01" and fields[7].startswith("/"):
                # Resolved, so a name through a link (/var/run is /run) is judged by where it lives.
                paths.add(Path(os.path.realpath(fields[7])))
    return sorted(paths)


def _shared_state_roots(home: Path) -> list[Path]:
    """Shared host state under HOME that a worker must not write: the product checkout, the task store and
    the Fleet Comms plane. Read-only binds over HOME's overlay make such a write fail instead of vanishing."""
    from lu_machinery.common.repo_root import ProductRootError, product_root
    from lu_machinery.common.task_store_paths import tasks_dir

    candidates = []
    with contextlib.suppress(ProductRootError, OSError, ValueError):
        candidates.append(main_checkout_root(product_root()))
    with contextlib.suppress(OSError, ValueError):
        candidates.append(tasks_dir())
    if os.environ.get("FLEET_COMMS_ROOT"):
        candidates.append(Path(os.environ["FLEET_COMMS_ROOT"]))
    roots = []
    for path in candidates:
        with contextlib.suppress(OSError):
            path = path.resolve(strict=True)
            if path.is_dir() and path.is_relative_to(home) and path != home and path not in roots:
                roots.append(path)
    return roots


def _github_credential_variable(name: str) -> bool:
    """An environment variable that can carry a GitHub credential or a credential program into the boundary."""
    upper = name.upper()
    return upper.startswith(("GH_", "GITHUB_", "GIT_SSH")) or upper.endswith("_ASKPASS")


def _github_credential_paths(home: Path) -> list[Path]:
    """The host login's GitHub credential stores: gh's configuration, Git's credential files, netrc and SSH keys.

    Each existing one is emptied inside (a directory becomes an empty tmpfs, a file reads as ``/dev/null``).
    Containment does not rest on this list: the egress proxy refuses GitHub's hosts inside the boundary.
    """
    config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    candidates = [config / "gh", home / ".config" / "gh", config / "git" / "credentials",
                  home / ".git-credentials", home / ".netrc", home / ".ssh"]
    if os.environ.get("GH_CONFIG_DIR"):
        candidates.insert(0, Path(os.environ["GH_CONFIG_DIR"]))
    paths = []
    for path in candidates:
        # A link is masked where it points, so the store is empty under either name.
        for name in (path, Path(os.path.realpath(path))):
            if name.is_absolute() and os.path.lexists(name) and not name.is_symlink() and name not in paths:
                paths.append(name)
    return paths


def github_networks() -> tuple:
    """GitHub's published address ranges for its web, API, Git and package hosts, read on the host now.

    Fails closed: without them the proxy could not refuse another name that resolves to GitHub.
    """
    import ipaddress

    from lu_machinery.common import github_client

    try:
        proc = github_client.run(["gh", "api", "meta"], capture_output=True, text=True, check=False, timeout=60)
        meta = json.loads(proc.stdout) if proc.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        meta = None
    networks = []
    ranges: dict = meta if isinstance(meta, dict) else {}
    complete = all(isinstance(ranges.get(key), list) and ranges[key] for key in _GITHUB_META_KEYS)
    for key in _GITHUB_META_KEYS if complete else ():
        for value in ranges[key]:
            try:
                networks.append(ipaddress.ip_network(value))
            except (TypeError, ValueError):
                complete = False
    if not complete or not any(network.version == 4 for network in networks):
        raise Refused("GitHub's published address ranges cannot be read; the boundary could not refuse GitHub")
    return tuple(dict.fromkeys(networks))


def _under(path: Path, roots) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def _overlaps(path: Path, other: Path) -> bool:
    return path == other or path.is_relative_to(other) or other.is_relative_to(path)


class _BrokerRefusal(Exception):
    pass


def _host_child_env(task_id: str) -> dict[str, str]:
    """The launcher's environment for a host step, minus the hosting CLI's session variables."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("CLAUDE", "CODEX_THREAD", "LU_WRITE_BOUNDARY")) and key != "CLAUDECODE"}
    env["LEARN_UKRAINIAN_DISPATCH_TASK_ID"] = task_id  # the review child records this task as its parent
    return env


class _Broker:
    """The launcher-owned host side of the worker chain (decision CONTAIN-BROKER-1113).

    One Unix socket in the read-only boundary directory. A connection is served only when its peer is a
    descendant of this run's bubblewrap process, so no other sandbox or host process can act as this task.
    A request is one JSON object ``{"verb", "request_id", "payload"}``; a request id is accepted once per run.
    Each verb validates a fixed schema and runs one host step with argv built here, never a shell, and with
    the identities of the launch: the task id, its branch, its author model and the primary's repository.

    * ``review-dispatch`` starts the read-only review of the worker's PR at its exact live head: the PR's head
      branch is the dispatched branch, its live head is the worker's branch tip and the requested pinned head,
      and the review task id is ``<task>-review-<suffix>``. The host fetches that commit from the worker's
      private Git layer (index-pack hashes every object), then runs the dispatcher, whose review then runs
      in its own normal sandbox. The author model is the launch's model, so the family check is not forgeable.
    * ``record-verdict`` runs the recorder for a review task of this worker, on the worker's PR; the recorder
      takes the host's PR lock and writes its receipts in the task store, which is read-only inside.
    * ``fleet-publish`` appends one Fleet Comms message whose sender is the task id and whose idempotency key
      is prefixed with it; the body is untrusted text.
    * ``closeout-request`` records the worker's merged PR in the receipt; the supervisor runs ``merge_closeout --apply``
      on the host after it has settled the task's worktree, because closeout deletes refs in the primary's Git
      directory and refuses while the worktree is registered.
    * ``push``, ``fetch-base``, ``pr-create``, ``pr-edit``, ``pr-comment``, ``pr-merge`` and ``github-read`` are
      the worker's only GitHub access (PUBLISH-CRED-1113); docs/runbooks/write-worker-boundary.md lists them.
    """

    def __init__(self, boundary: _Boundary, path: Path):
        import socket
        import threading

        self.boundary = boundary
        self.records: list[dict] = []
        self.seen: set[str] = set()
        self.closeout_request: dict | None = None
        self.child: subprocess.Popen[bytes] | None = None
        self.closed = False
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:  # the descriptor path avoids the sockaddr_un length limit, as in agy_sandbox.SourcesRelay
            self.server.bind(f"/proc/self/fd/{directory}/{path.name}")
        finally:
            os.close(directory)
        self.server.listen(4)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self.closed:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            with connection, contextlib.suppress(OSError, ValueError):
                self._handle(connection)

    def _peer_inside(self, connection) -> bool:
        """Whether the connecting process descends from this run's bubblewrap (PID namespaces nest under it)."""
        import socket
        import struct

        process = self.boundary.process
        if process is None:
            return False
        pid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                        struct.calcsize("3i")))[0]
        for _ in range(256):
            if pid == process.pid:
                return True
            if pid <= 1:
                return False
            try:
                pid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
            except (OSError, ValueError, IndexError):
                return False
        return False

    def _handle(self, connection) -> None:
        connection.settimeout(30)
        verb = request_id = None
        reply: dict[str, Any] = {"returncode": 2, "stdout": "", "stderr": ""}
        try:
            chunks, size = [], 0
            while size <= _MAX_REQUEST and (chunk := connection.recv(65536)):
                chunks.append(chunk)
                size += len(chunk)
            # Checked before the request is even parsed; read first so the refusal reaches the client.
            if not self._peer_inside(connection):
                raise _BrokerRefusal("the peer is not a process of this write boundary")
            try:
                request = json.loads(b"".join(chunks))
            except ValueError:
                request = None
            if not isinstance(request, dict) or set(request) != {"verb", "request_id", "payload"}:
                raise _BrokerRefusal("the request is malformed")
            verb, request_id, payload = request["verb"], request["request_id"], request["payload"]
            if verb not in _BROKER_VERBS:
                verb = str(verb)[:40]
                raise _BrokerRefusal("the verb is not one of " + ", ".join(_BROKER_VERBS))
            if self.boundary.args.mode == "read-only" and verb != "github-read":
                raise _BrokerRefusal("read-only dispatches may only use github-read")
            if not (isinstance(request_id, str) and _REQUEST_ID.fullmatch(request_id)) or not isinstance(payload, dict):
                raise _BrokerRefusal("the request is malformed")
            if request_id in self.seen:
                raise _BrokerRefusal("the request id was already used: a request runs once")
            self.seen.add(request_id)
            reply = getattr(self, "_" + verb.replace("-", "_"))(payload)
        except _BrokerRefusal as exc:
            reply["stderr"] = f"write boundary broker refused: {exc}\n"
        except Exception as exc:  # a host step that fails is reported, never retried
            reply.update(returncode=1, stderr=f"write boundary broker failed: {type(exc).__name__}\n")
        self.records.append({"verb": verb, "request_id": request_id, "returncode": reply["returncode"],
                             "refused": reply["stderr"][:200] if reply["returncode"] == 2 else None})
        connection.settimeout(None)
        connection.sendall(json.dumps(reply).encode())

    # -- launch-bound facts --------------------------------------------------------------------------------------

    def _repository_slug(self) -> str:
        url = _git(self.boundary.repository, "config", "--get", "remote.origin.url", git_dir=self.boundary.common)
        if not (match := _GITHUB_REMOTE.fullmatch(url.strip())):
            raise _BrokerRefusal("the primary's origin is not a github.com repository")
        return match.group(1)

    def _gh_json(self, argv: list[str]) -> Any:
        """One fresh read through the shared GitHub client (its budget and Retry-After admission apply)."""
        from lu_machinery.common import github_client

        proc = github_client.run(["gh", *argv], capture_output=True, text=True, check=False,
                                 timeout=_BROKER_TIMEOUTS["gh"], fresh=True)
        try:
            return json.loads(proc.stdout) if proc.returncode == 0 else None
        except (TypeError, ValueError):
            return None

    def _pull_request(self, payload: dict, *states: str) -> tuple[int, str, dict]:
        """The PR in ``payload["pr"]``, checked live on GitHub: same repository, head branch the dispatched one."""
        number = payload.get("pr")
        if type(number) is not int or not 0 < number < 10**8:
            raise _BrokerRefusal("pr must be a PR number")
        slug = self._repository_slug()
        self._claimed({"repo": payload.get("repo")}, slug)
        facts = self._gh_json(["pr", "view", str(number), "--repo", slug, "--json",
                               "state,headRefName,headRefOid,isCrossRepository,baseRefName"])
        if not isinstance(facts, dict):
            raise _BrokerRefusal("the PR cannot be read on GitHub")
        branch = self.boundary.branch.removeprefix("refs/heads/")
        if facts.get("isCrossRepository") is not False or facts.get("headRefName") != branch:
            raise _BrokerRefusal("the PR's head is not this task's dispatched branch")
        if facts.get("state") not in states:
            raise _BrokerRefusal(f"the PR is not {' or '.join(states)}")
        return number, slug, facts

    def _default_branch(self, slug: str) -> str:
        """The repository's default branch on GitHub: the only base a worker's PR gets."""
        facts = self._gh_json(["repo", "view", slug, "--json", "defaultBranchRef"])
        name = (facts or {}).get("defaultBranchRef", {}).get("name") if isinstance(facts, dict) else None
        if not (isinstance(name, str) and _BRANCH.fullmatch(name)):
            raise _BrokerRefusal("the repository's default branch cannot be read on GitHub")
        return name

    @staticmethod
    def _remote(slug: str) -> tuple[str, list[str]]:
        """The HTTPS remote of ``slug`` and the scoped host credential settings for it, from the registry."""
        from lu_machinery.common.git_credentials import verification_credentials

        url = f"https://github.com/{slug}.git"
        settings = verification_credentials(url)
        if not settings:
            raise _BrokerRefusal("the repository is not a registered github.com destination")
        return url, [arg for setting in settings for arg in ("-c", setting)]

    def _repo_key(self, slug: str) -> str:
        """The fleet repository key whose checkout is this primary and whose GitHub name is ``slug``."""
        import yaml

        from lu_machinery.dispatcher import git as dispatch_git

        for key in yaml.safe_load(machinery_path("config/fleet_repos.yaml").read_text())["repos"]:
            with contextlib.suppress(Exception):
                root, item = dispatch_git.resolve_repo(key)
                if root == self.boundary.repository and str(item.get("github", "")).lower() == slug.lower():
                    return str(key)
        raise _BrokerRefusal("the primary is not a registered fleet repository")

    def _claimed(self, payload: dict, slug: str, base: str | None = None) -> None:
        """Refuse a request whose named target is not the launch's own.

        A forwarded command (``git push``, the publisher's PR verbs) keeps the target its caller named. The broker
        still acts only on the launch's repository, branch and default branch, so a named target narrows the
        request: one that differs is refused, never replaced.
        """
        repo = payload.get("repo")
        if repo is not None and not (isinstance(repo, str) and repo.lower() == slug.lower()):
            raise _BrokerRefusal("repo is not this task's repository")
        branch = payload.get("branch")
        if branch is not None and not (isinstance(branch, str)
                                       and "refs/heads/" + branch.removeprefix("refs/heads/") == self.boundary.branch):
            raise _BrokerRefusal("branch is not this task's dispatched branch")
        claimed_base = payload.get("base")
        if claimed_base is not None and not (isinstance(claimed_base, str) and base is not None
                                             and claimed_base.removeprefix("refs/heads/") == base):
            raise _BrokerRefusal("base is not the repository's default branch")

    def _review_task(self, payload: dict) -> str:
        task = payload.get("task_id")
        prefix = f"{self.boundary.args.task_id}-review-"
        if not (isinstance(task, str) and task.startswith(prefix) and _TASK_ID.fullmatch(task)
                and re.fullmatch(r"[A-Za-z0-9._-]{1,40}", task[len(prefix):])):
            raise _BrokerRefusal(f"the review task id must be {prefix}<suffix>")
        return task

    # -- host steps ----------------------------------------------------------------------------------------------

    def _run(self, argv: list[str], timeout_key: str, *, stdin: bytes | None = None,
             env: dict[str, str] | None = None) -> dict:
        env = {**_host_child_env(self.boundary.args.task_id), **(env or {})}
        # A fixed argv built by this class from validated fields, never a shell.
        self.child = subprocess.Popen(argv, cwd=self.boundary.repository, env=env, stdin=subprocess.PIPE,  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            out, err = self.child.communicate(stdin, timeout=_BROKER_TIMEOUTS[timeout_key])
            returncode = self.child.returncode
        except subprocess.TimeoutExpired:
            self.child.kill()
            out, err = self.child.communicate()
            returncode, err = 124, err + b"\nwrite boundary broker: the host step timed out\n"
        return {"returncode": returncode, "stdout": out[:_MAX_REQUEST].decode("utf-8", "replace"),
                "stderr": err[:_MAX_REQUEST].decode("utf-8", "replace")}

    def _module(self, module: str, argv: list[str], timeout_key: str, stdin: bytes | None = None) -> dict:
        code = (f"import sys; sys.path.insert(0, sys.argv[1]); from {_PACKAGE}.{module} import main; "
                "raise SystemExit(main(sys.argv[2:]))")
        package_parent = str(machinery_path("dispatcher/write_boundary.py").parents[2])
        return self._run([sys.executable, "-I", "-c", code, package_parent, *argv], timeout_key, stdin=stdin)

    def _review_dispatch(self, payload: dict) -> dict:
        allowed = {"task_id", "pr", "pinned_head", "agent", "model", "effort", "prompt", "review_profile", "risk",
                   "review_mode", "codex_account", "claude_account"}
        if not set(payload) <= allowed:
            raise _BrokerRefusal("unknown fields: " + ", ".join(sorted(set(payload) - allowed)))
        task = self._review_task(payload)
        if not (isinstance(self.boundary.args.author_model, str) and _WORD.fullmatch(self.boundary.args.author_model)):
            raise _BrokerRefusal("the launch names no author model")
        words = {}
        for name in ("agent", "model", "effort", "review_profile", "risk", "review_mode", "codex_account",
                     "claude_account"):
            value = payload.get(name)
            if value is None and name in ("agent", "model", "review_profile", "risk"):
                raise _BrokerRefusal(f"{name} is required")
            if value is not None and not (isinstance(value, str) and _WORD.fullmatch(value)):
                raise _BrokerRefusal(f"{name} is malformed")
            words[name] = value
        if words["review_mode"] not in (None, "cross_family"):
            raise _BrokerRefusal("only a cross-family review is dispatched through the broker")
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > _MAX_REQUEST // 2:
            raise _BrokerRefusal("prompt must be non-empty text")
        pinned = payload.get("pinned_head")
        if not (isinstance(pinned, str) and _OBJECT_ID.fullmatch(pinned)):
            raise _BrokerRefusal("pinned_head must be a full commit id")
        number, slug, facts = self._pull_request(payload, "OPEN")
        repo_key = self._repo_key(slug)
        tip = self.boundary.current_tip()
        if not (pinned == facts.get("headRefOid") == tip):
            raise _BrokerRefusal("pinned_head is not both the PR's live head and this task's branch tip")
        self.boundary.fetch_objects(tip)
        prompt_file = self.boundary.base / f"review-prompt-{uuid.uuid4().hex}.md"
        _write_private(prompt_file, prompt.encode(), 0o400)
        argv = ["dispatch", "--repo", repo_key, "--agent", words["agent"], "--model", words["model"],
                "--mode", "read-only", "--worktree", "--task-id", task, "--prompt-file", str(prompt_file),
                "--branch", self.boundary.branch.removeprefix("refs/heads/"), "--pinned-head", pinned,
                "--pr", str(number), "--review-profile", words["review_profile"], "--risk", words["risk"],
                "--review-author-model", self.boundary.args.author_model, "--review-mode", "cross_family",
                "--initiator", self.boundary.args.task_id]
        for name in ("effort", "codex_account", "claude_account"):
            if words[name] is not None:
                argv += [f"--{name.replace('_', '-')}", words[name]]
        if words["review_profile"] in ("code", "infra"):
            argv.append("--require-review-verdict")
        return self._module("dispatcher.cli", argv, "review-dispatch")

    def _record_verdict(self, payload: dict) -> dict:
        if set(payload) != {"task_id", "pr"}:
            raise _BrokerRefusal("record-verdict takes exactly task_id and pr")
        task = self._review_task(payload)
        number, _slug, _facts = self._pull_request(payload, "OPEN")
        return self._module("review.record_cf_verdict", ["--task-id", task, "--pr", str(number)], "record-verdict")

    def _fleet_publish(self, payload: dict) -> dict:
        allowed = {"channel", "body", "recipients", "idempotency_key", "in_reply_to"}
        if not set(payload) <= allowed or not {"channel", "body", "idempotency_key"} <= set(payload):
            raise _BrokerRefusal("fleet-publish takes channel, body, idempotency_key, recipients and in_reply_to")
        channel, body, key = payload["channel"], payload["body"], payload["idempotency_key"]
        recipients = payload.get("recipients") or []
        if not (isinstance(channel, str) and _CHANNEL.fullmatch(channel)):
            raise _BrokerRefusal("channel is malformed")
        if not (isinstance(key, str) and _IDEMPOTENCY.fullmatch(key)):
            raise _BrokerRefusal("idempotency_key is malformed")
        if not (isinstance(recipients, list) and len(recipients) <= 8
                and all(isinstance(name, str) and _CHANNEL.fullmatch(name) for name in recipients)):
            raise _BrokerRefusal("recipients must be at most 8 agent names")
        if not isinstance(body, str) or not body.strip() or len(body.encode()) > _MAX_BODY:
            raise _BrokerRefusal(f"body must be non-empty text of at most {_MAX_BODY} bytes")
        task = self.boundary.args.task_id
        argv = ["channel", "publish", channel, "-", "--sender", task, "--source", "write-worker",
                "--idempotency-key", f"{task}:{key}"]
        for name in recipients:
            argv += ["--recipient", name]
        if (reply_to := payload.get("in_reply_to")) is not None:
            if not (isinstance(reply_to, str) and _IDEMPOTENCY.fullmatch(reply_to)):
                raise _BrokerRefusal("in_reply_to is malformed")
            argv += ["--in-reply-to", reply_to]
        return self._module("fleet_comms.cli", argv, "fleet-publish", stdin=body.encode())

    def _closeout_request(self, payload: dict) -> dict:
        if set(payload) != {"pr"}:
            raise _BrokerRefusal("closeout-request takes exactly pr")
        number, slug, _facts = self._pull_request(payload, "MERGED")
        self.closeout_request = {"pr": number, "repo": slug, "repo_root": str(self.boundary.repository)}
        return {"returncode": 0, "stdout": f"closeout of PR {number} runs on the host after this task's worktree "
                "is settled\n", "stderr": ""}

    # -- publication (decision PUBLISH-CRED-1113): the worker holds no GitHub credential and has no route to
    # GitHub, so every publication is one of these verbs. The host's own login does the write, through the
    # existing scanned publisher and push scanner; repository, branch, base and PR ownership come from the launch.

    def _text(self, payload: dict, name: str, limit: int) -> str:
        value = payload.get(name)
        if not isinstance(value, str) or not value.strip() or len(value.encode()) > limit or "\0" in value:
            raise _BrokerRefusal(f"{name} must be non-empty text of at most {limit} bytes")
        return value

    def _optional_text(self, payload: dict, name: str, limit: int) -> str | None:
        return None if payload.get(name) is None else self._text(payload, name, limit)

    def _text_file(self, text: str) -> str:
        """Write request text to the run's private scratch; the publisher reads it by the host's own path."""
        path = self.boundary.base / f"publish-{uuid.uuid4().hex}.md"
        _write_private(path, text.encode(), 0o400)
        return str(path)

    def _publish(self, verb: str, slug: str, fields: list[str], timeout_key: str = "publish") -> dict:
        return self._module("publish.github", [verb, "--repo", slug, *fields], timeout_key)

    def _push(self, payload: dict) -> dict:
        """Publish this task's branch at the exact commit named, fast-forward only, after the push scan."""
        if "head" not in payload or not set(payload) <= {"head", "branch", "repo", "lease"}:
            raise _BrokerRefusal("push takes head, and optionally branch, repo and lease")
        head = payload["head"]
        if not (isinstance(head, str) and _OBJECT_ID.fullmatch(head)):
            raise _BrokerRefusal("head must be a full commit id")
        if head != self.boundary.current_tip():
            raise _BrokerRefusal("head is not this task's branch tip")
        slug = self._repository_slug()
        self._claimed(payload, slug)
        lease = payload.get("lease")
        if lease is not None and not (isinstance(lease, str) and (lease == "" or _OBJECT_ID.fullmatch(lease))):
            raise _BrokerRefusal("lease must be a full commit id, or empty for an absent branch")
        url, credentials = self._remote(slug)
        branch = self.boundary.branch
        self.boundary.fetch_objects(head)
        listing = _git(self.boundary.repository, *credentials, "ls-remote", url, branch, git_dir=self.boundary.common,
                       network=True)
        # ls-remote matches a pattern against ref-name tails; only the exact ref counts.
        exact = [line.split("\t")[0] for line in listing.splitlines() if line.partition("\t")[2] == branch]
        if len(exact) > 1 or (exact and not _OBJECT_ID.fullmatch(exact[0])):
            raise _BrokerRefusal("the remote branch cannot be read")
        remote = exact[0] if exact else None
        if lease is not None and lease != (remote or ""):
            raise _BrokerRefusal("the remote branch is not at the lease's expected commit")
        if remote == head:
            return {"returncode": 0, "stdout": f"{branch} is already {head} on {slug}\n", "stderr": ""}
        if remote is not None:
            try:
                _git(self.boundary.repository, "merge-base", "--is-ancestor", remote, head, git_dir=self.boundary.common)
            except Refused:
                raise _BrokerRefusal("the remote branch is not an ancestor of head: only a fast-forward is "
                                     "published") from None
        lease = f"--force-with-lease={branch}:{remote or ''}"
        try:
            _git(self.boundary.repository, *credentials, "push", "--quiet", "--no-verify", "--porcelain", lease, url,
                 f"{head}:{branch}", git_dir=self.boundary.common, network=True)
        except Refused as exc:
            return {"returncode": 2, "stdout": "", "stderr": str(exc)}
        return {"returncode": 0, "stdout": f"pushed {head} to {branch} on {slug}\n", "stderr": ""}

    def _fetch_base(self, payload: dict) -> dict:
        """Fetch the repository's default branch into the primary's remote-tracking ref (fast-forward only)."""
        if payload:
            raise _BrokerRefusal("fetch-base takes no fields")
        slug = self._repository_slug()
        base = self._default_branch(slug)
        url, credentials = self._remote(slug)
        _git(self.boundary.repository, *credentials, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
             "--no-recurse-submodules", url, f"refs/heads/{base}:refs/remotes/origin/{base}",
             git_dir=self.boundary.common, network=True)
        return {"returncode": 0, "stdout": f"fetched {base} into refs/remotes/origin/{base}\n", "stderr": ""}

    def _pr_create(self, payload: dict) -> dict:
        if not set(payload) <= {"title", "body", "draft", "head", "base", "repo"} or not {"title", "body"} <= set(payload):
            raise _BrokerRefusal("pr-create takes title, body, draft, and optionally head, base and repo")
        title, body = self._text(payload, "title", 256), self._text(payload, "body", _MAX_BODY)
        draft = payload.get("draft", False)
        if type(draft) is not bool:
            raise _BrokerRefusal("draft must be true or false")
        slug = self._repository_slug()
        base = self._default_branch(slug)
        self._claimed({"repo": payload.get("repo"), "branch": payload.get("head"), "base": payload.get("base")}, slug,
                      base)
        fields = ["--head", self.boundary.branch.removeprefix("refs/heads/"), "--base", base,
                  f"--title={title}", "--body-file", self._text_file(body)]
        return self._publish("pr-create", slug, [*fields, *(["--draft"] if draft else [])])

    def _pr_edit(self, payload: dict) -> dict:
        if not set(payload) <= {"pr", "title", "body", "repo"} or not {"title", "body"} & set(payload):
            raise _BrokerRefusal("pr-edit takes pr, at least one of title and body, and optionally repo")
        title = self._optional_text(payload, "title", 256)
        body = self._optional_text(payload, "body", _MAX_BODY)
        number, slug, _facts = self._pull_request(payload, "OPEN")
        # ``--title=`` keeps a title that starts with a dash a value, never an option.
        fields = ["--number", str(number), *([f"--title={title}"] if title else []),
                  *(["--body-file", self._text_file(body)] if body else [])]
        return self._publish("pr-edit", slug, fields)

    def _pr_comment(self, payload: dict) -> dict:
        if not {"pr", "body"} <= set(payload) <= {"pr", "body", "repo"}:
            raise _BrokerRefusal("pr-comment takes pr, body and optionally repo")
        body = self._text(payload, "body", _MAX_BODY)
        number, slug, _facts = self._pull_request(payload, "OPEN", "MERGED")
        # The publisher refuses review-verdict markers in comment text: only the recorder writes them.
        return self._publish("pr-comment", slug, ["--number", str(number), "--body-file", self._text_file(body)])

    def _approval(self, head: str) -> str:
        """The review task of this worker that approved ``head``, from the host's task store, or a refusal.

        A worker cannot write the task store, and its comments cannot carry a verdict marker, so neither a
        comment nor a forged record grants this: the review must have been dispatched through this broker
        (``initiator`` is this task, its author model is the launch's), be done, cross-family, and its sealed
        reply must approve exactly ``head``.
        """
        import glob

        from lu_machinery.common.task_store_paths import tasks_dir
        from lu_machinery.review import record_cf_verdict as recorder

        task_id, author = self.boundary.args.task_id, self.boundary.args.author_model
        author_family = recorder.resolve_family(author) if isinstance(author, str) and author else "unknown"
        if author_family == "unknown" or author_family in recorder.UNRESOLVED_AUTHOR_FAMILIES:
            raise _BrokerRefusal("the launch names no author model family")
        root = tasks_dir()
        for path in sorted(root.glob(f"{glob.escape(task_id)}-review-*.json")):
            name = path.name.removesuffix(".json")
            if not _TASK_ID.fullmatch(name) or not re.fullmatch(r"[A-Za-z0-9._-]{1,40}",
                                                                name[len(task_id) + len("-review-"):]):
                continue
            try:
                task, reply = recorder._task(name, root)
                verdict = recorder._task_verdict(task, reply)
            except (recorder.RecordError, ValueError, OSError):
                continue
            if (verdict.verdict == "APPROVED" and verdict.sha == head and task.get("initiator") == task_id
                    and task.get("review_author_model") == author and task.get("review_mode") == "cross_family"
                    and verdict.family and verdict.family != author_family):
                return name
        raise _BrokerRefusal("no cross-family APPROVE of this head by a review this task dispatched is recorded "
                             "on the host")

    def _pr_merge(self, payload: dict) -> dict:
        """Enqueue the worker's own PR at its exact live head, once a host-held approval of that head exists.

        The publisher's own gates still run: the recorder's exact-head marker, the same-head green CI Gate,
        ``--match-head`` and merge-queue admission; the merge group's CI decides the landing.
        """
        if not {"pr", "head"} <= set(payload) <= {"pr", "head", "repo"}:
            raise _BrokerRefusal("pr-merge takes pr, head and optionally repo")
        head = payload["head"]
        if not (isinstance(head, str) and _OBJECT_ID.fullmatch(head)):
            raise _BrokerRefusal("head must be a full commit id")
        number, slug, facts = self._pull_request(payload, "OPEN")
        if not (head == facts.get("headRefOid") == self.boundary.current_tip()):
            raise _BrokerRefusal("head is not both the PR's live head and this task's branch tip")
        if facts.get("baseRefName") != self._default_branch(slug):
            raise _BrokerRefusal("the PR's base is not the repository's default branch")
        review = self._approval(head)
        reply = self._publish("pr-merge", slug, ["--number", str(number), "--match-head", head], "pr-merge")
        reply["stdout"] = f"approval: {review}\n" + reply["stdout"]
        return reply

    def _github_read(self, payload: dict) -> dict:
        """A named read of this repository through the publisher's fixed GET templates (no write is possible)."""
        if not set(payload) <= {"name", "number", "sha"} or "name" not in payload:
            raise _BrokerRefusal("github-read takes name, number and sha")
        name = payload["name"]
        if name not in _GITHUB_READS:
            raise _BrokerRefusal("name must be one of " + ", ".join(sorted(_GITHUB_READS)))
        fields = []
        if (number := payload.get("number")) is not None:
            if type(number) is not int or not 0 < number < 10**12:
                raise _BrokerRefusal("number must be a positive id")
            fields += ["--number", str(number)]
        if (sha := payload.get("sha")) is not None:
            if not (isinstance(sha, str) and _OBJECT_ID.fullmatch(sha)):
                raise _BrokerRefusal("sha must be a full commit id")
            fields += ["--sha", sha]
        return self._module("publish.github", ["read", name, "--repo", self._repository_slug(), *fields], "gh")

    def cleanup(self) -> None:
        self.closed = True
        with contextlib.suppress(OSError):
            self.server.close()
        if self.child is not None and self.child.poll() is None:
            with contextlib.suppress(OSError):
                self.child.kill()
        self.thread.join(timeout=5)


class _Boundary:
    def __init__(self, args: argparse.Namespace, receipt: dict):
        self.args, self.receipt = args, receipt
        self.worktree = Path(args.worktree).resolve(strict=True)
        self.base = Path(args.scratch)
        self.process: subprocess.Popen[bytes] | None = None
        self.egress: Any = None
        self.broker: _Broker | None = None
        self.relay: Any = None
        self.gh: Any = None
        self.created = False
        self.created_outputs: list[Path] = []

    def run(self) -> int:
        from lu_machinery.agent_runtime.attempt_network import AttemptEgress

        if reason := unavailable():
            raise Refused(reason)
        from lu_machinery.review.isolation import ReviewIsolationError, _resolve_fixed_system_executable

        try:
            bwrap = _resolve_fixed_system_executable("bwrap", reject_roots=())
        except ReviewIsolationError as exc:
            raise Refused("bubblewrap is unavailable") from exc
        # The private Git and HOME layers are bubblewrap overlays (--overlay-src, bubblewrap 0.10 and later).
        usage = subprocess.run([str(bwrap), "--help"], capture_output=True, text=True, timeout=30, check=False,  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
                               stdin=subprocess.DEVNULL)
        if "--overlay-src" not in usage.stdout + usage.stderr:
            raise Refused("bubblewrap 0.10 or later is required (--overlay-src)")
        command = self.args.command[1:] if self.args.command[:1] == ["--"] else self.args.command
        if not command or not Path(command[0]).is_absolute():
            raise Refused("the provider executable is not an absolute path")
        git_dir = Path(_git(self.worktree, "rev-parse", "--absolute-git-dir").strip()).resolve(strict=True)
        common = Path(_git(self.worktree, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
                      ).resolve(strict=True)
        repository = common.parent
        if main_checkout_root(self.worktree).resolve(strict=True) != repository or common.name != ".git":
            raise Refused("store_primary_disagreement")
        if git_dir == common or git_dir.parent != common / "worktrees":
            raise Refused("the task does not run in a linked worktree of the primary")
        if not self.worktree.is_relative_to(repository) and _overlaps(self.worktree, Path.home()):
            raise Refused("the worktree overlaps HOME outside the primary")
        dot_git = self.worktree / ".git"
        if dot_git.is_symlink() or not dot_git.is_file():
            raise Refused("the worktree's .git is not a link file")
        self.branch = _git(self.worktree, "symbolic-ref", "-q", "HEAD").strip()
        if not self.branch.startswith("refs/heads/"):
            raise Refused("the worktree is not on a branch")
        self.tip = _git(self.worktree, "rev-parse", "--verify", "HEAD^{commit}").strip()
        self.common, self.git_dir, self.repository = common, git_dir, repository
        # The primary's own Git state stays read-only inside, on top of the private overlay; that includes
        # its checked-out branch when the ref is a loose file (a packed one is covered by packed-refs).
        self.protected = [name for name in _PRIMARY_GIT_FILES if os.path.lexists(common / name)]
        try:
            primary_branch = _git(repository, "symbolic-ref", "-q", "HEAD").strip()
        except Refused:  # a detached primary has no checked-out branch; its HEAD file is protected
            primary_branch = ""
        if primary_branch.startswith("refs/heads/") and (common / primary_branch).is_file():
            self.protected.append(primary_branch)
        home = Path(os.environ.get("HOME") or Path.home()).resolve(strict=True)
        root = ensure_scratch_root().resolve()
        # The private layers must lie outside the overlay lowers and the read-only primary bind.
        for name, path in (("HOME", home), ("the primary", repository)):
            if root.is_relative_to(path):
                raise Refused(f"the scratch root is inside {name}")
        if not _under(self.base, [root]) or self.base.parent.resolve() != root:
            raise Refused("the scratch is not directly under the scratch root")

        self.base.mkdir(mode=0o700)  # exclusive: an existing scratch is never reused
        self.created = True
        boundary, tmp = self.base / "boundary", self.base / "tmp"
        read_only = self.args.mode == "read-only"
        layer_names = ("home-upper", "home-work") if read_only else ("home-upper", "home-work", "git-upper", "git-work")
        layers = {name: self.base / name for name in layer_names}
        for directory in (boundary, tmp, *layers.values()):
            directory.mkdir(mode=0o700)
        for name, source in _PROJECTED.items():
            _write_private(boundary / name, source.read_bytes(), 0o500)

        outputs = []
        for raw in self.args.output or []:
            path = Path(raw)
            if not path.is_absolute() or _overlaps(path, common) or _overlaps(path, self.worktree) or path.is_symlink():
                raise Refused("a declared output is not a plain path outside Git metadata and the worktree")
            if not path.exists():
                _write_private(path, b"", 0o600)
                self.created_outputs.append(path)
            elif not path.is_file():
                raise Refused("a declared output is not a regular file")
            outputs.append(path)
        credentials = []
        for raw in self.args.credential or []:
            path = Path(raw)
            if path.is_symlink() or not path.is_file() or _overlaps(path, repository):
                raise Refused("a credential file is not a regular file outside the primary")
            credentials.append(path)

        shared = _shared_state_roots(home)
        from lu_machinery.dispatcher.runtime_context import read_roots

        # A release or its advertised resources may live below /run or /tmp. Restore only the
        # declared read directories after the private tmpfs mounts, never their siblings.
        projected_reads = [root for path in read_roots() if (root := path.resolve()).is_dir()
                           and _under(root, _MASKED_ROOTS)]
        sockets = [path for path in _listening_socket_paths()
                   if (not _under(path, _MASKED_ROOTS) or _under(path, projected_reads)) and os.path.lexists(path)]
        nonce = uuid.uuid4().hex
        probes = self._probes(repository, common, sockets, outputs, tmp, nonce)
        for path in shared:
            probes.append({"op": "create", "path": str(path / f".write-boundary-canary-{nonce}"), "expect_any": ["EROFS"]})
        for path in projected_reads:
            probes.append({"op": "create", "path": str(path / f".write-boundary-canary-{nonce}"), "expect_any": ["EROFS"]})
        self.receipt["canary"] = {"probes_planned": len(probes), "passed": False}

        argv = [
            str(bwrap), "--unshare-all", "--unshare-user", "--disable-userns", "--die-with-parent", "--new-session",
            "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            *(arg for masked in _MASKED_ROOTS for arg in ("--tmpfs", str(masked))),
            "--overlay-src", str(home), "--overlay", str(layers["home-upper"]), str(layers["home-work"]), str(home),
            *(arg for path in shared for arg in ("--ro-bind", str(path), str(path))),
            "--ro-bind", str(repository), str(repository),
            *(["--ro-bind", str(common), str(common)] if read_only else
              ["--overlay-src", str(common), "--overlay", str(layers["git-upper"]), str(layers["git-work"]), str(common)]),
        ]
        for name in self.protected:
            argv += ["--ro-bind", str(common / name), str(common / name)]
        argv += ["--ro-bind" if read_only else "--bind", str(self.worktree), str(self.worktree),
                 "--ro-bind", str(dot_git), str(dot_git)]
        for path in credentials:
            argv += ["--bind", str(path), str(path)]
        for path in outputs:
            argv += ["--bind", str(path), str(path)]
        argv += ["--bind", str(tmp), str(tmp), "--ro-bind", str(boundary), str(boundary)]
        runtime = tmp
        if self.args.runtime is not None:
            from lu_machinery.agent_runtime.read_only_tmp import dispatch_lease

            runtime = Path(self.args.runtime)
            if not read_only or runtime != dispatch_lease(self.args.task_id) or runtime.is_symlink() or not runtime.is_dir():
                raise Refused("the runtime is not this read-only task's scratch lease")
            argv += ["--bind", str(tmp), str(runtime)]
        for path in projected_reads:
            argv += ["--ro-bind", str(path), str(path)]
        # No GitHub credential is readable inside (PUBLISH-CRED-1113): the host login's known stores are empty.
        for path in _github_credential_paths(home):
            argv += ["--tmpfs", str(path)] if path.is_dir() else ["--ro-bind", "/dev/null", str(path)]
        for path in sockets:
            argv += ["--ro-bind", "/dev/null", str(path)]
        self.broker = _Broker(self, boundary / "broker.sock")
        argv += ["--ro-bind", str(boundary), _INSIDE]
        interpreter = sources_command()[0]
        if _under(_interpreter_root(), [home]):
            # The writable HOME layer would let a worker replace the interpreter the Sources command runs.
            argv += ["--ro-bind", str(_interpreter_root()), str(_interpreter_root())]
        if read_only:
            (boundary / "bin").mkdir(mode=0o700)
            _write_private(boundary / "bin" / "gh", agy_sandbox._GH_SHIM.format(interpreter=interpreter).encode(), 0o500)
            self.gh = agy_sandbox._GhBroker(boundary / "gh.sock", self.worktree)
        try:
            # Sources goes through the read-only AGY boundary's relay: read-only tools only, one server per
            # connection, started by the launcher from the product checkout. Providers reach it through
            # ``sources_command``.
            from lu_machinery.agent_runtime.sources_read_only import sources_tool_sets

            readers, _writers = sources_tool_sets()
            self.relay = agy_sandbox.SourcesRelay(boundary / "sources.sock", frozenset(readers), {
                name: os.environ[name] for name in ("PATH", "HOME", "LANG", "TMPDIR") if name in os.environ})
        except Exception as exc:  # Sources is optional for a write worker; containment does not depend on it
            self.receipt["sources"] = {"status": "unavailable", "reason": f"{type(exc).__name__}: {exc}"[:200]}
        argv += ["--cap-drop", "ALL", "--chdir", str(self.worktree)]

        self.egress = AttemptEgress(boundary / "egress.sock", refused_domains=GITHUB_DOMAINS,
                                    refused_networks=github_networks())
        report_read, report_write = os.pipe()
        go_read, go_write = os.pipe()
        _write_private(boundary / "plan.json", json.dumps(
            {"report_fd": report_write, "go_fd": go_read, "probes": probes}).encode(), 0o444)
        argv += [interpreter, "-I", str(boundary / "canary.py"), str(boundary / "plan.json"), "--",
                 interpreter, "-I", str(boundary / "forwarder.py"), str(boundary / "egress.sock"), *command]
        self.receipt["bwrap"] = str(bwrap)
        self.receipt["argv_sha256"] = __import__("hashlib").sha256(json.dumps(argv).encode()).hexdigest()
        env = {key: value for key, value in os.environ.items()
               if key not in {"SSH_AUTH_SOCK", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "TMUX", "TMUX_PANE"}
               and key not in self.args.host_only and not _github_credential_variable(key)}
        (tmp / "scratch").mkdir(mode=0o700)
        (tmp / "run").mkdir(mode=0o700)
        # The namespace has no DNS or route out; Node's own fetch uses the forwarder's proxy only when told to.
        # The host's task scratch (/var/tmp/<task-id>) is read-only inside, so it moves under TMPDIR too; the worker
        # still creates it on demand, and it goes with the boundary's private scratch.
        env.update(TMPDIR=str(runtime), LU_SCRATCH_ROOT=str(runtime / "scratch"), XDG_RUNTIME_DIR=str(runtime / "run"),
                   LU_TASK_SCRATCH=str(runtime / "task"), GIT_TERMINAL_PROMPT="0", NODE_USE_ENV_PROXY="1", **{BROKER_ENV: _BROKER_INSIDE})
        if read_only:
            env["PATH"] = str(boundary / "bin") + os.pathsep + env.get("PATH", os.defpath)
        env.update(git_push_settings(env, interpreter, str(machinery_path("dispatcher/write_boundary.py").parents[2])))
        try:
            # The argv is the fixed bubblewrap binary and launcher-validated paths; env is the dispatcher's
            # provider environment, passed on deliberately (the provider runs inside with it).
            process = subprocess.Popen(argv, env=env, cwd=self.worktree, pass_fds=(report_write, go_read))  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit, python.lang.security.audit.dangerous-subprocess-use-tainted-env-args
            self.process = process
        finally:
            os.close(report_write)
            os.close(go_read)
        try:
            outcomes = self._report(report_read)
            failed = [probe for probe in outcomes or [] if probe.get("outcome") not in probe.get("expect_any", [])]
            passed = (isinstance(outcomes, list) and len(outcomes) == len(probes) and not failed and all(
                {key: probe.get(key) for key in planned} == planned for probe, planned in zip(outcomes, probes,
                                                                                              strict=True)))
            self.receipt["canary"].update(probes=outcomes, passed=passed)
            if not passed:
                raise Refused("the write boundary canary failed: " + ", ".join(
                    f"{probe.get('op')} {probe.get('path')} gave {probe.get('outcome')}" for probe in failed[:5]))
            os.write(go_write, b"1")
        finally:
            os.close(go_write)
            os.close(report_read)
        self.receipt["status"] = "ran"
        returncode = process.wait()
        self.receipt["returncode"] = returncode
        self.receipt["import"] = self._finish_git()
        if self.broker.closeout_request is not None:
            # Run by the supervisor once it has settled this task's worktree (merge_closeout leaves registered
            # worktrees in place and reports them), never by the launcher while the worktree still exists.
            self.receipt["closeout_request"] = self.broker.closeout_request
        return returncode

    def _probes(self, repository: Path, common: Path, sockets: list[Path], outputs: list[Path], tmp: Path,
                nonce: str) -> list[dict]:
        probes: list[dict] = []
        readonly = self.args.mode == "read-only"
        mount_refusal = ("EROFS", "EBUSY") if readonly else ("EBUSY",)

        def expect(op: str, path: Path | str, *outcomes: str, port: int | None = None) -> None:
            probes.append({"op": op, "path": str(path), "expect_any": list(outcomes), **({"port": port} if port else {})})

        tracked = _writable_tracked_file(repository)
        absent = repository / f".write-boundary-canary-{nonce}"
        expect("write_open", tracked, "EROFS")
        expect("chmod", tracked, "EROFS")
        for op in ("create", "mkdir", "rename", "unlink"):
            expect(op, absent, "EROFS")
        for name in self.protected:
            path = common / name
            if path.is_dir():
                expect("create", path / f"canary-{nonce}", "EROFS")
            else:
                expect("write_open", path, "EROFS")
                expect("unlink", path, *mount_refusal)
            expect("rename", path, *mount_refusal)  # read-only parent or mount point: neither can be replaced
        dot_git = self.worktree / ".git"
        expect("write_open", dot_git, "EROFS")
        expect("rename", dot_git, *mount_refusal)
        # Positive controls: the worktree, the private Git overlay, HOME's overlay and TMPDIR take writes.
        for directory in (self.worktree, common / "objects"):
            expect("create" if readonly else "scratch_cycle", directory / f".write-boundary-canary-{nonce}",
                   "EROFS" if readonly else "OK")
        for directory in (Path(os.environ.get("HOME") or Path.home()), tmp):
            expect("scratch_cycle", directory / f".write-boundary-canary-{nonce}", "OK")
        for path in outputs:
            expect("write_open", path, "OK")
        for port in _LOOPBACK_PORTS:
            expect("connect_tcp", "127.0.0.1", "ECONNREFUSED", "ENETUNREACH", port=port)
        # The fixed Sources command cannot be redirected: its directory is a mount point and its interpreter's
        # installation is read-only.
        expect("rename", _INSIDE, "EBUSY")
        expect("create", _interpreter_root() / f".write-boundary-canary-{nonce}", "EROFS")
        if (abstract := agy_sandbox._host_abstract_socket()) is not None:
            expect("connect_abstract", abstract, "ECONNREFUSED")
        for path in sockets:
            expect("connect_unix", path, "ECONNREFUSED")
        runtime_dir = Path(f"/run/user/{os.getuid()}")
        for masked in (runtime_dir / "bus", os.environ.get("SSH_AUTH_SOCK"), "/run/dbus/system_bus_socket"):
            if masked and os.path.lexists(masked):
                expect("connect_unix", masked, "ENOENT")
        return probes

    def _report(self, descriptor: int) -> list | None:
        chunks: list[bytes] = []
        deadline = time.monotonic() + _CANARY_TIMEOUT
        while (remaining := deadline - time.monotonic()) > 0:
            ready, _, _ = select.select([descriptor], [], [], remaining)
            if not ready:
                break
            chunk = os.read(descriptor, 65536)
            if not chunk:
                with contextlib.suppress(ValueError):
                    report = json.loads(b"".join(chunks))
                    return report.get("probes") if isinstance(report, dict) else None
                return None
            chunks.append(chunk)
        raise Refused("the write boundary canary did not report in time")

    def _upper_tip(self, upper: Path) -> str | None:
        """The dispatched branch's tip the worker left in the private overlay, or None when unchanged."""
        loose = upper / self.branch
        try:
            mode = os.lstat(loose).st_mode
        except FileNotFoundError:
            mode = None
        if mode is not None and stat.S_ISREG(mode):
            return loose.read_text(encoding="ascii", errors="replace").strip()
        if mode is not None and not stat.S_ISCHR(mode):
            raise Refused("the dispatched branch ref is not a file")
        # Absent from the upper layer, or a whiteout (the worker removed the loose ref, e.g. by packing it).
        packed = upper / "packed-refs"
        if os.path.lexists(packed):
            if not stat.S_ISREG(os.lstat(packed).st_mode):
                raise Refused("packed-refs is not a file")
            for line in packed.read_text(encoding="ascii", errors="replace").splitlines():
                value, _, name = line.partition(" ")
                if name == self.branch:
                    return value.strip()
        if mode is not None:
            raise Refused("the dispatched branch was deleted")
        return None

    def current_tip(self) -> str:
        """The dispatched branch's tip as the worker sees it now (its private layer, else the launch tip)."""
        tip = self._upper_tip(self.base / "git-upper")
        return self.tip if tip is None else tip

    def fetch_objects(self, tip: str) -> None:
        """Copy ``tip`` and its history from the worker's private layer into the primary's object store.

        A private repository whose alternates are the upper objects is fetched from, so index-pack hashes each
        received object and only objects whose names match their bytes arrive. No ref of the primary changes.
        """
        if not _OBJECT_ID.fullmatch(tip):
            raise Refused("the branch tip is not an object id")
        upper = self.base / "git-upper"
        private = self.base / f"import-{uuid.uuid4().hex}.git"
        _git(self.base, "init", "-q", "--bare", str(private))
        alternates = [str(self.common / "objects")]
        if (upper / "objects").is_dir():
            alternates.insert(0, str(upper / "objects"))
        (private / "objects" / "info").mkdir(parents=True, exist_ok=True)
        (private / "objects" / "info" / "alternates").write_text("\n".join(alternates) + "\n")
        _git(private, "update-ref", "refs/import", tip, git_dir=private)
        _git(self.repository, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules",
             str(private), "refs/import", git_dir=self.common)

    def _import(self) -> dict:
        upper = self.base / "git-upper"
        head = upper / self.git_dir.relative_to(self.common) / "HEAD"
        result: dict = {"branch": self.branch, "before": self.tip}
        try:
            with contextlib.suppress(FileNotFoundError):
                if head.read_text(encoding="ascii", errors="replace").strip() != f"ref: {self.branch}":
                    result["head_moved"] = True
            tip = self._upper_tip(upper)
            if tip is None or tip == self.tip:
                return {**result, "status": "unchanged", "after": self.tip}
            if not _OBJECT_ID.fullmatch(tip):
                return {**result, "status": "invalid_tip"}
            self.fetch_objects(tip)
            _git(self.repository, "update-ref", self.branch, tip, self.tip, git_dir=self.common)
            _git(self.worktree, "reset", "--quiet", "--mixed")
            return {**result, "status": "imported", "after": tip}
        except (Refused, OSError, subprocess.SubprocessError) as exc:
            return {**result, "status": "failed", "reason": str(exc)[:200]}

    def _finish_git(self) -> dict:
        # Reviewers cannot update Git metadata; there is no private branch tip to import.
        if self.args.mode == "read-only":
            return {"status": "unchanged", "before": self.tip, "after": self.tip}
        return self._import()

    def cleanup(self) -> None:
        if self.process is not None and self.process.poll() is None:
            # bwrap is the PID namespace's parent: every process of the sandbox ends with it.
            with contextlib.suppress(OSError):
                self.process.kill()
            self.process.wait()
        if self.receipt.get("status") in ("ran", "interrupted") and "import" not in self.receipt:
            # A cancelled or timed-out run still hands over the commits it made on the dispatched branch.
            self.receipt["import"] = self._finish_git()
        if self.gh is not None:
            self.gh.cleanup()
            self.receipt["github_reads"] = list(self.gh.records)
        if self.relay is not None:
            with contextlib.suppress(Exception):
                self.relay.cleanup()
            self.receipt["sources"] = self.relay.evidence()
        if self.broker is not None:
            with contextlib.suppress(Exception):
                self.broker.cleanup()
            self.receipt["broker"] = list(self.broker.records)
        if self.egress is not None:
            with contextlib.suppress(Exception):
                self.egress.cleanup()
            self.receipt["egress"] = list(self.egress.records)
            self.receipt["egress_overflow_count"] = self.egress.overflow_count
        for path in self.created_outputs:
            with contextlib.suppress(OSError):
                if path.stat().st_size == 0:
                    path.unlink()
        if self.created:
            try:
                _remove(self.base)
            except OSError as exc:
                self.receipt["cleanup"] = f"residue: {type(exc).__name__}"


# ``git push`` inside the boundary (#443). The launcher rewrites GitHub push URLs (``url.<base>.pushInsteadOf``) to
# Git's documented remote-helper transport (gitremote-helpers(7)) named ``GIT_HELPER``. Git runs a helper as
# ``git remote-<name>``; that name is a Git alias here, so no file, PATH entry or other tool is added or shadowed.
# Fetches keep their URL (and fail inside, as before). The helper sends each ref update as the broker's ``push``
# verb with the target the caller named; the broker refuses any target that is not the launch's own.
GIT_HELPER = "lu-write-boundary"
_GITHUB_PUSH_URLS = ("https://github.com/", "git@github.com:", "ssh://git@github.com/")


def git_push_settings(env: dict[str, str], interpreter: str, package_parent: str) -> dict[str, str]:
    """``GIT_CONFIG_*`` entries that send ``git push`` to GitHub through the broker, after any the caller set."""
    count = env.get("GIT_CONFIG_COUNT", "0")
    start = int(count) if count.isdecimal() else 0
    command = [interpreter, "-I", "-c", _BOOTSTRAP, package_parent, "git-remote"]
    settings = [(f"alias.remote-{GIT_HELPER}", "!exec " + " ".join(shlex.quote(word) for word in command))]
    settings += [(f"url.{GIT_HELPER}::https://github.com/.pushInsteadOf", prefix) for prefix in _GITHUB_PUSH_URLS]
    entries = {"GIT_CONFIG_COUNT": str(start + len(settings))}
    for index, (key, value) in enumerate(settings, start):
        entries[f"GIT_CONFIG_KEY_{index}"] = key
        entries[f"GIT_CONFIG_VALUE_{index}"] = value
    return entries


def git_remote_main(argv: list[str]) -> int:
    """Git's remote helper for ``git push`` inside the boundary: each ref update becomes one ``push`` request."""
    if len(argv) != 2:
        print("write boundary git helper: expected the remote and its URL", file=sys.stderr)
        return 2
    remote, url = argv
    match = _GITHUB_REMOTE.fullmatch(url)
    leases: dict[str, str] = {}
    pending: list[str] = []

    def reply(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], capture_output=True, text=True, check=False, timeout=_GIT_TIMEOUT)

    def listing() -> str:
        """The remote-tracking refs as the remote's refs: Git's own checks then see what it last pushed or fetched.

        The broker checks the live remote branch itself (fast-forward only, and any lease).
        """
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", remote):
            return ""
        prefix = f"refs/remotes/{remote}/"
        refs = git("for-each-ref", "--format=%(objectname) %(refname)", prefix).stdout.splitlines()
        return "".join(f"{oid} refs/heads/{name.removeprefix(prefix)}\n" for oid, _, name in
                       (line.partition(" ") for line in refs) if name != prefix + "HEAD")

    def push(spec: str) -> str:
        source, _, destination = spec.removeprefix("+").partition(":")
        if not source:
            return f"error {destination} deleting a branch is not published through the write boundary broker\n"
        if match is None:
            return f"error {destination} the remote is not a github.com repository\n"
        resolved = git("rev-parse", "--verify", "--end-of-options", source + "^{commit}")
        if resolved.returncode != 0:
            return f"error {destination} the source is not a commit\n"
        payload = {"head": resolved.stdout.strip(), "branch": destination, "repo": match.group(1)}
        if destination in leases:
            expected = leases[destination]
            payload["lease"] = "" if not expected.strip("0") else expected
        answer = broker_call("push", payload)
        sys.stderr.write(str(answer.get("stdout", "")) + str(answer.get("stderr", "")))
        if answer.get("returncode") == 0:
            return f"ok {destination}\n"
        reason = str(answer.get("stderr", "")).strip().splitlines()
        return f"error {destination} {(reason[-1] if reason else 'the broker did not push').replace(chr(13), ' ')}\n"

    for raw in sys.stdin:
        line = raw.rstrip("\n")
        if line == "capabilities":
            reply("push\noption\n\n")
        elif line.startswith("option "):
            name, _, value = line.removeprefix("option ").partition(" ")
            reference, colon, expected = value.partition(":")
            if name == "cas" and colon and reference.startswith("refs/") and _OBJECT_ID.fullmatch(expected):
                leases[reference] = expected
                reply("ok\n")
            else:
                reply("unsupported\n")
        elif line in {"list", "list for-push"}:
            reply(listing() + "\n")
        elif line.startswith("push "):
            pending.append(line.removeprefix("push "))
        elif not line and pending:
            reply("".join(push(spec) for spec in pending) + "\n")
            pending = []
        elif not line:
            return 0
        else:
            print(f"write boundary git helper: unsupported command {line[:40]!r}", file=sys.stderr)
            return 1
    return 0


def call_main(argv: list[str]) -> int:
    """``call <verb>``: send one broker request from inside a write boundary; the payload is JSON on stdin."""
    parser = argparse.ArgumentParser(
        prog="python -m lu_machinery.dispatcher.write_boundary call",
        description="Send one request to the write boundary's host broker (inside a dispatched write worker).\n"
                    "The payload is one JSON object on stdin; docs/runbooks/write-worker-boundary.md lists each verb's "
                    "fields.",
        epilog="Example:\n  echo '{\"head\": \"<40-hex>\"}' | python -m lu_machinery.dispatcher.write_boundary call push\n"
               "Outputs: the host step's stdout and stderr.\n"
               "Exit codes: the host step's status; 2 when the broker refuses the request or no broker is present.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("verb", choices=_BROKER_VERBS)
    args = parser.parse_args(argv)
    if broker_path() is None:
        print("write boundary broker: not inside a write boundary", file=sys.stderr)
        return 2
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        print("write boundary broker: the payload is not one JSON object", file=sys.stderr)
        return 2
    reply = broker_call(args.verb, payload)
    sys.stdout.write(str(reply.get("stdout", "")))
    sys.stderr.write(str(reply.get("stderr", "")))
    code = reply.get("returncode")
    return code if type(code) is int and 0 <= code < 256 else 1


def main(argv: list[str]) -> int:
    if argv[:1] == ["call"]:
        return call_main(argv[1:])
    if argv[:1] == ["git-remote"]:
        return git_remote_main(argv[1:])
    parser = argparse.ArgumentParser(
        prog="lu_machinery.dispatcher.write_boundary",
        description="Run one dispatched provider inside the dispatcher's bubblewrap boundary (#423, #100).\n"
                    "Internal launcher used by the worker; not run by hand.",
        epilog="Outputs: the provider's stdout and stderr; the boundary receipt at --receipt.\n"
               f"Exit codes: the provider's status, or {REFUSED} when the boundary refuses to start it.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--scratch", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--mode", choices=("workspace-write", "read-only"), default="workspace-write")
    parser.add_argument("--runtime", help="The read-only Codex task's existing private scratch lease")
    parser.add_argument("--author-model", help="The dispatched model, the review family check's author "
                        "(default: none; the broker then dispatches no review)")
    parser.add_argument("--output", action="append", help="A declared output file the provider writes")
    parser.add_argument("--credential", action="append", help="A provider credential file a refresh rewrites")
    parser.add_argument("--host-env", action="append", default=[],
                        help="NAME=VALUE of the dispatcher's host configuration, for the launcher only (default: none)")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    receipt = {"schema": SCHEMA, "task_id": args.task_id, "mode": args.mode, "status": "refused", "started_at": _now()}
    if not _TASK_ID.fullmatch(args.task_id):
        receipt["reason"] = "unsafe task id"
        _write_receipt(Path(args.receipt), receipt)
        return REFUSED
    args.host_only = set()
    for item in args.host_env:
        name, equals, value = item.partition("=")
        if name not in _HOST_CONTEXT or not equals or "\0" in value:
            receipt["reason"] = f"--host-env names an unknown variable: {name[:40]}"
            _write_receipt(Path(args.receipt), receipt)
            return REFUSED
        os.environ[name] = value  # the launcher's own host steps read it; the provider never sees it
        args.host_only.add(name)
    boundary = None

    def cancelled(_signum, _frame):
        raise Refused("cancelled")

    previous = signal.signal(signal.SIGTERM, cancelled)
    try:
        boundary = _Boundary(args, receipt)
        return boundary.run()
    except Exception as exc:  # every failure refuses; nothing falls back to an unsandboxed run
        if receipt["status"] != "ran":
            receipt["reason"] = str(exc) if isinstance(exc, Refused) else f"setup failed: {type(exc).__name__}: {exc}"[:500]
            print(f"write boundary refused: {receipt['reason']}", file=sys.stderr)
            return REFUSED
        receipt.update(status="interrupted", reason=f"{exc if isinstance(exc, Refused) else type(exc).__name__}")
        return REFUSED
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            if boundary is not None:
                boundary.cleanup()
        finally:
            receipt["finished_at"] = _now()
            _write_receipt(Path(args.receipt), receipt)
            signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

"""OS boundary for every dispatched write worker (#423, decision CONTAIN-OS-1113).

The primary-checkout write guard parses shell commands and is only an accident tripwire. This boundary
makes the primary checkout read-only for the operating system instead, so no command spelling writes it.
``worker.supervise`` launches every ``workspace-write`` provider (Claude, Codex, Cursor, AGY, OpenCode)
through ``launcher``; the launcher runs the provider and all of its descendants under bubblewrap and
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
  private upper directory, so the worker can commit, fetch and push, but the host's HEAD, index,
  configuration, hooks, refs and objects never change; the primary's HEAD, index, configuration,
  ``packed-refs`` and hooks are also read-only binds inside, and so is the worktree's ``.git`` link;
* HOME is an overlay as well, so the CLIs keep their configuration and state while nothing they write
  outlives the run, except the provider credential files named by ``--credential``, which are
  writable binds so a token refresh is not lost;
* the worktree, a private TMPDIR and the declared outputs (``--output``) are the only other writable
  paths.

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
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

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
        "--receipt", str(receipt_file(task_file)),
    ]
    for path in declared_outputs(spec, invocation):
        argv += ["--output", str(path)]
    for path in credential_files(spec.agent, invocation.env):
        argv += ["--credential", str(path)]
    return [*argv, "--", str(Path(found).absolute()), *invocation.argv[1:]]


def verify(task_file: Path, task_id: str) -> tuple[dict, str | None]:
    """The boundary evidence and the refusal that fails the run, if any."""
    path = receipt_file(task_file)
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "missing"}, "write_boundary_unproven: no boundary receipt"
    summary = {key: receipt.get(key) for key in (
        "status", "reason", "argv_sha256", "bwrap", "returncode", "import", "cleanup")}
    summary["canary_passed"] = bool(receipt.get("canary", {}).get("passed"))
    if receipt.get("schema") != SCHEMA or receipt.get("task_id") != task_id:
        return summary, "write_boundary_unproven: the receipt names another run"
    if receipt.get("status") != "ran" or not summary["canary_passed"]:
        return summary, f"write_boundary_refused: {receipt.get('reason') or receipt.get('status')}"
    if (receipt.get("import") or {}).get("status") not in ("unchanged", "imported"):
        return summary, f"write_boundary_import_failed: {(receipt.get('import') or {}).get('status')}"
    return summary, None


def _git(cwd: Path, *args: str, git_dir: Path | None = None) -> str:
    """Host Git with no user or system configuration, hooks or fsmonitor: nothing the worker wrote runs."""
    env = {"PATH": os.defpath, "HOME": os.environ.get("HOME", "/"), "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_OPTIONAL_LOCKS": "0"}
    result = subprocess.run(
        ["git", "--no-replace-objects", "-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false",
         "-c", "protocol.allow=never", "-c", "protocol.file.allow=always",
         *([f"--git-dir={git_dir}"] if git_dir else []), "-C", str(cwd), *args],
        capture_output=True, text=True, timeout=_GIT_TIMEOUT, check=False, env=env, stdin=subprocess.DEVNULL,
    )
    if result.returncode:
        raise Refused(f"git {args[0]} failed")
    return result.stdout


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


def _under(path: Path, roots) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def _overlaps(path: Path, other: Path) -> bool:
    return path == other or path.is_relative_to(other) or other.is_relative_to(path)


class _Boundary:
    def __init__(self, args: argparse.Namespace, receipt: dict):
        self.args, self.receipt = args, receipt
        self.worktree = Path(args.worktree).resolve(strict=True)
        self.base = Path(args.scratch)
        self.process = self.egress = None
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
        for name, path in (("HOME", home), ("the primary", repository)):
            if _overlaps(root, path):
                raise Refused(f"the scratch root overlaps {name}")
        if not _under(self.base, [root]) or self.base.parent.resolve() != root:
            raise Refused("the scratch is not directly under the scratch root")

        self.base.mkdir(mode=0o700)  # exclusive: an existing scratch is never reused
        self.created = True
        boundary, tmp = self.base / "boundary", self.base / "tmp"
        layers = {name: self.base / name for name in ("home-upper", "home-work", "git-upper", "git-work")}
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
        sockets = [path for path in _listening_socket_paths()
                   if not _under(path, _MASKED_ROOTS) and os.path.lexists(path)]
        nonce = uuid.uuid4().hex
        probes = self._probes(repository, common, sockets, outputs, tmp, nonce)
        for path in shared:
            probes.append({"op": "create", "path": str(path / f".write-boundary-canary-{nonce}"), "expect_any": ["EROFS"]})
        self.receipt["canary"] = {"probes_planned": len(probes), "passed": False}

        argv = [
            str(bwrap), "--unshare-all", "--unshare-user", "--disable-userns", "--die-with-parent", "--new-session",
            "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            *(arg for masked in _MASKED_ROOTS for arg in ("--tmpfs", str(masked))),
            "--overlay-src", str(home), "--overlay", str(layers["home-upper"]), str(layers["home-work"]), str(home),
            *(arg for path in shared for arg in ("--ro-bind", str(path), str(path))),
            "--ro-bind", str(repository), str(repository),
            "--overlay-src", str(common), "--overlay", str(layers["git-upper"]), str(layers["git-work"]), str(common),
        ]
        for name in self.protected:
            argv += ["--ro-bind", str(common / name), str(common / name)]
        argv += ["--bind", str(self.worktree), str(self.worktree), "--ro-bind", str(dot_git), str(dot_git)]
        for path in credentials:
            argv += ["--bind", str(path), str(path)]
        for path in outputs:
            argv += ["--bind", str(path), str(path)]
        argv += ["--bind", str(tmp), str(tmp), "--ro-bind", str(boundary), str(boundary)]
        for path in sockets:
            argv += ["--ro-bind", "/dev/null", str(path)]
        argv += ["--cap-drop", "ALL", "--chdir", str(self.worktree)]

        self.egress = AttemptEgress(boundary / "egress.sock")
        report_read, report_write = os.pipe()
        go_read, go_write = os.pipe()
        _write_private(boundary / "plan.json", json.dumps(
            {"report_fd": report_write, "go_fd": go_read, "probes": probes}).encode(), 0o444)
        interpreter = str(Path(sys.executable).resolve(strict=True))
        argv += [interpreter, "-I", str(boundary / "canary.py"), str(boundary / "plan.json"), "--",
                 interpreter, "-I", str(boundary / "forwarder.py"), str(boundary / "egress.sock"), *command]
        self.receipt["bwrap"] = str(bwrap)
        self.receipt["argv_sha256"] = __import__("hashlib").sha256(json.dumps(argv).encode()).hexdigest()
        env = {key: value for key, value in os.environ.items()
               if key not in {"SSH_AUTH_SOCK", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "TMUX", "TMUX_PANE"}}
        (tmp / "scratch").mkdir(mode=0o700)
        (tmp / "run").mkdir(mode=0o700)
        # The namespace has no DNS or route out; Node's own fetch uses the forwarder's proxy only when told to.
        env.update(TMPDIR=str(tmp), LU_SCRATCH_ROOT=str(tmp / "scratch"), XDG_RUNTIME_DIR=str(tmp / "run"),
                   GIT_TERMINAL_PROMPT="0", NODE_USE_ENV_PROXY="1")
        try:
            self.process = subprocess.Popen(argv, env=env, cwd=self.worktree, pass_fds=(report_write, go_read))
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
        returncode = self.process.wait()
        self.receipt["returncode"] = returncode
        self.receipt["import"] = self._import()
        return returncode

    def _probes(self, repository: Path, common: Path, sockets: list[Path], outputs: list[Path], tmp: Path,
                nonce: str) -> list[dict]:
        probes: list[dict] = []

        def expect(op: str, path: Path | str, *outcomes: str, port: int | None = None) -> None:
            probes.append({"op": op, "path": str(path), "expect_any": list(outcomes), **({"port": port} if port else {})})

        tracked = next((repository / name for name in _git(repository, "ls-files", "-z").split("\0")
                        if name and (repository / name).is_file() and not (repository / name).is_symlink()), None)
        if tracked is None:
            raise Refused("the primary has no tracked regular file to probe")
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
                expect("unlink", path, "EBUSY")
            expect("rename", path, "EBUSY")  # a mount point cannot be replaced or moved
        dot_git = self.worktree / ".git"
        expect("write_open", dot_git, "EROFS")
        expect("rename", dot_git, "EBUSY")
        # Positive controls: the worktree, the private Git overlay, HOME's overlay and TMPDIR take writes.
        for directory in (self.worktree, common / "objects", Path(os.environ.get("HOME") or Path.home()), tmp):
            expect("scratch_cycle", directory / f".write-boundary-canary-{nonce}", "OK")
        for path in outputs:
            expect("write_open", path, "OK")
        for port in _LOOPBACK_PORTS:
            expect("connect_tcp", "127.0.0.1", "ECONNREFUSED", "ENETUNREACH", port=port)
        if (name := agy_sandbox._host_abstract_socket()) is not None:
            expect("connect_abstract", name, "ECONNREFUSED")
        for path in sockets:
            expect("connect_unix", path, "ECONNREFUSED")
        runtime_dir = Path(f"/run/user/{os.getuid()}")
        for path in (runtime_dir / "bus", os.environ.get("SSH_AUTH_SOCK"), "/run/dbus/system_bus_socket"):
            if path and os.path.lexists(path):
                expect("connect_unix", path, "ENOENT")
        return probes

    def _report(self, descriptor: int) -> list | None:
        chunks, deadline = [], time.monotonic() + _CANARY_TIMEOUT
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
            private = self.base / "import.git"
            _git(self.base, "init", "-q", "--bare", str(private))
            alternates = [str(self.common / "objects")]
            if (upper / "objects").is_dir():
                alternates.insert(0, str(upper / "objects"))
            (private / "objects" / "info").mkdir(parents=True, exist_ok=True)
            (private / "objects" / "info" / "alternates").write_text("\n".join(alternates) + "\n")
            _git(private, "update-ref", "refs/import", tip, git_dir=private)
            # index-pack hashes each received object, so only objects whose names match their bytes arrive.
            _git(self.repository, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules",
                 str(private), "refs/import", git_dir=self.common)
            _git(self.repository, "update-ref", self.branch, tip, self.tip, git_dir=self.common)
            _git(self.worktree, "reset", "--quiet", "--mixed")
            return {**result, "status": "imported", "after": tip}
        except (Refused, OSError, subprocess.SubprocessError) as exc:
            return {**result, "status": "failed", "reason": str(exc)[:200]}

    def cleanup(self) -> None:
        if self.process is not None and self.process.poll() is None:
            # bwrap is the PID namespace's parent: every process of the sandbox ends with it.
            with contextlib.suppress(OSError):
                self.process.kill()
            self.process.wait()
        if self.receipt.get("status") in ("ran", "interrupted") and "import" not in self.receipt:
            # A cancelled or timed-out run still hands over the commits it made on the dispatched branch.
            self.receipt["import"] = self._import()
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


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="lu_machinery.dispatcher.write_boundary",
        description="Run one dispatched write worker inside the dispatcher's bubblewrap boundary (#423).\n"
                    "Internal launcher used by the worker for --mode workspace-write; not run by hand.",
        epilog="Outputs: the provider's stdout and stderr; the boundary receipt at --receipt.\n"
               f"Exit codes: the provider's status, or {REFUSED} when the boundary refuses to start it.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--scratch", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--output", action="append", help="A declared output file the provider writes")
    parser.add_argument("--credential", action="append", help="A provider credential file a refresh rewrites")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    receipt = {"schema": SCHEMA, "task_id": args.task_id, "status": "refused", "started_at": _now()}
    if not _TASK_ID.fullmatch(args.task_id):
        receipt["reason"] = "unsafe task id"
        _write_receipt(Path(args.receipt), receipt)
        return REFUSED
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

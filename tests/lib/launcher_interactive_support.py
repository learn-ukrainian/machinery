"""Non-test support from LU 5aba2adb tests/test_launcher_interactive_scope.py.

The test file stays unported as PORT-T8-B02. Definitions below are copied
verbatim from its adapted wheel fixture for the three driver-scope imports.
"""

from __future__ import annotations


import fcntl


import os


import re


import select


import shlex




import signal


import struct


import subprocess


import sys


import termios


import time


from collections.abc import Callable


from contextlib import suppress


from pathlib import Path


import pytest




from tests.helpers import machinery_package


from tests.helpers.product_tree import configured_roots as configured_roots


from tests.launcher_sandbox import (
    copy_launcher_sources,
    copy_slot_registry,
    launcher_environment,
)


from tests.lib.test_launcher_driver_scope import default_hup as default_hup


from tests.lib.test_launcher_driver_scope import (
    scope_sandbox,
)




MACHINERY = machinery_package()


DEADLINE_S = 60


def _test_processes(tmp_path: Path) -> list[int]:
    """Find processes still using this test's checkout or exported fixture paths."""
    root = os.fsencode(tmp_path)
    survivors = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            cwd = os.fsencode(os.readlink(entry / "cwd"))
        except OSError:
            cwd = b""
        try:
            environ = (entry / "environ").read_bytes()
        except OSError:
            environ = b""
        if cwd == root or cwd.startswith(root + b"/") or root + b"/" in environ or b"=" + root + b"\0" in environ:
            survivors.append(int(entry.name))
    return sorted(survivors)


def _assert_no_test_processes(tmp_path: Path) -> None:
    survivors = _test_processes(tmp_path)
    assert not survivors, f"test left running processes: {survivors}"


_EVENT = """lu_test_event() {
  local line cg=""
  while IFS= read -r line; do case "$line" in 0::*) cg="${line#0::}" ;; esac; done < "$FAKE_CGROUP"
  printf '%s cgroup=%s pid=%s\\n' "$1" "${cg##*/}" "$$" >> "$TEST_EVENTS"
}
"""


_ADAPTER = (
    _EVENT
    + """launcher_adapter_validate() { lu_test_event validate; }
launcher_adapter_preflight() { lu_test_event preflight; LC_AUTH_SOURCE=test; }
launcher_adapter_canary() { lu_test_event canary; }
launcher_adapter_exec() {
  if [ "$LC_DRY_RUN" = 1 ]; then printf 'would exec test provider\\n'; return 0; fi
  lu_test_event exec
  {
    printf 'wrapper=%s\\nprovider=%s\\nmode=%s\\nharness=%s\\nepic=%s\\n' \\
      "${0##*/}" "$LC_PROVIDER" "$LC_MODE" "$LC_HARNESS" "${LC_EPIC:-}"
    printf 'glm_flash=%s\\nunit=%s\\nscope_pid=%s\\npid=%s\\n' \\
      "${GLM_OPENCODE_FLASH-unset}" "${LU_DRIVER_SCOPE_UNIT:-}" "${LU_DRIVER_SCOPE_PID:-}" "$$"
    printf 'model=%s\\neffort=%s\\n' "$LC_MODEL" "$LC_EFFORT"
  } > "$TEST_IDENTITY"
  launcher_exec_command "$TEST_PROVIDER" ${LC_FORWARD_ARGS[@]+"${LC_FORWARD_ARGS[@]}"}
}
"""
)


_PROVIDER = (
    "#!/usr/bin/env bash\n"
    + _EVENT
    + """lu_test_event "provider xdist=${PYTEST_XDIST_AUTO_NUM_WORKERS-unset}"
printf '%s\\0' "$@" > "$TEST_ARGV"
env -0 > "$TEST_PROVIDER_ENV"
exit "${TEST_PROVIDER_RC:-42}"
"""
)


_SIGNAL_PROVIDER = """#!/usr/bin/env bash
log="$TEST_SIGNAL_LOG"
trap 'printf "INT\\n" >> "$log"' INT
trap 'printf "QUIT\\n" >> "$log"; exit 43' QUIT
trap 'printf "WINCH %s\\n" "$(stty size 2>/dev/null)" >> "$log"' WINCH
trap 'printf "CONT\\n" >> "$log"' CONT
stat="$(< /proc/$$/stat)"
read -r _state _ppid pgrp _session _tty tpgid _rest <<< "${stat##*) }"
tty=no
if [ -t 0 ] && [ -t 1 ]; then tty=yes; fi
printf 'ready pid=%s tty=%s pgrp=%s foreground=%s\\n' "$$" "$tty" "$pgrp" "$tpgid" >> "$log"
# Short reads: bash restarts a blocked read after WINCH and runs that trap only
# once the read returns.
while :; do
  IFS= read -r -t 0.2 line
  rc=$?
  if [ "$rc" -eq 0 ]; then
    printf 'LINE %s\\n' "$line" >> "$log"
    if [ "$line" = done ]; then exit 42; fi
  elif [ "$rc" -lt 128 ]; then
    exit 99
  fi
done
"""


_PYTHON = """#!/usr/bin/env bash
{event}
if [[ "${{1:-}}" == "-I" && "${{2:-}}" == "-c" && "${{3:-}}" == *"module bootstrap:"* ]]; then shift 5; fi
case "${{1:-}}:${{2:-}}" in
  -m:{package}.session_supervisor)
    lu_test_event "lease-${{3:-}}"
    if [ "${{3:-}}" = open ]; then
      cat <<'JSON'
{{"identity":{{"lease":{{"session_id":"session-test","lease_id":"lease-test","generation":1,"fencing_token":1,"expires_at":"2026-07-23T00:00:00Z"}}}}}}
JSON
    fi
    exit 0
    ;;
  -m:{package}.fleet_comms) lu_test_event publish; exit 0 ;;
  -m:{package}.orchestration.thread_handoff) lu_test_event rollover-import; exit 0 ;;
esac
case "${{1:-}}" in */{package}/orchestration/thread_handoff.py) lu_test_event rollover-import; exit 0 ;; esac
exec {python} "$@"
"""


_JOBSHELL = r"""
import fcntl, os, resource, signal, sys, termios, time

status_path, argv = sys.argv[1], sys.argv[2:]
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
JOB_SIGNALS = (signal.SIGINT, signal.SIGQUIT, signal.SIGTSTP, signal.SIGTTIN, signal.SIGTTOU)
for sig in JOB_SIGNALS:
    signal.signal(sig, signal.SIG_IGN)


def note(text):
    with open(status_path, "a") as stream:
        stream.write(text + "\n")


def group_states(pgid):
    states = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as stream:
                fields = stream.read().rsplit(")", 1)[1].split()
        except OSError:
            continue
        if int(fields[2]) == pgid:
            states.append(fields[0])
    return "".join(sorted(states))


pid = os.fork()
if pid == 0:
    os.setpgid(0, 0)
    os.tcsetpgrp(0, os.getpgrp())
    for sig in JOB_SIGNALS:
        signal.signal(sig, signal.SIG_DFL)
    os.execvp(argv[0], argv)
try:
    os.setpgid(pid, pid)
except OSError:
    pass
os.tcsetpgrp(0, pid)
note(f"job {pid}")
while True:
    _, status = os.waitpid(pid, os.WUNTRACED)
    if os.WIFSTOPPED(status):
        os.tcsetpgrp(0, os.getpgrp())
        # The group's other members stop as the signal reaches each of them.
        for _ in range(500):
            states = group_states(pid)
            if set(states) == {"T"}:
                break
            time.sleep(0.01)
        note(f"stopped {states}")
        os.tcsetpgrp(0, pid)
        os.killpg(pid, signal.SIGCONT)
        continue
    break
code = os.waitstatus_to_exitcode(status)
code = code if code >= 0 else 128 - code
note(f"exit {code}")
os._exit(code)
"""


def _write(path: Path, body: str, mode: int = 0o644) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(mode)
    return path


def _checkout(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """Copy every tracked wrapper and the shared core; stub only the edges."""
    root = tmp_path / "repo"
    copy_launcher_sources(root)
    # Recording adapters below model the retained wrapper ABI, independent of
    # production retirement. Real GLM launch refusal remains covered separately.
    import yaml

    from lu_machinery.review.model_catalog import validate_catalog

    catalog_path = root / MACHINERY / "config/model_catalog.yaml"
    catalog = yaml.safe_load(catalog_path.read_text())
    for identity in ("glm-5.3", "glm-5.3-flash"):
        catalog["models"][identity]["lifecycle"] = "active"
        catalog["models"][identity]["transports"] = ["opencode"]
    validate_catalog(catalog)
    catalog_path.write_text(yaml.safe_dump(catalog))
    copy_slot_registry(root)
    # The real helper with only its kernel cgroup read redirected.
    env = scope_sandbox(root, tmp_path)
    _write(
        root / MACHINERY / "lib/rules_core.sh",
        _EVENT
        + 'rules_core_load() { lu_test_event rules; LC_RULES_CORE=""; LC_RULES_SEAT=core; LC_RULES_CORE_BYTES=0; }\n'
        + "rules_core_prefix() { printf '%s' \"$1\"; }\n",
    )
    _write(root / MACHINERY / "lib/deploy_extensions.sh", _EVENT + "deploy_agent_extensions() { lu_test_event deploy; }\n")
    _write(root / MACHINERY / "lib/claude_weekly_used.py", "print('0')\n")
    # The interpreter intercepts the rollover import by path; the slot registry
    # may import the real module, so keep it when present.
    handoff = root / MACHINERY / "orchestration/thread_handoff.py"
    if not handoff.exists():
        _write(handoff, "raise SystemExit('intercepted by the test interpreter')\n")
    _write(root / MACHINERY / "ai_agent_bridge/inbox_watch.sh", "#!/usr/bin/env bash\nexec sleep 300\n", 0o755)
    for provider in ("claude", "codex", "cursor", "gemini", "glm", "grok", "kimi"):
        _write(root / MACHINERY / f"launchers/{provider}.sh", _ADAPTER)
    _write(root / ".venv/bin/python", _PYTHON.format(event=_EVENT, python=shlex.quote(sys.executable), package=MACHINERY), 0o755)
    provider = _write(root / "provider.sh", _PROVIDER, 0o755)
    signal_provider = _write(root / "signal-provider.sh", _SIGNAL_PROVIDER, 0o755)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True, capture_output=True, timeout=30, env=launcher_environment())
    base = launcher_environment()
    env = {
        **base,
        **env,
        "HOME": str(tmp_path / "home"),
        "LAUNCHER_DRY_RUN": "0",
        "TEST_EVENTS": str(tmp_path / "events"),
        "TEST_IDENTITY": str(tmp_path / "identity"),
        "TEST_ARGV": str(tmp_path / "argv"),
        "TEST_PROVIDER_ENV": str(tmp_path / "provider-env"),
        "TEST_PROVIDER": str(provider),
        "TEST_SIGNAL_PROVIDER": str(signal_provider),
        "TEST_SIGNAL_LOG": str(tmp_path / "signal-log"),
    }
    return root, env


def _launch(
    root: Path, env: dict[str, str], wrapper: str, *args: str, **extra: str
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["bash", str(root / wrapper), *args],
        cwd=root,
        env={**env, **extra},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=DEADLINE_S,
    )
    _assert_no_real_manager(env)
    return result


def _assert_no_real_manager(env: dict[str, str]) -> None:
    contact = Path(env["LU_TEST_REAL_MANAGER_CONTACT"])
    assert not contact.exists(), f"fixture reached for the real user manager: {contact.read_text()}"


def _lines(path: str | Path) -> list[str]:
    path = Path(path)
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _events(env: dict[str, str]) -> list[tuple[str, str]]:
    """(event, cgroup basename) in order."""
    rows = []
    for line in _lines(env["TEST_EVENTS"]):
        match = re.fullmatch(r"(.+) cgroup=(\S*) pid=\d+", line)
        assert match, line
        rows.append((match[1], match[2]))
    return rows


_SUPERVISOR = {
    "SESSION_SUPERVISOR_WAKE_DELIVERY": "fixture-delivery",
    "SESSION_SUPERVISOR_WAKE_STREAM": "epic:9999",
    "LC_SUPERVISORY_PREDECESSOR_GENERATION": "30",
    "SESSION_HANDOFF_AGENT": "fixture-driver",
    "SESSION_STREAM_ID": "epic:9999",
    "SESSION_STREAM_LEASE_ID": "inherited-lease",
}


class _Terminal:
    """A launcher job under the job-control shell on a fresh pseudo-terminal."""

    def __init__(self, tmp_path: Path, argv: list[str], env: dict[str, str], cwd: Path) -> None:
        self.status = tmp_path / "jobshell-status"
        jobshell = _write(tmp_path / "jobshell.py", _JOBSHELL)
        self.master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        self.output = b""
        self.closed = False
        self.process = subprocess.Popen(
            [sys.executable, "-I", str(jobshell), str(self.status), *argv],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
        os.close(slave)

    def drain(self, timeout: float = 0.05) -> None:
        while select.select([self.master], [], [], timeout)[0]:
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                return
            if not chunk:
                return
            self.output += chunk
            timeout = 0

    def wait_for(self, predicate: Callable[[], bool], what: str) -> None:
        deadline = time.monotonic() + DEADLINE_S
        while time.monotonic() < deadline:
            self.drain()
            if predicate():
                return
            if self.process.poll() is not None and not predicate():
                break
        pytest.fail(f"timed out waiting for {what}; output:\n{self.output.decode(errors='replace')}")

    def send(self, data: bytes) -> None:
        os.write(self.master, data)

    def resize(self, rows: int, columns: int) -> None:
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))

    def finish(self) -> int:
        deadline = time.monotonic() + DEADLINE_S
        while self.process.poll() is None and time.monotonic() < deadline:
            self.drain()
        self.drain()
        assert self.process.returncode is not None, self.output.decode(errors="replace")
        return self.process.returncode

    def close(self) -> None:
        if self.closed:
            return
        # Stop the shell before scanning so it cannot fork a new job after the
        # scan. Session membership survives reparenting and a missing status file.
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=10)
        groups = set()
        job = next((line.split()[1] for line in _lines(self.status) if line.startswith("job ")), None)
        if job is not None:
            groups.add(int(job))
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            except OSError:
                continue
            if int(fields[3]) == self.process.pid:
                groups.add(int(fields[2]))
        for group in groups:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(group, signal.SIGKILL)
        os.close(self.master)
        self.closed = True

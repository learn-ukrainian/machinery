"""Sign-in health of the subscription accounts, for the machinery home page.

Each Claude subscription label is asked ``claude auth status --json`` with its
own ``CLAUDE_CONFIG_DIR`` (the probe dispatcher admission runs before every
labelled launch). Each Codex subscription label is asked ``codex login status``
with its own ``CODEX_HOME`` (``codex_accounts._require_login``): exit 0 is
signed in, a non-zero exit that prints ``Not logged in`` is signed out. A row
carries the label, whether it is signed in, the login method, the plan and the
sign-in end date. It never carries an email, organization, account id,
directory or credential. A probe that fails, misses its deadline, exits
non-zero without that answer, or prints more than ``_MAX_OUTPUT_BYTES`` is
unknown (``None``), never signed in or out; output beyond the cap is never
buffered, and the probe's process group is killed on every path (``_run``).

The probes run in one background thread, never on the request path. The
serving function returns the last snapshot and starts a refresh when the
snapshot is missing or older than ``TTL_S``. Until the first snapshot exists,
``state`` is ``pending``.

``subscription_ends`` reads the operator's end dates for cancelled
subscriptions from ``subscription_ends.json`` in the user's learn-ukrainian
config directory (``{"grok": "YYYY-MM-DD", "muse": null, ...}``; ``null`` is a
cancelled subscription whose end date is not yet known). The dates stay on the
host; a missing or invalid file yields an empty mapping and a reason.
"""

from __future__ import annotations

import contextlib
import json
import os
import selectors
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from lu_machinery.agent_runtime.attempt_boundary import (
    CLAUDE_SUBSCRIPTION_ACCOUNT_LABELS,
    DEFAULT_CLAUDE_ACCOUNT,
    ClaudeAccountRefused,
    _open_claude_directory,
    _refuse_claude_launch,
    _user_config_file,
)
from lu_machinery.agent_runtime.codex_accounts import (
    CODEX_SUBSCRIPTION_ACCOUNTS,
    DEFAULT_CODEX_ACCOUNT,
    CodexAccountRefused,
    codex_account_home,
)
from lu_machinery.common.scratch import ensure_scratch_root

from .claude_account_usage import _await_exit, _ProbeFailed, account_binding, child_env

TTL_S = 600.0
_PROBE_TIMEOUT_S = 30
_MAX_OUTPUT_BYTES = 64 * 1024
_SUBSCRIPTION_ENDS_FILE = "subscription_ends.json"
_SUBSCRIPTION_ENDS_MAX_BYTES = 4096
# Display names: account a/b, the names the usage rows use.
_CLAUDE_NAMES = {DEFAULT_CLAUDE_ACCOUNT: "Claude a", "b": "Claude b"}
_CODEX_NAMES = {DEFAULT_CODEX_ACCOUNT: "Codex a", "b-sub": "Codex b"}
_CLAUDE_TEXT_FIELDS = ("authMethod", "subscriptionType")
# The line ``codex login status`` prints, with a non-zero exit, for a home without a login.
_CODEX_SIGNED_OUT = "Not logged in"

_lock = threading.Lock()
_snapshot: dict[str, Any] | None = None
_snapshot_at = 0.0
_refreshing = False


def _cli(name: str) -> str | None:
    """The provider CLI on PATH, else in ``~/.local/bin`` (the service PATH lacks it)."""
    found = shutil.which(name)
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / name
    return str(fallback) if fallback.is_file() and os.access(fallback, os.X_OK) else None


def _child_env(extra: Mapping[str, str]) -> dict[str, str]:
    """PATH, HOME and locale only, plus the account's own directory variable."""
    env = {name: os.environ[name] for name in ("PATH", "LANG", "LC_ALL") if os.environ.get(name)}
    env["HOME"] = str(Path.home())
    env.update(extra)
    return env


def _run(argv: list[str], env: dict[str, str], cwd: Path, *, with_stderr: bool) -> tuple[int, bytes] | None:
    """Exit status and output (stdout, plus stderr when asked) of one complete probe, or ``None``.

    The pattern of ``claude_account_usage._run_codexbar``: the child leads its
    own process group, and one deadline covers reading and exit. A probe is
    complete only when, before the deadline, the leader has exited and every
    holder of the pipe has closed it with at most ``_MAX_OUTPUT_BYTES``
    written. Anything else (timeout, overflow, a read error, a descendant still
    holding the pipe) is ``None``. On every path the whole group is killed
    before the leader is reaped, and the pipe is closed; no thread is left.
    """
    deadline = time.monotonic() + _PROBE_TIMEOUT_S
    try:
        # Fixed argv from a resolved CLI path, no shell, minimal environment.
        proc = subprocess.Popen(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
            argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if with_stderr else subprocess.DEVNULL,
            close_fds=True, start_new_session=True,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    output = bytearray()
    try:
        complete = _collect(proc, output, deadline)
    except (AttributeError, OSError, _ProbeFailed):
        complete = False
    finally:
        # The leader is running or an unreaped zombie here, so its group id
        # cannot have been reused yet.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        returncode = proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()
    return (returncode, bytes(output)) if complete else None


def _collect(proc: subprocess.Popen[bytes], output: bytearray, deadline: float) -> bool:
    """Read the probe's pipe until end of file and its leader's exit, without reaping it."""
    stdout = proc.stdout
    if stdout is None:
        return False
    with selectors.DefaultSelector() as selector:
        selector.register(stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                return False
            chunk = os.read(stdout.fileno(), _MAX_OUTPUT_BYTES + 1 - len(output))
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > _MAX_OUTPUT_BYTES:
                return False
    # The existing portable wait reserves the leader's PID until group cleanup.
    # No pidfd API is required, and pipe EOF alone never proves leader completion.
    _await_exit(proc.pid, deadline)
    return True


def _iso_or_none(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _short_text(value: Any) -> str | None:
    if isinstance(value, str) and 0 < len(value) <= 40 and value.isprintable():
        return value
    return None


def _claude_row(label: str) -> dict[str, Any]:
    row: dict[str, Any] = {"account": _CLAUDE_NAMES.get(label, f"Claude {label}"), "provider": "claude"}
    claude = _cli("claude")
    if claude is None:
        return {**row, "logged_in": None, "error": "claude_cli_not_found"}
    try:
        with account_binding(label) as binding, _open_claude_directory(binding.config_dir) as directory:
            _refuse_claude_launch(directory, binding.account, os.environ)
            with tempfile.TemporaryDirectory(prefix="lu-login-", dir=ensure_scratch_root()) as home:
                sandbox = Path(home)
                result = _run(
                    [claude, "--setting-sources", "project,local", "auth", "status", "--json"],
                    child_env(binding.config_dir, sandbox=sandbox), sandbox, with_stderr=False,
                )
            expected_directory, expected_account = binding.config_dir, binding.account
    except ClaudeAccountRefused as exc:
        return {**row, "logged_in": None, "error": exc.code}
    except OSError:
        return {**row, "logged_in": None, "error": "probe_failed"}
    if result is None or result[0] != 0:
        return {**row, "logged_in": None, "error": "probe_failed"}
    try:
        data = json.loads(result[1])
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("loggedIn"), bool):
        return {**row, "logged_in": None, "error": "unreadable_status"}
    if data["loggedIn"] and (
        data.get("authMethod") != "claude.ai" or data.get("apiProvider") != "firstParty"
        or data.get("configDirectory") != str(expected_directory)
        or data.get("accountUuid", expected_account) != expected_account
    ):
        return {**row, "logged_in": None, "error": "claude_account_auth_method_conflict"}
    row["logged_in"] = data["loggedIn"]
    for name in _CLAUDE_TEXT_FIELDS:
        row[name] = _short_text(data.get(name))
    row["expires_at"] = _iso_or_none(data.get("loginExpiresAt"))
    return row


def _codex_row(label: str) -> dict[str, Any]:
    row: dict[str, Any] = {"account": _CODEX_NAMES.get(label, f"Codex {label}"), "provider": "codex"}
    try:
        home = codex_account_home(label)
    except CodexAccountRefused as exc:
        return {**row, "logged_in": None, "error": exc.code}
    codex = _cli("codex")
    if codex is None:
        return {**row, "logged_in": None, "error": "codex_cli_not_found"}
    result = _run([codex, "login", "status"], _child_env({"CODEX_HOME": str(home)}), home, with_stderr=True)
    if result is None:
        return {**row, "logged_in": None, "error": "probe_failed"}
    returncode, output = result
    if returncode == 0:
        logged_in: bool | None = True
    elif _CODEX_SIGNED_OUT in output.decode("utf-8", "replace").splitlines():
        logged_in = False
    else:
        return {**row, "logged_in": None, "error": "probe_failed"}
    # Codex reports no sign-in end date; the row says so rather than guessing.
    return {**row, "logged_in": logged_in, "expires_at": None, "expiry_reported": False}


def _probe_all() -> list[dict[str, Any]]:
    probes: list[Callable[[], dict[str, Any]]] = [
        *(lambda label=label: _claude_row(label) for label in CLAUDE_SUBSCRIPTION_ACCOUNT_LABELS),
        *(lambda label=label: _codex_row(label) for label in CODEX_SUBSCRIPTION_ACCOUNTS),
    ]
    rows: list[dict[str, Any] | None] = [None] * len(probes)

    def run(index: int, probe: Callable[[], dict[str, Any]]) -> None:
        try:
            rows[index] = probe()
        except Exception:  # one broken probe must not blank the others
            rows[index] = {"account": None, "logged_in": None, "error": "probe_failed"}

    threads = [threading.Thread(target=run, args=(i, p), daemon=True) for i, p in enumerate(probes)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(_PROBE_TIMEOUT_S + 5)
    return [row or {"account": None, "logged_in": None, "error": "probe_timeout"} for row in rows]


def _refresh() -> None:
    global _snapshot, _snapshot_at, _refreshing
    try:
        rows = _probe_all()
        with _lock:
            _snapshot = {"accounts": rows, "read_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
            _snapshot_at = time.monotonic()
    finally:
        with _lock:
            _refreshing = False


def logins_snapshot() -> dict[str, Any]:
    """The last sign-in snapshot; starts one background refresh when it is missing or older than TTL_S."""
    global _refreshing
    with _lock:
        snapshot = _snapshot
        due = snapshot is None or time.monotonic() - _snapshot_at > TTL_S
        start = due and not _refreshing
        if start:
            _refreshing = True
    if start:
        threading.Thread(target=_refresh, name="account-logins-refresh", daemon=True).start()
    if snapshot is None:
        return {"state": "pending", "accounts": [], "read_at": None}
    return {"state": "ok", **snapshot}


def subscription_ends(today: date | None = None) -> dict[str, Any]:
    """Operator-supplied end dates of cancelled subscriptions; ended ones are flagged, never invented."""
    path = _user_config_file(None, "LU_SUBSCRIPTION_ENDS_FILE", _SUBSCRIPTION_ENDS_FILE)
    if path is None:
        return {"ends": {}, "reason": "not_configured"}
    try:
        with path.open("rb") as handle:
            raw = handle.read(_SUBSCRIPTION_ENDS_MAX_BYTES + 1)
    except FileNotFoundError:
        return {"ends": {}, "reason": "not_configured"}
    except OSError:
        return {"ends": {}, "reason": "unreadable"}
    try:
        data = json.loads(raw) if len(raw) <= _SUBSCRIPTION_ENDS_MAX_BYTES else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return {"ends": {}, "reason": "invalid"}
    current = today or datetime.now(UTC).date()
    ends: dict[str, dict[str, Any]] = {}
    for pool, value in data.items():
        if not isinstance(pool, str) or not pool.isidentifier() or len(pool) > 32:
            return {"ends": {}, "reason": "invalid"}
        if value is None:  # cancelled, end date not yet known
            ends[pool] = {"ends_on": None, "ended": False}
            continue
        if not isinstance(value, str):
            return {"ends": {}, "reason": "invalid"}
        try:
            end = date.fromisoformat(value)
        except ValueError:
            return {"ends": {}, "reason": "invalid"}
        ends[pool] = {"ends_on": end.isoformat(), "ended": end < current}
    return {"ends": ends, "reason": None}

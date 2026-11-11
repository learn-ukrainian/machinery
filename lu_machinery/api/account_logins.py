"""Sign-in health of the subscription accounts, for the machinery home page.

Each Claude subscription label is asked ``claude auth status --json`` with its
own ``CLAUDE_CONFIG_DIR`` (the probe dispatcher admission runs before every
labelled launch). Each Codex subscription label is asked ``codex login status``
with its own ``CODEX_HOME`` (``codex_accounts._require_login``): exit 0 is
signed in, a non-zero exit that prints ``Not logged in`` is signed out. A row
carries the label, whether it is signed in, the login method, the plan and the
sign-in end date. It never carries an email, organization, account id,
directory or credential. A probe that fails, times out, exits non-zero without
that answer, or prints more than ``_MAX_OUTPUT_BYTES`` is unknown (``None``),
never signed in or out; output beyond the cap is never buffered.

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

import json
import os
import shutil
import subprocess
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
    _user_config_file,
    load_claude_account_binding,
)
from lu_machinery.agent_runtime.codex_accounts import (
    CODEX_SUBSCRIPTION_ACCOUNTS,
    DEFAULT_CODEX_ACCOUNT,
    CodexAccountRefused,
    codex_account_home,
)

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
    """Exit status and output (stdout, plus stderr when asked) of one probe, or ``None``.

    At most ``_MAX_OUTPUT_BYTES`` are kept: a child that prints more, or runs
    past ``_PROBE_TIMEOUT_S``, is killed and the probe is ``None``.
    """
    stderr = subprocess.STDOUT if with_stderr else subprocess.DEVNULL
    try:
        # Fixed argv from a resolved CLI path, no shell, minimal environment.
        proc = subprocess.Popen(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
            argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=stderr,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    output = bytearray()
    stream = proc.stdout
    if stream is None:  # not reachable with stdout=PIPE; refuse rather than guess
        proc.kill()
        proc.wait()
        return None

    descriptor = stream.fileno()

    def drain() -> None:
        while len(output) <= _MAX_OUTPUT_BYTES:
            try:
                chunk = os.read(descriptor, _MAX_OUTPUT_BYTES + 1 - len(output))
            except OSError:
                return
            if not chunk:
                return
            output.extend(chunk)

    reader = threading.Thread(target=drain, name="account-login-probe-output", daemon=True)
    reader.start()
    reader.join(_PROBE_TIMEOUT_S)
    try:
        if reader.is_alive() or len(output) > _MAX_OUTPUT_BYTES:
            return None
        return proc.wait(timeout=_PROBE_TIMEOUT_S), bytes(output)
    except subprocess.TimeoutExpired:
        return None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        # Close only once the reader has seen end of file; closing under a
        # blocked read (a grandchild still holding the pipe) could block here.
        reader.join(1)
        if not reader.is_alive():
            stream.close()


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
    try:
        config_dir = Path.home() / ".claude" if label == DEFAULT_CLAUDE_ACCOUNT else load_claude_account_binding(label).config_dir
    except ClaudeAccountRefused as exc:
        return {**row, "logged_in": None, "error": exc.code}
    claude = _cli("claude")
    if claude is None:
        return {**row, "logged_in": None, "error": "claude_cli_not_found"}
    result = _run(
        [claude, "auth", "status", "--json"], _child_env({"CLAUDE_CONFIG_DIR": str(config_dir)}), config_dir,
        with_stderr=False,
    )
    if result is None or result[0] != 0:
        return {**row, "logged_in": None, "error": "probe_failed"}
    try:
        data = json.loads(result[1])
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("loggedIn"), bool):
        return {**row, "logged_in": None, "error": "unreadable_status"}
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

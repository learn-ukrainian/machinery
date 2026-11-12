"""Sign-in rows publish status and expiry only, and never read a failure as signed in."""

from __future__ import annotations

import json
import sys
import threading
import time
from contextlib import nullcontext
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from lu_machinery.api import account_logins


def _completed(stdout: bytes = b"", returncode: int = 0) -> tuple[int, bytes]:
    return returncode, stdout


@pytest.fixture
def probe_env(monkeypatch, tmp_path):
    monkeypatch.setattr(account_logins.Path, "home", lambda: tmp_path)
    (tmp_path / ".claude").mkdir()
    monkeypatch.setattr(account_logins, "_cli", lambda name: f"/opt/{name}")
    monkeypatch.setattr(account_logins, "codex_account_home", lambda label: tmp_path / f"codex-{label}")
    monkeypatch.setattr(account_logins, "account_binding", lambda label: nullcontext(
        SimpleNamespace(config_dir=tmp_path / ".claude", account="acct-secret-uuid")))
    monkeypatch.setattr(account_logins, "_open_claude_directory", lambda path: nullcontext(1))
    monkeypatch.setattr(account_logins, "_refuse_claude_launch", lambda *args: {})
    monkeypatch.setattr(account_logins, "ensure_scratch_root", lambda: tmp_path)
    return tmp_path


def test_claude_row_publishes_no_identity_and_unreadable_is_unknown(probe_env, monkeypatch):
    """Defect class: an email, org or account id leaks; an unreadable status reads as signed in."""
    status = {
        "loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "max",
        "loginExpiresAt": "2030-01-02T03:04:05.678Z", "email": "person@example.invalid",
        "orgId": "org-secret-id", "orgName": "Private Org", "accountUuid": "acct-secret-uuid",
        "configDirectory": str(probe_env / ".claude"),
    }
    monkeypatch.setattr(account_logins, "_run", lambda *a, **k: _completed(json.dumps(status).encode()))
    row = account_logins._claude_row("a")
    assert row == {"account": "Claude a", "provider": "claude", "logged_in": True, "authMethod": "claude.ai",
                   "subscriptionType": "max", "expires_at": "2030-01-02T03:04:05Z"}
    published = json.dumps(row)
    for private in ("person@example.invalid", "org-secret-id", "Private Org", "acct-secret-uuid", "/private"):
        assert private not in published

    for stdout in (b"not json", b"[]", json.dumps({"loggedIn": "yes"}).encode()):
        monkeypatch.setattr(account_logins, "_run", lambda *a, out=stdout, **k: _completed(out))
        unreadable = account_logins._claude_row("a")
        assert unreadable["logged_in"] is None and unreadable["error"] == "unreadable_status"
    # A failed probe is unknown even when its stdout claims a sign-in.
    monkeypatch.setattr(account_logins, "_run", lambda *a, **k: _completed(b'{"loggedIn": true}', returncode=2))
    assert account_logins._claude_row("a")["logged_in"] is None
    for key, wrong in (("authMethod", "api_key_helper"), ("apiProvider", "other"),
                       ("accountUuid", "other-account"), ("configDirectory", "/other")):
        monkeypatch.setattr(account_logins, "_run", lambda *a, **k: _completed(json.dumps({**status, key: wrong}).encode()))
        assert account_logins._claude_row("a")["logged_in"] is None

    def refuse(*args):
        raise account_logins.ClaudeAccountRefused("claude_account_binding_is_default")

    monkeypatch.setattr(account_logins, "_refuse_claude_launch", refuse)
    monkeypatch.setattr(account_logins, "_run", lambda *a, **k: pytest.fail("refused account launched"))
    assert account_logins._claude_row("b")["error"] == "claude_account_binding_is_default"
    monkeypatch.setattr(account_logins, "_refuse_claude_launch", lambda *args: {})

    def isolated_probe(argv, env, cwd, **kwargs):
        assert argv[1:3] == ["--setting-sources", "project,local"]
        assert env["HOME"] == str(cwd) and cwd != probe_env
        assert env["CLAUDE_CONFIG_DIR"] == str(probe_env / ".claude")
        return _completed(json.dumps(status).encode())

    monkeypatch.setattr(account_logins, "_run", isolated_probe)
    assert account_logins._claude_row("a")["logged_in"] is True
    monkeypatch.setattr(account_logins, "_run", lambda *a, **k: None)
    assert account_logins._claude_row("a")["logged_in"] is None


def test_codex_signed_out_only_on_its_answer_and_failures_are_unknown(probe_env, monkeypatch):
    """Defect class: a failed or refused login check reads as signed in, or a failure as signed out."""
    monkeypatch.setattr(account_logins, "_run", lambda *a, **k: _completed(b"Not logged in\n", returncode=1))
    row = account_logins._codex_row("sub")
    assert row["logged_in"] is False and row["expires_at"] is None and row["expiry_reported"] is False
    monkeypatch.setattr(account_logins, "_run", lambda *a, **k: _completed(b"Logged in using ChatGPT\n"))
    assert account_logins._codex_row("sub")["logged_in"] is True
    for failure in (_completed(b"error: config unreadable\n", returncode=2), _completed(returncode=1), None):
        monkeypatch.setattr(account_logins, "_run", lambda *a, out=failure, **k: out)
        assert account_logins._codex_row("b-sub")["logged_in"] is None


def test_probe_output_over_the_cap_is_unknown_and_not_kept(tmp_path, monkeypatch):
    """Defect class: an unbounded CLI output is buffered whole in the API process."""
    cap = account_logins._MAX_OUTPUT_BYTES
    noisy = [sys.executable, "-c", f"import os; os.write(1, b'x' * {cap + 1}); os.write(2, b'e' * {cap + 1})"]
    assert account_logins._run(noisy, {}, tmp_path, with_stderr=True) is None
    quiet = [sys.executable, "-c", "import os, sys; os.write(1, b'ok'); os.write(2, b'warn'); sys.exit(3)"]
    # The portable completion proof also works on Python builds without pidfd_open.
    monkeypatch.delattr(account_logins.os, "pidfd_open", raising=False)
    assert account_logins._run(quiet, {}, tmp_path, with_stderr=False) == (3, b"ok")


def _gone_or_zombie(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return True
    return state == "Z"


def test_incomplete_probe_is_unknown_and_leaves_no_process_or_reader(tmp_path, monkeypatch):
    """Defect class: a late, failed-read or descendant-held probe reads as complete, or leaks its process or reader."""
    monkeypatch.setattr(account_logins, "_PROBE_TIMEOUT_S", 1.0)
    valid = 'os.write(1, b\'{"loggedIn": true}\')'
    # Output complete and the pipe closed before the deadline, but the exit comes after it.
    late = [sys.executable, "-c", f"import os, time; {valid}; time.sleep(0.6); os.close(1); time.sleep(0.6)"]
    started = time.monotonic()
    assert account_logins._run(late, {}, tmp_path, with_stderr=False) is None
    assert time.monotonic() - started < 2.5
    marker = tmp_path / "descendant.pid"
    holder = [sys.executable, "-c",
              f"import os, time; {valid}\nif os.fork() == 0:\n    open({str(marker)!r}, 'w').write(str(os.getpid())); "
              "time.sleep(30)\nos._exit(0)"]
    before = {thread.name for thread in threading.enumerate()}
    assert account_logins._run(holder, {}, tmp_path, with_stderr=False) is None
    descendant = int(marker.read_text())
    deadline = time.monotonic() + 3
    while not _gone_or_zombie(descendant) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _gone_or_zombie(descendant)
    assert {thread.name for thread in threading.enumerate()} <= before
    # A read error is unknown, never the bytes read so far. Popen's own error
    # pipe read asks for at most 50000 bytes; the probe asks for more.
    real_read = account_logins.os.read

    def failing_read(fd: int, size: int) -> bytes:
        if size > 50000:
            raise OSError(5, "Input/output error")
        return real_read(fd, size)

    monkeypatch.setattr(account_logins.os, "read", failing_read)
    ok = [sys.executable, "-c", f"import os; {valid}"]
    assert account_logins._run(ok, {}, tmp_path, with_stderr=False) is None


@pytest.mark.parametrize(
    "content",
    ['{"grok": "soon"}', '{"grok": 20301231}', "[]", "not json", '{"grok": "2030-01-01"}' + " " * 5000],
    ids=["text-date", "number", "array", "not-json", "oversize"],
)
def test_subscription_ends_never_invents_a_date(tmp_path, monkeypatch, content):
    """Defect class: an invalid end-date file yields a date anyway."""
    path = tmp_path / "subscription_ends.json"
    path.write_text(content)
    monkeypatch.setenv("LU_SUBSCRIPTION_ENDS_FILE", str(path))
    assert account_logins.subscription_ends() == {"ends": {}, "reason": "invalid"}
    monkeypatch.setenv("LU_SUBSCRIPTION_ENDS_FILE", str(tmp_path / "missing.json"))
    assert account_logins.subscription_ends() == {"ends": {}, "reason": "not_configured"}
    path.write_text('{"grok": "2030-01-01", "muse": null}')
    monkeypatch.setenv("LU_SUBSCRIPTION_ENDS_FILE", str(path))
    assert account_logins.subscription_ends(today=date(2030, 1, 2)) == {
        "ends": {"grok": {"ends_on": "2030-01-01", "ended": True}, "muse": {"ends_on": None, "ended": False}},
        "reason": None,
    }
    assert Path(path).read_text()  # the reader never rewrites the operator's file

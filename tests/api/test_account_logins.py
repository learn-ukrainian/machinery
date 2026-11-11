"""Sign-in rows publish status and expiry only, and never read a failure as signed in."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

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
    return tmp_path


def test_claude_row_publishes_no_identity_and_unreadable_is_unknown(probe_env, monkeypatch):
    """Defect class: an email, org or account id leaks; an unreadable status reads as signed in."""
    status = {
        "loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max",
        "loginExpiresAt": "2030-01-02T03:04:05.678Z", "email": "person@example.invalid",
        "orgId": "org-secret-id", "orgName": "Private Org", "accountUuid": "acct-secret-uuid",
        "configDirectory": "/private/claude",
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


def test_probe_output_over_the_cap_is_unknown_and_not_kept(tmp_path):
    """Defect class: an unbounded CLI output is buffered whole in the API process."""
    cap = account_logins._MAX_OUTPUT_BYTES
    noisy = [sys.executable, "-c", f"import os; os.write(1, b'x' * {cap + 1}); os.write(2, b'e' * {cap + 1})"]
    assert account_logins._run(noisy, {}, tmp_path, with_stderr=True) is None
    quiet = [sys.executable, "-c", "import os, sys; os.write(1, b'ok'); os.write(2, b'warn'); sys.exit(3)"]
    assert account_logins._run(quiet, {}, tmp_path, with_stderr=False) == (3, b"ok")


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

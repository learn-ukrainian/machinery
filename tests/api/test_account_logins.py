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
    # A normal default account has no second global config inside its login directory.
    assert not (probe_env / ".claude/.claude.json").exists()
    monkeypatch.setattr(account_logins, "_open_claude_directory", lambda *a: pytest.fail("default got labelled gate"))
    monkeypatch.setattr(account_logins, "_refuse_claude_launch", lambda *a: pytest.fail("default got labelled gate"))
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


def _write_facts(tmp_path, monkeypatch, content: str) -> Path:
    path = tmp_path / "plan_facts.json"
    path.write_text(content)
    monkeypatch.setenv("LU_SUBSCRIPTION_ENDS_FILE", str(path))
    return path


@pytest.mark.parametrize(
    "content",
    [
        '{"claude_a": {"renews_on": "2030-02-30"}}',
        '{"claude_a": {"renews_on": null}}',
        '{"claude_a": {"plan": "x", "colour": "red"}}',
        '{"claude_a": {"plan": "' + "x" * 41 + '"}}',
        '{"claude_a": {"banked_resets": {"kind": "weekly", "count": 1}}}',
        '{"claude_a": {"banked_resets": [{"kind": "weekly"}]}}',
        '{"claude_a": {"banked_resets": [{"count": -1}]}}',
        '{"claude_a": {"banked_resets": [{"count": true}]}}',
        '{"claude_a": {"banked_resets": [{"count": 1, "expires_on": "soon"}]}}',
        '{"claude_a": {"banked_resets": [{"count": 1, "note": "x"}]}}',
        '{"claude_a": 3}',
    ],
    ids=["impossible-date", "null-renewal", "unknown-field", "long-plan", "banked-not-list", "banked-no-count",
         "negative-count", "boolean-count", "bad-expiry", "unknown-banked-field", "number-pool"],
)
def test_plan_facts_per_pool_fields_are_validated(tmp_path, monkeypatch, content):
    """Defect class: a malformed per-pool plan-facts entry yields an invented value or an error instead of
    reason=invalid with every plan field unknown, or the older end-date form stops reading."""
    _write_facts(tmp_path, monkeypatch, content)
    assert account_logins.plan_facts(today=date(2030, 1, 10)) == {"pools": {}, "reason": "invalid"}
    assert account_logins.subscription_ends(today=date(2030, 1, 10)) == {"ends": {}, "reason": "invalid"}

    path = _write_facts(tmp_path, monkeypatch, json.dumps({
        "claude_a": {"plan": "Plan A", "renews_on": "2030-01-20",
                     "banked_resets": [{"kind": "weekly", "count": 2, "expires_on": "2030-02-01"},
                                       {"count": 1, "expires_on": "2030-01-01"}]},
        "codex_b": {"banked_resets": []},
        "grok": {"ends_on": "2030-01-31"},
        "muse": None,
    }))
    facts = account_logins.plan_facts(today=date(2030, 1, 10))
    assert facts["reason"] is None
    claude = facts["pools"]["claude_a"]
    assert (claude["plan"], claude["next_renewal_on"], claude["days_to_renewal"], claude["cancelled"]) == (
        "Plan A", "2030-01-20", 10, False)
    assert claude["banked_resets"] == [
        {"kind": "weekly", "count": 2, "expires_on": "2030-02-01", "expired": False},
        {"kind": None, "count": 1, "expires_on": "2030-01-01", "expired": True},
    ]
    assert facts["pools"]["codex_b"]["banked_resets"] == []  # a known zero, not unknown
    assert facts["pools"]["codex_b"]["plan"] is None and facts["pools"]["codex_b"]["next_renewal_on"] is None
    grok = facts["pools"]["grok"]
    assert (grok["cancelled"], grok["ends_on"], grok["days_to_end"], grok["ended"], grok["next_renewal_on"]) == (
        True, "2030-01-31", 21, False, None)
    assert account_logins.subscription_ends(today=date(2030, 1, 10)) == {
        "ends": {"grok": {"ends_on": "2030-01-31", "ended": False}, "muse": {"ends_on": None, "ended": False}},
        "reason": None,
    }
    assert json.loads(path.read_text())["muse"] is None  # the reader never rewrites the operator's file


@pytest.mark.parametrize(
    ("anchor", "today", "expected"),
    [
        (date(2030, 1, 15), date(2030, 1, 15), date(2030, 1, 15)),
        (date(2030, 1, 15), date(2030, 1, 16), date(2030, 2, 15)),
        (date(2029, 11, 20), date(2030, 1, 25), date(2030, 2, 20)),
        (date(2030, 1, 31), date(2030, 2, 10), date(2030, 2, 28)),
        (date(2030, 1, 31), date(2030, 3, 1), date(2030, 3, 31)),
        (date(2031, 12, 31), date(2032, 2, 1), date(2032, 2, 29)),
        (date(2030, 6, 1), date(2030, 1, 10), date(2030, 6, 1)),
        (date(2029, 12, 5), date(2030, 1, 6), date(2030, 2, 5)),
    ],
    ids=["same-day", "day-after", "months-old", "month-end-short", "month-end-long", "leap", "future", "year-wrap"],
)
def test_next_renewal_rolls_forward_by_month(tmp_path, monkeypatch, anchor, today, expected):
    """Defect class: a past monthly renewal anchor is reported as the next renewal, or a month-end anchor
    (the 31st) skips or overflows a month."""
    assert account_logins._next_monthly(anchor, today) == expected
    _write_facts(tmp_path, monkeypatch, json.dumps({"codex_a": {"renews_on": anchor.isoformat()},
                                                    "grok": {"renews_on": anchor.isoformat(), "ends_on": None}}))
    pools = account_logins.plan_facts(today=today)["pools"]
    assert pools["codex_a"]["next_renewal_on"] == expected.isoformat()
    assert pools["codex_a"]["days_to_renewal"] == (expected - today).days
    assert pools["grok"]["next_renewal_on"] is None  # a cancelled plan does not renew


_NOW = "2030-01-01T12:00:00Z"


def _cache_pool(read_at: str | None, weekly_used: float, *, error: str | None = None, **report) -> dict:
    usage = {
        "primary": {"windowMinutes": 300, "usedPercent": 40},
        "secondary": {"windowMinutes": 10080, "usedPercent": weekly_used, "resetsAt": "2030-01-03T00:00:00Z",
                      "resetDescription": "resets Jan 3"},
        "extraRateWindows": [{"title": "Fable only", "window": {"windowMinutes": 10080, "usedPercent": 2,
                                                                 "resetsAt": "2030-01-03T00:00:00Z"}}],
        "accountEmail": "person@example.invalid",
    }
    entry = {
        "usage": {"usage": usage, "rateWindowLabels": {"primary": "Session", "secondary": "Weekly"},
                  "pace": {"secondary": {"deltaPercent": -8, "expectedUsedPercent": 44, "willLastToReset": True,
                                         "summary": "8% in reserve | Expected 44% used | Lasts until reset"}},
                  **report},
        "read_at": read_at, "error": error, "error_at": _NOW if error else None,
    }
    return entry


@pytest.fixture
def usage_files(tmp_path, monkeypatch):
    from lu_machinery.api import state_helpers, subscription_usage

    deficit = {"secondary": {"deltaPercent": 18, "expectedUsedPercent": 35, "willLastToReset": False,
                             "etaSeconds": 7200, "summary": "18% in deficit"}}
    cache = {"written_at": _NOW, "pools": {
        "Claude a": _cache_pool("2030-01-01T11:59:00Z", 36),
        "Claude b": _cache_pool("2030-01-01T11:30:00Z", 53, pace=deficit),
        "Codex a": _cache_pool(None, 0, error="TimeoutExpired: person@example.invalid"),
        "Grok+Cursor": _cache_pool("2030-01-01T11:58:00Z", 14),
    }}
    path = tmp_path / "codexbar.json"
    path.write_text(json.dumps(cache))
    monkeypatch.setenv("LU_USAGE_CACHE_FILE", str(path))
    _write_facts(tmp_path, monkeypatch, '{"claude_a": {"plan": "Plan A", "renews_on": "2029-12-15"}}')
    # No Monitor reading in this process, and no probe may start.
    probes = []
    monkeypatch.setattr(subscription_usage, "_last_good_data", {})
    monkeypatch.setattr(subscription_usage, "_last_failure_data", {})
    monkeypatch.setattr(subscription_usage, "trigger_background_refresh", lambda *a, **k: probes.append(a))
    monkeypatch.setattr(subscription_usage, "fetch_provider_usage", lambda *a, **k: probes.append(a))
    monkeypatch.setenv("CODEXBAR_ON_DEMAND_REFRESH", "1")
    state_helpers.cache_invalidate("codexbar_usage:")
    yield SimpleNamespace(cache=path, probes=probes, tmp=tmp_path)
    state_helpers.cache_invalidate("codexbar_usage:")


def _accounts(snapshot: dict) -> dict:
    return {row["account"]: row for provider in snapshot["providers"] for row in provider["accounts"]}


def test_subscriptions_view_is_cache_only_and_marks_missing_stale_unknown(usage_files, monkeypatch):
    """Defect class: a missing, stale (over 15 minutes) or failed cache pool shows its numbers as current or
    as zero, or the view starts a provider probe on the request path."""
    from datetime import UTC, datetime

    from lu_machinery.api import subscriptions

    now = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    snapshot = subscriptions.subscriptions_snapshot(now=now)
    rows = _accounts(snapshot)
    assert usage_files.probes == []
    assert [p["provider"] for p in snapshot["providers"]] == ["claude", "codex", "agy", "grok", "muse"]
    assert set(rows) == {"Claude a", "Claude b", "Codex a", "Codex b", "AGY Gemini", "AGY Claude/GPT", "Grok+Cursor", "Muse"}

    claude = rows["Claude a"]
    assert (claude["reading"]["status"], claude["reading"]["age_s"]) == ("fresh", 60)
    assert (claude["weekly"]["left_pct"], claude["weekly"]["resets_at"], claude["weekly"]["resets_in_s"]) == (
        64.0, "2030-01-03T00:00:00Z", 129600)
    assert (claude["pace"]["state"], claude["pace"]["headroom_pct"], claude["pace"]["lasts_to_reset"]) == ("reserve", 8.0, True)
    assert (claude["session"]["left_pct"], claude["session"]["resets_at"]) == (60.0, None)
    assert [(w["label"], w["left_pct"]) for w in claude["extra_windows"]] == [("Fable only", 98.0)]
    assert (claude["plan"]["name"], claude["plan"]["next_renewal_on"]) == ("Plan A", "2030-01-15")
    assert claude["banked_resets"] == {"status": "unknown", "reason": "not_in_plan_facts"}

    stale = rows["Claude b"]
    assert (stale["reading"]["status"], stale["weekly"]["left_pct"]) == ("stale", 47.0)
    assert (stale["pace"]["state"], stale["pace"]["headroom_pct"], stale["pace"]["runs_out_at"]) == (
        "deficit", -18.0, "2030-01-01T13:30:00Z")

    for name, reason in (("Codex a", "no_reading"), ("Codex b", "no_reading"), ("AGY Gemini", "no_reading"), ("Muse", "no_reading")):
        row = rows[name]
        assert row["reading"]["status"] == "unknown", name
        for section in ("weekly", "pace", "session", "credits"):
            assert row[section] == {"status": "unknown", "reason": reason}, (name, section)
    assert rows["Codex a"]["reading"]["last_refresh_error"] == {"at": _NOW, "kind": "TimeoutExpired"}
    assert rows["AGY Gemini"]["reading"]["reason"] == subscriptions.NO_MONITOR_READING
    assert rows["Grok+Cursor"]["plan"] == {"status": "unknown", "reason": "not_in_plan_facts", "name": None,
                                           "name_source": None}
    assert "example.invalid" not in json.dumps(snapshot)

    # A missing cache or an invalid plan-facts file is unknown with a reason, never an error.
    monkeypatch.setenv("LU_USAGE_CACHE_FILE", str(usage_files.tmp / "missing.json"))
    _write_facts(usage_files.tmp, monkeypatch, "not json")
    broken = subscriptions.subscriptions_snapshot(now=now)
    assert broken["sources"] == {"usage_cache": {"status": "missing", "written_at": None},
                                 "plan_facts": {"status": "unknown", "reason": "invalid"}}
    assert _accounts(broken)["Claude a"]["reading"]["reason"] == "usage_cache_missing"
    assert _accounts(broken)["Claude a"]["plan"]["reason"] == "invalid"
    assert usage_files.probes == []


def test_subscriptions_cli_prints_every_account_without_the_api(usage_files):
    """Defect class: the console command depends on the HTTP API, or drops account rows when the Monitor is not
    running (AGY and Muse must print unknown while the cache pools print their numbers)."""
    import os
    import subprocess
    from datetime import UTC, datetime, timedelta

    # The command runs on the real clock, so its reading is one minute old now.
    read_at = (datetime.now(UTC) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cache = json.loads(usage_files.cache.read_text())
    for pool in cache["pools"].values():
        if pool["read_at"]:
            pool["read_at"] = read_at
    usage_files.cache.write_text(json.dumps(cache))
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(usage_files.tmp),
           "LU_USAGE_CACHE_FILE": str(usage_files.cache),
           "LU_SUBSCRIPTION_ENDS_FILE": os.environ["LU_SUBSCRIPTION_ENDS_FILE"],
           # Any HTTP attempt fails fast instead of reaching a live API.
           "http_proxy": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9", "no_proxy": ""}
    text = subprocess.run([sys.executable, "-m", "lu_machinery.api.subscriptions"], env=env, capture_output=True,
                          text=True, timeout=60, check=True).stdout
    for account in ("Claude a", "Claude b", "Codex a", "Codex b", "AGY Gemini", "AGY Claude/GPT", "Grok+Cursor", "Muse"):
        assert f"\n{account}  [" in text, account
    assert "AGY and Muse readings live only in the Monitor API process" in text
    assert "Weekly       64% left" in text and "Claude a  [fresh, read 1m ago]" in text
    as_json = subprocess.run([sys.executable, "-m", "lu_machinery.api.subscriptions", "--json"], env=env,
                             capture_output=True, text=True, timeout=60, check=True).stdout
    rows = _accounts(json.loads(as_json))
    assert rows["Claude a"]["weekly"]["used_pct"] == 36.0
    assert rows["Muse"]["reading"]["reason"] == "no_monitor_reading"

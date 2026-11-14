"""Per-account subscription usage, resets and plan facts, for planning.

One reader, ``subscriptions_snapshot``, serves the machinery home page,
``GET /api/runtime/subscriptions`` and the console command
``python -m lu_machinery.api.subscriptions``. Every account is its own row,
grouped under its provider (``providers[].accounts[]``), so accounts of one
provider can be compared side by side. The reader takes its data only from:

* the background usage cache that the host's usage-cache timer writes every
  five minutes (``~/.cache/lu-usage/codexbar.json``; ``LU_USAGE_CACHE_FILE``
  overrides, empty means none). It holds one CodexBar reading per pool:
  Codex a and b, AGY and Grok+Cursor. The AGY pool carries two
  quota groups, Gemini and Claude/GPT, as titled weekly and 5-hour windows;
  each group is its own row;
* the Monitor's validated per-account Claude readings
  (``get_provider_usage_data(..., kick_refresh=False)``, which never probes).
  A process without the Monitor's scheduler, such as the console command,
  has none, and those accounts show as unknown. The existing Monitor scheduler
  owns Claude refresh; the page never reads the Claude rows of the
  external usage-cache writer;
* the operator's plan-facts file (``account_logins.plan_facts``).

It never calls CodexBar, a provider or the HTTP API. The fields follow
CodexBar's report: the weekly window, the pace (reserve or deficit, and
whether it lasts to the reset or when it runs out), the 5-hour session
window, the scoped windows (for example Fable only), credits and banked
resets. Where CodexBar reports no pace for a weekly window (AGY), the pace is
computed with the same algorithm (``compute_usage_pace``, a port of CodexBar's)
and marked ``source: computed``. Claude and Codex report no real 5-hour window,
so theirs is always ``unknown`` with reason ``not_reported``, never a number.
Free text from the cache that looks like an email address is dropped.

Each section carries a ``status``: ``ok``, ``none`` (the provider has no such
thing) or ``unknown`` with a ``reason``. A missing number is ``null``, never
zero. A reading older than ``STALE_AFTER_S`` is ``stale``; its numbers stay,
marked as such.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from prometheus_client.core import GaugeMetricFamily

from . import account_logins
from .subscription_usage import (
    PROVIDER_ERROR,
    compute_usage_pace,
    format_usage_pace_summary,
    get_provider_usage_data,
    pace_is_visible,
)

STALE_AFTER_S = 900
CACHE_ENV = "LU_USAGE_CACHE_FILE"
_CACHE_MAX_BYTES = 1 << 20
_WEEKLY_MINUTES = 10080
_SESSION_MINUTES = 300
_ON_PACE_BAND_PCT = 2.0  # CodexBar's "On pace" band
_MAX_LIST = 10
_MAX_COUNT = 999
_MAX_ETA_S = 400 * 86400  # beyond any monthly or weekly window
_CLOCK_SKEW_S = 300  # a reading timestamped further ahead than this is not believed
# The cache writer records ``<ExceptionName>: <message>``; only a bare exception name is published.
_ERROR_KIND = re.compile(r"[A-Z][A-Za-z0-9]{1,39}")
# Only named usage counters and percentage values are public breakdown data.
_BREAKDOWN_LABELS = frozenset({"Cursor", "Grok", "Gemini", "Claude/GPT"})
_BREAKDOWN_PERCENT = re.compile(r"(?:100|[0-9]{1,2})(?:\.[0-9]+)?%")
_EMAIL_SHAPED = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")

# provider id, provider label, accounts: (account, plan-facts key, source, source key)
PROVIDERS: tuple[tuple[str, str, tuple[tuple[str, str, str, str], ...]], ...] = (
    ("claude", "Claude", (("Claude a", "claude_a", "monitor", "claude:a"), ("Claude b", "claude_b", "monitor", "claude:b"))),
    ("codex", "Codex", (("Codex a", "codex_a", "cache", "Codex a"), ("Codex b", "codex_b", "cache", "Codex b"))),
    ("agy", "AGY", (("AGY Gemini", "agy", "cache", "AGY"), ("AGY Claude/GPT", "agy", "cache", "AGY"))),
    ("grok", "Grok", (("Grok+Cursor", "grok", "cache", "Grok+Cursor"),)),
)
NO_MONITOR_READING = "no_monitor_reading"
# A pool with quota groups: each account reads its group's titled "<group> weekly" and "<group> 5-hour" windows.
_CACHE_GROUPS = {"AGY Gemini": "Gemini", "AGY Claude/GPT": "Claude/GPT"}
# Providers without a real 5-hour window (CodexBar 0.69's "Session" mirrored the weekly percent).
_NO_SESSION_PROVIDERS = frozenset({"claude", "codex"})
_ACCOUNT_NOTES = {
    "AGY Gemini": "Carbon draws on the AGY pools.",
    "Codex a": "The Codex API credits belong to this account and renew with its monthly turnover.",
}
# Pools whose credits renew with the plan's monthly turnover.
_CREDITS_RENEW_WITH_PLAN = frozenset({"codex_a"})

MonitorReader = Callable[[str], Mapping[str, Any] | None]


def _monitor_reading(provider: str) -> Mapping[str, Any] | None:
    """The Monitor's cached reading; ``kick_refresh=False`` never starts a probe."""
    return get_provider_usage_data(provider, kick_refresh=False)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _moment(value: Any) -> datetime | None:
    """An aware ISO time or an epoch (seconds, or milliseconds above 1e11), else ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = _number(value)
        if seconds is None or seconds <= 0:
            return None
        try:
            return datetime.fromtimestamp(seconds / 1000 if seconds > 1e11 else seconds, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not 0 < len(value) <= 40:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return moment.astimezone(UTC) if moment.tzinfo is not None else None
    except (OverflowError, ValueError):  # out of range once normalized to UTC
        return None


def _number(value: Any) -> float | None:
    """A finite float, else ``None`` (an integer too large for a float is ``None``)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _count(value: Any) -> int | None:
    """A non-negative integer count, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_COUNT:
        return None
    return value


def _after(moment: datetime, seconds: float | None) -> datetime | None:
    """``moment`` plus a non-negative number of seconds, or ``None`` when absent or out of range."""
    if seconds is None or not 0 <= seconds <= _MAX_ETA_S:
        return None
    try:
        return moment + timedelta(seconds=seconds)
    except OverflowError:  # past the last representable date
        return None


def _pct(value: Any) -> float | None:
    number = _number(value)
    return round(number, 1) if number is not None and 0 <= number <= 100 else None


def _text(value: Any, limit: int = 60) -> str | None:
    if isinstance(value, str) and 0 < len(value) <= limit and value.isprintable() and not _EMAIL_SHAPED.search(value):
        return value
    return None


def _dict(value: Any) -> dict[str, Any]:
    """The value when it is a JSON object, else empty."""
    return value if isinstance(value, dict) else {}


def _items(value: Any) -> list[Any]:
    """At most ``_MAX_LIST`` entries of a list; anything else is empty."""
    return value[:_MAX_LIST] if isinstance(value, list) else []


def _unknown(reason: str) -> dict[str, Any]:
    return {"status": "unknown", "reason": reason}


def _window(label: str | None, used: Any, resets_at: Any, minutes: Any, now: datetime,
            description: Any = None) -> dict[str, Any]:
    used_pct = _pct(used)
    if used_pct is None:
        return {**_unknown("no_reading"), "label": label}
    reset = _moment(resets_at)
    seconds = None if reset is None else int((reset - now).total_seconds())
    window_minutes = _number(minutes)
    return {
        "status": "ok",
        "label": label,
        "window_minutes": int(window_minutes) if window_minutes else None,
        "used_pct": used_pct,
        "left_pct": round(100 - used_pct, 1),
        "resets_at": _iso(reset) if reset else None,
        "resets_in_s": seconds,
        "reset_passed": seconds is not None and seconds <= 0,
        "reset_description": _text(description),
    }


def _pace(delta: float | None, expected: float | None, lasts: Any, runs_out: datetime | None,
          summary: Any, now: datetime, *, source: str = "provider") -> dict[str, Any]:
    if delta is None or not -100 <= delta <= 100:
        return _unknown("not_reported")
    expected = _pct(expected)
    state = "on_pace" if abs(delta) <= _ON_PACE_BAND_PCT else "deficit" if delta > 0 else "reserve"
    lasts_to_reset = lasts if isinstance(lasts, bool) else None
    return {
        "status": "ok",
        "state": state,
        # Positive: percentage points in reserve; negative: in deficit.
        "headroom_pct": round(-delta, 1),
        "expected_used_pct": None if expected is None else round(expected, 1),
        "lasts_to_reset": lasts_to_reset,
        "runs_out_at": _iso(runs_out) if runs_out and lasts_to_reset is False else None,
        "runs_out_in_s": int((runs_out - now).total_seconds()) if runs_out and lasts_to_reset is False else None,
        "summary": _text(summary, 160),
        "source": source,
    }


def _reading(read_at: datetime | None, now: datetime, *, stale: bool = False) -> dict[str, Any]:
    if read_at is None:
        return {"status": "unknown", "read_at": None, "age_s": None, "reason": "no_reading"}
    age = max(0, int((now - read_at).total_seconds()))
    status = "stale" if stale or age > STALE_AFTER_S else "fresh"
    return {"status": status, "read_at": _iso(read_at), "age_s": age, "reason": None}


def _read_time(value: Any, now: datetime) -> tuple[datetime | None, str]:
    """The reading's time, or ``None`` and why: absent, unparseable or ahead of the clock."""
    read_at = _moment(value)
    if read_at is None:
        return None, "no_reading"
    if (read_at - now).total_seconds() > _CLOCK_SKEW_S:
        return None, "read_time_in_future"
    return read_at, ""


def _blank_usage(reason: str) -> dict[str, Any]:
    return {
        "weekly": _unknown(reason), "pace": _unknown(reason), "session": _unknown(reason),
        "extra_windows": [], "breakdown": [], "credits": _unknown(reason),
        "banked_resets": _unknown(reason), "provider_plan": None,
    }


def _computed_pace(weekly: Mapping[str, Any], read_at: datetime, now: datetime) -> dict[str, Any]:
    """CodexBar's pace algorithm applied to a weekly window, at the reading's time."""
    if weekly.get("status") != "ok" or weekly.get("resets_at") is None:
        return _unknown("not_reported")
    pace = compute_usage_pace(weekly["used_pct"], weekly["resets_at"], window_minutes=weekly.get("window_minutes"),
                              now=read_at)
    if pace is None or not pace_is_visible(pace):
        return _unknown("too_early_in_window")
    runs_out = _after(read_at, _number(pace.get("eta_seconds")))
    return _pace(pace["delta_pct"], pace["expected_pct"], pace["will_last_to_reset"], runs_out,
                 format_usage_pace_summary(pace), now, source="computed")


# --- the usage cache (Claude, Codex, AGY, Grok+Cursor) ----------------------------------------------


def _cache_path() -> Path | None:
    if CACHE_ENV in os.environ:
        value = os.environ[CACHE_ENV]
        return Path(value) if value else None
    return Path.home() / ".cache" / "lu-usage" / "codexbar.json"


def _read_cache() -> tuple[Mapping[str, Any], dict[str, Any]]:
    path = _cache_path()
    if path is None:
        return {}, {"status": "not_configured", "written_at": None}
    try:
        with path.open("rb") as handle:
            raw = handle.read(_CACHE_MAX_BYTES + 1)
    except FileNotFoundError:
        return {}, {"status": "missing", "written_at": None}
    except OSError:
        return {}, {"status": "unreadable", "written_at": None}
    try:
        data = json.loads(raw) if len(raw) <= _CACHE_MAX_BYTES else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("pools"), dict):
        return {}, {"status": "invalid", "written_at": None}
    pools: dict[str, Any] = data["pools"]
    written = _moment(data.get("written_at"))
    return pools, {"status": "ok", "written_at": _iso(written) if written else None}


def _cache_windows(usage: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {key: usage[key] for key in ("primary", "secondary", "tertiary") if isinstance(usage.get(key), dict)}


def _cache_usage(entry: Mapping[str, Any], read_at: datetime, now: datetime, *, provider: str,
                 group: str | None) -> dict[str, Any]:
    report = _dict(entry.get("usage"))
    usage = _dict(report.get("usage"))
    labels = _dict(report.get("rateWindowLabels"))
    windows = _cache_windows(usage)

    def window_from(label: str | None, raw: Mapping[str, Any]) -> dict[str, Any]:
        return _window(label, raw.get("usedPercent"), raw.get("resetsAt"), raw.get("windowMinutes"), now,
                       raw.get("resetDescription"))

    def window(key: str) -> dict[str, Any]:
        return window_from(_text(labels.get(key)) or key, windows[key])

    def shown(raw: Mapping[str, Any]) -> bool:
        """Claude and Codex have no real 5-hour window: none of their 300-minute windows is published."""
        return not (provider in _NO_SESSION_PROVIDERS and _number(raw.get("windowMinutes")) == _SESSION_MINUTES)

    titled = {}
    for extra in _items(usage.get("extraRateWindows")):
        if isinstance(extra, dict) and isinstance(extra.get("window"), dict) and _text(extra.get("title")):
            titled[extra["title"]] = extra["window"]
    result = _blank_usage("not_reported")
    weekly_key = None
    if group is not None:
        # One quota group of a shared pool: only its own titled windows; the plain windows repeat them.
        weekly_raw, session_raw = titled.get(f"{group} weekly"), titled.get(f"{group} 5-hour")
        result["weekly"] = window_from("Weekly", weekly_raw) if weekly_raw else _unknown("no_weekly_window")
        result["session"] = window_from("Session", session_raw) if session_raw else {"status": "none"}
    else:
        weekly_key = next((k for k in ("secondary", "primary", "tertiary")
                           if windows.get(k, {}).get("windowMinutes") == _WEEKLY_MINUTES), None)
        session_key = next((k for k in ("primary", "secondary", "tertiary")
                            if windows.get(k, {}).get("windowMinutes") == _SESSION_MINUTES), None)
        result["weekly"] = window(weekly_key) if weekly_key else _unknown("no_weekly_window")
        if provider in _NO_SESSION_PROVIDERS:
            result["session"] = _unknown("not_reported")
        else:
            result["session"] = window(session_key) if session_key else {"status": "none"}
        result["extra_windows"] = [window(k) for k in windows if k not in (weekly_key, session_key) and shown(windows[k])]
        result["extra_windows"] += [window_from(title, raw) for title, raw in titled.items() if shown(raw)]

    review = _dict(_dict(report.get("openaiDashboard")).get("codeReviewLimit"))
    if review and shown(review):
        result["extra_windows"].append(window_from("Code review", review))

    pace = _dict(report.get("pace")).get(weekly_key) if weekly_key else None
    if isinstance(pace, dict):
        runs_out = _after(read_at, _number(pace.get("etaSeconds")))
        result["pace"] = _pace(_number(pace.get("deltaPercent")), _number(pace.get("expectedUsedPercent")),
                               pace.get("willLastToReset"), runs_out, pace.get("summary"), now)
    else:  # CodexBar gives no pace for this window (AGY): the same algorithm, computed here
        result["pace"] = _computed_pace(result["weekly"], read_at, now)

    credits = report.get("credits")
    balance = _number(credits.get("remaining")) if isinstance(credits, dict) else None
    if not isinstance(credits, dict):
        result["credits"] = {"status": "none"}
    elif credits.get("balanceReadSucceeded") is False or balance is None or balance < 0:
        # Older CodexBar reports omit the success flag; only an explicit failure voids the balance.
        result["credits"] = _unknown("balance_not_read")
    else:
        result["credits"] = {"status": "ok", "balance": round(balance, 1), "unit": "credits", "renews_on": None}

    banked = usage.get("codexResetCredits")
    if _count(banked) is not None:  # older CodexBar reports a bare count
        result["banked_resets"] = {"status": "ok", "source": "provider", "count": banked, "items": []}
    elif isinstance(banked, dict):
        listed = banked.get("credits")
        # A list counts only when complete and every entry carries a readable status.
        readable = (isinstance(listed, list) and len(listed) <= _MAX_LIST
                    and all(isinstance(c, dict) and _text(c.get("status"), 40) for c in listed))
        items = []
        for credit in _items(listed) if readable else []:
            if credit.get("status") == "available":
                expires = _moment(credit.get("expires_at"))
                items.append({"kind": _text(credit.get("title")) or _text(credit.get("reset_type")), "count": 1,
                              "expires_on": _iso(expires) if expires else None, "expired": bool(expires and expires < now)})
        count = _count(banked.get("availableCount"))
        if count is None and readable:
            count = len(items)
        if count is not None:
            result["banked_resets"] = {"status": "ok", "source": "provider", "count": count, "items": items}

    rows = []
    for group in _items(usage.get("details")):
        for row in _items(group.get("rows")) if isinstance(group, dict) else []:
            if (isinstance(row, dict) and isinstance(row.get("label"), str)
                    and row["label"] in _BREAKDOWN_LABELS
                    and isinstance(row.get("value"), str)
                    and _BREAKDOWN_PERCENT.fullmatch(row["value"])):
                rows.append({"label": row["label"], "value": row["value"]})
    result["breakdown"] = rows[:_MAX_LIST]
    result["provider_plan"] = _text(usage.get("loginMethod"), 40)
    return result


def _cache_account(entry: Any, now: datetime, *, provider: str, group: str | None = None) -> dict[str, Any]:
    if not isinstance(entry, dict):
        return {"reading": _reading(None, now), **_blank_usage("no_reading")}
    read_at, why = _read_time(entry.get("read_at"), now)
    reading = _reading(read_at, now)
    if read_at is None:
        reading["reason"] = why
    error_at = _moment(entry.get("error_at"))
    if entry.get("error") and error_at:
        # Only the error's kind: the message can carry provider output.
        head, separator, _ = str(entry["error"]).partition(":")
        kind = head if separator and _ERROR_KIND.fullmatch(head) else "error"
        reading["last_refresh_error"] = {"at": _iso(error_at), "kind": kind}
    if read_at is None:
        return {"reading": reading, **_blank_usage(why)}
    return {"reading": reading, **_cache_usage(entry, read_at, now, provider=provider, group=group)}


# --- the Monitor's in-memory reading (Claude) --------------------------------------------------------


def _monitor_window(label: str, raw: Any, read_at: datetime, now: datetime) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {**_unknown("no_reading"), "label": label}
    return _window(label, raw.get("used_pct"), raw.get("resets_at"), raw.get("window_minutes"), now,
                   raw.get("reset_description"))


def _monitor_account(record: Mapping[str, Any] | None, now: datetime, *, provider: str) -> dict[str, Any]:
    record = record if isinstance(record, Mapping) else {}
    freshness = record.get("freshness")
    read_at, why = _read_time(record.get("fetched_at"), now)
    if why == "read_time_in_future":
        return {"reading": {**_reading(None, now), "reason": why}, **_blank_usage(why)}
    if freshness not in ("fresh", "stale_last_good") or read_at is None:
        if not record or (record.get("last_failure_at") is None and record.get("failure_kind") in (None, "unavailable")):
            reason = NO_MONITOR_READING  # nothing read in this process yet (or no Monitor here)
        else:
            reason = PROVIDER_ERROR if record.get("auth_error") or record.get("error_code") else "no_reading"
        reading = {**_reading(None, now), "reason": reason}
        return {"reading": reading, **_blank_usage("no_reading")}
    reading = _reading(read_at, now, stale=freshness != "fresh")
    result = _blank_usage("not_reported")
    windows = _dict(record.get("windows"))
    found = {raw.get("window_minutes"): raw for raw in windows.values() if isinstance(raw, dict)}
    weekly_raw, session_raw = found.get(_WEEKLY_MINUTES), found.get(_SESSION_MINUTES)
    result["weekly"] = _monitor_window("Weekly", weekly_raw, read_at, now)
    result["session"] = (_unknown("not_reported") if provider in _NO_SESSION_PROVIDERS else
                         _monitor_window("Session", session_raw, read_at, now) if session_raw else {"status": "none"})
    fable = _dict(record.get("fable_weekly"))
    if provider == "claude" and _number(fable.get("used_pct")) is not None:
        result["extra_windows"] = [_monitor_window("Fable only", fable, read_at, now)]
    result["pace"] = _computed_pace(result["weekly"], read_at, now)
    result["credits"] = {"status": "none"}
    return {"reading": reading, **result}


# --- plan facts and the snapshot --------------------------------------------------------------------


def _plan(facts: Mapping[str, Any], key: str, provider_plan: str | None) -> dict[str, Any]:
    row = (facts.get("pools") or {}).get(key)
    if not isinstance(row, Mapping):
        reason = facts.get("reason") or "not_in_plan_facts"
        return {"status": "unknown", "reason": reason, "name": provider_plan,
                "name_source": "provider" if provider_plan else None}
    # The provider's own plan name wins; the file only fills what the provider lacks.
    name = provider_plan or row.get("plan")
    return {
        "status": "ok", "reason": None, "name": name,
        "name_source": "provider" if provider_plan else "plan_facts" if name else None,
        **{field: row.get(field) for field in ("next_renewal_on", "days_to_renewal", "cancelled", "ends_on",
                                               "days_to_end", "ended")},
    }


def _banked_from_facts(facts: Mapping[str, Any], key: str) -> dict[str, Any]:
    row = (facts.get("pools") or {}).get(key)
    items = row.get("banked_resets") if isinstance(row, Mapping) else None
    if items is None:
        return _unknown(facts.get("reason") or "not_in_plan_facts")
    live = [item for item in items if not item["expired"]]
    return {"status": "ok", "source": "plan_facts", "count": sum(item["count"] for item in live), "items": items}


def subscriptions_snapshot(*, now: datetime | None = None, monitor_reader: MonitorReader | None = None) -> dict[str, Any]:
    """Every pool's usage, pace, windows, credits, banked resets and plan facts; reads caches and files only."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    reader = monitor_reader or _monitor_reading
    cache, cache_status = _read_cache()
    facts = account_logins.plan_facts(now.date())
    monitor: dict[str, Mapping[str, Any] | None] = {}
    providers = []
    for provider, label, accounts in PROVIDERS:
        rows = []
        for account, key, source, source_key in accounts:
            if source != "cache" and source_key not in monitor:
                try:
                    monitor[source_key] = reader(source_key)
                except Exception:  # one broken reading must not blank the others
                    monitor[source_key] = None
            try:
                if source == "cache":
                    usage = _cache_account(cache.get(source_key), now, provider=provider,
                                           group=_CACHE_GROUPS.get(account))
                    if cache_status["status"] != "ok" and usage["reading"]["status"] == "unknown":
                        usage["reading"]["reason"] = f"usage_cache_{cache_status['status']}"
                else:
                    usage = _monitor_account(monitor[source_key], now, provider=provider)
            except Exception:  # an unforeseen malformed reading is unknown for this account only
                usage = {"reading": {**_reading(None, now), "reason": "unreadable_reading"},
                         **_blank_usage("unreadable_reading")}
            provider_plan = usage.pop("provider_plan")
            if usage["banked_resets"].get("status") != "ok":
                usage["banked_resets"] = _banked_from_facts(facts, key)
            plan = _plan(facts, key, provider_plan)
            if key in _CREDITS_RENEW_WITH_PLAN and usage["credits"].get("status") == "ok":
                usage["credits"]["renews_on"] = plan.get("next_renewal_on")
            rows.append({
                "account": account, "plan_key": key,
                "source": "usage_cache" if source == "cache" else "monitor",
                **usage, "plan": plan, "note": _ACCOUNT_NOTES.get(account),
            })
        providers.append({"provider": provider, "label": label, "accounts": rows})
    return {
        "generated_at": _iso(now),
        "stale_after_s": STALE_AFTER_S,
        "sources": {"usage_cache": cache_status, "plan_facts": {"status": "ok" if facts["reason"] is None else "unknown",
                                                                 "reason": facts["reason"]}},
        "providers": providers,
    }


# --- one pace verdict per pool, for routing, alerts and the page -----------------------------------


# The routing pool id of each account row; AGY's two quota groups are separate pools.
POOL_IDS = {
    "Claude a": "claude:a", "Claude b": "claude:b", "Codex a": "codex:a", "Codex b": "codex:b",
    "AGY Gemini": "agy:gemini", "AGY Claude/GPT": "agy:claude_gpt", "Grok+Cursor": "grok",
}
POOL_STATUSES = ("cool", "hot", "near_cap", "unknown")
POOL_PACE_REASONS = frozenset({
    "lasts_to_reset", "too_early_in_window", "runs_out_before_reset", "ran_out_before_reset", "weekly_used_up",
    "no_reading", "read_time_in_future", NO_MONITOR_READING, PROVIDER_ERROR, "unreadable_reading",
    "usage_cache_missing", "usage_cache_not_configured", "usage_cache_unreadable", "usage_cache_invalid",
    "no_weekly_window", "not_reported", "no_pace", "last_to_reset_unknown", "no_account_row",
})
_EXHAUSTED_USED_PCT = 99.0  # the routing near-cap line (``credit_lane.SUBSCRIPTION_NEAR_CAP_USED_PCT``)


def pool_status(row: Mapping[str, Any]) -> dict[str, Any]:
    """One account row's pace verdict, from the same pace the page shows.

    ``hot``: the pace runs out before the weekly reset. ``near_cap``: the weekly
    window is used up, or the projected run-out has passed. ``cool``: the pace
    lasts to the reset, or the window is too young for a pace (CodexBar shows
    none yet). ``unknown``: no reading, no weekly window, or no pace verdict;
    never ``cool``. A stale reading keeps its verdict and says so.
    """
    reading, weekly, pace = row.get("reading") or {}, row.get("weekly") or {}, row.get("pace") or {}

    def verdict(status: str, reason: str | None) -> dict[str, Any]:
        out = {"status": status, "pace_reason": reason, "stale": reading.get("status") == "stale",
               "resets_at": weekly.get("resets_at"), "runs_out_at": None, "runs_out_in_s": None}
        if status in {"hot", "near_cap"} and pace.get("status") == "ok":
            out.update(runs_out_at=pace.get("runs_out_at"), runs_out_in_s=pace.get("runs_out_in_s"))
        return out

    if reading.get("status") not in {"fresh", "stale"}:
        return verdict("unknown", reading.get("reason") or "no_reading")
    if weekly.get("status") != "ok":
        return verdict("unknown", weekly.get("reason") or "no_weekly_window")
    if weekly["used_pct"] >= _EXHAUSTED_USED_PCT:
        return verdict("near_cap", "weekly_used_up")
    if pace.get("status") != "ok":
        if pace.get("reason") == "too_early_in_window":
            return verdict("cool", "too_early_in_window")
        return verdict("unknown", pace.get("reason") or "no_pace")
    if pace.get("lasts_to_reset") is True:
        return verdict("cool", "lasts_to_reset")
    if pace.get("lasts_to_reset") is False:
        seconds = pace.get("runs_out_in_s")
        if isinstance(seconds, int) and seconds <= 0:
            return verdict("near_cap", "ran_out_before_reset")
        return verdict("hot", "runs_out_before_reset")
    return verdict("unknown", "last_to_reset_unknown")


def pool_statuses(snapshot: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Every pool's :func:`pool_status`, keyed by pool id; a pool missing from the snapshot is ``unknown``."""
    rows = {row.get("account"): row for provider in snapshot.get("providers") or [] for row in provider.get("accounts") or []}
    return {pool: pool_status(rows[account]) if isinstance(rows.get(account), Mapping)
            else {"status": "unknown", "pace_reason": "no_account_row", "stale": False, "resets_at": None,
                  "runs_out_at": None, "runs_out_in_s": None}
            for account, pool in POOL_IDS.items()}


class PoolPaceCollector:
    """Prometheus collector: each pool's :func:`pool_status`, read from the snapshot at scrape time.

    ``monitor_subscription_pool_status{pool,status}`` is 1 for the pool's current status and 0 for the
    others, so an alert matches ``status=~"hot|near_cap"`` (runs out before its reset) or
    ``status="unknown"`` (not visible). ``monitor_subscription_pool_runs_out_seconds{pool}`` is the time
    to the projected run-out, for pools that run out before their reset. A reader failure reports every
    pool ``unknown``; the series never disappear.
    """

    def __init__(self, snapshot: Callable[[], Mapping[str, Any]] | None = None) -> None:
        self._snapshot = snapshot or subscriptions_snapshot

    def describe(self) -> list[Any]:
        return []  # collected at scrape time only, never at registration

    def collect(self) -> Any:
        try:
            pools = pool_statuses(self._snapshot())
        except Exception:
            pools = pool_statuses({})
        status = GaugeMetricFamily("monitor_subscription_pool_status",
                                   "1 for the subscription pool's pace status (cool, hot = runs out before reset, "
                                   "near_cap = used up, unknown = not visible), 0 for the others.",
                                   labels=("pool", "status"))
        runs_out = GaugeMetricFamily("monitor_subscription_pool_runs_out_seconds",
                                     "Seconds to the projected run-out of a pool that runs out before its reset.",
                                     labels=("pool",))
        for pool, verdict in pools.items():
            for name in POOL_STATUSES:
                status.add_metric((pool, name), 1.0 if verdict["status"] == name else 0.0)
            if verdict["status"] in {"hot", "near_cap"} and isinstance(verdict.get("runs_out_in_s"), int):
                runs_out.add_metric((pool,), float(verdict["runs_out_in_s"]))
        yield status
        yield runs_out


# --- console ------------------------------------------------------------------------------------------


def _span(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)):
        return "unknown"
    s = abs(int(seconds))
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{m}m"


def _when(iso: Any, seconds: Any) -> str:
    if not iso:
        return "unknown"
    stamp = str(iso)[:16].replace("T", " ") + "Z"
    if not isinstance(seconds, (int, float)):
        return stamp
    return f"in {_span(seconds)} ({stamp})" if seconds > 0 else f"passed {_span(seconds)} ago ({stamp})"


def _window_text(window: Mapping[str, Any], *, with_reset: bool = True) -> str:
    status = window.get("status")
    if status == "none":
        return "none"
    if status != "ok":
        return "not reported" if window.get("reason") == "not_reported" else f"unknown ({window.get('reason')})"
    usage = f"{window['left_pct']:g}% left ({window['used_pct']:g}% used)"
    if not with_reset:
        return usage
    reset = _when(window["resets_at"], window["resets_in_s"]) if window["resets_at"] else "not reported"
    return f"{usage}, resets {reset}"


def _pace_text(pace: Mapping[str, Any]) -> str:
    if pace.get("status") != "ok":
        return f"unknown ({pace.get('reason')})"
    headroom = pace["headroom_pct"]
    head = "on pace" if pace["state"] == "on_pace" else (
        f"{headroom:g} pp headroom" if headroom > 0 else f"{-headroom:g} pp deficit")
    if pace["lasts_to_reset"] is True:
        return f"{head}, lasts to reset"
    if pace["lasts_to_reset"] is False:
        seconds = pace["runs_out_in_s"]
        if isinstance(seconds, int) and seconds <= 0:
            return f"{head}, ran out {_span(seconds)} ago ({str(pace['runs_out_at'])[:16].replace('T', ' ')}Z)"
        return f"{head}, runs out {_when(pace['runs_out_at'], seconds)}"
    return f"{head}, last-to-reset unknown"


def _plan_text(plan: Mapping[str, Any]) -> str:
    name = plan.get("name") or "plan unknown"
    if plan.get("status") != "ok":
        return f"{name}; renewal unknown ({plan.get('reason')})"
    if plan.get("cancelled"):
        if not plan.get("ends_on"):
            return f"{name}; cancelled, end date not configured"
        return f"{name}; {'ENDED' if plan['ended'] else 'cancelled, ends'} {plan['ends_on']} ({plan['days_to_end']} d)"
    if plan.get("next_renewal_on"):
        return f"{name}; renews {plan['next_renewal_on']} (in {plan['days_to_renewal']} d)"
    return f"{name}; renewal not configured"


def _banked_text(banked: Mapping[str, Any]) -> str:
    if banked.get("status") != "ok":
        return f"unknown ({banked.get('reason')})"
    parts = [f"{item['count']}x {item.get('kind') or 'reset'}"
             + (f" expires {str(item['expires_on'])[:10]}" if item.get("expires_on") else "")
             + (" (EXPIRED)" if item.get("expired") else "") for item in banked["items"]]
    return f"{banked['count']} available ({banked['source']})" + (f": {'; '.join(parts)}" if parts else "")


def render_text(snapshot: Mapping[str, Any]) -> str:
    """The snapshot as a console table: one block per account, grouped by provider."""
    sources = snapshot["sources"]
    lines = [f"Subscriptions at {snapshot['generated_at']} (times UTC; STALE after {snapshot['stale_after_s'] // 60} min)",
             f"usage cache: {sources['usage_cache']['status']}; plan facts: "
             + (sources["plan_facts"]["status"] if sources["plan_facts"]["reason"] is None
                else f"unknown ({sources['plan_facts']['reason']})")]
    if any(row["reading"].get("reason") == NO_MONITOR_READING
           for provider in snapshot["providers"] for row in provider["accounts"]):
        lines.append("The Claude readings live only in the Monitor API process; this process has none.")
    for provider in snapshot["providers"]:
        lines.append("")
        lines.append(f"== {provider['label']} ==")
        for row in provider["accounts"]:
            reading = row["reading"]
            state = reading["status"].upper() if reading["status"] != "fresh" else "fresh"
            read = f"{state}, read {_span(reading['age_s'])} ago" if reading["read_at"] else f"{state} ({reading['reason']})"
            if reading.get("last_refresh_error"):
                read += f"; last refresh failed {reading['last_refresh_error']['at']}"
            lines.append(f"{row['account']}  [{read}]")
            weekly = row["weekly"]
            resets = (_when(weekly["resets_at"], weekly["resets_in_s"]) if weekly.get("status") == "ok" and weekly["resets_at"]
                      else "not reported" if weekly.get("status") == "ok" else f"unknown ({weekly.get('reason')})")
            fields = [("Resets", resets), ("Weekly", _window_text(weekly, with_reset=False)), ("Pace", _pace_text(row["pace"])),
                      ("5-hour", _window_text(row["session"]))]
            fields += [(w.get("label") or "window", _window_text(w)) for w in row["extra_windows"]]
            fields += [(item["label"], item["value"]) for item in row["breakdown"]]
            credits = row["credits"]
            fields.append(("Credits", f"{credits['balance']:,.1f}"
                           + (f" (renew {credits['renews_on']} with the plan)" if credits.get("renews_on") else "")
                           if credits.get("status") == "ok"
                           else "none" if credits.get("status") == "none" else f"unknown ({credits.get('reason')})"))
            fields += [("Plan", _plan_text(row["plan"])), ("Banked", _banked_text(row["banked_resets"]))]
            if row.get("note"):
                fields.append(("Note", row["note"]))
            lines += [f"  {name:<12} {value}" for name, value in fields]
    return "\n".join(lines) + "\n"


def _parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="python -m lu_machinery.api.subscriptions",
        description=(
            "Print every subscription account's weekly usage, pace (headroom or deficit), 5-hour window,\n"
            "scoped windows, credits, banked resets and plan renewal, grouped by provider.\n"
            "Use it on the host console when the machinery home page is not reachable; it never calls\n"
            "the HTTP API, CodexBar or a provider, so Claude (read only inside the Monitor API) shows unknown."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  .venv/bin/python -m lu_machinery.api.subscriptions\n"
            "  .venv/bin/python -m lu_machinery.api.subscriptions --json\n"
            "\n"
            "Inputs (read-only):\n"
            "  ~/.cache/lu-usage/codexbar.json, the usage cache (override: LU_USAGE_CACHE_FILE)\n"
            "  ~/.config/learn-ukrainian/subscription_ends.json, the plan facts (override: LU_SUBSCRIPTION_ENDS_FILE)\n"
            "\n"
            "Outputs: the table, or with --json the same JSON as GET /api/runtime/subscriptions, on stdout.\n"
            "Writes no file and changes no state.\n"
            "\n"
            "Exit codes: 0 printed (missing values print as unknown); 2 bad arguments.\n"
            "\n"
            "Related: GET /api/runtime/subscriptions, the machinery home page (/), Refs #100."
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    parser.add_argument("--json", action="store_true",
                        help="print the snapshot as JSON (same structure as GET /api/runtime/subscriptions); default: a table")
    args = parser.parse_args(argv)
    snapshot = subscriptions_snapshot()
    sys.stdout.write(json.dumps(snapshot, indent=1) + "\n" if args.json else render_text(snapshot))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

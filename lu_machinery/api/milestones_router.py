"""Cached GitHub milestone summaries and open issue progress for the fleet."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any, cast

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from lu_machinery.github_rest_cache import GitHubRestError, GitHubRestTimeout, shared_cache
from lu_machinery.orchestration.fleet_repos import FleetRepo, FleetRepoError, load_fleet_repos

from .monitor_context import MonitorContext, get_ctx
from .opsec_sanitize import sanitize_text
from .panel_values import timestamp
from .state_helpers import cache_get_stored, ctx_scoped_ttl_key, schedule_cache_refresh

router = APIRouter()
REFRESH_SECONDS = 300.0
REPOSITORY_TIMEOUT_SECONDS = 20.0
MAX_PAGES = 20
FRESHNESS = "Refreshes every 5 minutes while this page is open; healthy data is at most 7 minutes old. Older data and failures are marked."
FRESHNESS_BOUND_SECONDS = 420.0


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _text(value: Any) -> str:
    return sanitize_text(value) if isinstance(value, str) else ""


def _milestone(raw: dict[str, Any], *, private: bool) -> dict[str, Any]:
    opened, closed = _count(raw.get("open_issues")), _count(raw.get("closed_issues"))
    if opened is None or closed is None or not _count(raw.get("number")):
        raise GitHubRestError("invalid milestone counts or number")
    total = opened + closed if opened is not None and closed is not None else None
    percent = round(100 * closed / total, 1) if total else (0.0 if total == 0 else None)
    # Private metadata never enters the response, including milestone titles,
    # descriptions, dates, numbers and links. The registry supplies the repo id.
    return {
        "number": None if private else _count(raw.get("number")),
        "title": None if private else _text(raw.get("title")),
        "description_first_line": None if private else _text((raw.get("description") or "").splitlines()[0]
            if isinstance(raw.get("description"), str) and raw["description"].splitlines() else ""),
        "due_on": None if private else timestamp(raw.get("due_on")),
        "open_issues": opened,
        "closed_issues": closed,
        "percent_complete": percent,
        "issues_status": "withheld" if private else "unknown",
        "issues": [],
    }


def _read_pages(path: str, *, deadline: float, stats: dict[str, int]) -> tuple[list[dict[str, Any]], bool]:
    """Walk conditional collection pages; hitting the ceiling is explicit partial data."""
    rows = []
    cache = shared_cache()
    for _ in range(MAX_PAGES):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GitHubRestTimeout(REPOSITORY_TIMEOUT_SECONDS)
        stats["rest_reads"] += 1
        result = cache.get_json(path, timeout=min(5.0, remaining))
        if result.status == 404 or not isinstance(result.body, list) or result.stale:
            raise GitHubRestError("milestone source unavailable")
        if any(not isinstance(row, dict) for row in result.body):
            raise GitHubRestError("invalid milestone source")
        rows.extend(result.body)
        if not result.link_next:
            return rows, True
        path = result.link_next
    return rows, False


def _repository(repo: FleetRepo) -> dict[str, Any]:
    started = time.time()
    private = not repo.role.startswith("public-")
    entry: dict[str, Any] = {
        "repository": repo.github, "private": private, "status": "unknown", "reason": None,
        "observed_at": datetime.fromtimestamp(started, UTC).isoformat(), "rest_reads": 0, "milestones": [],
    }
    deadline = time.monotonic() + REPOSITORY_TIMEOUT_SECONDS
    try:
        rows, complete = _read_pages(
            f"repos/{repo.github}/milestones?state=open&per_page=100", deadline=deadline, stats=entry,
        )
        entry["milestones"] = [_milestone(row, private=private) for row in rows if row.get("state") == "open"]
        entry["status"] = "ok" if complete else "partial"
        entry["reason"] = None if complete else "milestone_page_limit"
        if private or not entry["milestones"]:
            return entry
        # One repository-wide collection, never one call per milestone.
        issues, issues_complete = _read_pages(
            f"repos/{repo.github}/issues?milestone=*&state=open&per_page=100", deadline=deadline, stats=entry,
        )
        by_number = {item["number"]: item for item in entry["milestones"]}
        for issue in issues:
            milestone = issue.get("milestone")
            number = _count(issue.get("number"))
            if "pull_request" in issue or issue.get("state") != "open" or not isinstance(milestone, dict) or not number:
                continue
            target = by_number.get(milestone.get("number"))
            if target is None:
                continue
            target["issues"].append({
                "number": number, "title": _text(issue.get("title")),
                "url": f"https://github.com/{repo.github}/issues/{number}",
                "labels": [_text(label.get("name")) for label in (issue.get("labels") or []) if isinstance(label, dict)],
            })
        for milestone in entry["milestones"]:
            milestone["issues_status"] = "ok" if issues_complete else "partial"
        if not issues_complete:
            entry.update(status="partial", reason="issue_page_limit")
    except (GitHubRestError, GitHubRestTimeout, OSError, ValueError):
        # Raw exception text may contain a private endpoint or diagnostic.
        entry.update(status="unknown", reason="github_unavailable")
    return entry


def collect_milestones() -> dict[str, Any]:
    """One collector for the live registry; failures never become zero milestones."""
    observed_at = datetime.now(UTC).isoformat()
    try:
        repos = list(load_fleet_repos().values())
    except FleetRepoError:
        return {"status": "unknown", "reason": "registry_unavailable", "observed_at": observed_at, "repositories": []}
    with ThreadPoolExecutor(max_workers=min(8, len(repos))) as pool:
        rows = list(pool.map(_repository, repos))
    return {
        "status": "ok" if all(row["status"] == "ok" for row in rows) else "partial",
        "observed_at": observed_at, "repositories": rows,
    }


def _with_age(snapshot: dict[str, Any]) -> dict[str, Any]:
    def age(observed: str) -> float:
        return round(max(0.0, time.time() - datetime.fromisoformat(observed).timestamp()), 1)

    return {
        **snapshot, "response_schema_version": "milestones.v1", "age_seconds": age(snapshot["observed_at"]), "refresh_seconds": REFRESH_SECONDS,
        "freshness": FRESHNESS,
        "stale": age(snapshot["observed_at"]) > FRESHNESS_BOUND_SECONDS,
        "repositories": [{**row, "age_seconds": age(row["observed_at"]),
                          "stale": age(row["observed_at"]) > FRESHNESS_BOUND_SECONDS}
                         for row in snapshot["repositories"]],
    }


@router.get("")
async def milestones(ctx: MonitorContext = Depends(get_ctx)) -> JSONResponse:
    """Serve one snapshot, refreshing off-loop with the established single-flight TTL cache."""
    key = ctx_scoped_ttl_key(ctx, "milestones")
    stored = cache_get_stored(key)
    if stored is None or stored[1] >= REFRESH_SECONDS:
        schedule_cache_refresh(key, REFRESH_SECONDS, collect_milestones)
    if stored is None:
        return JSONResponse({"status": "pending", "repositories": [], "freshness": FRESHNESS},
                            headers={"Cache-Control": "no-store", "Retry-After": "3"})
    payload = _with_age(cast(dict[str, Any], stored[0]))
    return JSONResponse(payload, headers={"Cache-Control": "no-store", "X-Snapshot-Age-S": str(payload["age_seconds"])})

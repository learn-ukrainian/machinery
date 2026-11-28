"""Model value statistics from durable summary rows, landing trailers and allowance readings (#581)."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import threading
import time
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry, generate_latest

from lu_machinery.agent_runtime import model_pause
from lu_machinery.api import model_stats_router as stats
from lu_machinery.api import model_stats_rows as rows
from lu_machinery.api import planning
from lu_machinery.api.delegate_router import active_snapshot
from lu_machinery.api.monitor_context import fixture_context, get_ctx
from lu_machinery.api.state_helpers import cache_get_or_compute, cache_invalidate, ctx_scoped_ttl_key
from lu_machinery.common import git_credentials
from lu_machinery.common.repo_root import machinery_path
from lu_machinery.dispatcher import store, worker
from lu_machinery.github_rest_cache import RestPage, RestResult
from lu_machinery.orchestration.fleet_repos import FleetRepo

NOW = datetime(2026, 11, 15, 12, tzinfo=UTC)
SINCE = NOW - timedelta(days=7)


@pytest.mark.parametrize("recorded", ["fixture/machinery", "moved/machinery"])
def test_model_stats_binds_both_recorded_slugs_to_the_public_row(monkeypatch, recorded):
    """Defect class: model statistics drop public review records anchored after a repository transfer."""
    row = FleetRepo(
        "machinery", "fixture/machinery", "machinery", "public-machinery",
        alternate_github="moved/machinery", repository_id=42, visibility="public",
    )
    repos = {row.key: row}
    monkeypatch.setattr(rows, "load_fleet_repos", lambda: repos)
    rows._fleet_repo_visibility.cache_clear()
    try:
        assert rows.repository_keys(repos)[recorded] == "machinery"
        assert rows._record_allows_sample({"repository": recorded, "repository_id": "fixture/machinery"})
    finally:
        rows._fleet_repo_visibility.cache_clear()


def test_planning_and_model_stats_reject_non_ascii_repository_spellings(monkeypatch):
    """Case conversion must not admit a repository look-alike as worker or public review evidence."""
    registered = "kit/machinery"
    lookalike = "\N{KELVIN SIGN}it/machinery"
    alternate = "moved/machinery"
    repo = FleetRepo(
        "machinery", registered, "machinery", "public-machinery",
        alternate_github=alternate, repository_id=42, visibility="public",
    )
    repos = {repo.key: repo}
    monkeypatch.setattr(rows, "load_fleet_repos", lambda: repos)
    claims = {"planning_claims": [(lookalike, ["src/work.py"])]}
    monkeypatch.setattr(planning, "active_snapshot", lambda _ctx: SimpleNamespace(read=lambda: (claims, 0)))
    aliases = {repo.key: alternate, registered: alternate, alternate: alternate}
    assert planning._workers(None, aliases) is None

    rows._fleet_repo_visibility.cache_clear()
    try:
        assert not rows._record_allows_sample({"repository": lookalike})
    finally:
        rows._fleet_repo_visibility.cache_clear()

    keys = rows.repository_keys(repos)
    raw = {"task_id": "review", "role": "review", "pinned_head": "a" * 40, "repo": repo.key}
    entry = {"head": raw["pinned_head"], "repository": lookalike, "pr": 42}
    assert stats._publication(raw, {"review": [entry]}, keys) is None
    invalid_catalog = {"other": FleetRepo("other", lookalike, "other", "public-machinery")}
    assert rows.repository_keys(invalid_catalog) == {}


def test_model_value_sections_explain_how_to_read_the_figures():
    """A dashboard section can keep showing numbers after losing the nearby reading guidance."""
    html = machinery_path("api/dashboards/model-stats.html").read_text()
    sections = re.findall(r"<section\b[^>]*>.*?</section>", html, re.DOTALL)
    explanations = {
        "Window summary": ("Landed share divides those merges by all write tasks", "Low sample means"),
        "Token use and waste": ("Codex fresh input is input minus cache reads", "Cache hit is cached input divided by fresh plus cached input"),
        "Value frontier": ("first-round approval with recorded hours per landed task", "Look toward the upper left"),
        "Best model per job": ("Compare landed share within each job class", "A star marks the highest positive share"),
        "Where the work went": ("how many write tasks settled", "A larger Done share"),
        "Week by week": ("Higher points mean a larger share of that UTC week's write tasks landed", "Use full weeks with enough tasks"),
        "Neon medium against high": ("write landings, reviewer approvals and typical runtime", "Use the counts"),
        "Reviewer strictness": ("points farther right report more P0–P2 findings", "Compare similar assignments"),
        "Time to finish": ("box spans the middle half of timed writes", "Shorter times are better"),
        "Ukrainian tasks": ("report merged writes and how often reviewers approved", "Prefer a higher landed share"),
        "Pool allowance": ("remaining allowance and reset time", "Cool means more headroom", "Lower points per landed task"),
        "Author or reviewer?": ("approved first time", "Read down one reviewer column"),
        "Write tasks": ("includes settled writes", "higher landed share"),
        "Never started": ("refused before execution by cause and role", "Fewer refusals"),
        "Reviews": ("includes settled reviews", "fewer unreadable or unfinished results"),
        "Compare reviewer levels": ("readable results, first-round approval", "lower time or allowance cost"),
        "Reviewed PRs": ("reviewed PRs for the shown authors", "higher first-round approval and fewer rounds"),
        "Reviewer accuracy": ("counts as fixed when the author changed its file", "check the settled count"),
        "Authors by risk tier": ("risk tier of each PR's first published review", "Compare authors within one tier"),
    }
    for section, phrases in explanations.items():
        rendered = next(fragment for fragment in sections if f"<h2>{section}" in fragment
                        or f'aria-label="{section}"' in fragment)
        for phrase in phrases:
            assert phrase in rendered, (section, phrase)


def _record(tasks: Path, task_id: str, **fields) -> dict:
    record = {"schema_version": 2, "task_id": task_id, "agent": "codex", "model": "gpt-6.1-sol", "effort": "high",
              "mode": "workspace-write", "status": "done", "started_at": (NOW - timedelta(hours=2)).isoformat(),
              "finished_at": (NOW - timedelta(hours=1)).isoformat(), **fields}
    (tasks / f"{task_id}.json").write_text(json.dumps(record))
    return record


@pytest.mark.parametrize("machinery", ["sample/machinery", "new-owner/machinery"])
def test_planning_ranks_product_blocker_and_withholds_colliding_cards(tmp_path, monkeypatch, machinery):
    """Diamond dependencies count once, and a ready issue cannot overlap a live grant."""
    product, historical_machinery, private = "sample/product", "sample/machinery", "sample/private.dev"
    repos = {
        product: FleetRepo(product, product, "product", "public-product"),
        "machinery": FleetRepo(
            "machinery", historical_machinery, "machinery", "public-machinery",
            alternate_github="new-owner/machinery", repository_id=42, visibility="public",
        ),
        private: FleetRepo(private, private, "private.dev", "private-infra"),
    }
    monkeypatch.setattr(
        git_credentials, "load_selector",
        lambda **_kwargs: git_credentials.SelectorState({"machinery": machinery}, "d" * 64),
    )
    title = "Product: remove the classified in-tree machinery copy"
    product_ref = f"{product}#13032"

    def issue(value, name, deps, scope="src/later.py"):
        return {"number": value, "title": name, "state": "OPEN",
                "body": f"## In scope\n{scope}\n\n## Dependencies\n{deps}\n"}

    ambiguous_scopes = ["N/A", "CI/CD", machinery, private, "github.com/docs/readme.md"]
    inventories = {
        product: [issue(13032, title, "None", "src/shared.py")],
        machinery: [
            issue(503, "Redirect runtime callers", title),
            issue(505, "Remove duplicate API", product_ref),
            issue(519, "Remove mirrored readers", f"{historical_machinery}#503; #505"),
            issue(525, "Run issue checks", "#519"),
            issue(584, "Ready card with collision", "#581",
                  f"- src/elsewhere.py, **`{historical_machinery}/src/owned/worker.py:12`**"),
            issue(585, "Ready card on a sibling prefix", "#581", "src/owned-extra/file.py"),
            issue(586, "Unknown dependency", "#404", "docs/guide.md"),
            issue(587, "Scope is in prose", "None", "Refine the reader, use `owned_paths`, e.g. Ruff 0.15.21."),
            issue(588, "Unresolved cycle", "#589"),
            issue(589, "Unresolved cycle partner", "#588"),
            issue(590, "Blocked by a private issue", f"{private}#999"),
            issue(591, "Ready card with partial scope", "None", "- src/elsewhere.py\n- Its callers and tests"),
            *(issue(number, "Ready card with a slash in prose", "None", scope)
              for number, scope in enumerate(ambiguous_scopes, start=592)),
        ],
        private: [issue(999, "Private work", product_ref)],
    }
    monkeypatch.setattr(planning, "load_fleet_repos", lambda: repos)
    monkeypatch.setattr(planning.github, "list_open_issues",
                        lambda repo, **_: RestPage(inventories[repo], truncated=False))
    monkeypatch.setattr(planning.github, "issue_states", lambda repo, numbers, **_: {
        issue: "closed" for issue in numbers if (repo, issue) in {(machinery, 581), (product, 13032)}})

    def search(endpoint, **_):
        terms = parse_qs(urlsplit(endpoint).query)["q"][0]
        candidates = [{"title": title, "repository_url": f"https://api.github.com/repos/{product}", "number": 13032}
                      ] if title in terms else []
        return RestResult(200, {"items": candidates, "total_count": len(candidates), "incomplete_results": False},
                          None, None, False)

    monkeypatch.setattr(planning.github, "shared_cache", lambda: SimpleNamespace(get_json=search))
    context = fixture_context(tmp_path)
    tasks = context.roots.batch_state_dir / "tasks"
    tasks.mkdir(parents=True)
    _record(tasks, "live-writer", status="spawning", repository=historical_machinery,
            owned_paths=["src/shared.py"], run_nonce="run",
            owned_path_grants=[{"grant_id": "grant", "task_id": "live-writer", "run_nonce": "run",
                                "owned_paths": ["src/owned"]}])
    _record(tasks, "observer", status="spawning", mode="read-only", owned_paths=["src/owned-extra"])
    active_snapshot(context).refresh()
    monkeypatch.setattr(stats, "cached_snapshot", lambda _: None)
    app = FastAPI()
    app.state.ctx = context
    app.include_router(stats.router, prefix="/api/fleet/model-stats")

    key = ctx_scoped_ttl_key(context, "planning", "issues", context.roots)
    cache_get_or_compute(key, 300, planning.collect_issues)
    meter_key = ctx_scoped_ttl_key(context, "planning", "meters", context.roots)
    cache_get_or_compute(meter_key, 30, lambda: planning.MeterSnapshot({}, {}, {}))
    with TestClient(app) as client:
        response = client.get("/api/fleet/model-stats/planning")
        assert response.status_code == 200
        result = response.json()["issues"]
        assert result["coverage"]["repositories_read"] == result["coverage"]["repositories_total"] == 3
        assert result["coverage"]["open_issues"] == 19
        assert result["coverage"]["dependency_unknown"] == 1
        assert result["blockers"][0] == {"ref": product_ref, "direct": 3, "total": 6}
        assert {"ref": None, "direct": 1, "total": 1} in result["blockers"]
        assert result["blocked"][0] == {"ref": f"{machinery}#503", "blocked_by": [product_ref], "private_blockers": 0}
        assert result["ready_now"] == [{"ref": f"{machinery}#585"}, {"ref": product_ref}]
        assert result["coverage"]["path_conflicts"] == 1
        assert result["coverage"]["scope_unknown"] == 7
        assert {"ref": f"{machinery}#584", "path_status": "path_conflicts"} in result["ready"]
        for issue_number in range(591, 597):
            assert {"ref": f"{machinery}#{issue_number}", "path_status": "scope_unknown"} in result["ready"]
        assert private not in response.text
        assert "src/owned" not in response.text

        inventories[product] = []
        cache_invalidate(key)
        cache_get_or_compute(key, 300, planning.collect_issues)
        closed = client.get("/api/fleet/model-stats/planning").json()["issues"]
        assert all(row["ref"] != product_ref for row in closed["blockers"])
        assert {"ref": f"{machinery}#503"} in closed["ready_now"]
        assert {"ref": f"{machinery}#505"} in closed["ready_now"]


def test_planning_reads_a_registered_repository_prefix_as_that_repository():
    """Defect class: a registered owner/name prefix was a path in the issue's own repository.

    The planning view then missed a live worker in the named repository and marked the issue ready.
    """
    product, machinery, private = "sample/product", "sample/machinery", "sample/private.dev"
    work, tools = "oak/work", "oak/tools.dev"
    lookalike = "sample/ma\u212ainery/src/owned/file.py"
    folded = "oa\u212a/tools.dev"
    snap = planning.IssueSnapshot(
        frozenset({product, machinery, private, work, tools}),
        {"product": product, "machinery": machinery, "private": private, "work": work, "tools": tools},
        {
            (product, 1): planning.Issue("prefixed", f"## In scope\n{machinery}/src/owned/worker.py\n"),
            (product, 2): planning.Issue("plain", "## In scope\nsrc/owned/worker.py\n"),
            (product, 3): planning.Issue("explicit relative", f"## In scope\n./{machinery}/src/owned/worker.py\n"),
            (product, 4): planning.Issue("unregistered", "## In scope\nother/tree/src/file.py\n"),
            (product, 5): planning.Issue("dotted slug", f"## In scope\n{private}/docs/guide.md\n"),
            (product, 6): planning.Issue("case and letters", "## In scope\nSample/Machinery/файл.py\n"),
            (machinery, 7): planning.Issue("own prefix", f"## In scope\n{machinery}/src/owned/file.py\n"),
            (machinery, 8): planning.Issue("own plain", "## In scope\nsrc/elsewhere.py\n"),
            (product, 9): planning.Issue("lookalike", f"## In scope\n{lookalike}\n"),
            (product, 10): planning.Issue(
                "parent segment",
                f"## In scope\n{machinery}/docs/../src/owned/file.py\n{machinery}/src/owned/file.py\n",
            ),
            (product, 11): planning.Issue("dotted directory", f"## In scope\n{machinery}/release.v2/file.py\n"),
            (product, 12): planning.Issue("dotted subtree", f"## In scope\n{machinery}/release.v2/docs/**\n"),
            (product, 13): planning.Issue("repeated slash", f"## In scope\n{machinery}//src/owned/worker.py\n"),
            (product, 14): planning.Issue("folded remainder", f"## In scope\n{work}/{folded}\n"),
            (product, 15): planning.Issue("folded subtree", f"## In scope\n{work}/{folded}/**\n"),
            (product, 16): planning.Issue("ascii remainder", f"## In scope\n{work}/{tools}\n"),
            (product, 17): planning.Issue("bare slug", f"## In scope\n{tools}\n"),
        },
        {}, frozenset(), 5,
    )
    workers = {
        machinery: (planning.expand_claim("src/owned") + planning.expand_claim("файл.py")
                    + planning.expand_claim("release.v2/file.py") + planning.expand_claim("release.v2")),
        private: planning.expand_claim("docs"),
        product: planning.expand_claim(lookalike),
        work: planning.expand_claim(folded),
    }
    ready = {row["ref"]: row["path_status"] for row in planning.project_issues(snap, workers, stale=False)["ready"]}
    assert ready == {
        f"{product}#1": "path_conflicts",
        f"{product}#2": "ready_now",
        f"{product}#3": "ready_now",
        f"{product}#4": "ready_now",
        f"{product}#5": "path_conflicts",
        f"{product}#6": "path_conflicts",
        f"{machinery}#7": "path_conflicts",
        f"{machinery}#8": "ready_now",
        f"{product}#9": "path_conflicts",
        f"{product}#10": "scope_unknown",
        f"{product}#11": "path_conflicts",
        f"{product}#12": "path_conflicts",
        f"{product}#13": "scope_unknown",
        f"{product}#14": "path_conflicts",
        f"{product}#15": "path_conflicts",
        f"{product}#16": "scope_unknown",
        f"{product}#17": "scope_unknown",
    }
    withheld = planning.project_issues(snap, {machinery: None, private: [], product: []}, stale=False)
    assert {"ref": f"{product}#1", "path_status": "worker_scope_unknown"} in withheld["ready"]
    assert all(row["ref"] != f"{product}#1" for row in withheld["ready_now"])


def test_planning_forecast_matches_two_meter_readings(tmp_path, monkeypatch):
    """Recent burn comes from two observations of one reset window, independent of the reported average."""
    now = datetime.now(UTC)
    earlier = now - timedelta(hours=2, minutes=10)
    reset = now + timedelta(hours=25)

    def series(name, value, *, previous=False, pool=None):
        metric = {"__name__": name}
        if previous:
            metric["planning_sample"] = "previous"
        if pool is not None:
            metric["pool"] = pool
        return {"metric": metric, "value": [now.timestamp(), str(value)]}

    samples = []
    for previous, when, remaining in ((False, now, 20), (True, earlier, 21)):
        samples.extend([
            series("lu_cto_source_ok", 1, previous=previous),
            series("node_textfile_mtime_seconds", when.timestamp(), previous=previous),
            series("lu_cto_pool_left_percent", remaining, previous=previous, pool="claude-a"),
            series("lu_cto_pool_reset_days", round((reset - when).total_seconds() / 86400, 1),
                   previous=previous, pool="claude-a"),
        ])
    samples.extend([
        series("lu_cto_pool_burn_percent_per_hour", 0.9, pool="claude-a"),
        series("lu_cto_pool_sustainable_percent_per_hour", 0.8, pool="claude-a"),
    ])
    monkeypatch.setattr(planning, "_prometheus", lambda: samples)
    monkeypatch.setattr(planning.subscriptions, "subscriptions_snapshot", lambda **_: {"providers": [{
        "accounts": [{"account": "Claude a", "reading": {"status": "fresh"},
                      "weekly": {"status": "ok", "used_pct_exact": 80, "resets_at": reset.isoformat()}},
                     {"account": "Codex b", "reading": {"status": "fresh"},
                      "weekly": {"status": "ok", "used_pct_exact": 40, "resets_at": reset.isoformat()}}],
    }]})
    rates = {"claude:a": {"points_per_hour": 0.2, "tasks": 12}, "codex:b": {"points_per_hour": 0.35, "tasks": 4},
             "gemini": {"points_per_hour": 0.05, "tasks": 3}}
    monkeypatch.setattr(stats, "cached_snapshot", lambda _: ({"worker_allowance": rates}, 0.0))
    context = fixture_context(tmp_path)
    for name, value in (("issues", None), ("meters", planning.collect_meters())):
        key = ctx_scoped_ttl_key(context, "planning", name, context.roots)
        cache_get_or_compute(key, 300, lambda value=value: value)
    app = FastAPI()
    app.state.ctx = context
    app.include_router(stats.router, prefix="/api/fleet/model-stats")
    with TestClient(app) as client:
        data = client.get("/api/fleet/model-stats/planning").json()
    pools = {row["pool"]: row for row in data["capacity"]}
    assert set(pools) == {"claude:a", "claude:b", "codex:a", "codex:b", "agy:gemini", "agy:claude_gpt", "grok"}
    claude = pools["claude:a"]
    # 20 points left, 1 point used in 130 minutes, and 25 hours to reset.
    assert abs(claude["unused_pct"] - 8.46) < 0.1
    assert claude["burn_pct_per_hour"] == 0.462
    assert claude["reported_burn_pct_per_hour"] == 0.9
    assert claude["workers_needed"] == 2
    assert claude["worker_samples"] == 12
    assert pools["codex:b"]["remaining_pct"] == 60
    assert pools["codex:b"]["unused_pct"] is None

    meter_key = ctx_scoped_ttl_key(context, "planning", "meters", context.roots)
    for row in samples:
        if row["metric"] == {"__name__": "lu_cto_pool_left_percent", "planning_sample": "previous", "pool": "claude-a"}:
            row["value"][1] = "19"  # The remaining allowance rose between samples.
    cache_invalidate(meter_key)
    cache_get_or_compute(meter_key, 30, planning.collect_meters)
    with TestClient(app) as client:
        data = client.get("/api/fleet/model-stats/planning").json()
    assert next(row for row in data["capacity"] if row["pool"] == "claude:a")["unused_pct"] is None


def _review(tasks: Path, task_id: str, correctness: str, priorities: list[str], **fields) -> None:
    _record(tasks, task_id, **{"mode": "read-only", "agent": "claude", "model": "opus",
                               "resolved_model": "claude-opus-5-5", "resolved_model_known": True,
                               "review_profile": "code", "review_risk": "medium", **fields})
    findings = [{"id": f"F{i}", "priority": p, "body": f"finding body {i}"} for i, p in enumerate(priorities)]
    (tasks / f"{task_id}.result").write_text(json.dumps({"overall": {"correctness": correctness}, "findings": findings}))


def _state(tmp_path: Path) -> tuple[Path, Path]:
    tasks = tmp_path / "batch_state" / "tasks"
    tasks.mkdir(parents=True)
    return tmp_path / "batch_state", tasks


def _window(batch_state: Path, landing: stats.Landing, name: str = "7d") -> dict:
    return stats.collect(batch_state, batch_state.parent, NOW, landing=landing)["windows"][name]


def _row(payload: dict, model: str, role: str) -> dict:
    return next(r for r in payload["models"] if r["model"] == model and r["role"] == role)


def test_never_started_refusals_survive_backfill_and_keep_failed_review_counts_separate(tmp_path):
    """Pre-start refusals must stay out of failed reviews and runtime observations (#889)."""
    batch_state, tasks = _state(tmp_path)
    boundary = {"returncode": 125, "error": "write_boundary_refused: local diagnostic", "provider_pgid": 321}
    cases = {
        "account": {"never_started_reason": "account_check", "duration_s": 0},
        "capacity": {"never_started_reason": "admission_capacity"},
        "eligibility": {"never_started_reason": "route_eligibility"},
        "headroom": {"status": "rate_limited", "never_started_reason": "headroom"},
        "placement": {"returncode_reason": "dispatch_fallback_refused"},
        "setup": {"never_started_reason": "setup_refusal"},
        "review-setup": {"never_started_reason": "review_setup_refusal"},
        "model-selection": {"error": "agy_model_unavailable: unavailable"},
        "boundary-refused": {**boundary, "write_boundary": {"status": "refused"}, "duration_s": 99},
        "boundary-continuation": {**boundary, "write_boundary": {"status": "refused"}, "never_started_reason": None},
        "boundary-interrupted": {**boundary, "write_boundary": {"status": "interrupted"}},
        "boundary-missing": boundary,
        "model-ran": {"never_started_reason": "setup_refusal", "duration_s": 12,
                      "usage": {"input_tokens": 7, "cached_input_tokens": 0}},
    }
    for task_id, fields in cases.items():
        record = _record(tasks, task_id, **{"mode": "read-only", "review_profile": "code", "status": "failed", **fields})
        path = tasks / f"{task_id}.json"
        old_row = rows.summary_row(path, record, path.stat().st_mtime_ns)
        assert old_row is not None
        old_row["status"] = record["status"]
        del old_row["never_started_reason"]
        rows.append_row(batch_state, old_row)
    assert rows.backfill(tasks, batch_state)["refreshed"] == 9
    assert rows.backfill(tasks, batch_state)["refreshed"] == 0
    payload = _row(_window(batch_state, stats.Landing()), "gpt-6.1-sol", "review")
    assert payload["status"]["never_started"] == 9
    assert payload["status"]["failed"] == payload["verdicts"]["unparsed"] == 4
    assert payload["wall_seconds"]["n"] == 1
    assert payload["wall_seconds"]["total_seconds"] == payload["wall_seconds"]["median"] == 12
    assert payload["never_started_reasons"] == {
        "account_check": 1, "admission_capacity": 1, "boundary_refusal": 1, "headroom": 1,
        "model_unavailable": 1, "review_setup_refusal": 1, "route_eligibility": 1,
        "setup_refusal": 1, "worker_placement": 1,
    }
    stored = {row["task_id"]: row for row in rows.read_rows(batch_state)[0]}
    assert stored["boundary-refused"]["tokens"] == {"fresh_input": 0, "cached_input": 0, "output": 0}
    assert "local diagnostic" not in rows.rows_path(batch_state).read_text()


def test_per_model_figures_survive_settlement_backfill_and_retention(tmp_path, monkeypatch):
    """Defect class: wrong aggregation, or a summary row lost, stale, corrupted or dropped by record retention."""
    batch_state, tasks = _state(tmp_path)
    monkeypatch.setenv("LU_TASKS_DIR", str(tasks))
    # Codex output_tokens already include the reasoning tokens.
    usage = {"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 100, "reasoning_output_tokens": 50}
    # Settled through the dispatcher store: the row is appended after the record's write.
    settled = _record(tasks, "w1", duration_s=600, usage=usage, research_task_family="code",
                      research_role="implementation")
    store.replace(settled, tasks / "w1.json")
    # A later terminal rewrite whose row write fails leaves the record as written and an older row stored.
    rewritten = _record(tasks, "w3", duration_s=1800, usage=usage)
    store.replace(rewritten, tasks / "w3.json")
    time.sleep(0.05)  # a later record version gets a later modification time
    monkeypatch.setattr(rows, "append_row", lambda *_a: (_ for _ in ()).throw(OSError("disk full")))
    store.replace({**rewritten, "status": "no_deliverable"}, tasks / "w3.json")
    assert json.loads((tasks / "w3.json").read_text())["status"] == "no_deliverable"
    monkeypatch.undo()
    with rows.rows_path(batch_state).open("ab") as stream:
        stream.write(b'{"schema": "model-stats-row.v1", "task_id": "torn')  # an append cut short
    (tasks / "archive").mkdir()
    _record(tasks / "archive", "w2", duration_s=1200, usage=usage, research_task_family="code")
    # A legacy record's id is not checked on read: one that is no dispatcher task id is never stored.
    (tasks / "archive" / "legacy.json").write_text(json.dumps(
        {"schema_version": 1, "task_id": "/srv/private/model", "agent": "codex", "status": "done"}))
    # The next settlement closes the torn line first, so only the fragment is lost.
    store.replace(_record(tasks, "old", finished_at=(SINCE - timedelta(days=40)).isoformat(), duration_s=99),
                  tasks / "old.json")
    _record(tasks, "live", status="running")
    head1, head2 = "1" * 40, "2" * 40
    _review(tasks, "r1-review", "incorrect", ["P0", "P3"], pr=7, review_author_model="gpt-6.1-sol",
            pinned_head=head1, duration_s=300, started_at=(NOW - timedelta(hours=3)).isoformat())
    _review(tasks, "r2-review", "correct", ["P1", "P2"], pr=7, review_author_model="gpt-6.1-sol",
            pinned_head=head2, duration_s=500)
    reply = (tasks / "r2-review.result").read_text()
    (tasks / "r2-review.result").write_text(f"```json\n{reply}\n```\n")  # a fenced reply the contract accepts
    _review(tasks, "r3-review", "incorrect", [])
    (tasks / "r3-review.result").write_text('{"overall": {"correctness": "incorrect"}, "findings": 1}')

    first = rows.backfill(tasks, batch_state)
    again = rows.backfill(tasks, batch_state)
    shutil.rmtree(tasks)  # retention removes every task record; the rows remain
    landing = stats.Landing(task_ids={"w1", "r2-review"})
    landing.repositories["read"] = 1
    landing.prs["public", "7"] = stats._MergedPR(head2, NOW - timedelta(days=2), {"w1"}, set(), set())
    snapshot = stats.collect(batch_state, tmp_path, NOW, landing=landing)
    payload = snapshot["windows"]["7d"]

    assert (first["added"], first["refreshed"], first["already_present"]) == (4, 1, 2)
    assert first["invalid_task_id"] == 1 and "/srv/private/model" not in rows.rows_path(batch_state).read_text()
    assert (again["added"], again["refreshed"]) == (0, 0)
    assert rows.read_rows(batch_state)[1]["malformed_lines"] == 1
    write = _row(payload, "gpt-6.1-sol", "write")
    assert write["tasks"] == 3 and write["low_sample"] is True
    assert write["status"]["done"] == 2 and write["status"]["no_deliverable"] == 1
    assert write["landed"] == 1 and write["landed_rate"] == {"value": 0.333, "n": 3, "low_sample": True}
    assert write["wall_seconds"] == {"p25": 600.0, "median": 1200.0, "p75": 1800.0, "p90": 1800.0,
                                     "total_hours": 1.0, "total_seconds": 3600.0, "n": 3}
    assert write["wall_hours_per_landed"]["value"] == 1.0
    assert write["tokens"] == {"fresh_input": 1800, "cached_input": 1200, "output": 300}
    assert write["output_tokens_per_landed"]["value"] == 300
    assert [(c["class"], c["tasks"], c["landed"]) for c in write["by_class"]] == [("code", 2, 1), ("unclassified", 1, 0)]
    token_use = payload["token_use"]
    assert (token_use["tasks"], token_use["reported_tasks"], token_use["merged_prs"]) == (6, 3, 1)
    assert token_use["totals"] == {"fresh_input": 1800, "cached_input": 1200, "output": 300}
    assert token_use["per_merged_pr"] == 3300
    assert snapshot["windows"]["24h"]["token_use"]["per_merged_pr"] is None
    assert token_use["by_work"]["implementation"]["tokens"] == 1100
    assert token_use["by_work"]["landing_helpers"] is None
    assert token_use["waste"]["failed_or_empty"]["share"] == 1 / 3
    assert token_use["waste"]["capped_and_replaced"] is token_use["avoidable_share"] is None
    assert token_use["cache_by_model"] == [
        {"model": "claude-opus-5-5", "tasks": 3, "reported_tasks": 0, "ratio": None},
        {"model": "gpt-6.1-sol", "tasks": 3, "reported_tasks": 3, "ratio": 0.4},
    ]
    assert [r["status"] for r in token_use["top_tasks"]] == ["no_deliverable", "done", "done"]
    ctx = fixture_context(tmp_path)
    monkeypatch.setattr(stats, "cached_snapshot", lambda _ctx: (snapshot, 1.0))
    registry = CollectorRegistry()
    registry.register(stats.ModelStatsCollector(ctx))
    for window in ("7d", "24h"):
        labels = {"window": window, "model": "gpt-6.1-sol", "work_type": "implementation"}
        assert registry.get_sample_value("lu_tokens_fresh_input", labels) == 600
        assert registry.get_sample_value("lu_tokens_cached_input", labels) == 400
        assert registry.get_sample_value("lu_tokens_output", labels) == 100
        assert registry.get_sample_value("lu_tokens_per_merged_pr", labels) == (1100 if window == "7d" else None)
    assert registry.get_sample_value("lu_token_data_age_seconds") == 1
    assert registry.get_sample_value("lu_tokens_waste", {
        "window": "7d", "model": "gpt-6.1-sol", "waste_class": "failed_or_empty",
    }) == 1100
    assert registry.get_sample_value("lu_tokens_waste", {
        "window": "7d", "model": "gpt-6.1-sol", "waste_class": "capped_and_replaced",
    }) is None
    landing.repositories["unavailable"] = 1
    snapshot = stats.collect(batch_state, tmp_path, NOW, landing=landing)
    partial = snapshot["page_windows"]["7d"]["token_use"]
    assert partial["totals"] == token_use["totals"]
    assert partial["merged_prs"] is partial["per_merged_pr"] is None
    assert partial["by_work"]["implementation"]["per_merged_pr"] is None
    labels = {"window": "7d", "model": "gpt-6.1-sol", "work_type": "implementation"}
    assert registry.get_sample_value("lu_tokens_per_merged_pr", labels) is None
    assert registry.get_sample_value("lu_tokens_fresh_input", labels) == 600

    review = _row(payload, "claude-opus-5-5", "review")
    assert review["tasks"] == 3 and "landed" not in review
    # r3's malformed reply is visible in the missing-verdict count, despite having no PR.
    assert review["verdicts"] == {"correct": 1, "incorrect": 1, "uncertain": 0, "unparsed": 1}
    assert review["unparsed_reasons"] == {
        "missing_result": 0, "too_big": 0, "bad_format": 0, "bad_fields": 1, "not_done": 0, "unknown": 0,
    }
    assert review["unparsed_samples"] == {"bad_fields": '{"overall": {"correctness": "incorrect"}, "findings": 1}'}
    assert review["blocking_per_review"]["value"] == 1.0 and review["p0_p2_per_review"]["value"] == 1.5
    assert review["by_class"][0]["class"] == "code/medium"

    (author,) = payload["authors"]
    assert author["model"] == "gpt-6.1-sol" and author["prs"] == 1
    assert author["rounds_per_pr"]["value"] == 2.0
    assert author["approved_eventually"]["value"] == 1.0
    assert author["first_round_approved"]["value"] == 0.0
    assert author["blocking_per_pr"]["value"] == 2.0 and author["p0_p2_per_pr"]["value"] == 3.0
    assert payload["records"] == 6
    assert _window(batch_state, stats.Landing(), "all")["records"] == 7

    # Split of unparsed reasons, the earliest escaped 200-char sample per model (model total only), archive
    # binding, the upgrade of a legacy row and fail-closed row validation. Sidecar outcomes: the table test below.
    u_state, u_tasks = _state(tmp_path / "unparsed_causes")
    # Conflicting unstamped result_file declaration must never attach another task's sidecar.
    (u_tasks / "other-secret.result").write_text("OTHER-TASK-REPLY-NEVER-SHOWN", encoding="utf-8")
    _record(u_tasks, "u-conflict-sidecar-review", mode="read-only", agent="claude", model="claude-opus-5-5",
            review_profile="code", started_at=_at(5.05), result_file=str(u_tasks / "other-secret.result"))
    # Later bad_format reply sits on a different effort and risk class: must not leak via by_effort or by_class.
    _record(u_tasks, "u-fmt-later-review", mode="read-only", agent="claude", model="claude-opus-5-5",
            effort="medium", review_profile="infra", review_risk="high", started_at=_at(2))
    (u_tasks / "u-fmt-later-review.result").write_text("later-effort-reply", encoding="utf-8")
    _record(u_tasks, "u-fmt-first-review", mode="read-only", agent="claude", model="claude-opus-5-5",
            effort="high", review_profile="code", review_risk="medium", started_at=_at(4))
    (u_tasks / "u-fmt-first-review.result").write_text(
        "first\n\t\x1b[31m\u202e<script>" + "B" * 240 + "TAIL-BEYOND-200", encoding="utf-8",
    )
    _record(u_tasks, "u-fields-review", mode="read-only", agent="claude", model="claude-opus-5-5",
            review_branch="pinned-recon", research_task_family="recon", started_at=_at(3), duration_s=11,
            usage={"input_tokens": 10, "cache_read_input_tokens": 4, "output_tokens": 3})
    (u_tasks / "u-fields-review.result").write_text(
        '{"overall": {"correctness": "maybe"}, "findings": []}', encoding="utf-8",
    )
    _record(u_tasks, "u-envelope-fields-review", mode="read-only", agent="claude", model="claude-opus-5-5",
            options={"review_profile": "code"}, started_at=_at(2.5))
    (u_tasks / "u-envelope-fields-review.result").write_text(
        'VERDICT: APPROVE\nSCORE: 9/10\n\n```json\n{"schema_version": "code-review-findings.v1", "overall": {}}\n```',
        encoding="utf-8",
    )
    # Valid JSON with a non-object root (bare or inside a valid envelope) classifies as bad_fields.
    assert rows.review_summary(b"[]")["unparsed_reason"] == "bad_fields"
    assert rows.review_summary(b'{"overall": {"correctness": "correct"}, "findings": [null]}')["unparsed_reason"] == (
        "bad_fields")
    assert rows.review_summary(b"VERDICT: APPROVE\nSCORE: 9/10\n\n```json\n[]\n```")["unparsed_reason"] == "bad_fields"
    # Stamped archive collision: renamed record pairs with its stamped sidecar, never an older unstamped basename.
    (u_tasks / "archive").mkdir()
    (u_tasks / "archive" / "u-stamped-review.result").write_text("WRONG-UNSTAMPED-ARCHIVE", encoding="utf-8")
    stamped_bytes = b"stamped archive reply"
    (u_tasks / "archive" / "u-stamped-review.20261115T050000Z.archived.result").write_bytes(stamped_bytes)
    (u_tasks / "archive" / "u-stamped-review.20261115T050000Z.archived.json").write_text(json.dumps({
        "schema_version": 2, "task_id": "u-stamped-review", "agent": "claude", "model": "claude-opus-5-5",
        "effort": "high", "mode": "read-only", "status": "done", "started_at": _at(4.5), "finished_at": _at(1),
        "review_profile": "code",
        "result_file": str(u_tasks / "u-stamped-review.result"),
        "result_sha256": hashlib.sha256(stamped_bytes).hexdigest(),
    }), encoding="utf-8")
    _record(u_tasks, "u-failed-review", mode="workspace-write", agent="claude", model="claude-opus-5-5",
            status="failed", started_at=_at(1), duration_s=5,
            usage={"input_tokens": 7, "cache_read_input_tokens": 1, "output_tokens": 2})
    # Seed a legacy pre-change unparsed row without unparsed_reason; reader marks it unknown, backfill upgrades it.
    legacy_path = u_tasks / "u-legacy-review.json"
    legacy_rec = _record(u_tasks, "u-legacy-review", mode="read-only", agent="claude", model="claude-opus-5-5",
                         review_profile="code", started_at=_at(1.5), duration_s=100)
    (u_tasks / "u-legacy-review.result").write_text("legacy prose reply", encoding="utf-8")
    legacy_row = rows.summary_row(legacy_path, legacy_rec, os.stat(legacy_path).st_mtime_ns)
    assert legacy_row is not None
    legacy_row["review"] = {"verdict": "unparsed", "priorities": None}
    rows.append_row(u_state, legacy_row)
    before_backfill = _row(_window(u_state, stats.Landing()), "claude-opus-5-5", "review")
    assert before_backfill["verdicts"]["unparsed"] == 1
    assert before_backfill["unparsed_reasons"]["unknown"] == 1 and before_backfill["unparsed_samples"] == {}

    bf1 = rows.backfill(u_tasks, u_state)
    assert (bf1["added"], bf1["refreshed"]) == (7, 1)
    assert rows.backfill(u_tasks, u_state)["refreshed"] == 0

    stored_u = {row["task_id"]: row for row in rows.read_rows(u_state)[0]}
    assert stored_u["u-legacy-review"]["review"] == {
        "verdict": "unparsed", "priorities": None,
        "unparsed_reason": "bad_format", "unparsed_sample": "legacy prose reply",
    }
    assert stored_u["u-stamped-review"]["review"]["unparsed_sample"] == "stamped archive reply"
    assert stored_u["u-conflict-sidecar-review"]["review"] == {
        "verdict": "unparsed", "priorities": None, "unparsed_reason": "missing_result", "unparsed_sample": None,
    }

    u_window = _window(u_state, stats.Landing())
    u_review = _row(u_window, "claude-opus-5-5", "review")
    assert u_review["tasks"] == 6 and u_review["verdicts"]["unparsed"] == 6
    assert u_review["review_evidence_absent"] == {}
    assert u_review["tokens"] == {"fresh_input": 0, "cached_input": 0, "output": 0}
    # The recon and the writer whose id merely contains "review" keep their tokens on the write row.
    assert stored_u["u-fields-review"]["role"] == "write" and stored_u["u-fields-review"]["class"] == "recon"
    assert stored_u["u-fields-review"]["review"] is None
    assert "review_evidence_absent" not in stored_u["u-fields-review"]
    assert stored_u["u-failed-review"]["role"] == "write" and stored_u["u-failed-review"]["review"] is None
    assert "review_evidence_absent" not in stored_u["u-failed-review"]
    u_write = _row(u_window, "claude-opus-5-5", "write")
    assert u_write["tasks"] == 2
    assert u_write["status"]["done"] == 1 and u_write["status"]["failed"] == 1
    assert u_write["tokens"] == {"fresh_input": 17, "cached_input": 5, "output": 5}
    totals = [0, 0, 0]
    for model in u_window["models"]:
        for index, key in enumerate(("fresh_input", "cached_input", "output")):
            totals[index] += model["tokens"][key]
    assert totals == [17, 5, 5]
    assert u_review["unparsed_reasons"] == {
        "missing_result": 1, "too_big": 0, "bad_format": 4, "bad_fields": 1, "not_done": 0, "unknown": 0,
    }
    assert set(u_review["unparsed_samples"]) == {"bad_format", "bad_fields"}
    # Samples are emitted only at model total, never on by_effort or by_class subgroups.
    assert u_review["unparsed_samples"]["bad_format"] == "stamped archive reply"
    assert all("unparsed_samples" not in item for item in (*u_review["by_effort"], *u_review["by_class"]))
    assert all("unparsed_samples" not in c for e in u_review["by_effort"] for c in e["by_class"])
    assert "later-effort-reply" not in json.dumps(u_window)
    assert "WRONG-UNSTAMPED-ARCHIVE" not in json.dumps(u_window)
    assert "OTHER-TASK-REPLY-NEVER-SHOWN" not in json.dumps(u_window)
    fmt_stored = stored_u["u-fmt-first-review"]["review"]["unparsed_sample"]
    assert fmt_stored.startswith(r"first\n\t\x1b[31m\u202e<script>B")
    assert "TAIL-BEYOND-200" not in fmt_stored and len(fmt_stored) <= rows.MAX_SAMPLE_CHARS
    assert u_review["unparsed_samples"]["bad_fields"].startswith(r"VERDICT: APPROVE\nSCORE: 9/10")
    page = stats.page_window(u_window)
    page_u = _row(page, "claude-opus-5-5", "review")
    assert page_u["unparsed_reasons"] == u_review["unparsed_reasons"]
    assert page_u["review_evidence_absent"] == u_review["review_evidence_absent"]
    assert "unparsed_samples" not in page_u
    assert "stamped archive reply" not in json.dumps(page)
    assert all("unparsed_samples" not in e for e in page_u["by_effort"])
    # Fail-closed cross-field validation rejects writer-impossible rows.
    base_row = stored_u["u-legacy-review"]
    bad_rows = [
        {**base_row, "role": "write"},
        {**base_row, "status": "failed"},
        {**base_row, "repo": "infra-private"},
        {**base_row, "review": {"verdict": "unparsed", "priorities": {"P0": 1}, "unparsed_reason": "bad_format", "unparsed_sample": "x"}},
        {**base_row, "review": {"verdict": "unparsed", "priorities": None, "unparsed_reason": "bogus", "unparsed_sample": None}},
        {**base_row, "review": {"verdict": "unparsed", "priorities": None, "unparsed_reason": "missing_result", "unparsed_sample": "x"}},
        {**base_row, "review": {"verdict": "unparsed", "priorities": None, "unparsed_reason": "bad_format", "unparsed_sample": "x" * 201}},
        {**base_row, "review": {"verdict": "unparsed", "priorities": None, "unparsed_reason": "bad_format", "unparsed_sample": "raw\nline"}},
        {**base_row, "review": {"verdict": "unparsed", "priorities": None, "unparsed_reason": "bad_format", "unparsed_sample": "bidi\u202e"}},
        {**base_row, "review": {"verdict": "unparsed", "priorities": None, "unparsed_reason": "bad_format", "unparsed_sample": "/home/ops/secret"}},
        {**base_row, "data_class": "private-code"},
        {**base_row, "data_class": ["private-code"]},
        {**base_row, "review": {"verdict": "correct", "priorities": {}, "unparsed_reason": "bad_format", "unparsed_sample": None}},
    ]
    assert all(not rows._well_formed(bad) for bad in bad_rows)


def test_each_sidecar_outcome_yields_one_review_and_never_carries_older_evidence(tmp_path, monkeypatch):
    """Defect class: retained review evidence outliving the current sidecar, so a verdict or reply sample read
    from an earlier sidecar survives a sidecar that is now absent, a link, a special file, unreadable or replaced."""
    batch_state, tasks = _state(tmp_path)
    monkeypatch.setattr(rows, "MAX_RESULT_BYTES", 128)
    monkeypatch.setattr(rows, "MAX_SAMPLE_SCAN_BYTES", 64)
    body, big, long_prose = b"prose reply, not JSON", b"BIG\n" + b"A" * 156, b"long prose " * 9
    gone = {"verdict": "unparsed", "priorities": None, "unparsed_reason": "missing_result", "unparsed_sample": None}

    def sampled(reason: str, sample: str | None) -> dict:
        return {"verdict": "unparsed", "priorities": None, "unparsed_reason": reason, "unparsed_sample": sample}

    def unreadable(path: Path) -> None:
        path.write_bytes(body)
        path.chmod(0)

    def link(path: Path) -> None:
        path.with_name(f"{path.name}.target").write_bytes(body)
        path.symlink_to(f"{path.name}.target")

    parsed = json.dumps({"overall": {"correctness": "incorrect"}, "findings": [{"id": "F1", "priority": "P1"}]})
    # outcome: (what replaces the first readable sidecar, record fields, the one review that outcome yields)
    table = {
        "absent": (lambda p: None, {}, gone),
        "absent-recorded-verdict": (lambda p: None, {"verdict": "CHANGES_REQUESTED"},
                                    {"verdict": "incorrect", "priorities": None}),
        "regular": (lambda p: p.write_bytes(b"another prose reply"), {}, sampled("bad_format", "another prose reply")),
        "regular-parsed": (lambda p: p.write_text(parsed), {}, {"verdict": "incorrect", "priorities": {"P1": 1}}),
        "symlink": (link, {}, gone),
        "directory": (lambda p: p.mkdir(), {}, gone),
        "socket": (lambda p: os.mknod(p, stat.S_IFSOCK | 0o600), {}, gone),
        "fifo": (os.mkfifo, {}, gone),
        "unreadable": (unreadable, {}, gone),
        "replaced-bytes": (lambda p: p.write_bytes(b"replacement"), {"result_sha256": hashlib.sha256(body).hexdigest()},
                           gone),
        "over-scan-budget": (lambda p: p.write_bytes(long_prose), {}, sampled("bad_format", None)),
        "too-big": (lambda p: p.write_bytes(big), {}, sampled("too_big", None)),
        "too-big-replaced": (lambda p: p.write_bytes(big), {"result_sha256": hashlib.sha256(body).hexdigest()},
                             sampled("too_big", None)),
    }
    for outcome, (_, fields, expected) in table.items():
        # An unparsed sidecar is a review only when the task is typed. A parsed reply is the evidence.
        typed = {} if expected.get("verdict") in ("correct", "incorrect", "uncertain") else {"review_profile": "code"}
        _record(tasks, f"{outcome}-review", mode="read-only", **typed, **fields)
        (tasks / f"{outcome}-review.result").write_bytes(body)
    rows.backfill(tasks, batch_state)
    earlier = {row["task_id"]: row["review"] for row in rows.read_rows(batch_state)[0]}
    assert earlier["absent-review"] == sampled("bad_format", "prose reply, not JSON")  # evidence that could be kept
    for outcome, (replace, _, _) in table.items():
        sidecar = tasks / f"{outcome}-review.result"
        sidecar.unlink()
        replace(sidecar)
    # The same record version, then a rewritten one: both show only what the current sidecar proves.
    rows.backfill(tasks, batch_state)
    same_version = {row["task_id"]: row["review"] for row in rows.read_rows(batch_state)[0]}
    for outcome in table:
        record = json.loads((tasks / f"{outcome}-review.json").read_text())
        store.replace({**record, "duration_s": 7}, tasks / f"{outcome}-review.json")
    rewritten = {row["task_id"]: row for row in rows.read_rows(batch_state)[0]}
    for outcome, (_, _, expected) in table.items():
        assert (outcome, same_version[f"{outcome}-review"]) == (outcome, expected)
        assert (outcome, rewritten[f"{outcome}-review"]["review"]) == (outcome, expected)
        assert rewritten[f"{outcome}-review"]["duration_s"] == 7.0
    assert rows.backfill(tasks, batch_state)["refreshed"] == 0


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def _git_fixture(tmp_path, monkeypatch) -> Path:
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "fixture")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "fixture@example.invalid")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main", "--template=")
    return repo


def test_landing_joins_only_exact_x_agent_trailer_lines(tmp_path, monkeypatch):
    """Defect class: false landing attribution from prose, quotes, code fences, suffixed values or another branch."""
    repo = _git_fixture(tmp_path, monkeypatch)
    messages = [
        # A squash merge appends its own paragraph after the trailer: the line still counts.
        "feat: real (#1)\n\nRefs #9\n\nX-Agent: codex/t-real\n\nCo-authored-by: A <a@example.invalid>\n",
        "docs: prose\n\nThe earlier run carried X-Agent: claude/t-prose and failed.\n",
        "docs: quoted\n\n> X-Agent: claude/t-quote\n",
        "fix: suffixed\n\nX-Agent: claude/t-suffix (retry of t-real)\n",
        "docs: example\n\n```text\nX-Agent: codex/t-fence\n```\n",
        "docs: unclosed example\n\n~~~\nX-Agent: claude/t-tilde\n",
        "docs: inline code is no fence\n\n```X-Agent``` names the task.\n\nX-Agent: codex/t-inline\n",
    ]
    for message in messages:
        _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "unmerged\n\nX-Agent: codex/t-branch\n")
    monkeypatch.setattr(stats, "load_fleet_repos", lambda: {"fixture": FleetRepo("fixture", "example/repo", "repo", "test")})
    monkeypatch.setattr(stats, "resolve_fleet_repo", lambda key, **_: (None, repo))

    landing = stats.scan_landing(tmp_path, datetime.now(UTC) - timedelta(days=1))

    assert landing.task_ids == {"t-real", "t-inline"}
    assert landing.repositories == {"read": 1} and landing.by_repository == {"fixture": 2}
    batch_state, tasks = _state(tmp_path)
    for task_id in ("t-real", "t-real-2", "t-prose", "t-quote", "t-suffix", "t-fence", "t-tilde", "t-inline",
                    "t-branch"):
        _record(tasks, task_id)
    rows.backfill(tasks, batch_state)
    write = _row(_window(batch_state, landing), "gpt-6.1-sol", "write")
    assert write["tasks"] == 9 and write["landed"] == 2


def test_route_rows_and_metrics_carry_no_prompt_body_path_or_account(tmp_path, monkeypatch):
    """Defect class: data leak of prompts, result bodies, host paths, errors, task ids or account identities."""
    batch_state, tasks = _state(tmp_path)
    secrets = ["PROMPT-TEXT-x1", "/home/leaky/worktree", "kk", "ERROR-DETAIL-x2", "EXPLANATION-x3",
               "finding body 0", "/srv/private/model", "../escape", "acct-identity@example.invalid",
               "PRIVATE-BRIEF-x5", "INVALID-REPO-x6", "UNKNOWN-CLASS-x7", "AFTER-200-DIFF-TEXT-x9",
               "STRADDLE-x8"]
    _record(tasks, "leaky", prompt="PROMPT-TEXT-x1", prompt_file="/home/leaky/worktree/prompt.md",
            worktree_path="/home/leaky/worktree", claude_account="kk", error="ERROR-DETAIL-x2",
            research_task_family="../escape", allowance={"pool": "claude:kk", "start": {"remaining_pct": 50},
                                                        "finish": {"remaining_pct": 40}})
    _record(tasks, "odd-model", model="/srv/private/model", codex_account="acct-identity@example.invalid")
    _review(tasks, "leaky-review", "correct", ["P1"], pr=3, review_author_model="gpt-6.1-sol",
            worktree_path="/home/leaky/worktree")
    result = tasks / "leaky-review.result"
    payload = json.loads(result.read_text())
    payload["overall"]["explanation"] = "EXPLANATION-x3"
    result.write_text(json.dumps(payload))
    # Private-repo, invalid-repo, unknown-data-class, and OPSEC-bearing replies suppress the sample (F1).
    _record(tasks, "unparsed-private-review", mode="read-only", agent="claude", model="claude-opus-5-5",
            review_profile="code", repo="infra-private", started_at=_at(5))
    (tasks / "unparsed-private-review.result").write_text("PRIVATE-BRIEF-x5 quoted line", encoding="utf-8")
    _record(tasks, "unparsed-invalid-repo-review", mode="read-only", agent="claude", model="claude-opus-5-5",
            review_profile="code", repo="/srv/private/repo", started_at=_at(4.5))
    (tasks / "unparsed-invalid-repo-review.result").write_text("INVALID-REPO-x6 quoted line", encoding="utf-8")
    _record(tasks, "unparsed-bad-class-review", mode="read-only", agent="claude", model="claude-opus-5-5",
            review_profile="code", repo="public", data_class="public-unadmitted", started_at=_at(4))
    (tasks / "unparsed-bad-class-review.result").write_text("UNKNOWN-CLASS-x7 quoted line", encoding="utf-8")
    _record(tasks, "unparsed-opsec-review", mode="read-only", agent="claude", model="claude-opus-5-5",
            review_profile="code", repo="public", started_at=_at(3))
    (tasks / "unparsed-opsec-review.result").write_text(
        "failed for acct-identity@example.invalid in /home/leaky/worktree", encoding="utf-8",
    )
    # An identity that starts before the 200-character cut and ends after it withholds the whole sample.
    _record(tasks, "unparsed-straddle-review", mode="read-only", agent="claude", model="claude-opus-5-5",
            review_profile="code", repo="public", started_at=_at(2.5))
    (tasks / "unparsed-straddle-review.result").write_text(
        "STRADDLE-x8 " + "s" * 178 + " acct-identity@example.invalid", encoding="utf-8",
    )
    _record(tasks, "unparsed-public-review", mode="read-only", agent="claude", model="claude-opus-5-5",
            review_profile="code", repo="public", data_class="public-code", started_at=_at(2))
    (tasks / "unparsed-public-review.result").write_text(
        "UNPARSED-PREFIX-x4 " + "z" * 220 + "AFTER-200-DIFF-TEXT-x9", encoding="utf-8",
    )
    rows.backfill(tasks, batch_state)
    snapshot = stats.collect(batch_state, tmp_path, NOW, landing=stats.Landing())
    ctx = SimpleNamespace(roots=SimpleNamespace(batch_state_dir=batch_state, live_repo_root=tmp_path))
    monkeypatch.setattr(stats, "cached_snapshot", lambda _ctx: (snapshot, 1.0))
    registry = CollectorRegistry()
    registry.register(stats.ModelStatsCollector(ctx))

    stored = rows.rows_path(batch_state).read_text()
    text = json.dumps(snapshot)
    metrics = generate_latest(registry).decode()

    assert [s for s in secrets if s in stored + text + metrics] == []
    assert "UNPARSED-PREFIX-x4" in text and "UNPARSED-PREFIX-x4" not in metrics
    assert "UNPARSED-PREFIX-x4" not in json.dumps(snapshot["metrics"])
    assert [t for t in ("leaky", "odd-model") if t in metrics] == []
    assert '"model": "unknown"' in text and '{effort="high",model="unknown",role="write",window="7d"} 1.0' in metrics
    assert snapshot["windows"]["7d"]["allowance"] == []  # an API-key label is no admitted pool


def test_pr_reviewed_for_two_author_models_exports_other_on_metrics(tmp_path, monkeypatch):
    """Defect class: an unbounded /metrics label value, from author ids joined after each one was bounded."""
    batch_state, tasks = _state(tmp_path)
    _review(tasks, "a-review", "incorrect", [], pr=5, review_author_model="gpt-6.1-sol", pinned_head="1" * 40)
    _review(tasks, "b-review", "correct", [], pr=5, review_author_model="claude-sonnet-5-5", pinned_head="2" * 40)
    rows.backfill(tasks, batch_state)
    snapshot = stats.collect(batch_state, tmp_path, NOW, landing=stats.Landing())
    ctx = SimpleNamespace(roots=SimpleNamespace(batch_state_dir=batch_state, live_repo_root=tmp_path))
    monkeypatch.setattr(stats, "cached_snapshot", lambda _ctx: (snapshot, 1.0))
    registry = CollectorRegistry()
    registry.register(stats.ModelStatsCollector(ctx))

    metrics = generate_latest(registry).decode()

    assert [a["model"] for a in snapshot["windows"]["7d"]["authors"]] == ["claude-sonnet-5-5+gpt-6.1-sol"]
    assert 'model="other",role="author",window="7d"} 2.0' in metrics
    assert "claude-sonnet-5-5+gpt-6.1-sol" not in metrics


def test_page_buckets_unknown_labels_before_grouping(tmp_path, monkeypatch):
    """Defect class: grammar-valid hostnames relayed as page labels or aliased into duplicate buckets."""
    batch_state, tasks = _state(tmp_path)
    model, effort, task_class = "worker.example.invalid", "effort.example.invalid", "class.example.invalid"
    author, reviewer, risk = "author.example.invalid", "reviewer.example.invalid", "risk.example.invalid"
    repo = "repository.example.invalid"
    for i in range(10):
        _record(tasks, f"w{i}", model=model, resolved_model=model, resolved_model_known=True, effort="medium",
                research_task_family=task_class, duration_s=3600)
    _record(tasks, "w10", model="second-worker.example.invalid", effort=effort,
            research_task_family="second-class.example.invalid", duration_s=3600)
    for i in range(10):
        _record(tasks, f"known{i}", research_task_family="bounded", duration_s=3600)
    _record(tasks, "cursor-auto", agent="cursor", model="auto")
    _record(tasks, "agy-unreported", agent="agy", resolved_model="unrecognized", resolved_model_known=True)
    _review(tasks, "r1-review", "correct", [], pr=1, repo=repo, review_author_model=author,
            model=reviewer, resolved_model=reviewer, effort="medium", review_risk=risk)
    _review(tasks, "r2-review", "incorrect", [], pr=2, repo="public", review_author_model="gpt-6.1-sol")
    _review(tasks, "unknown-effort-review", "incorrect", [], pr=3, repo="public", review_author_model="gpt-6.1-sol",
            effort=None, pinned_head="1" * 40, started_at=_at(4))
    _review(tasks, "later-known-review", "correct", [], pr=3, repo="public", review_author_model="gpt-6.1-sol",
            pinned_head="2" * 40, started_at=_at(3))
    rows.backfill(tasks, batch_state)
    landing = stats.Landing(task_ids={"w0", "w10", "known0"})
    landing.by_repository.update({"public": 1, repo: 2, "second-repository.example.invalid": 3})
    snapshot = stats.collect(batch_state, tmp_path, NOW, landing=landing)
    ctx = SimpleNamespace(roots=SimpleNamespace(project_root=tmp_path, batch_state_dir=batch_state,
                                                live_repo_root=tmp_path))
    monkeypatch.setattr(stats, "_compute", lambda _ctx: lambda: snapshot)
    app = FastAPI()
    app.include_router(stats.router, prefix="/api/fleet/model-stats")
    app.dependency_overrides[get_ctx] = lambda: ctx
    with TestClient(app) as client:
        full = client.get("/api/fleet/model-stats?view=full")
        page = client.get("/api/fleet/model-stats?view=page")

    assert full.status_code == page.status_code == 200
    assert all(label in full.text for label in (model, task_class, author, reviewer, risk, repo))
    assert effort not in full.text
    assert ".example.invalid" not in page.text
    payload = page.json()
    other = _row(payload, "other", "write")
    assert (other["tasks"], other["landed"], other["wall_seconds"]["total_seconds"]) == (10, 1, 36000)
    assert [(e["effort"], e["tasks"], e["landed"]) for e in other["by_effort"]] == [
        ("medium", 10, 1), ("unknown", 1, 1),
    ]
    assert _row(payload, "gpt-6.1-sol", "write")["tasks"] == 10
    assert _row(payload, "auto", "write")["identity"] == "harness_choice"
    assert _row(payload, "unrecognized", "write")["identity"] == "unreported"
    assert [(a["model"], a["prs"]) for a in payload["authors"]] == [("gpt-6.1-sol", 1), ("other", 1)]
    opus = _row(payload, "claude-opus-5-5", "review")
    assert (opus["tasks"], opus["approve_rate"]["n"]) == (2, 2)
    assert [(e["effort"], e["tasks"]) for e in opus["by_effort"]] == [("high", 2), ("unknown", 1)]
    assert {(c["author"], c["reviewer"], c["class"]) for c in payload["first_round_matrix"]} == {
        ("other", "other", "all"), ("other", "other", "other"),
        ("gpt-6.1-sol", "claude-opus-5-5", "all"), ("gpt-6.1-sol", "claude-opus-5-5", "code/medium"),
    }
    assert [(r["model"], [level["effort"] for level in r["levels"]])
            for r in payload["review_levels"] if r["model"] == "other"] == [("other", ["medium"])]
    assert [(job["class"], job["tasks"], job["models"][0]["model"], job["models"][0]["efforts"])
            for job in payload["charts"]["jobs"]] == [
        ("bounded", 10, "gpt-6.1-sol", ["high"]), ("other", 10, "other", ["medium"]),
    ]
    assert payload["coverage"]["landing"]["trailer_task_ids_by_repository"] == {"other": 5, "public": 1}
    # A joint author keeps its join on the page, each member bounded like a model label.
    assert stats._page_author(f"{author}+gpt-6.1-sol") == "gpt-6.1-sol+other"
    assert stats._page_author(f"{author}+{model}") == "other"


# Prose a failing reply can carry. It is not a label, a timestamp, a week or one of the page's fixed sentences.
_LEAK = "The reviewer wrote prose that is not a closed label."
_PAGE_SENTENCES = frozenset({
    "finished_at, else started_at, in [since, until)",
    "most first-round approvals per recorded wall hour, among catalog models "
    "with at least 10 landed tasks and 10 reviewed PRs",
    "not enough data yet",
    "Precision is inferred from files: a blocking finding counts as fixed when the author's "
    "later head changed its file before the same reviewer approved, else dismissed; a file "
    "changed for another reason (another finding, a merge of main) still counts as fixed, and "
    "a fix made in a different file counts as dismissed. Only verdicts the recorder published "
    "count; a blocking reply with no recorded publication (an unpublished run, or one from "
    "before the publication ledger and not imported) is of unknown publication and left out. "
    "Real precision uses the author's recorded label instead (fixed over fixed, wrong, "
    "invented and no live trigger); a label is the author's word unless a later review "
    "contested it.",
    "No planted-defect result is recorded in the summary rows.",
    "This snapshot does not include task ids.",
    "An allowance reading is the whole pool, not the one model. Overlapping tasks are charged "
    "that pool's net drop once. A cluster shared by more than one model or effort, or one that "
    "contains a reset or two readings at one timestamp that disagree, stays unknown. "
    "It is never reported as zero.",
    "There is no difficulty weight. Compare models inside one task class.",
    "Wall hours per landed stay blank when any task has no duration. The observed figure "
    "uses the recorded hours when at most one task, or one percent, is untimed.",
    "auto is Cursor Auto: the harness chose the model. A Cursor display name maps only "
    "where the shared cursor map admits it. A harness prefix resolves to its catalog id. "
    "Stored rows keep the old label until backfill.",
    "planted-defect results per reviewer: the summary rows record none. A review-eval run "
    "(lu_machinery/bakeoff/review_eval.py over docs/review-eval) writes per-seat recall to its "
    "own output directory, which this route does not read.",
})
_SCHEMA_KEY = re.compile(r"[a-z][a-z0-9_]*\Z")
_PAGE_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z")
_PAGE_WEEK = re.compile(r"\d{4}-W\d{2}\Z")


def _leaky_snapshot(tmp_path: Path) -> dict:
    batch_state, tasks = _state(tmp_path)
    _record(tasks, "write-one", duration_s=3600)
    _record(tasks, "odd-host", resolved_model="worker.example.invalid", resolved_model_known=True,
            effort="effort.example.invalid", research_task_family="class.example.invalid", duration_s=10)
    _review(tasks, "leaky-review", "correct", [], pr=1)
    (tasks / "leaky-review.result").write_text(_LEAK)
    rows.backfill(tasks, batch_state)
    return stats.collect(batch_state, tmp_path, NOW, landing=stats.Landing())


def _serve(snapshot: dict, tmp_path: Path, monkeypatch) -> TestClient:
    ctx = SimpleNamespace(roots=SimpleNamespace(project_root=tmp_path, batch_state_dir=tmp_path / "batch_state",
                                                live_repo_root=tmp_path))
    monkeypatch.setattr(stats, "_compute", lambda _ctx: lambda: snapshot)
    app = FastAPI()
    app.include_router(stats.router, prefix="/api/fleet/model-stats")
    app.dependency_overrides[get_ctx] = lambda: ctx
    return TestClient(app)


def _page_label_ok(text: str, labels: set[str], models: set[str]) -> bool:
    if text in labels or text in models or text in _PAGE_SENTENCES:
        return True
    if rows.VARIANT_SUFFIX.fullmatch(text):
        return True
    if "+" in text:
        return all(part in models for part in text.split("+"))
    return False


def _page_strings(node: object, path: str = "$"):
    if isinstance(node, dict):
        for key, value in node.items():
            yield f"{path}.{key}<key>", key
            yield from _page_strings(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _page_strings(value, f"{path}[{index}]")
    elif isinstance(node, str):
        yield path, node


def test_samples_view_returns_the_reply_text_view_page_omits(tmp_path, monkeypatch):
    """Defect class: view=page relaying reviewer reply text that view=samples is supposed to carry alone."""
    snapshot = _leaky_snapshot(tmp_path)
    with _serve(snapshot, tmp_path, monkeypatch) as client:
        page = client.get("/api/fleet/model-stats?window=7d&view=page")
        samples = client.get("/api/fleet/model-stats?window=7d&view=samples")
        full = client.get("/api/fleet/model-stats?window=7d&view=full")
        token_view = client.get("/api/fleet/model-stats?window=7d&view=tokens")
    assert page.status_code == samples.status_code == full.status_code == token_view.status_code == 200
    assert _LEAK not in page.text
    assert "unparsed_samples" not in page.text
    assert {"gemini-4-neon", "subq-savant-1.4"} <= set(page.json()["idle_catalog_models"])
    assert "savant" not in page.json()["charts"]["limits"]
    body = samples.json()
    assert body["status"] == "ok" and set(body) == {"schema_version", "status", "age_seconds", "window", "models"}
    found = [model for model in body["models"] if model["model"] == "claude-opus-5-5" and model["role"] == "review"]
    assert found == [{"model": "claude-opus-5-5", "role": "review",
                      "unparsed_samples": {"bad_format": _LEAK}, "unparsed_sample_efforts": {"bad_format": "high"}}]
    assert _LEAK in full.text
    assert _LEAK not in token_view.text and "leaky-review" not in token_view.text
    assert set(token_view.json()) == {"schema_version", "status", "age_seconds", "window", "token_use"}
    assert token_view.json()["token_use"] == page.json()["token_use"] == full.json()["token_use"]
    assert token_view.json()["token_use"]["totals"] is None
    review = _row(page.json(), "claude-opus-5-5", "review")
    assert review["unparsed_reasons"]["bad_format"] == 1


def test_page_view_string_leaves_stay_closed_for_every_window(tmp_path, monkeypatch):
    """Defect class: a free-text leaf on view=page, for any of the five windows, other than a closed label, a
    timestamp or week, or one of the page's fixed sentences."""
    from lu_machinery.orchestration.fleet_repos import FleetRepoError, load_fleet_repos

    snapshot = _leaky_snapshot(tmp_path)
    try:
        repo_keys = set(load_fleet_repos())
    except FleetRepoError:
        repo_keys = set()
    models = set(stats._known_models()) | set(stats._NOT_CONCRETE) | {stats.OTHER}
    labels = {
        *stats.WINDOWS, stats.SCHEMA_VERSION, "ok", *stats.STATUSES, "other", *rows.VERDICTS,
        *stats.UNPARSED_REASON_KEYS, *stats._PAGE_CLASSES, stats.ALL_CLASSES, rows.UNKNOWN_EFFORT,
        stats.ALL_EFFORTS, *rows.POOLS, *stats.FINDING_OUTCOMES, *stats.AFTER_BLOCK, *stats._RISK_ORDER,
        *stats.VALID_RISKS, *stats._SIZE_ORDER, *stats.START_REFUSAL_CODES, *{v for v in stats._NOT_CONCRETE.values()},
        "catalog", "joint", "not_in_catalog", "write", "review", *repo_keys, "read", "unavailable",
        "registry_unavailable",
    }
    assert not _page_label_ok(_LEAK, labels, models)
    assert not _page_label_ok("worker.example.invalid", labels, models)
    assert not _page_label_ok("file:///home/fixture/reply.txt", labels, models)
    assert not _page_label_ok("/home/fixture/reply.txt", labels, models)
    with _serve(snapshot, tmp_path, monkeypatch) as client:
        payloads = []
        for name in stats.WINDOWS:
            response = client.get(f"/api/fleet/model-stats?window={name}&view=page")
            assert response.status_code == 200, name
            assert _LEAK not in response.text and "example.invalid" not in response.text, name
            payloads.append((name, response.json()))
    offenders = []
    for name, payload in payloads:
        for path, text in _page_strings(payload):
            if path.endswith("<key>"):
                allowed = _SCHEMA_KEY.fullmatch(text) is not None or _page_label_ok(text, labels, models)
            else:
                allowed = (_page_label_ok(text, labels, models) or _PAGE_TIMESTAMP.fullmatch(text) is not None
                           or _PAGE_WEEK.fullmatch(text) is not None)
            if not allowed:
                offenders.append((name, path, text))
    assert not offenders, offenders[:8]


def test_untimed_landed_task_leaves_wall_hours_per_landed_unknown(tmp_path):
    """Defect class: a missing duration read as zero wall time, understating wall hours per landed task."""
    batch_state, tasks = _state(tmp_path)
    _record(tasks, "timed", duration_s=3600)
    _record(tasks, "untimed")
    rows.backfill(tasks, batch_state)

    write = _row(_window(batch_state, stats.Landing(task_ids={"timed", "untimed"})), "gpt-6.1-sol", "write")

    assert write["landed"] == 2 and write["wall_seconds"]["n"] == 1
    assert write["wall_hours_per_landed"] == {"value": None, "n": 2, "low_sample": True}


def test_missing_meter_reading_records_unknown_never_zero(tmp_path, monkeypatch):
    """Defect class: fabricated allowance (a missing reading recorded or aggregated as 0), telemetry that blocks
    launch or settlement on an unready Monitor, the wrong window recorded as the weekly allowance, overlapping
    tasks on one pool each charged the pool's whole drop, a nested reset reported as zero spend, a rise
    between two tasks' readings reported as measured spend, or a number taken from two readings that share
    a timestamp and disagree."""
    readings = iter([None, 35.0])
    monkeypatch.setattr(worker.model_pause, "weekly_used_pct_reading", lambda lane, *, account: next(readings))
    record = {"agent": "codex", "codex_account": "sub", "model": "gpt-6.1-sol"}

    record["allowance"] = worker._allowance(record, "start")
    record["allowance"] = worker._allowance(record, "finish")

    assert record["allowance"]["pool"] == "codex:a"
    assert record["allowance"]["start"] == "unknown"
    assert record["allowance"]["finish"]["remaining_pct"] == 65.0
    assert worker._allowance({"agent": "opencode", "model": "deepseek-v4.1-flash"}, "start") == {
        "pool": None, "meter": "weekly_remaining_pct", "start": "unknown", "finish": "unknown"}
    monkeypatch.undo()
    # A Monitor answering 503 gets one request: no readiness wait, no retry, no admission down memo.
    requests = []

    def unready(url, timeout):
        requests.append(url)
        raise urllib.error.HTTPError(url, 503, "starting", None, None)

    monkeypatch.setattr(model_pause.urllib.request, "urlopen", unready)
    memo = model_pause._monitor_wait_spent_at
    assert worker._allowance({"agent": "cursor"}, "finish")["finish"] == "unknown"
    assert len(requests) == 1 and model_pause._monitor_wait_spent_at == memo
    # AGY Claude records its weekly window, never the stricter five-hour one.
    windows = {"five_hour": {"used_pct": 90.0}, "weekly": {"used_pct": 20.0}}
    snapshot = {"agents": {"gemini": {"status": "ok", "codexbar": {"stale": False, "freshness": "fresh"},
                                      "claude_gpt_windows": windows}}}
    monkeypatch.setattr(model_pause.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(json.dumps(snapshot).encode()))
    agy = worker._allowance({"agent": "agy", "model": "claude-opus-5-5"}, "start")
    assert agy["pool"] == "agy_claude" and agy["start"]["remaining_pct"] == 80.0

    batch_state, tasks = _state(tmp_path)
    _record(tasks, "half", allowance=record["allowance"])
    _record(tasks, "both", allowance={"pool": "codex:a", "start": {"remaining_pct": 70.0},
                                      "finish": {"remaining_pct": 64.5}})
    rows.backfill(tasks, batch_state)
    stored = {row["task_id"]: row for row in rows.read_rows(batch_state)[0]}
    assert stored["half"]["allowance"]["start"] == "unknown"
    _record(tasks, "overlap-a", model="gemini-4-hydrogen", effort="medium",
            started_at=(NOW - timedelta(hours=3)).isoformat(),
            finished_at=(NOW - timedelta(hours=1)).isoformat(),
            allowance={"pool": "gemini", "start": {"remaining_pct": 80.0}, "finish": {"remaining_pct": 60.0}})
    _record(tasks, "overlap-b", model="gemini-4-hydrogen", effort="medium",
            started_at=(NOW - timedelta(hours=3) + timedelta(minutes=3)).isoformat(),
            finished_at=(NOW - timedelta(hours=2)).isoformat(),
            allowance={"pool": "gemini", "start": {"remaining_pct": 80.0}, "finish": {"remaining_pct": 70.0}})
    _record(tasks, "shared-a", model="grok-4.7",
            started_at=(NOW - timedelta(hours=8)).isoformat(),
            finished_at=(NOW - timedelta(hours=7)).isoformat(),
            allowance={"pool": "cursor", "start": {"remaining_pct": 100.0}, "finish": {"remaining_pct": 88.0}})
    _record(tasks, "shared-b", model="composer-2.5",
            started_at=(NOW - timedelta(hours=8) + timedelta(minutes=5)).isoformat(),
            finished_at=(NOW - timedelta(hours=6)).isoformat(),
            allowance={"pool": "cursor", "start": {"remaining_pct": 95.0}, "finish": {"remaining_pct": 82.0}})
    _record(tasks, "reset-a", model="gpt-6.2-sol",
            started_at=(NOW - timedelta(hours=8)).isoformat(),
            finished_at=(NOW - timedelta(hours=7)).isoformat(),
            allowance={"pool": "codex:b", "start": {"remaining_pct": 80.0}, "finish": {"remaining_pct": 75.0}})
    _record(tasks, "reset-b", model="gpt-6.2-sol",
            started_at=(NOW - timedelta(hours=8) + timedelta(minutes=5)).isoformat(),
            finished_at=(NOW - timedelta(hours=6)).isoformat(),
            allowance={"pool": "codex:b", "start": {"remaining_pct": 95.0}, "finish": {"remaining_pct": 70.0}})
    _record(tasks, "outer", model="claude-opus-5-5",
            started_at=(NOW - timedelta(hours=8)).isoformat(),
            finished_at=(NOW - timedelta(hours=6)).isoformat(),
            allowance={"pool": "agy_claude", "start": {"remaining_pct": 80.0}, "finish": {"remaining_pct": 75.0}})
    _record(tasks, "inner", model="claude-opus-5-5",
            started_at=(NOW - timedelta(hours=7)).isoformat(),
            finished_at=(NOW - timedelta(hours=6, minutes=30)).isoformat(),
            allowance={"pool": "agy_claude", "start": {"remaining_pct": 75.0}, "finish": {"remaining_pct": 95.0}})
    # The reset is a different effort from the task it runs inside. Dropping it used to report 0 points spent.
    _record(tasks, "cross-outer", model="claude-sonnet-5-5", effort="high",
            started_at=(NOW - timedelta(hours=8)).isoformat(),
            finished_at=(NOW - timedelta(hours=6)).isoformat(),
            allowance={"pool": "claude:a", "start": {"remaining_pct": 80.0}, "finish": {"remaining_pct": 75.0}})
    _record(tasks, "cross-inner", model="claude-sonnet-5-5", effort="medium",
            started_at=(NOW - timedelta(hours=7)).isoformat(),
            finished_at=(NOW - timedelta(hours=6, minutes=30)).isoformat(),
            allowance={"pool": "claude:a", "start": {"remaining_pct": 75.0}, "finish": {"remaining_pct": 95.0}})
    # The inner reading rises, but stays below the cluster's first reading. That is still a reset.
    _record(tasks, "buried-outer", model="claude-haiku-5-5",
            started_at=(NOW - timedelta(hours=8)).isoformat(),
            finished_at=(NOW - timedelta(hours=6)).isoformat(),
            allowance={"pool": "claude:b", "start": {"remaining_pct": 80.0}, "finish": {"remaining_pct": 70.0}})
    _record(tasks, "buried-inner", model="claude-haiku-5-5",
            started_at=(NOW - timedelta(hours=7)).isoformat(),
            finished_at=(NOW - timedelta(hours=6, minutes=30)).isoformat(),
            allowance={"pool": "claude:b", "start": {"remaining_pct": 40.0}, "finish": {"remaining_pct": 55.0}})
    # The pool falls, then a later reading from the outer task sits above the inner finish. Chronological
    # readings are 80, 40, 35, 70. That rise is a reset even though neither task's own finish rose.
    _record(tasks, "gap-outer", model="gemini-3.8-flash-high",
            started_at=(NOW - timedelta(hours=8)).isoformat(),
            finished_at=(NOW - timedelta(hours=6)).isoformat(),
            allowance={"pool": "gemini", "start": {"remaining_pct": 80.0}, "finish": {"remaining_pct": 70.0}})
    _record(tasks, "gap-inner", model="gemini-3.8-flash-high",
            started_at=(NOW - timedelta(hours=7)).isoformat(),
            finished_at=(NOW - timedelta(hours=6, minutes=30)).isoformat(),
            allowance={"pool": "gemini", "start": {"remaining_pct": 40.0}, "finish": {"remaining_pct": 35.0}})
    # Crossing tasks: 80, 70, 50, 65. The rise from 50 to 65 is a reset.
    _record(tasks, "cross-read-a", model="gpt-6.2-sol",
            started_at=(NOW - timedelta(hours=8)).isoformat(),
            finished_at=(NOW - timedelta(hours=7)).isoformat(),
            allowance={"pool": "codex:a", "start": {"remaining_pct": 80.0}, "finish": {"remaining_pct": 50.0}})
    _record(tasks, "cross-read-b", model="gpt-6.2-sol",
            started_at=(NOW - timedelta(hours=7, minutes=30)).isoformat(),
            finished_at=(NOW - timedelta(hours=6)).isoformat(),
            allowance={"pool": "codex:a", "start": {"remaining_pct": 70.0}, "finish": {"remaining_pct": 65.0}})
    # Same start timestamp, 80 and 95. Finish order does not say which start came first, so 15 points would
    # be a guess. The clock sits between the other codex:a clusters so this pair is its own row.
    _record(tasks, "tie-a", model="gpt-6.2-sol", effort="medium",
            started_at=(NOW - timedelta(hours=5)).isoformat(),
            finished_at=(NOW - timedelta(hours=4)).isoformat(),
            allowance={"pool": "codex:a", "start": {"remaining_pct": 80.0}, "finish": {"remaining_pct": 70.0}})
    _record(tasks, "tie-b", model="gpt-6.2-sol", effort="medium",
            started_at=(NOW - timedelta(hours=5)).isoformat(),
            finished_at=(NOW - timedelta(hours=3)).isoformat(),
            allowance={"pool": "codex:a", "start": {"remaining_pct": 95.0}, "finish": {"remaining_pct": 65.0}})
    rows.backfill(tasks, batch_state)
    landed = {"both", "half", "overlap-a", "overlap-b", "shared-a", "shared-b", "reset-a", "reset-b", "outer",
              "inner", "cross-outer", "cross-inner", "buried-outer", "buried-inner", "gap-outer", "gap-inner",
              "cross-read-a", "cross-read-b", "tie-a", "tie-b"}
    window = _window(batch_state, stats.Landing(task_ids=landed))
    unknown = {"value": None, "n": 1, "low_sample": True}
    assert window["allowance"] == [
        {"pool": "agy_claude", "model": "claude-opus-5-5", "effort": "high", "tasks": 2, "landed": 2,
         "reset_crossed": 1, "low_sample": True, "overlapped_tasks": 2, "pct_points_spent": None,
         "pct_points_per_landed": {"value": None, "n": 2, "low_sample": True}},
        {"pool": "claude:a", "model": "claude-sonnet-5-5", "effort": "high", "tasks": 1, "landed": 1,
         "reset_crossed": 0, "low_sample": True, "overlapped_tasks": 1, "pct_points_spent": None,
         "pct_points_per_landed": unknown},
        {"pool": "claude:a", "model": "claude-sonnet-5-5", "effort": "medium", "tasks": 1, "landed": 1,
         "reset_crossed": 1, "low_sample": True, "overlapped_tasks": 1, "pct_points_spent": None,
         "pct_points_per_landed": unknown},
        {"pool": "claude:b", "model": "claude-haiku-5-5", "effort": "high", "tasks": 2, "landed": 2,
         "reset_crossed": 1, "low_sample": True, "overlapped_tasks": 2, "pct_points_spent": None,
         "pct_points_per_landed": {"value": None, "n": 2, "low_sample": True}},
        {"pool": "codex:a", "model": "gpt-6.1-sol", "effort": "high", "tasks": 1, "landed": 1, "reset_crossed": 0,
         "low_sample": True, "overlapped_tasks": 0, "pct_points_spent": 5.5,
         "pct_points_per_landed": {"value": 5.5, "n": 1, "low_sample": True}},
        {"pool": "codex:a", "model": "gpt-6.2-sol", "effort": "high", "tasks": 2, "landed": 2, "reset_crossed": 0,
         "low_sample": True, "overlapped_tasks": 2, "pct_points_spent": None,
         "pct_points_per_landed": {"value": None, "n": 2, "low_sample": True}},
        {"pool": "codex:a", "model": "gpt-6.2-sol", "effort": "medium", "tasks": 2, "landed": 2, "reset_crossed": 0,
         "low_sample": True, "overlapped_tasks": 2, "pct_points_spent": None,
         "pct_points_per_landed": {"value": None, "n": 2, "low_sample": True}},
        {"pool": "codex:b", "model": "gpt-6.2-sol", "effort": "high", "tasks": 2, "landed": 2, "reset_crossed": 0,
         "low_sample": True, "overlapped_tasks": 2, "pct_points_spent": None,
         "pct_points_per_landed": {"value": None, "n": 2, "low_sample": True}},
        {"pool": "cursor", "model": "composer-2.5", "effort": "high", "tasks": 1, "landed": 1, "reset_crossed": 0,
         "low_sample": True, "overlapped_tasks": 1, "pct_points_spent": None,
         "pct_points_per_landed": unknown},
        {"pool": "cursor", "model": "grok-4.7", "effort": "high", "tasks": 1, "landed": 1, "reset_crossed": 0,
         "low_sample": True, "overlapped_tasks": 1, "pct_points_spent": None,
         "pct_points_per_landed": unknown},
        {"pool": "gemini", "model": "gemini-3.8-flash-high", "effort": "high", "tasks": 2, "landed": 2,
         "reset_crossed": 0, "low_sample": True, "overlapped_tasks": 2, "pct_points_spent": None,
         "pct_points_per_landed": {"value": None, "n": 2, "low_sample": True}},
        {"pool": "gemini", "model": "gemini-4-hydrogen", "effort": "medium", "tasks": 2, "landed": 2,
         "reset_crossed": 0, "low_sample": True, "overlapped_tasks": 2, "pct_points_spent": 20.0,
         "pct_points_per_landed": {"value": 10.0, "n": 2, "low_sample": True}},
    ]
    assert _row(window, "gpt-6.1-sol", "write")["allowance_pct_points_per_landed"]["value"] == 5.5
    # view=page is what the dashboard prints. None is a dash; 0.0 would be shown as measured spend.
    page = stats.page_window(window)
    medium = next(row for row in page["allowance"] if row["model"] == "claude-sonnet-5-5" and row["effort"] == "medium")
    assert medium["pct_points_spent"] is None and medium["tasks"] == 1 and medium["overlapped_tasks"] == 1
    sonnet = {item["effort"]: item for item in _row(window, "claude-sonnet-5-5", "write")["by_effort"]}
    assert sonnet["medium"]["allowance_pct_points_per_landed"]["value"] is None
    assert sonnet["high"]["allowance_pct_points_per_landed"]["value"] is None
    assert _row(window, "claude-haiku-5-5", "write")["allowance_pct_points_per_landed"] == {
        "value": None, "n": 2, "low_sample": True}


def test_model_version_and_effort_are_never_collapsed_or_defaulted(tmp_path, monkeypatch):
    """Defect class: effort or model version collapsed into a family, a default or another class's figures."""
    batch_state, tasks = _state(tmp_path)
    cases = {
        "grok": dict(agent="cursor", model="grok-4.7-high", effort=None),
        "flash-medium": dict(agent="agy", model="gemini-3.8-flash-medium", effort=None),
        "flash-id": dict(agent="agy", model="gemini-3.8-flash-high", effort=None),
        "bare-alias": dict(agent="claude", model="opus", effort=None),
        "observed": dict(agent="claude", model="opus", effort="xhigh", resolved_model="claude-opus-5-5",
                         resolved_model_known=True),
        "unobserved": dict(agent="claude", model="opus", effort="high", resolved_model="claude-sonnet-5-5",
                           resolved_model_known=False),
        "context": dict(agent="claude", model="claude-opus-5-5[1m]", effort="not-exposed"),
        "unknown-version": dict(agent="claude", model="claude-opus-5-6", effort="High"),
        "free-effort": dict(agent="codex", model="gpt-6.1-sol", effort="request-0", research_task_family="docs"),
        "cursor-display": dict(agent="cursor", model="Grok 4.7 256K High", resolved_model="Grok 4.7 256K High",
                               resolved_model_known=True, effort=None),
        "cursor-fast": dict(agent="cursor", model="Grok 4.7 256K High Fast",
                            resolved_model="Grok 4.7 256K High Fast", resolved_model_known=True, effort=None),
        "composer": dict(agent="cursor", model="Composer 2.5", resolved_model="Composer 2.5",
                         resolved_model_known=True, effort=None),
        "kimi-slash": dict(agent="kimi", model="kimi-code/k3-256k", effort=None),
        "kimi-alias": dict(agent="kimi", model="k3-256k", effort=None),
        "glm-alias": dict(agent="glm", model="zai-coding-plan/glm-5.3", effort="high"),
        "glm-legacy": dict(agent="glm", model="retired_glm", effort="high"),
        "deepseek-prefix": dict(agent="deepseek", model="deepseek-direct/deepseek-v4-flash", effort=None),
        "attested-over-auto": dict(agent="cursor", model="auto", resolved_model="Grok 4.7 256K High",
                                   resolved_model_known=True, effort=None),
        "attested-fast-over-request": dict(agent="cursor", model="grok-4.7",
                                           resolved_model="Grok 4.7 256K High Fast",
                                           resolved_model_known=True, effort=None),
        "attested-fast-over-suffixed-request": dict(agent="cursor", model="grok-4.7-high",
                                                    resolved_model="Grok 4.7 256K High Fast",
                                                    resolved_model_known=True, effort="high"),
        "attested-unrecognized-over-request": dict(agent="agy", model="gemini-4-neon-high",
                                                   resolved_model="unrecognized",
                                                   resolved_model_known=True, effort="high"),
        "attested-other-model-over-suffixed-request": dict(agent="agy", model="gemini-4-neon-high",
                                                           resolved_model="gemini-4-hydrogen",
                                                           resolved_model_known=True, effort="high"),
        "attested-bare-over-suffixed-request": dict(agent="agy", model="gemini-4-neon-high",
                                                    resolved_model="gemini-4-neon",
                                                    resolved_model_known=True, effort="high"),
        "attested-agy-alias-other-model": dict(agent="agy", model="gemini-4-neon",
                                               resolved_model="gemini-4-hydrogen",
                                               resolved_model_known=True, effort="high"),
        "attested-agy-alias-bare-options-effort": dict(agent="agy", model="gemini-4-neon",
                                                       resolved_model="gemini-4-neon",
                                                       resolved_model_known=True, effort=None,
                                                       options={"effort": "high"}),
        "attested-cursor-auto-with-effort": dict(agent="cursor", model="auto",
                                                 resolved_model="Composer 2.5",
                                                 resolved_model_known=True, effort="high"),
        "attested-missing-agent-with-effort": dict(agent=None, model="gemini-4-neon",
                                                   resolved_model="gemini-4-hydrogen",
                                                   resolved_model_known=True, effort="high"),
        "attested-malformed-agent-options-effort": dict(agent=123, model="gemini-4-neon",
                                                        resolved_model="gemini-4-hydrogen",
                                                        resolved_model_known=True, effort=None,
                                                        options={"effort": "high"}),
        "attested-claude-mismatched-executor": dict(agent="claude", model="sonnet",
                                                    resolved_model="claude-sonnet-4-6",
                                                    resolved_model_known=True, effort="high"),
        "attested-claude-alias-only": dict(agent="claude", model="sonnet",
                                           resolved_model="sonnet",
                                           resolved_model_known=True, effort="high"),
        "attested-runtime-variant-missing-agent": dict(agent=None, model="gemini-4-hydrogen",
                                                       resolved_model="gemini-4-neon-medium",
                                                       resolved_model_known=True, effort="high"),
        "attested-opencode-deepseek-with-effort": dict(agent="opencode", provider="openrouter",
                                                       model="deepseek-v4.1-flash",
                                                       resolved_model="deepseek-v4.1-flash",
                                                       resolved_model_known=True, effort="medium"),
        "attested-cursor-high-with-suffixed-request": dict(agent="cursor", model="grok-4.7-high",
                                                           resolved_model="Grok 4.7 256K High",
                                                           resolved_model_known=True, effort=None),
        "attested-kimi-over-request": dict(agent="kimi", model="k3", resolved_model="kimi-code/k3-256k",
                                           resolved_model_known=True, effort=None),
    }
    for task_id, fields in cases.items():
        _record(tasks, task_id, **fields)
    rows.backfill(tasks, batch_state)

    stored = {row["task_id"]: (row["model"], row["effort"]) for row in rows.read_rows(batch_state)[0]}
    window = _window(batch_state, stats.Landing())

    assert stored == {
        "grok": ("grok-4.7", "high"),
        "flash-medium": ("gemini-3.8-flash-high", "medium"),
        "flash-id": ("gemini-3.8-flash-high", "unset"),
        "bare-alias": ("opus", "unset"),
        "observed": ("claude-opus-5-5", "xhigh"),
        "unobserved": ("opus", "high"),
        "context": ("claude-opus-5-5", "not-exposed"),
        "unknown-version": ("claude-opus-5-6", "high"),
        "free-effort": ("gpt-6.1-sol", "request-0"),
        "cursor-display": ("grok-4.7", "unset"),
        "cursor-fast": ("unknown", "unset"),
        "composer": ("composer-2.5", "unset"),
        "kimi-slash": ("kimi-code/k3-256k", "unset"),
        "kimi-alias": ("kimi-code/k3-256k", "unset"),
        "glm-alias": ("glm-5.3", "high"),
        "glm-legacy": ("retired_glm", "high"),
        "deepseek-prefix": ("deepseek-v4-flash", "unset"),
        "attested-over-auto": ("grok-4.7", "unset"),
        "attested-fast-over-request": ("unknown", "unset"),
        "attested-fast-over-suffixed-request": ("unknown", "unset"),
        "attested-unrecognized-over-request": ("unrecognized", "unset"),
        "attested-other-model-over-suffixed-request": ("gemini-4-hydrogen", "unset"),
        "attested-bare-over-suffixed-request": ("gemini-4-neon", "unset"),
        "attested-agy-alias-other-model": ("gemini-4-hydrogen", "unset"),
        "attested-agy-alias-bare-options-effort": ("gemini-4-neon", "unset"),
        "attested-cursor-auto-with-effort": ("composer-2.5", "unset"),
        "attested-missing-agent-with-effort": ("gemini-4-hydrogen", "unset"),
        "attested-malformed-agent-options-effort": ("gemini-4-hydrogen", "unset"),
        "attested-claude-mismatched-executor": ("claude-sonnet-4.6-thinking", "unset"),
        "attested-claude-alias-only": ("sonnet", "unset"),
        "attested-runtime-variant-missing-agent": ("gemini-4-neon", "medium"),
        "attested-opencode-deepseek-with-effort": ("deepseek-v4.1-flash", "unset"),
        "attested-cursor-high-with-suffixed-request": ("grok-4.7", "high"),
        "attested-kimi-over-request": ("kimi-code/k3-256k", "unset"),
    }
    flash = _row(window, "gemini-3.8-flash-high", "write")
    assert (flash["effort"], flash["efforts"], flash["tasks"]) == ("all", ["medium"], 1)
    assert [(e["effort"], e["tasks"]) for e in flash["by_effort"]] == [("medium", 1), ("unknown", 1)]
    assert {(r["model"], e["effort"]) for r in window["models"] for e in r["by_effort"]} == {
        ("grok-4.7", "high"), ("grok-4.7", "unknown"), ("gemini-3.8-flash-high", "medium"),
        ("gemini-3.8-flash-high", "unknown"), ("opus", "unknown"), ("claude-opus-5-5", "xhigh"),
        ("opus", "high"), ("claude-opus-5-5", "unknown"), ("claude-opus-5-6", "high"),
        ("gpt-6.1-sol", "unknown"), ("unknown", "unknown"), ("unrecognized", "unknown"),
        ("gemini-4-hydrogen", "unknown"), ("gemini-4-neon", "medium"), ("gemini-4-neon", "unknown"),
        ("claude-sonnet-4.6-thinking", "unknown"), ("sonnet", "unknown"), ("composer-2.5", "unknown"),
        ("deepseek-v4-flash", "unknown"), ("deepseek-v4.1-flash", "unknown"),
    }
    for model_name in ("unrecognized", "gemini-4-hydrogen", "claude-sonnet-4.6-thinking",
                       "sonnet", "composer-2.5", "deepseek-v4.1-flash"):
        assert _row(window, model_name, "write")["tasks"] == 0
    neon = _row(window, "gemini-4-neon", "write")
    assert (neon["tasks"], neon["efforts"]) == (1, ["medium"])
    sol = _row(window, "gpt-6.1-sol", "write")
    assert sol["tasks"] == 0
    assert [(e["effort"], [c["class"] for c in e["by_class"]]) for e in sol["by_effort"]] == [("unknown", ["docs"])]
    # /metrics keeps finite label sets: values outside the catalog's known set export as "other".
    snapshot = stats.collect(batch_state, tmp_path, NOW, landing=stats.Landing())
    with _serve(snapshot, tmp_path, monkeypatch) as client:
        for view in ("full", "page", "samples", "tokens"):
            response = client.get(f"/api/fleet/model-stats?window=all&view={view}")
            assert response.status_code == 200
            assert not re.search(r"\b(?:glm|kimi)", response.text, re.I), view
    assert "24h" in snapshot["windows"] and "24h" not in snapshot["metrics"]["windows"]
    assert set(snapshot["metrics"]["token_windows"]) == {"24h", "7d"}
    metrics = snapshot["metrics"]["windows"]["all"]
    exported = {(r["model"], e["effort"]) for r in metrics["models"] for e in r["by_effort"]}
    assert ("gpt-6.1-sol", "unknown") in exported and ("other", "high") in exported
    assert not {"request-0", "claude-opus-5-6"} & {value for pair in exported for value in pair}
    page = stats.page_window(snapshot["windows"]["all"])
    dropped = ("by_class", "tokens", "fresh_input_tokens_per_landed", "output_tokens_per_landed",
               "unparsed_samples")
    assert all(key not in model and key not in effort
               for model in page["models"] for effort in model["by_effort"] for key in dropped)
    html = machinery_path("api/dashboards/model-stats.html").read_text()
    assert "view=page" in html and "view=samples" in html and "Best value needs more data" in html and "Low sample" in html
    assert "samples.window.until === pageWindow.until" in html
    assert "unparsed_reasons" in html and "unparsed_samples" in html
    assert "output_tokens_per_landed" not in html and "p0_p2 || 0" not in html
    assert all(token in html for token in ("pareto-class", "pareto-x", "trends-metric",
                                           "Not enough data", "pool_pace", "/subscriptions",
                                           "landed_ci"))
    assert "quota-alerts" not in html and "Cached snapshot fallback:" not in html
    assert all(token not in html for token in ("planning-blockers", "planning-ready", "planning-blocked"))
    assert "planning-pools" in html and 'href="/milestones"' in html
    # Ten short tasks must stay eligible, and the score is approvals over recorded hours.
    rank_root = tmp_path / "value"
    rank_tasks = rank_root / "tasks"
    rank_tasks.mkdir(parents=True)
    for index in range(10):
        _record(rank_tasks, f"short-{index}", model="gpt-6.2-sol", duration_s=3, research_task_family="code")
        _review(rank_tasks, f"short-review-{index}", "correct", [], pr=100 + index,
                review_author_model="gpt-6.2-sol")
        _record(rank_tasks, f"long-{index}", model="gpt-6.1-sol", duration_s=3600, research_task_family="code")
        _review(rank_tasks, f"long-review-{index}", "correct" if index < 5 else "incorrect", [], pr=200 + index,
                review_author_model="gpt-6.1-sol")
    # Per-class Pareto frontier: a faster model with lower landed share stays on the frontier beside the higher-rate
    # model, a dominated model does not, and a low-sample model never joins or dominates the frontier (#875).
    for index in range(10):
        _record(rank_tasks, f"fast-{index}", model="claude-sonnet-5-5", duration_s=600, research_task_family="code")
        _record(rank_tasks, f"slow-{index}", model="claude-opus-5-5", duration_s=7200, research_task_family="code")
    for index in range(4):
        _record(rank_tasks, f"tiny-{index}", model="grok-4.7", duration_s=1, research_task_family="code")
    for index in range(5):
        _record(rank_tasks, f"doc-a-{index}", model="gpt-6.2-sol", duration_s=300, research_task_family="docs")
        _record(rank_tasks, f"doc-b-{index}", model="grok-4.7", duration_s=300, research_task_family="docs")
    rows.backfill(rank_tasks, rank_root)
    landed_ids = (
        {f"short-{index}" for index in range(10)}
        | {f"long-{index}" for index in range(10)}
        | {f"fast-{index}" for index in range(8)}
        | {f"slow-{index}" for index in range(7)}
        | {f"tiny-{index}" for index in range(4)}
        | {f"doc-a-{index}" for index in range(5)}
    )
    ranked = stats.collect(rank_root, tmp_path, NOW, landing=stats.Landing(task_ids=landed_ids))
    charts = ranked["windows"]["7d"]["charts"]
    recommendation = charts["recommendation"]
    assert recommendation["model"] == "gpt-6.2-sol"
    assert recommendation["approvals"] == 10
    pareto_by_model = {p["model"]: p for p in charts["pareto"]}
    assert pareto_by_model["gpt-6.2-sol"]["hours_exact"] > 0
    speed_by_model = {p["model"]: p for p in charts["speed_quality"]}
    assert speed_by_model["gpt-6.2-sol"]["median_seconds"] == 3.0
    assert speed_by_model["gpt-6.2-sol"]["timed_n"] == 15
    assert speed_by_model["gpt-6.2-sol"]["low_sample"] is False
    assert speed_by_model["gpt-6.2-sol"]["frontier"] is True
    assert speed_by_model["gpt-6.1-sol"]["median_seconds"] == 3600.0
    assert speed_by_model["gpt-6.1-sol"]["timed_n"] == 10
    assert speed_by_model["gpt-6.1-sol"]["low_sample"] is False
    assert speed_by_model["gpt-6.1-sol"]["frontier"] is False
    durations_by_model = {d["model"]: d for d in charts["durations"]}
    assert (durations_by_model["claude-sonnet-5-5"]["tasks"], durations_by_model["claude-sonnet-5-5"]["landed"],
            durations_by_model["claude-sonnet-5-5"]["landed_rate"],
            durations_by_model["claude-sonnet-5-5"]["landed_ci"]) == (10, 8, 0.8, stats._wilson(8, 10))
    jobs_by_class = {j["class"]: {m["model"]: m for m in j["models"]} for j in charts["jobs"]}
    # In code: gpt-6.2-sol is 100% @ 3s (hours=0.0, hours_exact>0, frontier=True); gpt-6.1-sol is 100% @ 3600s
    # (dominated by gpt-6.2-sol); claude-sonnet-5-5 is 80% @ 750s (dominated); claude-opus-5-5 is 70% @ 10286s
    # (dominated); grok-4.7 is 100% @ 1s but n=4 (low_sample, never frontier).
    assert jobs_by_class["code"]["gpt-6.2-sol"]["hours"] == 0.0
    assert jobs_by_class["code"]["gpt-6.2-sol"]["hours_exact"] > 0
    assert jobs_by_class["code"]["gpt-6.2-sol"]["frontier"] is True
    assert jobs_by_class["code"]["gpt-6.1-sol"]["frontier"] is False
    assert jobs_by_class["code"]["claude-sonnet-5-5"]["frontier"] is False
    assert jobs_by_class["code"]["claude-opus-5-5"]["frontier"] is False
    assert jobs_by_class["code"]["grok-4.7"]["frontier"] is False
    # In docs: 10 total tasks, max n=5 per model, so no model reaches the frontier.
    assert all(m["frontier"] is False for m in jobs_by_class["docs"].values())
    # Two eligible non-dominated models in one class, plus: (1) unrounded sub-0.1s duration tie-breaking (F2),
    # (2) PR-only author admitted to weekly approval trends without write tasks (F4), and (3) speed_quality point
    # retained when wall_hours_observed is unavailable (>1% untimed writes, F5).
    split_root = tmp_path / "split_frontier"
    split_tasks = split_root / "tasks"
    split_tasks.mkdir(parents=True)
    for index in range(10):
        _record(split_tasks, f"hi-{index}", model="claude-opus-5-5", duration_s=3600, research_task_family="infra")
        _record(split_tasks, f"fast-{index}", model="grok-4.7", duration_s=600, research_task_family="infra")
        _record(split_tasks, f"dom-{index}", model="gpt-6.1-sol", duration_s=7200, research_task_family="infra")
        _record(split_tasks, f"tie-a-{index}", model="gpt-6.2-sol", duration_s=1.001, research_task_family="docs")
        _record(split_tasks, f"tie-b-{index}", model="claude-sonnet-5-5", duration_s=1.002, research_task_family="docs")
        _review(split_tasks, f"pr-only-{index}", "correct", [], pr=500 + index, review_author_model="claude-haiku-5-5")
        _review(split_tasks, f"pr-zero-{index}", "correct", [], pr=800 + index, review_author_model="gpt-6-luna")
    # Sub-cutoff counts on models qualifying in the other metric stay preserved, never zeroed out (F9).
    for index in range(3):
        _record(split_tasks, f"haiku-w-{index}", model="claude-haiku-5-5", duration_s=60, research_task_family="docs")
        _review(split_tasks, f"opus-pr-{index}", "correct", [], pr=700 + index, review_author_model="claude-opus-5-5")
    for index in range(20):
        _record(split_tasks, f"spd-t-{index}", model="gemini-4-hydrogen", duration_s=120, research_task_family="code")
    for index in range(2):
        _record(split_tasks, f"spd-u-{index}", model="gemini-4-hydrogen", research_task_family="code")
    for index in range(10):
        _review(split_tasks, f"spd-r-{index}", "correct", [], pr=600 + index, review_author_model="gemini-4-hydrogen")
        _record(split_tasks, f"zmed-w-{index}", model="gemini-4-neon",
                duration_s=0 if index < 6 else 1, research_task_family="code")
        _review(split_tasks, f"zmed-r-{index}", "correct", [], pr=900 + index, review_author_model="gemini-4-neon")
    rows.backfill(split_tasks, split_root)
    split_landed = (
        {f"hi-{index}" for index in range(9)}
        | {f"fast-{index}" for index in range(7)}
        | {f"dom-{index}" for index in range(6)}
        | {f"tie-a-{index}" for index in range(10)}
        | {f"tie-b-{index}" for index in range(10)}
        | {f"haiku-w-{index}" for index in range(2)}
        | {f"spd-t-{index}" for index in range(10)}
    )
    split_charts = stats.collect(split_root, tmp_path, NOW,
                                 landing=stats.Landing(task_ids=split_landed))["windows"]["7d"]["charts"]
    split_jobs = {j["class"]: {m["model"]: m for m in j["models"]} for j in split_charts["jobs"]}
    assert split_jobs["infra"]["claude-opus-5-5"]["frontier"] is True
    assert split_jobs["infra"]["grok-4.7"]["frontier"] is True
    assert split_jobs["infra"]["gpt-6.1-sol"]["frontier"] is False
    # Unrounded durations: 10.01s vs 10.02s both round to 10.0s, but only the faster model sits on the docs frontier.
    assert split_jobs["docs"]["gpt-6.2-sol"]["frontier"] is True
    assert split_jobs["docs"]["claude-sonnet-5-5"]["frontier"] is False
    assert split_jobs["docs"]["gpt-6.2-sol"]["hours_exact"] < split_jobs["docs"]["claude-sonnet-5-5"]["hours_exact"]
    # Weekly trends admit a model qualifying in either metric, including zero-write authors (F4, F11), and keep
    # sub-cutoff counts in the other metric (F9).
    haiku_trends = [t for t in split_charts["trends"] if t["model"] == "claude-haiku-5-5"]
    assert len(haiku_trends) == 1 and haiku_trends[0]["prs"] == 10 and haiku_trends[0]["first_approved"] == 10
    assert haiku_trends[0]["tasks"] == 3 and haiku_trends[0]["landed"] == 2
    luna_trends = [t for t in split_charts["trends"] if t["model"] == "gpt-6-luna"]
    assert len(luna_trends) == 1 and luna_trends[0]["prs"] == 10 and luna_trends[0]["first_approved"] == 10
    assert luna_trends[0]["tasks"] == 0 and luna_trends[0]["landed"] == 0
    opus_trends = [t for t in split_charts["trends"] if t["model"] == "claude-opus-5-5"]
    assert len(opus_trends) == 1 and opus_trends[0]["tasks"] == 10 and opus_trends[0]["prs"] == 3
    assert opus_trends[0]["first_approved"] == 3
    # Speed/quality keeps gemini-4-hydrogen (20 timed + 2 untimed writes -> no hours_observed, 10 PRs, F5)
    # and keeps finite zero medians (6x 0s + 4x 1s writes -> median 0.0, 10 PRs, F17).
    assert "gemini-4-hydrogen" not in {p["model"] for p in split_charts["pareto"]}
    split_speed = {p["model"]: p for p in split_charts["speed_quality"]}
    assert split_speed["gemini-4-hydrogen"]["median_seconds"] == 120.0
    assert split_speed["gemini-4-hydrogen"]["timed_n"] == 20
    assert split_speed["gemini-4-hydrogen"]["prs"] == 10
    assert split_speed["gemini-4-hydrogen"]["frontier"] is False
    assert split_speed["gemini-4-neon"]["median_seconds"] == 0.0
    assert split_speed["gemini-4-neon"]["timed_n"] == 10
    assert split_speed["gemini-4-neon"]["prs"] == 10
    assert split_speed["gemini-4-neon"]["frontier"] is True
    # Equal approval counts: the unrounded durations decide, not the one-decimal total or the model name.
    fine = tmp_path / "fine"
    fine_tasks = fine / "tasks"
    fine_tasks.mkdir(parents=True)
    for index in range(10):
        _record(fine_tasks, f"a-{index}", model="gpt-6.1-sol", duration_s=3.001)
        _review(fine_tasks, f"a-review-{index}", "correct", [], pr=300 + index, review_author_model="gpt-6.1-sol")
        _record(fine_tasks, f"b-{index}", model="gpt-6.2-sol", duration_s=3.004)
        _review(fine_tasks, f"b-review-{index}", "correct", [], pr=400 + index, review_author_model="gpt-6.2-sol")
    rows.backfill(fine_tasks, fine)
    fine_window = stats.collect(fine, tmp_path, NOW, landing=stats.Landing(
        task_ids={f"a-{index}" for index in range(10)} | {f"b-{index}" for index in range(10)}))
    assert fine_window["windows"]["7d"]["charts"]["recommendation"]["model"] == "gpt-6.1-sol"
    classes = []
    for index in range(9):
        classes.append({"role": "write", "model": "gpt-6.1-sol", "identity": "catalog", "tasks": 11, "landed": 11,
                        "landed_rate": {"value": 1.0, "n": 11, "low_sample": False}, "status": {"done": 11},
                        "wall_seconds": {"n": 0, "total_seconds": 0},
                        "by_class": [{"class": f"job-{index}", "tasks": 11, "landed": 11,
                                      "landed_rate": {"value": 1.0}}]})
    assert len(stats._charts(classes, [], [])["jobs"]) == 9


def _at(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).isoformat()


def test_supplemental_reviews_enter_reviewer_figures_without_changing_author_outcomes(tmp_path):
    """Defect class: supplemental reviews dropped from reviewer totals or attributed to the wrong PR round."""
    batch_state, tasks = _state(tmp_path)
    _review(tasks, "u-review", "incorrect", [], pr=9, review_author_model="gpt-6.1-sol", pinned_head="1" * 40,
            started_at=_at(3))
    (tasks / "u-review.result").write_text("Looks fine to me.")  # no verdict the review contract reads
    _review(tasks, "p-review", "correct", [], pr=9, review_author_model="gpt-6.1-sol", pinned_head="2" * 40)
    _record(tasks, "neon-initial-review", mode="read-only", agent="agy", model="gemini-4-neon-high",
            effort="not-exposed", resolved_model="gemini-4-neon-medium", resolved_model_known=True,
            options={"pinned_head": "1" * 40}, duration_s=120,
            allowance={"pool": "gemini", "start": {"remaining_pct": 98}, "finish": {"remaining_pct": 96}})
    _record(tasks, "neon-rate-limited-review", mode="read-only", agent="agy", model="gemini-4-neon-medium",
            options={"pinned_head": "1" * 40}, status="rate_limited", started_at=_at(4), duration_s=60)
    _record(tasks, "neon-later-review", mode="read-only", agent="agy", model="gemini-4-neon-high",
            options={"pinned_head": "2" * 40}, duration_s=180)
    (tasks / "neon-later-review.result").write_text("VERDICT: APPROVE\nSCORE: 9/10\n\nReviewed the complete diff.")
    _review(tasks, "neon-unknown-review", "correct", [], agent="agy", model="gemini-4-neon",
            resolved_model="gemini-4-neon", effort=None, duration_s=10000)
    _record(tasks, "grok-review", mode="read-only", agent="cursor", model="grok-4.7-high",
            status="failed")
    # Reviewer-effort costs use the PR's landing even when the writer effort is unknown.
    _record(tasks, "landed", effort=None, delivery_pr="example/repo#9", delivery_merge_sha="3" * 40)
    rows.backfill(tasks, batch_state)
    # A retained structured result can repair an existing summary even when its task record did not change.
    (tasks / "neon-initial-review.result").write_text(json.dumps({
        "schema_version": "code-review-findings.v1",
        "overall": {"correctness": "correct", "explanation": "The linked change passed its targeted checks.",
                    "confidence": 0.95},
        "findings": [{
            "id": "F1", "priority": "P3", "category": "docs", "confidence": 0.95,
            "title": "Name the denominator", "body": "The count label omits its denominator.",
            "location": {"path": "page.html", "start_line": 7, "end_line": 7, "claim_type": "present"},
            "verbatim": "Total", "why_wrong": "The label is ambiguous.",
            "smallest_fix": "Name the denominator in the label.", "sources": ["none"],
        }],
    }))
    assert rows.backfill(tasks, batch_state, dry_run=True)["would_refresh"] == 1
    assert rows.backfill(tasks, batch_state)["refreshed"] == 1
    assert rows.backfill(tasks, batch_state)["refreshed"] == 0

    window = _window(batch_state, stats.Landing())

    review = _row(window, "claude-opus-5-5", "review")
    assert review["tasks"] == 2
    assert review["verdicts"] == {"correct": 1, "incorrect": 0, "uncertain": 0, "unparsed": 1}
    assert review["unparsed_reasons"] == {
        "missing_result": 0, "too_big": 0, "bad_format": 1, "bad_fields": 0, "not_done": 0, "unknown": 0,
    }
    assert review["unparsed_samples"] == {"bad_format": "Looks fine to me."}
    assert review["approve_rate"] == {"value": 1.0, "n": 1, "low_sample": True}
    assert review["blocking_per_review"]["value"] == 0.0
    assert [model for model in window["models"] if model["model"] == "grok-4.7" and model["role"] == "review"] == []
    grok = _row(window, "grok-4.7", "write")
    assert grok["status"]["failed"] == grok["tasks"] == 1
    neon = _row(window, "gemini-4-neon", "review")
    assert (neon["approve_rate"]["n"], neon["findings"], neon["findings_n"]) == (2, 1, 1)
    levels = {level["effort"]: level for model in window["review_levels"] if model["model"] == "gemini-4-neon"
              for level in model["levels"]}
    assert (levels["high"]["results"], levels["low"]["results"]) == (1, 0)
    assert levels["high"]["first_round_approved"]["n"] == 0
    assert levels["high"]["findings"] is None
    assert levels["medium"] == {
        "effort": "medium", "tasks": 1, "results": 1, "findings": 1, "findings_n": 1, "first_round_missing": 0,
        "first_round_approved": {"value": 1.0, "n": 1, "low_sample": True}, "median_seconds": 120.0,
        "wall_minutes_per_landed": {"value": 2.0, "n": 1, "low_sample": True},
        "allowance_per_landed": [{"pool": "gemini", "value": 2.0, "n": 1, "low_sample": True}],
    }
    (author,) = window["authors"]
    assert (author["prs"], author["rounds_per_pr"]["value"], author["first_round_approved"]["value"]) == (1, 1.0, 1.0)
    assert author["after_blocked_first_round"]["blocked"] == 0
    neon_chart = window["charts"]["neon"]
    assert neon_chart["enough"] is False
    unknown_arm = neon_chart["unknown_effort"]
    assert (unknown_arm["effort"], unknown_arm["review_tasks"], unknown_arm["review_results"]) == ("unknown", 1, 1)
    arms = {arm["effort"]: arm for arm in neon_chart["arms"]}
    assert {effort: arm["review_results"] for effort, arm in arms.items()} == {"medium": 1, "high": 1}
    assert (arms["medium"]["review_tasks"], arms["medium"]["blocking_per_review"], arms["medium"]["p0_p2_per_review"],
            arms["medium"]["findings_n"], arms["medium"]["review_median_seconds"],
            arms["medium"]["review_p90_seconds"], arms["medium"]["review_timed_n"],
            arms["medium"]["write_median_seconds"], arms["medium"]["write_timed_n"]) == (
                1, 0.0, 0.0, 1, 120.0, 120.0, 1, 60.0, 1)
    assert (arms["high"]["review_tasks"], arms["high"]["blocking_per_review"], arms["high"]["p0_p2_per_review"],
            arms["high"]["findings_n"], arms["high"]["review_median_seconds"],
            arms["high"]["review_p90_seconds"], arms["high"]["review_timed_n"],
            arms["high"]["write_median_seconds"], arms["high"]["write_timed_n"]) == (
                1, None, None, 0, 180.0, 180.0, 1, None, 0)


def test_first_round_matrix_splits_author_from_reviewer_and_class(tmp_path):
    """Defect class: one first-round figure per author that mixes author, reviewer and risk; the first round taken
    in row order instead of by start; or a blocked PR's later outcome misread (new head, same head, none)."""
    batch_state, tasks = _state(tmp_path)
    sol, grok, sonnet = "gpt-6.1-sol", "grok-4.7", {"model": "claude-sonnet-5-5", "resolved_model": "claude-sonnet-5-5"}
    critical = {"review_profile": "infra", "review_risk": "critical"}
    heads = {name: name[-1] * 40 for name in ("h1", "h2", "h3", "h4", "h5", "h6")}
    # PR 1: Sol, blocked by Opus, then a new head approved.
    _review(tasks, "p1a-review", "incorrect", ["P1"], pr=1, review_author_model=sol, pinned_head=heads["h1"],
            started_at=_at(6), **critical)
    _review(tasks, "p1b-review", "correct", [], pr=1, review_author_model=sol, pinned_head=heads["h2"],
            started_at=_at(4), **critical)
    # PR 2: Sol, approved by Sonnet at once.
    _review(tasks, "p2-review", "correct", [], pr=2, review_author_model=sol, pinned_head=heads["h3"], **sonnet)
    # PR 3: Grok, approved by Opus first; a later Sonnet review of the same head (stored first) is no first round.
    _review(tasks, "a3-review", "incorrect", [], pr=3, review_author_model=grok, pinned_head=heads["h4"],
            started_at=_at(2), **sonnet, **critical)
    _review(tasks, "p3-review", "correct", [], pr=3, review_author_model=grok, pinned_head=heads["h4"],
            started_at=_at(5), **critical)
    # PR 4: Grok, blocked by Opus, then the same head approved; PR 5: Grok, blocked by Sonnet, nothing after.
    _review(tasks, "p4a-review", "incorrect", [], pr=4, review_author_model=grok, pinned_head=heads["h5"],
            started_at=_at(5), **critical)
    _review(tasks, "p4b-review", "correct", [], pr=4, review_author_model=grok, pinned_head=heads["h5"],
            started_at=_at(3), **sonnet, **critical)
    _review(tasks, "p5-review", "uncertain", [], pr=5, review_author_model=grok, pinned_head=heads["h6"], **sonnet)
    rows.backfill(tasks, batch_state)

    window = _window(batch_state, stats.Landing())

    cells = {(c["author"], c["reviewer"], c["class"]): (c["approved"], c["first_round_approved"]["n"])
             for c in window["first_round_matrix"]}
    assert cells == {
        (sol, "claude-opus-5-5", "all"): (0, 1), (sol, "claude-opus-5-5", "infra/critical"): (0, 1),
        (sol, "claude-sonnet-5-5", "all"): (1, 1), (sol, "claude-sonnet-5-5", "code/medium"): (1, 1),
        (grok, "claude-opus-5-5", "all"): (1, 2), (grok, "claude-opus-5-5", "infra/critical"): (1, 2),
        (grok, "claude-sonnet-5-5", "all"): (0, 1), (grok, "claude-sonnet-5-5", "code/medium"): (0, 1),
    }
    after = {a["model"]: a["after_blocked_first_round"] for a in window["authors"]}
    assert after[sol]["blocked"] == 1 and after[sol]["approved_on_new_head"]["value"] == 1.0
    assert after[grok]["blocked"] == 2
    assert [after[grok][k]["value"] for k in stats.AFTER_BLOCK] == [0.0, 0.5, 0.5]


def test_aliases_map_to_the_model_they_ran_only_when_unambiguous(tmp_path):
    """Defect class: a harness alias shown as a separate model, mapped to a version it may not have run, or a
    routed catalog model missing from the page because it ran nothing in the window."""
    batch_state, tasks = _state(tmp_path)
    _record(tasks, "opus-observed", agent="claude", model="claude-opus-5-5", finished_at=_at(3))
    _record(tasks, "opus-alias", agent="claude", model="opus", finished_at=_at(2))
    _record(tasks, "sonnet-alias-early", agent="claude", model="sonnet", finished_at=_at(5))
    _record(tasks, "sonnet-observed", agent="claude", model="claude-sonnet-5-5", finished_at=_at(3))
    _record(tasks, "cursor-auto", agent="cursor", model="auto")
    _record(tasks, "no-model", agent="cursor", model=None)
    _record(tasks, "hydrogen", agent="agy", model="gemini-4-hydrogen")
    _review(tasks, "alias-review", "correct", [], model="opus", resolved_model_known=False, pr=1,
            review_author_model="opus")
    rows.backfill(tasks, batch_state)

    window = _window(batch_state, stats.Landing())

    seen = {(r["model"], r["role"]): (r["identity"], r["tasks"]) for r in window["models"]}
    assert seen == {
        ("claude-opus-5-5", "write"): ("catalog", 2), ("claude-opus-5-5", "review"): ("catalog", 1),
        ("claude-sonnet-5-5", "write"): ("catalog", 1), ("sonnet", "write"): ("alias", 1),
        ("auto", "write"): ("harness_choice", 1), ("unknown", "write"): ("unreported", 1),
        ("gemini-4-hydrogen", "write"): ("catalog", 1),
    }
    assert [(a["model"], a["identity"]) for a in window["authors"]] == [("claude-opus-5-5", "catalog")]
    idle = window["idle_catalog_models"]
    assert idle and not {"gemini-4-hydrogen", "claude-opus-5-5"} & set(idle)
    assert all(stats.identity(model) == "catalog" for model in idle)


def test_cold_request_waits_for_the_collect_instead_of_a_poll(tmp_path, monkeypatch):
    """Defect class: a fresh Monitor answering pending so the page waits a fixed poll interval although the collect
    finishes sooner; a second collect for one cold start (a background refresh that runs after the cold request's
    collect stored its snapshot); a slow collect holding the request without bound; or a failed collect, even one
    that raises TimeoutError itself, answered as pending so the page keeps polling a failure."""
    release, background, calls, failures = threading.Event(), threading.Event(), [], []

    def fake_collect(*_args, **_kwargs):
        calls.append(1)
        if failures:
            raise failures.pop()
        if len(calls) == 2:
            release.wait(10)
        return {"schema_version": stats.SCHEMA_VERSION, "windows": {"7d": {"records": len(calls)}}, "metrics": {}}

    real_refresh = stats.cache_get_or_compute

    def late_refresh(*args, **kwargs):  # the background refresh thread runs only once the test lets it
        background.wait(10)
        return real_refresh(*args, **kwargs)

    def settled() -> None:
        deadline = time.monotonic() + 5
        while stats._refreshing and time.monotonic() < deadline:
            time.sleep(0.01)

    monkeypatch.setattr(stats, "collect", fake_collect)
    monkeypatch.setattr(stats, "cache_get_or_compute", late_refresh)
    ctx = SimpleNamespace(roots=SimpleNamespace(project_root=tmp_path, batch_state_dir=tmp_path,
                                                live_repo_root=tmp_path))
    app = FastAPI()
    app.include_router(stats.router, prefix="/model-stats")
    app.dependency_overrides[get_ctx] = lambda: ctx
    try:
        with TestClient(app) as client:
            assert stats.cached_snapshot(ctx) is None  # a /metrics scrape first: its refresh thread waits
            cold = client.get("/model-stats")
            assert cold.json()["status"] == "ok" and cold.json()["records"] == 1 and calls == [1]
            background.set()
            settled()
            assert calls == [1]  # the late background refresh found the stored snapshot fresh

            cache_invalidate(str(tmp_path.resolve()))
            monkeypatch.setattr(stats, "COLD_WAIT_SECONDS", 0.05)
            slow = client.get("/model-stats")
            assert slow.json()["status"] == "pending" and slow.headers["Retry-After"] == "1"
            release.set()
            deadline = time.monotonic() + 5
            while (body := client.get("/model-stats").json())["status"] != "ok" and time.monotonic() < deadline:
                time.sleep(0.02)
            assert body["records"] == 2 and calls == [1, 1]
            settled()

            cache_invalidate(str(tmp_path.resolve()))
            monkeypatch.setattr(stats, "COLD_WAIT_SECONDS", 5.0)
            failures.append(TimeoutError("the collect itself timed out"))
            failed = client.get("/model-stats")
            assert failed.status_code == 503 and "Retry-After" not in failed.headers
            assert failed.json()["status"] == "unavailable" and failed.json()["error"] == "TimeoutError"
            assert calls == [1, 1, 1]
            settled()
    finally:
        release.set()
        background.set()
        cache_invalidate(str(tmp_path.resolve()))


def test_pinned_astra_rows_counted_separately(tmp_path, monkeypatch):
    """Defect class: a pinned model's runs merged into, or dropped beside, the routed Sol rows.

    Astra 6.2 runs only on request (operator 2026-11-17); each pinned run, read-only security
    review or write, must stay its own model row next to Sol 6.2 and historical Sol 6.1 rows.
    """
    batch_state, tasks = _state(tmp_path)
    monkeypatch.setenv("LU_TASKS_DIR", str(tasks))
    _record(tasks, "astra-write", model="gpt-6.2-astra", duration_s=900)
    _record(tasks, "sol62-write", model="gpt-6.2-sol", duration_s=300)
    _record(tasks, "sol61-write", duration_s=600)
    _review(tasks, "astra-review", "correct", ["P2"], agent="codex", model="gpt-6.2-astra",
            resolved_model="gpt-6.2-astra", duration_s=400)
    added = rows.backfill(tasks, batch_state)["added"]
    payload = _window(batch_state, stats.Landing(task_ids={"astra-write"}))

    assert added == 4
    astra_write = _row(payload, "gpt-6.2-astra", "write")
    assert (astra_write["tasks"], astra_write["landed"]) == (1, 1)
    assert astra_write["wall_seconds"]["total_seconds"] == 900.0
    assert _row(payload, "gpt-6.2-astra", "review")["tasks"] == 1
    assert (_row(payload, "gpt-6.2-sol", "write")["tasks"], _row(payload, "gpt-6.2-sol", "write")["landed"]) == (1, 0)
    assert _row(payload, "gpt-6.1-sol", "write")["tasks"] == 1


def _findings_review(tasks: Path, task_id: str, correctness: str, findings: list[tuple[str, str]], **fields) -> None:
    """A review of record whose reply is a ``code-review-findings.v1`` object with (priority, path) findings."""
    _record(tasks, task_id, **{"mode": "read-only", "agent": "claude", "model": "opus",
                               "resolved_model": "claude-opus-5-5", "resolved_model_known": True,
                               "review_profile": "code", "review_risk": "medium", "repo": "machinery",
                               "review_author_model": "grok-4.7", **fields})
    (tasks / f"{task_id}.result").write_text(json.dumps({
        "schema_version": "code-review-findings.v1",
        "overall": {"correctness": correctness, "explanation": "Checked the change.", "confidence": 0.9},
        "findings": [{
            "id": f"F{index}", "priority": priority, "category": "bug", "confidence": 0.9,
            "title": "A defect", "body": "The change breaks a case.",
            "location": {"path": path, "start_line": 1, "end_line": 1, "claim_type": "present"},
            "verbatim": "x", "why_wrong": "It is wrong.", "smallest_fix": "Fix it.", "sources": ["none"],
        } for index, (priority, path) in enumerate(findings)],
    }))


def _publish(batch_state: Path, *published: tuple[str, int, str, str],
             repository: str = "learn-ukrainian/machinery") -> int:
    """Ledger lines, as the verdict recorder writes them, for (task id, PR, head, verdict) publications."""
    return rows.record_publications(batch_state, [rows.publication(
        task_id=task_id, repository=repository, pr=pr, head=head, verdict=verdict,
        published_at=NOW.isoformat()) for task_id, pr, head, verdict in published])


def test_finding_precision_follows_the_files_the_author_changed_before_the_same_reviewer_approved(
        tmp_path, monkeypatch):
    """Defect class: reviewer precision that counts a finding as real without the author changing its file, lets
    another reviewer's approval (or an unrecordable same-head approval, or one that finished first) settle it,
    counts a verdict that was never published (or settles a finding on an unpublished approval), trusts a
    publication from another repository, drops a review dispatched by branch alone, reads a path as a commit header,
    guesses an outcome for a head the checkout lacks, reports a planted-defect recall the rows cannot support, or
    states how many approved-later reviews had their files read from any rows but each reviewer's own.
    Every expected figure below is counted by hand."""
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "fixture")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "fixture@example.invalid")
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _git(repo, "init", "-q", "--template=")
    for name in ("a.py", "b.py", "c.py"):
        (repo / "src" / name).write_text("one\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "blocked head")
    h1 = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "src" / "a.py").write_text("two\n")
    _git(repo, "commit", "-q", "-am", "fix a.py only")
    h2 = _git(repo, "rev-parse", "HEAD").strip()
    missing = "e" * 40

    def resolve(key, **_):
        if key != "machinery":
            raise stats.FleetRepoError(f"no checkout for {key}")
        return None, repo

    monkeypatch.setattr(stats, "resolve_fleet_repo", resolve)
    batch_state, tasks = _state(tmp_path)
    sonnet = {"model": "claude-sonnet-5-5", "resolved_model": "claude-sonnet-5-5"}
    sol = {"agent": "codex", "model": "gpt-6.1-sol", "resolved_model": "gpt-6.1-sol"}
    blocked, approved = {"started_at": _at(7), "finished_at": _at(6)}, {"started_at": _at(5), "finished_at": _at(4)}
    # PR 1: two blocking findings; the next head changed a.py only, then Opus approved: 1 fixed, 1 dismissed.
    # The P2 finding is not blocking and never counts.
    _findings_review(tasks, "p1a-review", "incorrect", [("P1", "src/a.py"), ("P1", "src/b.py"), ("P2", "src/c.py")],
                     pr=1, pinned_head=h1, **blocked)
    _findings_review(tasks, "p1b-review", "correct", [], pr=1, pinned_head=h2, **approved)
    # PR 2: an approval of the blocked head itself can never be recorded, so the finding stays open.
    _findings_review(tasks, "p2a-review", "incorrect", [("P0", "src/a.py")], pr=2, pinned_head=h1, **blocked)
    _findings_review(tasks, "p2b-review", "correct", [], pr=2, pinned_head=h1, **approved)
    # PR 3: only another reviewer approved: Opus's finding stays open.
    _findings_review(tasks, "p3a-review", "incorrect", [("P1", "src/a.py")], pr=3, pinned_head=h1, **blocked)
    _findings_review(tasks, "p3b-review", "correct", [], pr=3, pinned_head=h2, **approved, **sonnet)
    # PR 4: the blocked head is not in the checkout: 1 unknown, never guessed.
    _findings_review(tasks, "p4a-review", "incorrect", [("P1", "src/a.py")], pr=4, pinned_head=missing, **blocked)
    _findings_review(tasks, "p4b-review", "correct", [], pr=4, pinned_head=h2, **approved)
    # PR 5: a Sol reply without finding locations: its two blocking findings are unknown.
    _review(tasks, "p5a-review", "incorrect", ["P1", "P1"], pr=5, review_author_model="grok-4.7",
            repo="machinery", pinned_head=h1, **blocked, **sol)
    _review(tasks, "p5b-review", "correct", [], pr=5, review_author_model="grok-4.7", repo="machinery",
            pinned_head=h2, **approved, **sol)
    # PR 6: the approval started after the block but finished before it: it settles nothing, the finding is open.
    _findings_review(tasks, "p6a-review", "incorrect", [("P1", "src/a.py")], pr=6, pinned_head=h1,
                     started_at=_at(7), finished_at=_at(2))
    _findings_review(tasks, "p6b-review", "correct", [], pr=6, pinned_head=h2, started_at=_at(5), finished_at=_at(3))
    # PR 7: two uncatalogued reviewers the page groups as "other": one's approval never settles the other's finding.
    _findings_review(tasks, "p7a-review", "incorrect", [("P1", "src/a.py")], pr=7, pinned_head=h1, **blocked,
                     model="pilot-model-a", resolved_model="pilot-model-a")
    _findings_review(tasks, "p7b-review", "correct", [], pr=7, pinned_head=h2, **approved,
                     model="pilot-model-b", resolved_model="pilot-model-b")
    # PR 8: a blocking review with only a P2 finding has no P0-P1 finding to settle and needs no head pair.
    _findings_review(tasks, "p8a-review", "incorrect", [("P2", "src/c.py")], pr=8, pinned_head=h2, **blocked)
    _findings_review(tasks, "p8b-review", "correct", [], pr=8, pinned_head=h1, **approved)
    # PR 9: a blocking reply whose verdict was never published: of unknown publication, none of its findings count.
    _findings_review(tasks, "p9a-review", "incorrect", [("P1", "src/a.py")], pr=9, pinned_head=h1, **blocked)
    _findings_review(tasks, "p9b-review", "correct", [], pr=9, pinned_head=h2, **approved)
    # PR 10: the ledger names the task at another head than the row's: still of unknown publication.
    _findings_review(tasks, "p10a-review", "incorrect", [("P1", "src/a.py")], pr=10, pinned_head=h1, **blocked)
    # PR 11: the same reviewer approved a new head, but that approval was never published: the finding stays open.
    _findings_review(tasks, "p11a-review", "incorrect", [("P1", "src/b.py")], pr=11, pinned_head=h1, **blocked)
    _findings_review(tasks, "p11b-review", "correct", [], pr=11, pinned_head=h2, **approved)
    # PR 12: reviews dispatched by branch alone keep no PR in their rows; the ledger names it: 1 fixed.
    _findings_review(tasks, "p12a-review", "incorrect", [("P1", "src/a.py")], pinned_head=h1, **blocked)
    _findings_review(tasks, "p12b-review", "correct", [], pinned_head=h2, **approved)
    # PR 13: the ledger names the task in another repository than the row's: of unknown publication.
    _findings_review(tasks, "p13a-review", "incorrect", [("P1", "src/a.py")], pr=13, pinned_head=h1, **blocked)
    # A rejection dispatched by branch alone with no publication: of unknown publication too.
    _findings_review(tasks, "p14a-review", "incorrect", [("P1", "src/a.py")], pinned_head=h1, **blocked)
    # PR 15: a published blocking review with unknown effort stays out of reviewer accuracy and head_pairs.
    _findings_review(tasks, "p15a-review", "incorrect", [("P1", "src/a.py")], pr=15, pinned_head=h2,
                     effort=None, **blocked)
    _findings_review(tasks, "p15b-review", "correct", [], pr=15, pinned_head=h1, **approved)
    rows.backfill(tasks, batch_state)
    assert "src/a.py" not in rows.rows_path(batch_state).read_text()
    blocked_by, approved_by = "CHANGES_REQUESTED", "APPROVED"
    published = [
        ("p1a-review", 1, h1, blocked_by), ("p1b-review", 1, h2, approved_by),
        ("p2a-review", 2, h1, blocked_by), ("p2b-review", 2, h1, approved_by),
        ("p3a-review", 3, h1, blocked_by), ("p3b-review", 3, h2, approved_by),
        ("p4a-review", 4, missing, blocked_by), ("p4b-review", 4, h2, approved_by),
        ("p5a-review", 5, h1, blocked_by), ("p5b-review", 5, h2, approved_by),
        ("p6a-review", 6, h1, blocked_by), ("p6b-review", 6, h2, approved_by),
        ("p7a-review", 7, h1, blocked_by), ("p7b-review", 7, h2, approved_by),
        ("p8a-review", 8, h2, blocked_by), ("p8b-review", 8, h1, approved_by),
        ("p9b-review", 9, h2, approved_by), ("p10a-review", 10, h2, blocked_by), ("p11a-review", 11, h1, blocked_by),
    ]
    published += [("p12a-review", 12, h1, blocked_by), ("p12b-review", 12, h2, approved_by),
                  ("p15a-review", 15, h2, blocked_by), ("p15b-review", 15, h1, approved_by)]
    assert _publish(batch_state, *published) == 23
    assert _publish(batch_state, ("p13a-review", 13, h1, blocked_by),
                    repository="learn-ukrainian/learn-ukrainian.github.io") == 1
    # The ledger holds each publication once: a recorder rerun adds nothing.
    assert _publish(batch_state, published[0]) == 0

    snapshot = stats.collect(batch_state, tmp_path, NOW, landing=stats.Landing())

    # Distinct (repository, blocked, approved) heads with finding paths: PR 1 and PR 7 share h1 to h2 (PR 5 keeps no
    # paths, so it needs no pair); PR 4's blocked head is missing; PR 15 has unknown effort and adds no pair.
    assert snapshot["coverage"]["finding_heads"] == {"pairs": 2, "read": 1}
    assert snapshot["coverage"]["publications"] == {"lines": 24, "tasks": 24}
    opus = {
        "model": "claude-opus-5-5", "identity": "catalog", "efforts": ["high"],
        "blocking_reviews": 8, "reviews_unknown_publication": 4,
        "reviews_without_paths": 0, "blocking_findings": 8, "fixed": 2, "dismissed": 1, "open": 4, "unknown": 1,
        # PRs 1, 4 and 12 were approved later by Opus at another head; PR 4's blocked head could not be read.
        "reviews_approved_later": 3, "reviews_approved_later_read": 2,
        "precision": stats._rate(2, 3), "precision_ci": stats._wilson(2, 3),
    }
    assert opus["precision"]["low_sample"] and opus["precision"]["n"] == 3
    pilot = {"blocking_reviews": 1, "blocking_findings": 1, "open": 1, "fixed": 0, "dismissed": 0, "unknown": 0,
             "reviews_approved_later": 0, "reviews_approved_later_read": 0}
    for payload, pilot_label in ((snapshot["windows"]["7d"], "pilot-model-a"),
                                 (snapshot["page_windows"]["7d"], "other")):
        accuracy = payload["reviewer_accuracy"]
        found = {row["model"]: row for row in accuracy["findings"]}
        assert set(found) == {"claude-opus-5-5", "gpt-6.1-sol", pilot_label}
        assert found["claude-opus-5-5"] == opus
        assert {key: found[pilot_label][key] for key in pilot} == pilot
        sol_row = found["gpt-6.1-sol"]
        assert (sol_row["blocking_reviews"], sol_row["reviews_without_paths"]) == (1, 1)
        assert (sol_row["blocking_findings"], sol_row["unknown"], sol_row["precision"]["value"]) == (2, 2, None)
        assert (sol_row["reviews_approved_later"], sol_row["reviews_approved_later_read"]) == (0, 0)
        recall = accuracy["planted_defect_recall"]
        assert recall["value"] is None and recall["status"] == "unknown"
        assert "review_eval" in recall["missing_input"]

    # A changed file named like the next pair's header is still a path: every path follows a raw record.
    record = b":100644 100644 " + b"1" * 40 + b" " + b"2" * 40 + b" M"
    output = b"\0".join([b"a" * 40, record, b"b" * 40, b"b" * 40, record, b"real.py", b""])
    assert stats._diff_blocks(output, ["a" * 40, "b" * 40]) == [["b" * 40], ["real.py"]]
    assert stats._diff_blocks(output, ["a" * 40]) is None


_HEAD_A = "a" * 40
_HEAD_B = "b" * 40
_START_A = _at(2)
_START_B = _at(1.5)


def _label(batch_state: Path, task_id: str, pr: int, finding_id: str, label: str, evidence: str | None = None,
           *, head: str = _HEAD_A, started_at: str = _START_A, reviewer_model: str = "claude-opus-5-5",
           repository: str = "learn-ukrainian/machinery", recorded_at: str = _at(1)) -> int:
    """An author label line, as the verdict recorder's finding-outcome verb writes it."""
    return rows.record_finding_outcomes(batch_state, [rows.finding_outcome(
        kind="label", task_id=task_id, repository=repository, pr=pr, finding_id=finding_id, head=head,
        started_at=started_at, reviewer_model=reviewer_model, label=label, evidence=evidence,
        recorded_by="author-task", recorded_at=recorded_at)])


def _contest(batch_state: Path, task_id: str, pr: int, finding_id: str, contested_by: str,
             *, head: str = _HEAD_A, started_at: str = _START_A, repository: str = "learn-ukrainian/machinery",
             recorded_at: str = _at(0.5)) -> int:
    """A contest line, as the verdict recorder writes it when a later review contests a label."""
    return rows.record_finding_outcomes(batch_state, [rows.finding_outcome(
        kind="contest", task_id=task_id, repository=repository, pr=pr, finding_id=finding_id, head=head,
        started_at=started_at, contested_by=contested_by, recorded_at=recorded_at)])


def test_finding_outcome_labels_count_per_reviewer_and_a_contest_holds_the_label_out(tmp_path):
    """Defect class: label stats depending on a rows or publications join, a contested author claim counted as
    settled, a contest from another run of a reused task id (different head or start) or an unbound legacy contest
    suppressing an earlier run's label, a future contest or label leaking into an earlier snapshot, a contest on
    another PR or repository holding out a finding, a contest without a label counted, a legacy line without
    reviewer_model or run binding rejected as malformed or misattributed, a retired model kept, or a non-catalog
    model leaking onto the page view. Every expected figure below is counted by hand."""
    batch_state, _ = _state(tmp_path)
    # No summary rows and no publications: label stats read only finding-outcomes.jsonl by stored reviewer_model.
    # Opus: fixed, wrong, invented (contested before NOW), out_of_scope, duplicate; plus one label contested only
    # after NOW (still counts as fixed at NOW) and one label recorded at NOW (outside [since, NOW)).
    assert _label(batch_state, "o1-review", 1, "F0", "fixed") == 1
    assert _label(batch_state, "o1-review", 1, "F1", "wrong", "the guard on line 3 already returns early") == 1
    assert _label(batch_state, "o1-review", 1, "F2", "invented", "src/c.py has no such function") == 1
    assert _contest(batch_state, "o1-review", 1, "F2", "o1b-review") == 1
    assert _label(batch_state, "o2-review", 2, "F0", "out_of_scope", "outside the frozen obligation") == 1
    assert _label(batch_state, "o2-review", 2, "F1", "duplicate", "same defect as F0") == 1
    assert _label(batch_state, "o2-review", 2, "F2", "fixed") == 1
    assert _contest(batch_state, "o2-review", 2, "F2", "o2b-review",
                    recorded_at=(NOW + timedelta(hours=1)).isoformat()) == 1
    assert _label(batch_state, "o2-review", 2, "F3", "fixed", recorded_at=NOW.isoformat()) == 1
    # Contests on another PR, another repository, another run (different head or start of the same task id on the same
    # PR), or an unlabelled finding never affect PR 1's labels.
    assert _contest(batch_state, "o1-review", 9, "F0", "o9-review") == 1
    assert _contest(batch_state, "o1-review", 1, "F1", "o9-review", repository="other/repo") == 1
    assert _contest(batch_state, "o1-review", 1, "F0", "o1c-review", head=_HEAD_B, started_at=_START_B) == 1
    assert _contest(batch_state, "o1-review", 1, "F0", "o1d-review", head=_HEAD_A, started_at=_START_B) == 1
    assert _contest(batch_state, "o2-review", 2, "F9", "o2b-review") == 1
    # Sol: one wrong inside 7d, one fixed 10 days ago (inside 30d/all, outside 7d).
    assert _label(batch_state, "s1-review", 3, "F0", "wrong", "already handled", reviewer_model="gpt-6.2-sol") == 1
    assert _label(batch_state, "s2-review", 4, "F0", "fixed", reviewer_model="gpt-6.2-sol",
                  recorded_at=(NOW - timedelta(days=10)).isoformat()) == 1
    # Effort-suffixed Grok selector normalizes to catalog id grok-4.7; retired Kimi is excluded; an uncatalogued
    # spelling stays concrete in full windows and bounds to "other" on the page view.
    assert _label(batch_state, "g1-review", 5, "F0", "invented", "no such symbol", reviewer_model="grok-4.7-high") == 1
    assert _label(batch_state, "k1-review", 6, "F0", "fixed", reviewer_model="kimi-k3") == 1
    assert _label(batch_state, "u1-review", 7, "F0", "fixed", reviewer_model="custom-reviewer") == 1
    # Legacy review-finding-outcome.v1 lines without reviewer_model/head/started_at are not malformed, count in
    # unattributed_labels / unbound_contests, and never suppress bound labels.
    legacy_label = {
        "schema": rows.OUTCOME_SCHEMA, "kind": "label", "task_id": "legacy-review",
        "repository": "agentic-machinery/machinery", "pr": 8, "finding_id": "F1", "label": "fixed",
        "evidence_sha256": None, "recorded_by": "author-task", "recorded_at": _at(1),
    }
    legacy_contest = {
        "schema": rows.OUTCOME_SCHEMA, "kind": "contest", "task_id": "o1-review",
        "repository": "agentic-machinery/machinery", "pr": 1, "finding_id": "F0",
        "contested_by": "legacy-r2", "recorded_at": _at(0.5),
    }
    with (batch_state / rows.ROWS_DIR / rows.OUTCOMES_FILE).open("ab") as stream:
        stream.write(json.dumps(legacy_label).encode() + b"\n" + json.dumps(legacy_contest).encode() + b"\n")

    snapshot = stats.collect(batch_state, tmp_path, NOW, landing=stats.Landing())
    assert snapshot["coverage"]["finding_outcomes"] == {"lines": 21, "unattributed_labels": 1, "unbound_contests": 1}
    zero = dict.fromkeys(("fixed", "wrong", "invented", "no_live_trigger", "out_of_scope", "duplicate", "contested"), 0)
    full_7d = {row["model"]: row for row in snapshot["windows"]["7d"]["reviewer_accuracy"]["labels"]}
    assert set(full_7d) == {"claude-opus-5-5", "gpt-6.2-sol", "grok-4.7", "custom-reviewer"}
    assert full_7d["claude-opus-5-5"]["labels"] == {
        **zero, "fixed": 2, "wrong": 1, "out_of_scope": 1, "duplicate": 1, "contested": 1,
    }
    assert full_7d["claude-opus-5-5"]["real_precision"] == stats._rate(2, 3)
    assert full_7d["gpt-6.2-sol"]["labels"] == {**zero, "wrong": 1}
    assert full_7d["gpt-6.2-sol"]["real_precision"] == stats._rate(0, 1)
    assert full_7d["grok-4.7"]["labels"] == {**zero, "invented": 1}
    assert full_7d["grok-4.7"]["real_precision"] == stats._rate(0, 1)
    assert full_7d["custom-reviewer"]["labels"] == {**zero, "fixed": 1}
    assert full_7d["custom-reviewer"]["identity"] == "not_in_catalog"

    page_7d = {row["model"]: row for row in snapshot["page_windows"]["7d"]["reviewer_accuracy"]["labels"]}
    assert set(page_7d) == {"claude-opus-5-5", "gpt-6.2-sol", "grok-4.7", "other"}
    assert page_7d["other"]["labels"] == {**zero, "fixed": 1}

    full_30d = {row["model"]: row for row in snapshot["windows"]["30d"]["reviewer_accuracy"]["labels"]}
    assert full_30d["gpt-6.2-sol"]["labels"] == {**zero, "fixed": 1, "wrong": 1}
    assert full_30d["gpt-6.2-sol"]["real_precision"] == stats._rate(1, 2)
    full_all = {row["model"]: row for row in snapshot["windows"]["all"]["reviewer_accuracy"]["labels"]}
    assert full_all["gpt-6.2-sol"]["labels"] == {**zero, "fixed": 1, "wrong": 1}
    assert "reviewer_miss_proxy" not in snapshot["windows"]["30d"]
    assert "reviewer_miss_proxy" not in snapshot["page_windows"]["30d"]


def test_no_live_trigger_counts_in_precision_denominator_not_numerator(tmp_path):
    """Defect class: a finding whose trigger cannot occur on the real system either has no label (refused as wrong
    shape), counts as a real error (in the numerator: 2/2), or leaves real precision untouched (outside the
    denominator: 1/1). One fixed plus one no_live_trigger label must read 1/2 with the matching interval."""
    batch_state, _ = _state(tmp_path)
    assert _label(batch_state, "o1-review", 1, "F0", "fixed") == 1
    assert _label(batch_state, "o1-review", 1, "F1", "no_live_trigger", "only main() calls it, after the guard") == 1
    snapshot = stats.collect(batch_state, tmp_path, NOW, landing=stats.Landing())
    [row] = snapshot["windows"]["7d"]["reviewer_accuracy"]["labels"]
    assert row["labels"]["fixed"] == 1 and row["labels"]["no_live_trigger"] == 1
    assert row["real_precision"] == stats._rate(1, 2)
    assert row["real_precision_ci"] == stats._wilson(1, 2)


def test_finding_outcome_ledger_keeps_one_label_per_finding_and_skips_damaged_lines(tmp_path):
    """Defect class: a relabel, a same-label retry refused for its new evidence, a repeated contest, a label or
    contest without required run binding (head and started_at) or reviewer_model, a half-bound line accepted, a
    legacy line without run binding losing conflict protection or counted as malformed, a self-contest, or a damaged
    ledger line (including one whose member names repeat) silently changing what the statistics count."""
    batch_state, _ = _state(tmp_path)
    assert _label(batch_state, "o1-review", 1, "F0", "fixed") == 1
    # The same label again adds nothing; another label for the finding is refused and writes nothing.
    assert _label(batch_state, "o1-review", 1, "F0", "fixed") == 0
    with pytest.raises(rows.OutcomeConflict):
        _label(batch_state, "o1-review", 1, "F0", "wrong", "already correct")
    # A retry with the same label and other evidence is a no-op too: the first line and its evidence stay.
    assert _label(batch_state, "o1-review", 1, "F0", "fixed", "now with evidence") == 0
    # The repository's other slug names the same repository: still one label per finding. Another repository's
    # publication at the same PR number is another key.
    with pytest.raises(rows.OutcomeConflict):
        _label(batch_state, "o1-review", 1, "F0", "wrong", "already correct", repository="agentic-machinery/machinery")
    assert _label(batch_state, "o1-review", 1, "F0", "fixed", repository="agentic-machinery/machinery") == 0
    assert _label(batch_state, "o1-review", 1, "F0", "wrong", "already correct", repository="other/repo") == 1
    # Every label but fixed needs its evidence; every new line needs head + started_at; labels need reviewer_model.
    for label in ("wrong", "invented", "no_live_trigger", "out_of_scope", "duplicate"):
        with pytest.raises(ValueError, match="wrong shape"):
            _label(batch_state, "o1-review", 1, "F1", label)
    for bad_model in (None, "", "unknown", "has space", "../escape"):
        with pytest.raises(ValueError, match="wrong shape"):
            rows.finding_outcome(kind="label", task_id="o1-review", repository="learn-ukrainian/machinery", pr=1,
                                 finding_id="F1", head=_HEAD_A, started_at=_START_A, reviewer_model=bad_model,
                                 label="fixed", recorded_at=_at(1))
    for bad_head, bad_start in (
        (None, _START_A), ("not-a-sha", _START_A), ("a" * 64, _START_A), ("A" * 40, _START_A),
        (_HEAD_A, None), (_HEAD_A, "not-a-time"),
    ):
        with pytest.raises(ValueError, match="wrong shape"):
            rows.finding_outcome(kind="label", task_id="o1-review", repository="learn-ukrainian/machinery", pr=1,
                                 finding_id="F1", head=bad_head, started_at=bad_start,
                                 reviewer_model="claude-opus-5-5", label="fixed", recorded_at=_at(1))
        with pytest.raises(ValueError, match="wrong shape"):
            rows.finding_outcome(kind="contest", task_id="o1-review", repository="learn-ukrainian/machinery", pr=1,
                                 finding_id="F0", head=bad_head, started_at=bad_start, contested_by="o1b-review",
                                 recorded_at=_at(1))
    with pytest.raises(ValueError, match="wrong shape"):
        rows.finding_outcome(kind="contest", task_id="o1-review", repository="learn-ukrainian/machinery", pr=1,
                             finding_id="F0", head=_HEAD_A, started_at=_START_A, reviewer_model="claude-opus-5-5",
                             contested_by="o1b-review", recorded_at=_at(1))
    with pytest.raises(ValueError, match="wrong shape"):
        _label(batch_state, "o1-review", 1, "F1", "dismissed", "a word outside the vocabulary")
    with pytest.raises(ValueError, match="wrong shape"):
        _contest(batch_state, "o1-review", 1, "F0", "o1-review")
    assert _contest(batch_state, "o1-review", 1, "F0", "o1b-review") == 1
    assert _contest(batch_state, "o1-review", 1, "F0", "o1b-review") == 0
    path = batch_state / rows.ROWS_DIR / rows.OUTCOMES_FILE
    good = path.read_bytes()
    assert b"already correct" not in good and b"now with evidence" not in good
    machinery = rows.repository_identity("learn-ukrainian/machinery")
    # Writing a raw legacy label line without reviewer_model/head/started_at via record_finding_outcomes is refused.
    first = json.loads(good.splitlines()[0])
    legacy = {k: v for k, v in first.items() if k not in ("reviewer_model", "head", "started_at")}
    legacy["finding_id"] = "F9"
    with pytest.raises(ValueError, match="wrong shape"):
        rows.record_finding_outcomes(batch_state, [legacy])
    extra = {**first, "note": "free text"}
    half_bound = {k: v for k, v in first.items() if k != "started_at"}
    # A 64-hex head is refused on raw write as well as on read (only 40-hex Git commit SHAs are valid).
    sha256_head = {**first, "finding_id": "F8", "head": "a" * 64}
    with pytest.raises(ValueError, match="wrong shape"):
        rows.record_finding_outcomes(batch_state, [sha256_head])
    # Unhashable values, half-bound run keys, 64-hex heads, invalid reviewer_model, and wrong types are damaged lines.
    damaged = [extra, half_bound, sha256_head, {**first, "label": ["fixed"]}, {**first, "kind": {"label": 1}},
               {**first, "pr": "1"}, {**first, "reviewer_model": "unknown"},
               {**first, "reviewer_model": ["claude-opus-5-5"]}]
    # A repeated member name: the decoder would keep "wrong" and hide "fixed".
    repeated = json.dumps({**first, "finding_id": "F5"})[:-1].encode() + b', "label": "wrong"}'
    with path.open("ab") as stream:
        stream.write(json.dumps(legacy).encode() + b"\n" + b"{not json\n"
                     + b"".join(json.dumps(line).encode() + b"\n" for line in damaged) + repeated + b"\n")
    labels, contests, counts = rows.read_finding_outcomes(batch_state)
    t_label = rows.timestamp(_at(1))
    t_contest = rows.timestamp(_at(0.5))
    t_start = rows.timestamp(_START_A)
    assert labels == {
        ("o1-review", machinery, 1, "F0", _HEAD_A, t_start): ("claude-opus-5-5", "fixed", t_label),
        ("o1-review", "other/repo", 1, "F0", _HEAD_A, t_start): ("claude-opus-5-5", "wrong", t_label),
    }
    assert contests == {("o1-review", machinery, 1, "F0", _HEAD_A, t_start): t_contest}
    assert counts == {"lines": 4, "unattributed_labels": 1, "malformed_lines": 10}
    # A writer that finds damaged and legacy lines still keeps one label per finding (legacy F9 recorded at _at(1)
    # protects a run that started at _START_A = _at(2) <= _at(1)).
    assert _label(batch_state, "o1-review", 1, "F0", "fixed") == 0
    assert _label(batch_state, "o1-review", 1, "F9", "fixed") == 0
    with pytest.raises(rows.OutcomeConflict):
        _label(batch_state, "o1-review", 1, "F9", "wrong", "already correct")
    # PR1553-R2-F1: a newer run of the same task id that started AFTER legacy F9 was recorded (_at(0.5) > _at(1))
    # gets its own run-bound label without conflicting with or being suppressed by the earlier run's legacy line.
    assert _label(batch_state, "o1-review", 1, "F9", "wrong", "new run finding is wrong",
                  head=_HEAD_B, started_at=_at(0.5), recorded_at=_at(0.25)) == 1
    labels_after, _, _ = rows.read_finding_outcomes(batch_state)
    assert ("o1-review", machinery, 1, "F9", _HEAD_B, rows.timestamp(_at(0.5))) in labels_after


def test_author_results_split_by_risk_tier(tmp_path):
    """Defect class: author first-round approval and findings per PR that mix risk tiers, so an author given the
    critical work looks worse than its peers, that take a later round for the first when a window starts mid-PR, or
    that read a reply instead of the published verdict (a P3-only rejection published as an approval, an unpublished
    run taken for the first round, its window or its author, a published review whose reply is unparsed left out),
    or that miscount the PRs the tiers leave out of a window. Every expected figure below is counted by hand."""
    batch_state, tasks = _state(tmp_path)
    grok, sol = {"review_author_model": "grok-4.7"}, {"review_author_model": "gpt-6.1-sol"}
    critical = {"review_profile": "infra", "review_risk": "critical"}
    _review(tasks, "r10-review", "correct", [], pr=10, pinned_head="a" * 40, **grok, **critical)
    _review(tasks, "r11-review", "incorrect", ["P1", "P2"], pr=11, pinned_head="b" * 40, **grok, **critical)
    _review(tasks, "r12-review", "correct", ["P3"], pr=12, pinned_head="c" * 40, review_risk="low", **grok)
    _review(tasks, "r13-review", "incorrect", ["P0", "P1"], pr=13, pinned_head="d" * 40, **grok)
    _review(tasks, "r14-review", "correct", [], pr=14, pinned_head="f" * 40, review_risk="high", **sol)
    # PR 15 was first blocked at high risk before the 7-day window; its later medium approval is inside it.
    _review(tasks, "r15a-review", "incorrect", ["P1"], pr=15, pinned_head="1" * 40, review_risk="high",
            started_at=(NOW - timedelta(days=9)).isoformat(), finished_at=(NOW - timedelta(days=8)).isoformat(), **grok)
    _review(tasks, "r15b-review", "correct", [], pr=15, pinned_head="2" * 40, **grok)
    # PR 16: a reply with only a P3 finding, published as an approval: a first-round approval.
    _review(tasks, "r16-review", "incorrect", ["P3"], pr=16, pinned_head="3" * 40, review_risk="low", **grok)
    # PR 17: an unpublished high-risk run by another author's PR came first; the first review of record is the medium
    # approval, and its author is the published review's alone.
    _review(tasks, "r17a-review", "incorrect", ["P1"], pr=17, pinned_head="4" * 40, review_risk="high", **sol)
    _review(tasks, "r17b-review", "correct", [], pr=17, pinned_head="5" * 40, **grok)
    # PR 18: no review of it was published: left out of the tiers and counted.
    _review(tasks, "r18-review", "correct", [], pr=18, pinned_head="6" * 40, **grok)
    # PR 19: an unpublished high-risk run 8 days ago, then a published medium approval inside the week: the PR is in
    # the week's tiers, from its first published review.
    _review(tasks, "r19a-review", "incorrect", ["P1"], pr=19, pinned_head="7" * 40, review_risk="high",
            started_at=(NOW - timedelta(days=8, hours=1)).isoformat(),
            finished_at=(NOW - timedelta(days=8)).isoformat(), **grok)
    _review(tasks, "r19b-review", "correct", [], pr=19, pinned_head="8" * 40, **grok)
    # PR 20: its only review was published as an approval, but its reply is not parsed: counted, findings unknown.
    _review(tasks, "r20-review", "correct", [], pr=20, pinned_head="9" * 40, review_risk="low", **grok)
    (tasks / "r20-review.result").unlink()
    # PR 21: an approval dispatched by branch alone (its row names no PR), published on PR 21: it joins the published
    # cohort, and never the legacy author figures, which read only rows that name their PR.
    _review(tasks, "r21-review", "correct", [], pinned_head="e" * 40, review_risk="low", **grok)
    # PR 22: its first published review, a low-risk approval with an unparsed reply, finished 8 days ago; a parsed but
    # unpublished approval inside the week puts it in the week's author totals, and outside the week's tiers.
    _review(tasks, "r22a-review", "correct", [], pr=22, pinned_head="0" * 40, review_risk="low",
            started_at=(NOW - timedelta(days=8, hours=1)).isoformat(),
            finished_at=(NOW - timedelta(days=8)).isoformat(), **grok)
    (tasks / "r22a-review.result").unlink()
    _review(tasks, "r22b-review", "correct", [], pr=22, pinned_head="0" * 40, **grok)
    rows.backfill(tasks, batch_state)
    approved, blocked = "APPROVED", "CHANGES_REQUESTED"
    assert _publish(batch_state, ("r10-review", 10, "a" * 40, approved), ("r11-review", 11, "b" * 40, blocked),
                    ("r12-review", 12, "c" * 40, approved), ("r13-review", 13, "d" * 40, blocked),
                    ("r14-review", 14, "f" * 40, approved), ("r15a-review", 15, "1" * 40, blocked),
                    ("r15b-review", 15, "2" * 40, approved), ("r16-review", 16, "3" * 40, approved),
                    ("r17b-review", 17, "5" * 40, approved), ("r19b-review", 19, "8" * 40, approved),
                    ("r20-review", 20, "9" * 40, approved), ("r21-review", 21, "e" * 40, approved),
                    ("r22a-review", 22, "0" * 40, approved),
                    repository="learn-ukrainian/learn-ukrainian.github.io") == 13

    snapshot = stats.collect(batch_state, batch_state.parent, NOW, landing=stats.Landing())

    def tiers(window: str, author: str, view: str = "windows") -> list[tuple]:
        (found,) = [a for a in snapshot[view][window]["author_risks"] if a["model"] == author]
        return [(r["risk"], r["prs"], r["first_round_approved"]["value"], r["blocking_per_pr"]["value"],
                 r["p0_p2_per_pr"]["value"], r["low_sample"]) for r in found["by_risk"]]

    # Medium: r13 blocked (P0, P1), r17b and r19b approved without findings. Low: r12, r16, r20 and r21 approved;
    # r20's findings are unknown, so the per-PR findings rates count r12, r16 and r21 only.
    week = [("critical", 2, 0.5, 0.5, 1.0, True),
            ("medium", 3, stats._rate(2, 3)["value"], stats._rate(2, 3, 2)["value"],
             stats._rate(2, 3, 2)["value"], True),
            ("low", 4, 1.0, 0.0, 0.0, True)]
    assert tiers("7d", "grok-4.7") == week == tiers("7d", "grok-4.7", "page_windows")
    # Over 30 days PR 15 adds the high tier and PR 22 a fifth low-tier approval with unknown findings.
    assert tiers("30d", "grok-4.7") == [week[0], ("high", 1, 0.0, 1.0, 1.0, True), week[1],
                                        ("low", 5, 1.0, 0.0, 0.0, True)]
    # Sol authored only the unpublished r17a and the published r14.
    assert [r[:3] for r in tiers("7d", "gpt-6.1-sol")] == [("high", 1, 1.0)]
    # Left out of the week's tiers though in the week's author totals: PR 18 (never published) and PR 22 (first
    # published before the week).
    for view in ("windows", "page_windows"):
        without = {a["model"]: a["prs_without_review_of_record"] for a in snapshot[view]["7d"]["author_risks"]}
        assert without == {"grok-4.7": 2, "gpt-6.1-sol": 0}
    # The legacy cohort is the parsed replies of rows that name their PR: grok's PRs 10-13, 16, 18 and 22 this week
    # (PR 17 is a joint author's, PR 19 started before the week, PR 20 is unparsed, PR 21's row names no PR).
    (legacy,) = [a for a in snapshot["windows"]["7d"]["authors"] if a["model"] == "grok-4.7"]
    assert legacy["prs"] == 7
    # PR 17's joint author stays joint on the page too, so either member's picker selection finds it.
    for view in ("windows", "page_windows"):
        assert ("gpt-6.1-sol+grok-4.7", 1) in [(a["model"], a["prs"]) for a in snapshot[view]["7d"]["authors"]]


def _merged_history(tmp_path, monkeypatch):
    repo = _git_fixture(tmp_path, monkeypatch)
    fleet_repo = FleetRepo("machinery", "learn-ukrainian/machinery", "repo", "test",
                           alternate_github="agentic-machinery/machinery")
    monkeypatch.setattr(stats, "load_fleet_repos", lambda: {"machinery": fleet_repo})
    monkeypatch.setattr(stats, "resolve_fleet_repo", lambda key, **_: (fleet_repo, repo))

    def merge(title: str, day: float, *, lines: int = 0, body: str = "") -> str:
        if lines:
            path = f"change-{title.rpartition('#')[2].rstrip(')')}.txt"
            (repo / path).write_text("new line\n" * lines)
            _git(repo, "add", "--", path)
        at = (NOW + timedelta(days=day)).isoformat()
        monkeypatch.setenv("GIT_COMMITTER_DATE", at)
        _git(repo, "commit", "-q", "--allow-empty", "--date", at, "-m", title, "-m", body)
        sha = _git(repo, "rev-parse", "HEAD").strip()
        _git(repo, "update-ref", "refs/remotes/origin/main", sha)
        return sha

    merge("Initial commit", -31)
    return merge


def test_author_quality_stratifies_published_rounds_by_risk_and_landed_diff_size(tmp_path, monkeypatch):
    """Defect class: combining PRs on opposite size boundaries or dropping rejected PRs without a landed diff."""
    merge = _merged_history(tmp_path, monkeypatch)
    batch_state, tasks = _state(tmp_path)
    for number, size in ((31, 100), (32, 101), (33, 1000), (34, 1001), (36, 10)):
        merge(f"Feature (#{number})", -2, lines=size)
    grok = {"repo": "machinery", "review_author_model": "grok-4.7"}
    _review(tasks, "size31a", "incorrect", ["P1", "P2"], pr=31, pinned_head="a" * 40, **grok,
            started_at=(NOW - timedelta(days=4)).isoformat(), finished_at=(NOW - timedelta(days=3)).isoformat())
    _review(tasks, "size31b", "correct", [], pr=31, pinned_head="b" * 40, **grok, review_risk="high")
    for number, sha in ((32, "c"), (33, "d"), (34, "e")):
        _review(tasks, f"size{number}", "correct", [], pr=number, pinned_head=sha * 40, **grok)
    _review(tasks, "size35", "incorrect", ["P2"], pr=35, pinned_head="f" * 40, **grok)
    _review(tasks, "size36", "correct", [], pr=36, pinned_head="1" * 40, **grok, review_risk="critical")
    rows.backfill(tasks, batch_state)
    approve, changes = "APPROVED", "CHANGES_REQUESTED"
    assert _publish(batch_state, ("size31a", 31, "a" * 40, changes), ("size31b", 31, "b" * 40, approve),
                    ("size32", 32, "c" * 40, approve), ("size33", 33, "d" * 40, approve),
                    ("size34", 34, "e" * 40, approve), ("size35", 35, "f" * 40, changes),
                    ("size36", 36, "1" * 40, approve)) == 7

    snapshot = stats.collect(batch_state, tmp_path, NOW)
    with _serve(snapshot, tmp_path, monkeypatch) as client:
        response = client.get("/api/fleet/model-stats?window=7d&view=page")
    assert response.status_code == 200
    (author,) = response.json()["author_risks"]
    critical, medium = author["by_risk"]
    assert (critical["risk"], critical["prs"], critical["by_size"][0]["size"]) == ("critical", 1, "small")
    assert (medium["risk"], medium["prs"]) == ("medium", 5)
    assert medium["rounds_per_pr"] == {"value": 1.2, "n": 5, "low_sample": True}
    assert [(band["size"], band["prs"], band["rounds_per_pr"]["value"], band["first_round_approved"]["value"],
             band["approved_eventually"]["value"], band["p0_p2_per_pr"]["value"], band["low_sample"])
            for band in medium["by_size"]] == [
        ("small", 1, 2.0, 0.0, 1.0, 2.0, True), ("medium", 2, 1.0, 1.0, 1.0, 0.0, True),
        ("large", 1, 1.0, 1.0, 1.0, 0.0, True), ("unknown", 1, 1.0, 0.0, 0.0, 1.0, True),
    ]
    # The shallow boundary contains only PR 36. Its tree is large, but its parent and exact diff are missing.
    shallow = tmp_path / "shallow"
    _git(tmp_path, "clone", "-q", "--depth=1", (tmp_path / "repo").as_uri(), str(shallow))
    monkeypatch.setattr(stats, "resolve_fleet_repo", lambda *args, **kwargs: (None, shallow))
    (author,) = stats.collect(batch_state, tmp_path, NOW)["page_windows"]["7d"]["author_risks"]
    assert author["by_risk"][0]["by_size"][0]["size"] == "unknown"


def test_post_merge_rework_deduplicates_same_repo_fixes_in_a_complete_week(tmp_path, monkeypatch):
    """Defect class: counting references from another repo, unrelated PRs, duplicate fixes or incomplete weeks."""
    merge = _merged_history(tmp_path, monkeypatch)
    batch_state, tasks = _state(tmp_path)
    terra, sol = "gpt-6.1-terra", "gpt-6.2-sol"
    for number, day, model in ((11, -13, terra), (12, -12, sol), (13, -11, terra), (14, -10, sol), (15, -1, terra)):
        _record(tasks, f"written-{number}", repo="machinery", model=model,
                started_at=(NOW - timedelta(days=20)).isoformat())
        if number == 15:
            continue
        sha = merge(f"Feature (#{number})", day, body=f"X-Agent: codex/written-{number}")
        if number == 14:
            reverted_sha = sha
    _record(tasks, "written-16", repo="machinery", model=terra, started_at=(NOW - timedelta(days=20)).isoformat())
    merge("Feature (#16)", -10, body="X-Agent: codex/written-16")
    merge("fix: repair agentic-machinery/machinery#16 (#20)", -10)
    merge("fix: repair a prior change (#21)", -9,
          body="Fixes https://github.com/agentic-machinery/machinery/pull/11\nRefs other/repo#12\n\n```\nExample #13\n```")
    merge("docs: cite #12 (#22)", -8, body="https://example.invalid/leaky-commit-body")
    merge("fix: repair #11 again (#23)", -6)
    merge("fix: late repair for #12 (#24)", -4)
    merge("Revert feature (#25)", -3, body=f"This reverts commit {reverted_sha}.")
    merge("Feature (#15)", -1, body="X-Agent: codex/written-15")
    merge("fix: repair recent #15 (#26)", -0.5)
    rows.backfill(tasks, batch_state)

    snapshot = stats.collect(batch_state, tmp_path, NOW)
    with _serve(snapshot, tmp_path, monkeypatch) as client:
        response = client.get("/api/fleet/model-stats?window=7d&view=page")
        day = client.get("/api/fleet/model-stats?window=24h&view=page").json()
    assert response.status_code == 200 and "leaky-commit-body" not in response.text
    assert day["post_merge_rework_proxy"] == []
    assert [(r["model"], r["reworked"], r["rate"]) for r in response.json()["post_merge_rework_proxy"]] == [
        (terra, 2, {"value": 0.667, "n": 3, "low_sample": True}), (sol, 1, {"value": 0.5, "n": 2, "low_sample": True}),
    ]


def test_no_deliverable_counts_closed_cause_labels_from_settled_records(tmp_path, monkeypatch):
    """Defect class: collapsing stated causes, leaking their details, or making old rows unreadable."""
    batch_state, tasks = _state(tmp_path)
    for task, reason, extra in (
        ("outside", "outside_owned_paths: /private/escaped-path", {}), ("scope", "scope-breaker stop: note", {}),
        ("empty", "no_commits_or_unknown", {}), ("unknown", None, {}),
        ("deadline", None, {"error": "process deadline exceeded"}),
        ("other", "worktree_dirty_or_unknown", {}),
    ):
        _record(tasks, task, status="no_deliverable", no_deliverable_reason=reason, **extra)
    _record(tasks, "landed-with-old-error", no_deliverable_reason="outside_owned_paths")
    path = tasks / "outside.json"
    old = rows.summary_row(path, json.loads(path.read_text()), path.stat().st_mtime_ns)
    assert old is not None
    old.pop("no_deliverable_cause")
    rows.append_row(batch_state, old)
    assert _row(_window(batch_state, stats.Landing()), "gpt-6.1-sol", "write")["no_deliverable_causes"][
        "no_commits_or_unknown"] == 1
    assert rows.backfill(tasks, batch_state)["refreshed"] == 1
    snapshot = stats.collect(batch_state, tmp_path, NOW, landing=stats.Landing())
    with _serve(snapshot, tmp_path, monkeypatch) as client:
        response = client.get("/api/fleet/model-stats?window=7d&view=page")
    assert response.status_code == 200 and "escaped-path" not in response.text
    model = _row(response.json(), "gpt-6.1-sol", "write")
    assert model["status"]["no_deliverable"] == 6
    assert model["no_deliverable_causes"] == model["by_effort"][0]["no_deliverable_causes"] == {
        "outside_owned_paths": 1, "scope_breaker_stop": 1, "no_commits_or_unknown": 2, "deadline": 1, "other": 1,
    }


def test_publication_ledger_writes_only_well_formed_lines_and_trusts_no_damaged_one(tmp_path, monkeypatch):
    """Defect class: a malformed publication is written, or a damaged ledger line is trusted, so a review counts as
    published without proof; a missing ledger reads as anything but "no publication known"; or an older publication
    appended later (a history import) supersedes a newer one; or a line for another repository, PR or head blocks or
    hides the real publication; or a collection reuses a fleet or model catalog reading of an earlier one (an
    unreadable catalog then hides publications and reply samples after its repair, or a valid one outlives the
    file)."""
    from collections import Counter

    import pytest

    batch_state, tasks = _state(tmp_path)
    assert rows.read_publications(batch_state) == ({}, Counter({"missing": 1, "tasks": 0}))
    fields = {"task_id": "t-review", "repository": "learn-ukrainian/machinery", "pr": 3, "head": "a" * 40,
              "verdict": "APPROVED", "published_at": NOW.isoformat()}
    good = rows.publication(**fields)
    for bad in ({"verdict": "APPROVE"}, {"pr": 0}, {"head": "a" * 39}, {"published_at": None},
                {"repository": "../x"}, {"task_id": "../x"}):
        with pytest.raises(ValueError):
            rows.publication(**{**fields, **bad})
    # One malformed entry in a batch writes nothing at all.
    with pytest.raises(ValueError):
        rows.record_publications(batch_state, [good, {**good, "pr": True}])
    ledger = batch_state / rows.ROWS_DIR / rows.PUBLICATIONS_FILE
    assert not ledger.exists()
    assert rows.record_publications(batch_state, [good]) == 1
    current = {**good, "repository": "agentic-machinery/machinery"}
    assert rows.publication_key(good) == rows.publication_key(current) == (
        "t-review", 1445647449, 3, "a" * 40, "APPROVED",
    )
    assert rows.record_publications(batch_state, [current]) == 0
    with ledger.open("ab") as stream:
        stream.write(b'{"schema": "review-publication.v1", "task_id": "t-review"}\nnot json\n')
    later = {**good, "verdict": "CHANGES_REQUESTED"}
    assert rows.record_publications(batch_state, [later, good]) == 1
    # Newest first; of two at the same comment time, the later line first.
    assert rows.read_publications(batch_state) == (
        {"t-review": [later, good]}, Counter({"lines": 2, "malformed_lines": 2, "tasks": 1}))
    # An import appends an older publication after the newer one: the newer one still stands.
    older = {**good, "verdict": "BLOCKED", "published_at": (NOW - timedelta(days=1)).isoformat()}
    assert rows.record_publications(batch_state, [older]) == 1
    assert rows.read_publications(batch_state)[0] == {"t-review": [later, good, older]}
    # Newer lines naming the task on another PR, repository or head are other publications: each is stored, and the
    # row's join still takes the newest entry that matches its own repository, PR and head.
    newer = (NOW + timedelta(hours=1)).isoformat()
    elsewhere = [{**later, "pr": 4, "published_at": newer}, {**later, "head": "b" * 40, "published_at": newer},
                 {**later, "repository": "learn-ukrainian/learn-ukrainian.github.io", "published_at": newer}]
    assert rows.record_publications(batch_state, elsewhere) == 3
    row = {"task_id": "t-review", "role": "review", "repo": "machinery", "pr": 3, "pinned_head": "a" * 40}
    keys = rows.repository_keys(rows.fleet_catalog())
    assert stats._publication(row, rows.read_publications(batch_state)[0], keys) == later

    # Each collection reads the catalogs afresh through every cache, the real loader included: one that cannot be
    # read matches no publication and admits no reply sample in that collection only, and a valid reading is never
    # kept once the file breaks.
    from lu_machinery.orchestration import fleet_repos

    _review(tasks, "c-review", "incorrect", ["P1"], pr=3, pinned_head="c" * 40, review_author_model="grok-4.7",
            repo="machinery", usage={"input_tokens": 5, "cache_read_input_tokens": 8,
                                     "cache_creation_input_tokens": 2, "output_tokens": 3})
    _review(tasks, "supp-review", "correct", [], pinned_head="c" * 40, repo="machinery", agent="agy",
            model="gemini-4-neon", usage={"input_tokens": 7, "cache_read_tokens": 9, "output_tokens": 4,
                                         "thinking_tokens": 6})
    _review(tasks, "same-head-other-repo", "correct", [], pinned_head="c" * 40, repo="public", agent="cursor",
            model="grok-4.7", usage={"inputTokens": 10, "cacheReadTokens": 12, "cacheWriteTokens": 2,
                                     "outputTokens": 2, "reasoningTokens": 1})
    _record(tasks, "missing-output", usage={"input_tokens": 20, "cached_input_tokens": 10})
    _record(tasks, "zero-usage", usage={"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0})
    _record(tasks, "u-review", mode="read-only", agent="claude", model="claude-opus-5-5", data_class="public-code",
            review_profile="code")
    (tasks / "u-review.result").write_text("prose reply", encoding="utf-8")
    rows.backfill(tasks, batch_state)
    stored = {row["task_id"]: row for row in rows.read_rows(batch_state)[0]}
    assert stored["u-review"]["review"]["unparsed_sample"] == "prose reply"
    assert stored["missing-output"]["usage_known"] is False and stored["zero-usage"]["usage_known"] is True
    assert _publish(batch_state, ("c-review", 3, "c" * 40, "CHANGES_REQUESTED")) == 1
    token_use = stats.collect(batch_state, tmp_path, NOW, landing=stats.Landing())["page_windows"]["7d"]["token_use"]
    assert token_use["totals"] == {"fresh_input": 26, "cached_input": 29, "output": 16}
    assert token_use["reported_tasks"] == 4 and token_use["merged_prs"] is None
    assert token_use["by_work"]["review_of_record"]["tokens"] == 18
    assert token_use["waste"]["supplemental_review"]["share"] == 26 / 71
    assert token_use["by_work"]["unclassified_review"]["tokens"] == 27
    # A label line is read through the registry too: unreadable, the collection counts no label and still answers.
    assert _label(batch_state, "c-review", 3, "F0", "fixed") == 1
    registry = fleet_repos._CONFIG_PATH.read_text(encoding="utf-8")
    assert "\n    github: agentic-machinery/machinery\n" in registry
    catalog = tmp_path / "fleet_repos.yaml"
    monkeypatch.setattr(fleet_repos, "_CONFIG_PATH", catalog)

    def collected() -> tuple[int, int, int, bool]:
        snapshot = stats.collect(batch_state, batch_state.parent, NOW, landing=stats.Landing())
        (found,) = snapshot["windows"]["7d"]["reviewer_accuracy"]["findings"]
        return (found["blocking_reviews"], found["reviews_unknown_publication"],
                snapshot["coverage"]["rows"].get("malformed_lines", 0),
                snapshot["windows"]["7d"]["idle_catalog_models"] is not None)

    unreadable, valid = (0, 1, 1, True), (1, 0, 0, True)
    for text, expected in (
            (registry.replace("github: agentic-machinery/machinery\n", "github: agentic-machinery-machinery\n"), unreadable),
            (registry, valid), ("repos: [", unreadable), (registry, valid)):
        catalog.write_text(text, encoding="utf-8")
        assert collected() == expected, text[:40]
    # The model catalog through its own loader's cache: unreadable, it admits no public data class (the sample row
    # is skipped) and leaves the idle models unknown (null, never an empty list), in that collection only.
    from lu_machinery.review import model_catalog

    models_file = tmp_path / "model_catalog.yaml"
    models_text = model_catalog.CATALOG_PATH.read_text(encoding="utf-8")
    monkeypatch.setattr(model_catalog.load_model_catalog.__wrapped__, "__defaults__", (models_file,))
    for text, expected in ((models_text, valid), ("models: [", (1, 0, 1, False)), (models_text, valid)):
        models_file.write_text(text, encoding="utf-8")
        assert collected() == expected, text[:40]
    # Leave no reading of the scratch catalog to later tests.
    monkeypatch.undo()
    rows.refresh_readings()

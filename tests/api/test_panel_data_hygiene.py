"""Defect class: unvetted provider values and metadata escape through UI panel APIs."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lu_machinery.api import (
    comms_router, delegate_router, git_hygiene_router, ops_router,
    runtime_router, state_helpers, state_router,
)
from lu_machinery.api.monitor_context import fixture_context
from tests.opsec_fixtures import TOKEN

STAMP = "2030-01-01T00:00:00Z"
DIRTY = (TOKEN, "/fixture-private/home/config", "account@example.invalid", "host.fixture.invalid",
         "192.0.2.17", "synthetic-token-123456", "private prompt and brief body", "Exception: synthetic failure",
         "X" * 4096)
ROUTES = (
    "/api/delegate/active", "/api/delegate/recent", "/api/runtime/subscriptions",
    "/api/state/routing-budget", "/api/runtime/logins", "/api/git/pull-requests",
    "/api/ops/v1/alerts", "/api/comms/v1/metrics",
)


@pytest.fixture
def panel_client(tmp_path, monkeypatch, synthetic_opsec):
    """All external inputs are synthetic. No provider, GitHub or host probes run."""
    state_helpers.cache_invalidate()
    app = FastAPI()
    app.state.ctx = fixture_context(tmp_path)
    for prefix, module in (("delegate", delegate_router), ("runtime", runtime_router),
                           ("state", state_router), ("git", git_hygiene_router),
                           ("ops", ops_router), ("comms", comms_router)):
        app.include_router(module.router, prefix=f"/api/{prefix}")

    async def accounts():
        return {}

    monkeypatch.setattr(state_router, "revalidated_routing_budget_fields", accounts)
    client = TestClient(app)
    yield client, app.state.ctx
    state_helpers.cache_invalidate()


def _inputs(monkeypatch, ctx, dirty):
    task = {"task_id": dirty, "agent": "codex", "model": "gpt-6.1-sol", "status": "running",
            "observed_model": dirty, "started_at": STAMP, "finished_at": dirty, "duration_s": 17,
            "age_s": 10, "alive": True, "error": dirty, "cli_version": dirty, "run_nonce": dirty,
            "codex_account": dirty, "claude_account": dirty, "research_role": dirty,
            "push_state": dirty, "delivery_pr": dirty, "prompt": dirty, "brief": dirty,
            "worktree_path": dirty, "unknown_new_field": dirty}
    payload = {"total": 1, "tasks": [task], "needs_attention": {"total": 1, "tasks": [task]}}
    snapshot = SimpleNamespace(read=lambda: (payload, 0))
    monkeypatch.setattr(delegate_router, "active_snapshot", lambda _ctx: snapshot)
    monkeypatch.setattr(delegate_router, "recent_snapshot", lambda _ctx: snapshot)
    window = {"status": "ok", "used_pct": 25, "left_pct": 75, "resets_at": STAMP,
              "label": dirty, "reset_description": dirty, "summary": dirty, "reason": dirty,
              "name": dirty, "kind": dirty, "unit": dirty, "source": dirty, "state": dirty,
              "count": 2, "expires_on": dirty, "items": [{"kind": dirty, "count": 2}]}
    account = {"account": "Codex a", "source": "usage_cache", "note": dirty,
               **dict.fromkeys(("weekly", "session", "pace", "credits", "banked_resets", "plan", "reading"), window),
               "extra_windows": [window], "breakdown": [{"label": dirty, "value": dirty}]}
    monkeypatch.setattr(runtime_router.subscriptions, "subscriptions_snapshot", lambda: {
        "generated_at": STAMP, "stale_after_s": 900,
        "sources": {"usage_cache": {"status": "ok", "reason": dirty}, "plan_facts": window},
        "providers": [{"provider": "codex", "label": dirty, "accounts": [account]}], dirty: dirty})
    monkeypatch.setattr(runtime_router.account_logins, "logins_snapshot", lambda: {
        "state": "ok", "read_at": STAMP,
        "accounts": [{"account": "Codex a", "provider": dirty, "logged_in": True,
                      "error": dirty, "authMethod": dirty, "subscriptionType": dirty,
                      "expires_at": dirty, "email": dirty, "path": dirty}]})
    monkeypatch.setattr(runtime_router.account_logins, "subscription_ends", lambda: {
        "ends": {"codex_a": dirty, dirty: dirty}, "reason": dirty})

    def budget(**kwargs):
        kwargs["task_generation"].append(state_router._tasks_content_token(kwargs["tasks_dir"]))
        return {"generated_at": STAMP, "agents": {"codex": {
                    "status": "cool", "weekly_cap_usd": 100, "eligible": True,
                    "codexbar": {"pace_summary": dirty, "windows": {dirty: window}, "source": dirty},
                    "runtime": {"summary_error": dirty, "headroom_reason": dirty, "models_rate_limited": [dirty]},
                    "health": {"last_error": dirty, "error": dirty}, "model": dirty}},
                "api_accounts": {"deepseek": {"total_balance": 2.5, "currency": "USD", "error": dirty,
                                              "auth_error": dirty, "probe_state": dirty, "freshness": dirty}, dirty: window},
                "recommendation": {"primary_agent_for_code": "codex", "rationale": dirty, "warnings": [dirty]},
                "diagnostics": {"stale": False, "data_age_s": 0, "stale_threshold_s": 900},
                "in_flight": {"codex": 1, dirty: 1},
                "ranked_by_headroom": [{"lane": dirty, "status": dirty, "health": {"error": dirty}}]}

    monkeypatch.setattr(state_router, "compute_routing_budget", budget)
    monkeypatch.setattr(state_router, "scan_lane_health", lambda *_args, **_kwargs: {})
    node = {"number": 7, "title": dirty, "headRefName": "codex/" + dirty, "url": "https://github.com/" + dirty,
            "createdAt": dirty, "mergeStateStatus": dirty, "mergeQueueEntry": {"state": dirty, "position": 1},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": dirty},
                                              "status": {"context": {"state": dirty}}}}]}}
    monkeypatch.setattr(git_hygiene_router.github_client, "run", lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0, stdout=json.dumps({"data": {f"r{i}": {"isPrivate": False,
                   "pullRequests": {"totalCount": 1, "nodes": [node]}} for i in range(3)}})))
    raw_alerts = [{"labels": {"alertname": dirty, "severity": dirty, "owner_lane": dirty, "repo": dirty,
                              "instance": dirty, "job": dirty}, "annotations": {"summary": dirty, "action": dirty},
                   "startsAt": dirty, "status": {"state": "active"}, "generatorURL": dirty}]
    connection = SimpleNamespace(request=lambda *_args, **_kwargs: None, close=lambda: None,
                                 getresponse=lambda: SimpleNamespace(status=200, read=lambda _limit: json.dumps(raw_alerts).encode()))
    monkeypatch.setattr(ops_router.http.client, "HTTPConnection", lambda *_args, **_kwargs: connection)
    metrics = {"deliveries": {"queued": 2, dirty: 3}, "jobs": {dirty: 1}, "requests": {dirty: 2},
               "messages_legacy": {"by_status": {dirty: 4}, "by_message_type": {dirty: 5}},
               "dead_letters": 1, "db_error": dirty, "source": dirty,
               "latency_seconds": {dirty: dirty, "delivery_dispatch_to_done": {"n": 2, "avg": 3}},
               "store": {"kind": dirty, "reachable": True, "path": dirty}}
    monkeypatch.setattr(comms_router, "resolve_metrics_source", lambda: "legacy")
    ctx.roots.message_db_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.roots.message_db_path.touch()
    monkeypatch.setattr(comms_router, "collect_efficiency_metrics", lambda _path: metrics)
    return metrics


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("dirty", DIRTY, ids=("host-token", "path", "email", "host", "ip", "token", "brief", "exception", "oversized"))
def test_panel_response_never_echoes_unvetted_provider_values(panel_client, monkeypatch, route, dirty):
    """Every panel must reject dirty values while still showing real measured data."""
    client, ctx = panel_client
    _inputs(monkeypatch, ctx, dirty)
    response = client.get(route)
    assert response.status_code == 200, response.text
    assert dirty not in response.text
    data = response.json()
    if route.startswith("/api/delegate/"):
        assert data["total"] == 1 and data["tasks"][0]["duration_s"] == 17
        assert data["tasks"][0]["model"] == "gpt-6.1-sol"
        if len(dirty) > 512:
            assert data["tasks"][0]["task_id"] is None
        else:
            assert data["tasks"][0]["task_id"].startswith("task-")
    elif route.endswith("subscriptions"):
        assert data["providers"][0]["accounts"][0]["weekly"]["left_pct"] == 75
        assert data["providers"][0]["accounts"][0]["weekly"]["reason_code"] == "provider_error"
    elif route.endswith("routing-budget"):
        assert data["api_accounts"]["deepseek"]["total_balance"] == 2.5
        assert data["agents"]["codex"]["weekly_cap_usd"] == 100
        assert data["in_flight"]["codex"] == 1
    elif route.endswith("logins"):
        assert data["accounts"][0]["logged_in"] is True
        assert data["accounts"][0]["reason_code"] == "provider_error"
    elif route.endswith("pull-requests"):
        assert data["repositories"][0]["pull_requests"][0]["number"] == 7
        assert data["repositories"][0]["pull_requests"][0]["url"] == "https://github.com/learn-ukrainian/machinery/pull/7"
    elif route.endswith("alerts"):
        assert data["status"] == "ok" and len(data["alerts"]) == 1
        assert data["alerts"][0]["alertname"] == "UnrecognizedAlert"
    else:
        assert data["deliveries"] == {"queued": 2, "unknown": 3}
        assert data["messages_legacy"]["by_status"] == {"unknown": 4}


@pytest.mark.parametrize("source", ("authority", "legacy"))
def test_metrics_optional_model_groups_cannot_echo_dirty_keys(panel_client, monkeypatch, source):
    """Legacy status keys and optional task identity groups must not carry diagnostics."""
    client, ctx = panel_client
    metrics = _inputs(monkeypatch, ctx, TOKEN)
    monkeypatch.setattr(comms_router, "resolve_metrics_source", lambda: source)
    monkeypatch.setattr(comms_router, "_authority_metrics_payload", lambda *_args, **_kwargs: metrics)
    monkeypatch.setattr(comms_router, "collect_agent_success", lambda **_kwargs: {
        "tasks": 2, "by_agent": {TOKEN: {"tasks": 1}, "codex": {"tasks": 1}},
        "by_model": {TOKEN: {"tasks": 1}, "gpt-6.1-sol": {"tasks": 1, "success": {"percent": 100}}},
        "review_scores": {"by_author_model": {TOKEN: {"median": 9}}}, "window": {"since": TOKEN}})
    response = client.get("/api/comms/v1/metrics?include_agent_success=true")
    assert response.status_code == 200
    assert TOKEN not in response.text
    assert response.json()["agent_success"]["by_model"]["gpt-6.1-sol"]["success"]["percent"] == 100

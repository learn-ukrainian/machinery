"""``GET /api/ops/v1/test-slots``: the host test-slot pool, read-only and path-free (#10662)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lu_machinery.api.monitor_context import fixture_context
from lu_machinery.api.ops_router import router
from lu_machinery.common import test_slots

ROUTE = "/api/ops/v1/test-slots"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv(test_slots.ENABLE_ENV, "on")
    monkeypatch.setenv(test_slots.DIR_ENV, str(tmp_path / "pool"))
    monkeypatch.setenv(test_slots.COUNT_ENV, "3")
    monkeypatch.delenv(test_slots.HOLDER_ENV, raising=False)
    app = FastAPI()
    app.state.ctx = fixture_context(tmp_path)
    app.include_router(router, prefix="/api/ops")
    return TestClient(app)


def test_route_reports_holders_by_task_without_paths(client: TestClient, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(test_slots.TASK_ENV, "impl-7")
    with test_slots.acquire(2, kind="gate"):
        response = client.get(ROUTE)
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema"] == "test-slots.status.v1" and payload["enabled"] is True
    assert payload["observed_at"].endswith("Z")
    assert payload["age_s"] == 0.0
    assert payload["requested_at"].endswith("Z")
    assert (payload["pool_size"], payload["slots_in_use"], payload["slots_free"]) == (3, 2, 1)
    assert payload["queue_depth"] == 0
    [holder] = payload["holders"]
    assert (holder["task_id"], holder["kind"], holder["slots"], holder["pid"]) == ("impl-7", "gate", 2, os.getpid())
    assert str(tmp_path) not in json.dumps(payload) and "/" not in json.dumps(payload)
    assert client.get(ROUTE).json()["slots_in_use"] == 0


def test_route_reports_a_missing_or_disabled_pool_without_creating_it(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    payload = client.get(ROUTE).json()
    assert (payload["enabled"], payload["pool_exists"], payload["queue_depth"]) == (True, False, 0)
    monkeypatch.setenv(test_slots.ENABLE_ENV, "off")
    assert client.get(ROUTE).json()["enabled"] is False
    monkeypatch.setenv(test_slots.ENABLE_ENV, "maybe")
    broken = client.get(ROUTE)
    assert broken.status_code == 200
    failed = broken.json()
    assert failed["error"]["reason"] == "invalid_configuration"
    # A failed observation has no source freshness; the request time is separate.
    assert failed["observed_at"] is None
    assert failed["age_s"] is None
    assert failed["requested_at"].endswith("Z")
    assert not (tmp_path / "pool").exists()


def test_alerts_route_emits_only_allowlisted_fields(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Defect class: Alertmanager labels, annotations or URLs that name hosts or scrape targets leak to the page."""
    from lu_machinery.api import ops_router
    from lu_machinery.api.state_helpers import cache_invalidate

    alerts = [
        {"labels": {"alertname": "ApprovedNotLanded30m", "severity": "warning", "owner_lane": "cto",
                    "instance": "leak-instance:9100", "job": "leak-job", "repo": "public"},
         "annotations": {"summary": "An approved PR has not merged", "action": "Land it", "runbook": "leak-runbook"},
         "startsAt": "2030-01-01T00:00:00Z", "status": {"state": "active"},
         "generatorURL": "http://leak-host:9090/graph", "fingerprint": "leak-fingerprint",
         "receivers": [{"name": "leak-receiver"}]},
        {"labels": {"alertname": "Watchdog", "severity": "none"}, "annotations": {},
         "startsAt": "2030-01-01T00:00:00Z", "status": {"state": "active"}},
    ]

    class Response:
        status = 200

        def read(self, _limit):
            return json.dumps(alerts).encode()

    class Connection:
        def __init__(self, *_args, **_kwargs):
            pass

        def request(self, *_args, **_kwargs):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(ops_router.http.client, "HTTPConnection", Connection)
    cache_invalidate()
    response = client.get("/api/ops/v1/alerts")
    cache_invalidate()
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok" and body["heartbeat"] is True
    assert [row["alertname"] for row in body["alerts"]] == ["ApprovedNotLanded30m"]
    assert set(body["alerts"][0]) == {"alertname", "severity", "owner_lane", "repo", "summary", "action", "starts_at"}
    assert "leak" not in response.text
    # One malformed item makes the whole reading unknown, never a healthy "nothing firing".
    for bad in ({}, None, {"labels": {"alertname": "X"}}):
        alerts[:] = [bad]
        cache_invalidate()
        body = client.get("/api/ops/v1/alerts").json()
        assert (body["status"], body["reason"], body["alerts"]) == ("unknown", "alertmanager_invalid_response", [])
    cache_invalidate()

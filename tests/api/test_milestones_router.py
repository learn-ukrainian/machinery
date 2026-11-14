"""Milestone completion and private text exclusion at the HTTP boundary."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from lu_machinery.api import milestones_router as milestones
from lu_machinery.api import state_helpers
from lu_machinery.api.monitor_context import fixture_context
from lu_machinery.github_rest_cache import GitHubRestCache
from lu_machinery.orchestration.fleet_repos import FleetRepo


def test_milestone_percent_and_private_counts_only(tmp_path, monkeypatch):
    """Defect classes: wrong completion denominator and private milestone/issue text leakage."""
    repos = {
        "public": FleetRepo("public", "fixture/public", "public", "public-content", True),
        "private": FleetRepo("private", "fixture/private", "private", "private-infra"),
    }
    calls = []
    milestone = {
        "number": 1, "state": "open", "title": "Ship <progress>", "description": "First line\nSecond line",
        "due_on": "2030-01-01T00:00:00Z", "open_issues": 2, "closed_issues": 6,
    }

    def transport(path, headers, timeout):
        calls.append((path, headers))
        if headers.get("If-None-Match") == '"fixture"':
            return 304, {}, b""
        if "/milestones?" in path:
            row = {**milestone}
            if "fixture/private/" in path:
                row.update(title="private-title", description="private-description", html_url="private-link")
            body = [row]
        else:
            assert "fixture/private" not in path
            body = [
                {"number": number, "state": "open", "title": f"Work {number}", "milestone": {"number": 1},
                 "html_url": "javascript:untrusted", "labels": [{"name": "bug"}]}
                for number in (7, 8)
            ]
            # GitHub's issues collection also returns pull requests.
            body.append({**body[0], "number": 9, "pull_request": {}})
        return 200, {"etag": '"fixture"'}, json.dumps(body).encode()

    cache = GitHubRestCache(transport, identity="fixture")
    monkeypatch.setattr(milestones, "shared_cache", lambda: cache)
    monkeypatch.setattr(milestones, "load_fleet_repos", lambda: repos)
    monkeypatch.setattr(milestones, "schedule_cache_refresh",
                        lambda key, ttl, compute: state_helpers.cache_set(key, compute()))
    app = FastAPI()
    app.state.ctx = fixture_context(tmp_path)
    app.include_router(milestones.router, prefix="/api/milestones")
    with TestClient(app) as client:
        assert client.get("/api/milestones").json()["status"] == "pending"
        response = client.get("/api/milestones")
        data = response.json()
        assert response.status_code == 200
        assert data["status"] == "ok"
        assert data["refresh_seconds"] == 300
        assert float(response.headers["X-Snapshot-Age-S"]) >= 0
        public, private = data["repositories"]
        item = public["milestones"][0]
        assert (item["open_issues"], item["closed_issues"], item["percent_complete"]) == (2, 6, 75.0)
        assert item["description_first_line"] == "First line"
        assert item["due_on"] == "2030-01-01T00:00:00Z"
        assert [issue["number"] for issue in item["issues"]] == [7, 8]
        assert item["issues"][0] == {"number": 7, "title": "Work 7", "labels": ["bug"],
                                     "url": "https://github.com/fixture/public/issues/7"}
        assert private["milestones"] == [{
            "number": None, "title": None, "description_first_line": None, "due_on": None,
            "open_issues": 2, "closed_issues": 6, "percent_complete": 75.0, "issues_status": "withheld", "issues": [],
        }]
        assert "private-title" not in response.text
        assert "private-description" not in response.text
        assert "private-link" not in response.text
        assert len(calls) == 3  # One milestones list each; only the public repo needs issue details.
        assert client.get("/api/milestones").json()["repositories"] == data["repositories"]
        assert len(calls) == 3  # Warm page polls never reach GitHub.

    # The same representative artifact proves validators are reused on the next collection.
    assert milestones.collect_milestones()["status"] == "ok"
    assert len(calls) == 6
    assert all(headers == {"If-None-Match": '"fixture"'} for _, headers in calls[3:])

    page = Path(milestones.__file__).with_name("dashboards").joinpath("index.html").read_text()
    script = page.split("<script>", 1)[1].split("</script>", 1)[0].rsplit("refreshAll();", 1)[0]
    rendered = subprocess.run(
        ["node", "-e", 'global.document = {querySelectorAll: () => []};\n' + script
         + '\nprocess.stdout.write(renderMilestones(' + json.dumps(data) + '));'],
        capture_output=True, text=True, check=True, timeout=10,
    ).stdout
    assert "75% complete" in rendered
    assert 'value="75"' in rendered
    assert "Ship &lt;progress&gt;" in rendered
    assert "Work 7" in rendered
    assert "private-title" not in rendered
    assert "Private milestone" in rendered

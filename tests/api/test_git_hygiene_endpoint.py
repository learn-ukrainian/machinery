import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from lu_machinery.api import delegate_router, git_hygiene_router
from lu_machinery.api import state_helpers
from lu_machinery.api.main import app
from lu_machinery.api.monitor_context import fixture_context

client = TestClient(app, raise_server_exceptions=False)


def _git(repo: Path, *args: str) -> str:
    env = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX"):
        env.pop(key, None)
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        env=env,
        text=True,
        check=True,
        timeout=30,
    )
    return proc.stdout


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _init_repo(repo: Path) -> None:
    _git(repo, "init")
    _git(repo, "config", "user.email", "codex@example.invalid")
    _git(repo, "config", "user.name", "Codex Test")
    _write(
        repo / "docs" / "best-practices" / "git-hygiene.md",
        """# Git Hygiene

## Exemption paths

- `wiki/**` - wiki builder output
- `registry/corpus_audit/draft_tickets/*.md` - draft tickets

## Other section
""",
    )


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)


def _dirty_repo_with_all_buckets(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    _write(repo / "stale.py", "def old_name():\n    return 'old'\n")
    _write(repo / "wip.py", "def stable():\n    return 'stable'\n")
    for name in ("a.txt", "b.txt", "c.txt"):
        _write(repo / "feature" / "remove" / name, f"{name}\n")
    _commit_all(repo, "initial")

    for idx in range(20):
        _write(repo / "fillers" / f"{idx:02d}.txt", f"{idx}\n")
        _commit_all(repo, f"filler {idx}")

    _write(repo / "stale.py", "def new_name():\n    return 'new'\n")
    _commit_all(repo, "update stale file")

    _write(repo / "stale.py", "def old_name():\n    return 'old'\n")
    _write(repo / "wip.py", "def stable():\n    return 'stable'\n\n\ndef local_wip():\n    return 'wip'\n")
    for name in ("a.txt", "b.txt", "c.txt"):
        (repo / "feature" / "remove" / name).unlink()
    _write(repo / "scratch" / "todo.txt", "local note\n")
    _write(repo / "wiki" / "draft.md", "wiki output\n")
    _write(repo / "registry" / "corpus_audit" / "draft_tickets" / "ticket.md", "draft\n")

    return repo


def test_hygiene_classifies_each_dirty_bucket(tmp_path: Path) -> None:
    repo = _dirty_repo_with_all_buckets(tmp_path)

    result = git_hygiene_router.compute_git_hygiene(repo)

    assert result["health"] == "dirty"
    assert result["dirty_total"] == 6
    assert result["exempt"]["wiki"] == 1
    assert result["exempt"]["draft_tickets"] == 1
    assert result["exempt"]["total"] == 2
    assert result["buckets"]["stale_behind_main"]["files"] == ["stale.py"]
    assert result["buckets"]["real_wip"]["files"] == ["wip.py"]
    assert result["buckets"]["untracked_unexempted"]["files"] == ["scratch/todo.txt"]
    assert result["buckets"]["intentional_deletions"]["count"] == 3
    assert result["buckets"]["intentional_deletions"]["pattern"] == "feature/remove/*"
    assert {item["action"] for item in result["suggestions"]} == {
        "restore_to_head",
        "stash_wip",
        "gitignore_pattern",
        "commit_deletions",
    }


def test_hygiene_endpoint_is_registered_and_uses_project_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = _dirty_repo_with_all_buckets(tmp_path)
    ctx = app.state.ctx.with_roots(live_repo_root=repo, project_root=repo)
    monkeypatch.setattr(app.state, "ctx", ctx)

    response = client.get("/api/git/hygiene")

    assert response.status_code == 200
    body = response.json()
    assert body["health"] == "dirty"
    assert body["buckets"]["real_wip"]["files"] == ["wip.py"]


def test_hygiene_is_clean_when_all_dirty_files_are_exempt(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _write(repo / "README.md", "clean base\n")
    _commit_all(repo, "initial")
    _write(repo / "wiki" / "draft.md", "wiki output\n")
    _write(repo / "registry" / "corpus_audit" / "draft_tickets" / "ticket.md", "draft\n")

    result = git_hygiene_router.compute_git_hygiene(repo)

    assert result["health"] == "clean"
    assert result["dirty_total"] == 0
    assert result["exempt"]["total"] == 2
    assert all(bucket["count"] == 0 for bucket in result["buckets"].values())


def test_hygiene_marks_deleted_python_as_blocked_imports(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _write(repo / "scripts" / "common" / "text_utils.py", "def normalize():\n    return None\n")
    _commit_all(repo, "initial")
    (repo / "scripts" / "common" / "text_utils.py").unlink()

    result = git_hygiene_router.compute_git_hygiene(repo)

    assert result["health"] == "blocked_imports"
    assert result["dirty_total"] == 1


def _graphql_result(monkeypatch, returncode: int, stdout: str) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(
        git_hygiene_router.github_client,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=returncode, stdout=stdout, stderr=""),
    )


def test_pull_requests_route_hides_private_repo_titles_and_branches(monkeypatch, tmp_path: Path) -> None:
    """Defect class: a private repository's PR titles or branch names leak through the Monitor."""

    node = {"number": 7, "title": "secret-title", "url": "https://github.com/o/r/pull/7", "isDraft": False,
            "createdAt": "2030-01-01T00:00:00Z", "headRefName": "codex/secret-branch", "mergeStateStatus": "CLEAN",
            "mergeQueueEntry": {"position": 1, "state": "QUEUED"},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"},
                                              "status": {"context": {"state": "SUCCESS"}}}}]}}
    repos = {"r0": {"isPrivate": False, "pullRequests": {"totalCount": 1, "nodes": [node]}},
             "r1": {"isPrivate": True, "pullRequests": {"totalCount": 1, "nodes": [node]}},
             "r2": {"pullRequests": {"totalCount": 1, "nodes": [node]}}}
    _graphql_result(monkeypatch, 0, json.dumps({"data": repos}))
    payload = git_hygiene_router.compute_pull_requests(tmp_path)
    public, private, unstated = payload["repositories"]
    assert "title" not in public["pull_requests"][0]
    # An unrecorded branch suffix is withheld on a public repository too.
    assert public["pull_requests"][0]["task_reference"] is None
    assert "secret-branch" not in json.dumps(public["pull_requests"])
    assert public["pull_requests"][0]["queue"] == {"position": 1, "state": "queued"}
    for repo in (private, unstated):
        assert repo["private"] is True
        assert "title" not in repo["pull_requests"][0]
        assert repo["pull_requests"][0]["task_reference"] is None
        assert "secret" not in json.dumps(repo)


def _heading_repo(*, loaded: bool) -> dict:
    if loaded:
        return {"isPrivate": False, "pullRequests": {"totalCount": 0, "nodes": []}}
    return {"isPrivate": False}


def _heading_payload(*loaded: bool) -> dict:
    return {f"r{index}": _heading_repo(loaded=flag) for index, flag in enumerate(loaded)}


def test_pull_request_heading_uses_loaded_repositories(monkeypatch, tmp_path: Path) -> None:
    """Defect class: one unreadable repository marks the pull-request heading unavailable and drops the age of repositories that loaded."""
    state_helpers.cache_invalidate()
    app = FastAPI()
    app.state.ctx = fixture_context(tmp_path)
    app.include_router(git_hygiene_router.router, prefix="/api/git")
    payload: dict = {}
    call = {"code": 0}

    def fake_run(*_args, **_kwargs):
        return SimpleNamespace(returncode=call["code"], stdout=json.dumps({"data": payload}), stderr="")

    monkeypatch.setattr(git_hygiene_router.github_client, "run", fake_run)
    client = TestClient(app)

    def heading(loaded: tuple[bool, ...]) -> tuple[object, dict]:
        call["code"] = 0
        payload.clear()
        payload.update(_heading_payload(*loaded))
        state_helpers.cache_invalidate()
        response = client.get("/api/git/pull-requests")
        assert response.status_code == 200
        return response.headers, response.json()

    headers, body = heading((True, True, True))
    assert headers["X-Snapshot-State"] == "fresh"
    assert float(headers["X-Snapshot-Age-S"]) >= 0
    assert headers["X-Snapshot-Stale-After-S"] == "210.0"
    assert [repo["status"] for repo in body["repositories"]] == ["ok", "ok", "ok"]

    headers, body = heading((True, False, True))
    assert headers["X-Snapshot-State"] == "stale"
    assert float(headers["X-Snapshot-Age-S"]) >= 0
    assert [repo["status"] for repo in body["repositories"]] == ["ok", "unknown", "ok"]
    assert body["repositories"][1]["reason"] == "not_readable"

    headers, body = heading((False, False, False))
    assert (headers["X-Snapshot-State"], headers["X-Snapshot-Age-S"]) == ("unavailable", "unknown")
    assert [repo["status"] for repo in body["repositories"]] == ["unknown", "unknown", "unknown"]

    call["code"] = 1
    state_helpers.cache_invalidate()
    failed = client.get("/api/git/pull-requests")
    assert failed.status_code == 200
    assert (failed.headers["X-Snapshot-State"], failed.headers["X-Snapshot-Age-S"]) == ("unavailable", "unknown")
    assert failed.json()["status"] == "unknown"
    assert failed.json()["repositories"] == []
    state_helpers.cache_invalidate()


def test_pull_requests_route_reports_unknown_when_github_fails(monkeypatch, tmp_path: Path) -> None:
    """Defect class: a failed or malformed GitHub read renders as "no open PRs" or crashes the route."""
    _graphql_result(monkeypatch, 1, '{"error": "github_rate_limited"}')
    payload = git_hygiene_router.compute_pull_requests(tmp_path)
    assert payload["status"] == "unknown"
    assert payload["reason"] == "github_unavailable"
    assert payload["repositories"] == []
    _graphql_result(monkeypatch, 0, "not json")
    assert git_hygiene_router.compute_pull_requests(tmp_path)["reason"] == "github_invalid_response"
    # GitHub's nullable commit nodes and contexts read as "no CI yet", never an exception.
    node = {"number": 1, "commits": {"nodes": [None]}}
    nulls = {"number": 2, "commits": {"nodes": [{"commit": {"statusCheckRollup": None, "status": None}}]}}
    repos = {f"r{i}": {"isPrivate": False, "pullRequests": {"totalCount": 2, "nodes": [node, nulls]}} for i in range(3)}
    _graphql_result(monkeypatch, 0, json.dumps({"data": repos}))
    rows = git_hygiene_router.compute_pull_requests(tmp_path)["repositories"][0]["pull_requests"]
    assert [(row["number"], row["ci"], row["review"]) for row in rows] == [(1, None, None), (2, None, None)]
    # A null PR node or a non-list connection marks that repository unknown, never "fewer PRs".
    repos["r1"] = {"isPrivate": False, "pullRequests": {"totalCount": 2, "nodes": [node, None]}}
    repos["r2"] = {"isPrivate": False, "pullRequests": {"totalCount": 2, "nodes": None}}
    _graphql_result(monkeypatch, 0, json.dumps({"data": repos}))
    _ok, null_node, no_list = git_hygiene_router.compute_pull_requests(tmp_path)["repositories"]
    for repo in (null_node, no_list):
        assert (repo["status"], repo["reason"], repo["pull_requests"]) == ("unknown", "github_invalid_response", [])


# Untrusted identifier text. These strings are filename-safe under the old check.
_DOTTED = "host.fixture.invalid"
_ADDRESS = "192.0.2.17"
_ARBITRARY = "not a task"
_UPPER = "Kept-Task"
_UNDERSCORE = "has_underscore"
_OVER_LONG = "a" * 65
_UNKNOWN = "no-such-task"
_MISMATCH_NAME = "looks-valid"
_MISMATCH_RECORDED = "other-task"
_KEPT = "kept-task"
_KEPT_LONG = "k" * 64
_KEPT_ARCHIVED = "kept-archived"


def _open_pr(branch: str, number: int) -> dict:
    return {
        "number": number, "isDraft": False, "createdAt": "2030-01-01T00:00:00Z",
        "headRefName": branch, "mergeStateStatus": "CLEAN",
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"},
                                          "status": {"context": {"state": "SUCCESS"}}}}]},
    }


def _graphql_repos(*repo_nodes: list[dict]) -> str:
    repos = {
        f"r{index}": {"isPrivate": False, "pullRequests": {"totalCount": len(nodes), "nodes": nodes}}
        for index, nodes in enumerate(repo_nodes)
    }
    return json.dumps({"data": repos})


def _write_task_record(tasks: Path, task_id: str, *, recorded: str | None = None) -> None:
    tasks.mkdir(parents=True, exist_ok=True)
    body = {"schema_version": 2, "task_id": task_id if recorded is None else recorded, "status": "running"}
    (tasks / f"{task_id}.json").write_text(json.dumps(body), encoding="utf-8")


def _identifier_client(tmp_path: Path, monkeypatch, repos: str, tasks: dict):
    """Serve the three routes with task records outside the project batch_state default."""
    state_helpers.cache_invalidate()
    store = tmp_path / "dispatcher-store"
    ctx = fixture_context(tmp_path).with_roots(batch_state_dir=store)
    panel = FastAPI()
    panel.state.ctx = ctx
    panel.include_router(delegate_router.router, prefix="/api/delegate")
    panel.include_router(git_hygiene_router.router, prefix="/api/git")
    monkeypatch.setattr(
        git_hygiene_router.github_client, "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=repos, stderr=""),
    )
    snapshot = SimpleNamespace(read=lambda: (tasks, 0.0))
    monkeypatch.setattr(delegate_router, "active_snapshot", lambda _ctx: snapshot)
    monkeypatch.setattr(delegate_router, "recent_snapshot", lambda _ctx: snapshot)
    return TestClient(panel), store / "tasks"


def test_identifier_routes_withhold_untrusted_text(tmp_path, monkeypatch):
    """Defect class: identifier fields accept untrusted text and echo it on the public routes."""
    refused = (_DOTTED, _ADDRESS, _ARBITRARY, _UPPER, _UNDERSCORE, _OVER_LONG)
    nodes = [_open_pr(f"contributor/{value}", number) for number, value in enumerate(refused, start=1)]
    nodes.append(_open_pr(f"contributor/{_UNKNOWN}", 7))
    nodes.append(_open_pr(f"contributor/{_MISMATCH_NAME}", 8))
    attention = {"task_id": _DOTTED, "agent": "codex", "model": "gpt-6.1-sol", "status": "needs_finalize"}
    listed = [{"task_id": value, "agent": "codex", "model": "gpt-6.1-sol", "status": "running"} for value in refused]
    listed.append({"task_id": _UNKNOWN, "agent": "codex", "model": "gpt-6.1-sol", "status": "running"})
    payload = {"total": len(listed), "tasks": listed, "needs_attention": {"total": 1, "tasks": [attention]}}
    client, tasks = _identifier_client(tmp_path, monkeypatch, _graphql_repos(nodes, [], []), payload)
    _write_task_record(tasks, _MISMATCH_NAME, recorded=_MISMATCH_RECORDED)

    pulls = client.get("/api/git/pull-requests")
    assert pulls.status_code == 200
    rows = pulls.json()["repositories"][0]["pull_requests"]
    assert [row["task_reference"] for row in rows] == [None] * len(nodes)
    assert all("task_reference" in row for row in rows)
    body = pulls.text
    for value in (*refused, _UNKNOWN, _MISMATCH_NAME, _MISMATCH_RECORDED):
        assert value not in body

    for route in ("/api/delegate/active", "/api/delegate/recent"):
        response = client.get(route)
        assert response.status_code == 200
        data = response.json()
        assert [row["task_id"] for row in data["tasks"]] == [*([None] * len(refused)), _UNKNOWN]
        assert data["needs_attention"]["tasks"][0]["task_id"] is None
        assert _DOTTED not in response.text
        assert _OVER_LONG not in response.text


def test_identifier_routes_keep_a_recorded_task_id(tmp_path, monkeypatch):
    """Defect class: a real dispatcher task id is dropped when untrusted text is withheld."""
    kept = (_KEPT, _KEPT_LONG, _KEPT_ARCHIVED)
    nodes = [_open_pr(f"cursor/{task_id}", number) for number, task_id in enumerate(kept, start=1)]
    private = [_open_pr(f"cursor/{_KEPT}", 9)]
    listed = [{"task_id": task_id, "agent": "codex", "model": "gpt-6.1-sol", "status": "running"} for task_id in kept]
    payload = {"total": len(listed), "tasks": listed}
    repos = {
        "r0": {"isPrivate": False, "pullRequests": {"totalCount": len(nodes), "nodes": nodes}},
        "r1": {"isPrivate": True, "pullRequests": {"totalCount": 1, "nodes": private}},
        "r2": {"isPrivate": False, "pullRequests": {"totalCount": 0, "nodes": []}},
    }
    client, tasks = _identifier_client(tmp_path, monkeypatch, json.dumps({"data": repos}), payload)
    _write_task_record(tasks, _KEPT)
    _write_task_record(tasks, _KEPT_LONG)
    _write_task_record(tasks / "archive", _KEPT_ARCHIVED)

    pulls = client.get("/api/git/pull-requests")
    assert pulls.status_code == 200
    public, hidden, empty = pulls.json()["repositories"]
    assert [row["task_reference"] for row in public["pull_requests"]] == list(kept)
    assert hidden["pull_requests"][0]["task_reference"] is None
    assert empty["pull_requests"] == []
    assert "cursor/" not in pulls.text
    assert _KEPT not in json.dumps(hidden)
    # The route reads the context task store, not the empty project default.
    missed = git_hygiene_router.compute_pull_requests(tmp_path)
    assert missed["repositories"][0]["pull_requests"][0]["task_reference"] is None

    active = client.get("/api/delegate/active")
    recent = client.get("/api/delegate/recent")
    assert active.status_code == recent.status_code == 200
    assert [row["task_id"] for row in active.json()["tasks"]] == list(kept)
    assert [row["task_id"] for row in recent.json()["tasks"]] == list(kept)
    # A second context with the same project root and a different task store
    # must not reuse the cached authorization.
    other = FastAPI()
    other.state.ctx = fixture_context(tmp_path).with_roots(batch_state_dir=tmp_path / "other-store")
    other.include_router(git_hygiene_router.router, prefix="/api/git")
    second = TestClient(other).get("/api/git/pull-requests")
    assert second.status_code == 200
    assert second.json()["repositories"][0]["pull_requests"][0]["task_reference"] is None
    assert _KEPT not in second.text

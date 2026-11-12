import json
import os
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from lu_machinery.api import git_hygiene_router
from lu_machinery.api.main import app

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
    assert public["pull_requests"][0]["task_reference"] == "secret-branch"
    assert public["pull_requests"][0]["queue"] == {"position": 1, "state": "queued"}
    for repo in (private, unstated):
        assert repo["private"] is True
        assert "title" not in repo["pull_requests"][0]
        assert repo["pull_requests"][0]["task_reference"] is None
        assert "secret" not in json.dumps(repo)


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

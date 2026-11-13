"""Hermetic tests for settle-stale / archive / restore (#8625).

Every test runs against a tmp task directory, a tmp git repository with a
bare ``origin``, and a fake REST pull pager; nothing reads the real
``batch_state/tasks`` or reaches GitHub.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from lu_machinery.dispatcher import store
from lu_machinery.orchestration import dead_worker_state, task_record_store, worktree_claims
from lu_machinery.orchestration import stale_task_records as str_mod
from tests.helpers.product_tree import configured_roots as configured_roots

pytestmark = pytest.mark.usefixtures("configured_roots")

SLUG = "owner/repo"
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
OLD_FINISH = NOW - timedelta(days=10)
OLD_START = OLD_FINISH - timedelta(hours=1)


def _git_env() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GIT_", "PRE_COMMIT")) and key != "AGENT_NO_MERGE"
    }
    env.update(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
    return env


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=_git_env(), timeout=30
    ).stdout.strip()


def _orphan_commit(repo: Path, name: str) -> str:
    """Commit on a throwaway branch, then delete the branch: no ref holds the commit."""
    _git(repo, "checkout", "-b", f"tmp/{name}")
    (repo / f"{name}.txt").write_text(f"{name}\n")
    _git(repo, "add", f"{name}.txt")
    _git(repo, "commit", "-m", name)
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    _git(repo, "branch", "-D", f"tmp/{name}")
    return sha


def _auto_finalized(sha: str) -> dict[str, Any]:
    return {"ok": False, "commit_sha": sha, "pr_url": None, "error": "push failed", "changed_files": ["x"]}


SCAN_KEY = "test"
SCAN_PREFIX = f"refs/lu-stale-scan/{SCAN_KEY}"


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    monkeypatch.setattr(worktree_claims, "repository_lock_dir", lambda _root: tmp_path / "lu-worktree-locks")
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path / "tasks"))
    # The tool drops every GIT_CONFIG* variable, so git reads $HOME's config: keep it empty.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"):
        monkeypatch.delenv(key, raising=False)
    # The allowlisted fetch URL of SLUG is the tmp bare repository, not GitHub.
    target = str_mod.FetchTarget(key=SCAN_KEY, url=str(tmp_path / "origin.git"))
    monkeypatch.setattr(str_mod, "fleet_fetch_target", lambda slug: target if slug == SLUG else None)


@pytest.fixture
def repo(tmp_path) -> Path:
    """A checkout whose ``origin`` is a local bare repository."""
    origin = tmp_path / "origin.git"
    checkout = tmp_path / "checkout"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "init", "-b", "main", str(checkout))
    (checkout / "README").write_text("base\n")
    _git(checkout, "add", "README")
    _git(checkout, "commit", "-m", "base")
    _git(checkout, "remote", "add", "origin", str(origin))
    _git(checkout, "push", "-u", "origin", "main")
    return checkout


@pytest.fixture
def tasks_dir(tmp_path) -> Path:
    path = tmp_path / "tasks"
    path.mkdir()
    return path


def _record(tasks_dir: Path, name: str, **fields: Any) -> Path:
    record = {
        "task_id": name,
        "agent": "codex",
        "mode": "danger",
        "status": "needs_finalize",
        "needs_finalize": True,
        "repository": SLUG,
        "worktree_branch": f"codex/{name}",
        "worktree_base": "main",
        "worktree_path": str(tasks_dir.parent / "gone" / name),
        "started_at": OLD_START.isoformat(),
        "finished_at": OLD_FINISH.isoformat(),
        "returncode": 0,
        "commits_ahead": 0,
        "worktree_dirty_on_exit": False,
    }
    record.update(fields)
    path = tasks_dir / f"{name}.json"
    path.write_text(json.dumps(record, indent=2))
    return path


def _merged(
    branch: str, number: int, merged_at: datetime, *, slug: str = SLUG, head_sha: str | None = None
) -> dict[str, Any]:
    return {
        "number": number,
        "html_url": f"https://github.com/{slug}/pull/{number}",
        "merged_at": merged_at.isoformat(),
        "updated_at": merged_at.isoformat(),
        "merge_commit_sha": f"{number + 0xABC:040x}",
        "head": {"ref": branch, "sha": head_sha or f"{number:040x}", "repo": {"full_name": slug}},
    }


class FakePager:
    def __init__(self, pulls: list[dict[str, Any]]):
        self.pulls = pulls
        self.calls: list[tuple[str, int]] = []

    def __call__(self, slug: str, page: int) -> list[dict[str, Any]]:
        self.calls.append((slug, page))
        start = (page - 1) * str_mod.PR_PAGE_SIZE
        return self.pulls[start : start + str_mod.PR_PAGE_SIZE]


def _snapshot(directory: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(path.relative_to(directory)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


@pytest.fixture
def mixed(tmp_path, repo, tasks_dir):
    """One record of every class, plus a young record that must be left alone."""
    # A: branch published on origin, local branch deleted.
    _git(repo, "branch", "codex/published")
    _git(repo, "push", "origin", "codex/published")
    _git(repo, "branch", "-D", "codex/published")
    _record(tasks_dir, "published")
    # B: local branch with an unpushed commit, worktree gone.
    _git(repo, "checkout", "-b", "codex/local-only")
    (repo / "work.txt").write_text("unpushed\n")
    _git(repo, "add", "work.txt")
    _git(repo, "commit", "-m", "unpushed")
    _git(repo, "checkout", "main")
    _record(tasks_dir, "local-only", commits_ahead=1)
    # B: dirty detached worktree, no branch anywhere.
    dirty_wt = tmp_path / "wt-dirty"
    _git(repo, "worktree", "add", "--detach", str(dirty_wt))
    (dirty_wt / "scratch.txt").write_text("uncommitted\n")
    _record(tasks_dir, "dirty", worktree_path=str(dirty_wt), worktree_branch="codex/dirty")
    # D: clean detached worktree, no branch anywhere.
    clean_wt = tmp_path / "wt-clean"
    _git(repo, "worktree", "add", "--detach", str(clean_wt))
    _record(tasks_dir, "clean-detached", worktree_path=str(clean_wt), worktree_branch="codex/clean-detached")
    # D: repository unknown.
    _record(tasks_dir, "no-repo", repository=None)
    # C: a recorded commit no ref holds, merged by PR #7 -> done; clean no-commit
    # exit -> no_deliverable; crash -> failed.
    merged_sha = _orphan_commit(repo, "merged")
    _record(
        tasks_dir, "merged", commits_ahead=0, worktree_dirty_on_exit=True, auto_finalize=_auto_finalized(merged_sha)
    )
    _record(tasks_dir, "no-commits")
    _record(tasks_dir, "crashed", returncode=1)
    # C: a PR for the same branch merged BEFORE the task started is not its deliverable.
    stale_sha = _orphan_commit(repo, "stale-pr")
    _record(tasks_dir, "stale-pr", worktree_dirty_on_exit=True, auto_finalize=_auto_finalized(stale_sha))
    # Young needs_finalize record: below --min-age-days.
    _record(tasks_dir, "young", finished_at=(NOW - timedelta(days=1)).isoformat())
    # Terminal record: not a settle candidate.
    _record(tasks_dir, "already-done", status="done")
    pager = FakePager(
        [
            _merged("codex/merged", 7, OLD_FINISH + timedelta(hours=2), head_sha=merged_sha),
            _merged("codex/merged", 8, OLD_FINISH + timedelta(hours=1), slug="fork/repo", head_sha=merged_sha),
            _merged("codex/stale-pr", 5, OLD_START - timedelta(days=3)),
        ]
    )
    return {"repo": repo, "pager": pager, "merged_sha": merged_sha}


def _run(tasks_dir: Path, mixed: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return str_mod.settle_stale(
        tasks_dir,
        repo_checkouts={SLUG: mixed["repo"]},
        pager=mixed["pager"],
        open_pager=FakePager([]),
        now=NOW,
        **kwargs,
    )


def _by_file(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["file"]: row for row in report["records"]}


def test_classifies_every_class_like_the_report(tasks_dir, mixed):
    report = _run(tasks_dir, mixed)
    rows = _by_file(report)

    assert report["classes"] == {"A": 0, "B": 1, "C": 6, "D": 2}
    assert report["younger_left_alone"] == 1
    assert "young.json" not in rows and "already-done.json" not in rows
    assert rows["published.json"]["class"] == "C"
    assert rows["published.json"]["evidence"]["branch_on_origin"] is True
    assert rows["local-only.json"]["class"] == "C"
    assert rows["local-only.json"]["evidence"]["local_commits_ahead_of_base"] == 1
    assert rows["dirty.json"]["class"] == "B"
    assert rows["dirty.json"]["evidence"]["worktree_dirty"] is True
    assert rows["clean-detached.json"]["class"] == "D"
    assert rows["no-repo.json"]["class"] == "D"
    assert rows["merged.json"]["outcome"] == "done"
    assert rows["merged.json"]["merged_pr"]["number"] == 7
    assert rows["no-commits.json"]["outcome"] == "no_deliverable"
    assert rows["crashed.json"]["outcome"] == "failed"
    assert rows["stale-pr.json"]["outcome"] == "failed"
    assert report["outcomes"] == {"done": 1, "no_deliverable": 1, "failed": 4}
    assert report["actions"] == {"would_settle": 6, "report": 3}
    assert mixed["pager"].calls == [(SLUG, 1)]


def test_dry_run_writes_nothing(tasks_dir, mixed):
    before = _snapshot(tasks_dir)
    report = _run(tasks_dir, mixed)
    assert report["mode"] == "dry-run"
    assert report["actions"]["would_settle"] == 6
    assert _snapshot(tasks_dir) == before


def test_apply_settles_class_c_and_leaves_other_classes(tasks_dir, mixed):
    """Defect class: settling a needs_finalize record rewrites a class that still has work."""
    untouched = ["dirty.json", "clean-detached.json", "no-repo.json", "young.json"]
    before = {name: (tasks_dir / name).read_bytes() for name in untouched}
    report = _run(tasks_dir, mixed, apply=True)
    assert report["actions"] == {"settled": 6, "report": 3}
    for name in untouched:
        assert (tasks_dir / name).read_bytes() == before[name], name
    merged = json.loads((tasks_dir / "merged.json").read_text())
    assert merged["status"] == "done"
    assert merged["needs_finalize"] is False
    assert merged["settled_by"] == "settle-stale"
    assert merged["settled_at"] == NOW.isoformat()
    assert "PR #7" in merged["settle_reason"]
    no_commits = json.loads((tasks_dir / "no-commits.json").read_text())
    assert no_commits["status"] == "no_deliverable"
    assert no_commits["no_deliverable_reason"] == "unclassified_error"
    assert no_commits["settle_reason"] in (tasks_dir / "no-commits.diag").read_text(encoding="utf-8")
    assert json.loads((tasks_dir / "crashed.json").read_text())["status"] == "failed"
    assert all(
        json.loads((tasks_dir / name).read_text())["status"] in worktree_claims.RELEASED_TASK_STATUSES
        for name in ("merged.json", "no-commits.json", "crashed.json", "stale-pr.json")
    )


def test_pull_list_failure_settles_nothing(tasks_dir, mixed):
    """Defect class: a missing pull list settles an orphaned record without its evidence."""

    def broken(_slug, _page):
        raise RuntimeError("gh api failed: HTTP 502")

    before = _snapshot(tasks_dir)
    report = str_mod.settle_stale(tasks_dir, repo_checkouts={SLUG: mixed["repo"]}, pager=broken, open_pager=FakePager([]), now=NOW, apply=True)
    assert report["classes"]["C"] == 4
    assert "settled" not in report["actions"]
    assert all("pull request list unavailable" in row["skip_reason"] for row in report["records"] if row["class"] == "C")
    assert _snapshot(tasks_dir) == before


def test_rescue_ref_settles_failed_and_the_ref_stays(tasks_dir, repo):
    """Defect class: a reaper pin is treated as live unpushed work, or settlement deletes the pin."""
    sha = _orphan_commit(repo, "rescued")
    _git(repo, "update-ref", f"refs/reaper-rescue/stamp/rescued-{sha[:12]}", sha)
    _record(tasks_dir, "rescued", commits_ahead=1, worktree_dirty_on_exit=True, auto_finalize=_auto_finalized(sha))
    live = _orphan_commit(repo, "live-branch")
    _git(repo, "branch", "codex/live-branch", live)
    _record(tasks_dir, "live-branch", commits_ahead=1, worktree_dirty_on_exit=True, auto_finalize=_auto_finalized(live))
    report = _settle(tasks_dir, repo, [], apply=True)
    rows = _by_file(report)
    assert rows["rescued.json"]["outcome"] == "failed"
    assert rows["rescued.json"]["action"] == "settled"
    assert rows["live-branch.json"]["class"] == "C"
    assert rows["live-branch.json"]["action"] == "settled"
    rescued = json.loads((tasks_dir / "rescued.json").read_text())
    assert rescued["status"] == "failed"
    assert rescued["failure_reason"] == "unpushed work - needs rescue"
    assert _git(repo, "rev-parse", f"refs/reaper-rescue/stamp/rescued-{sha[:12]}") == sha
    assert json.loads((tasks_dir / "live-branch.json").read_text())["status"] == "failed"
    assert _git(repo, "rev-parse", "codex/live-branch") == live


def test_base_sha_is_not_tied_to_a_parent_pull_request(tasks_dir, repo):
    """Defect class: a stacked task whose final head is its base is settled done via the parent's pull request."""
    parent = _orphan_commit(repo, "parent")
    _record(
        tasks_dir,
        "stacked",
        commits_ahead=1,
        final_branch_head_commit=parent,
        worktree_base_sha=parent,
        worktree_branch="codex/stacked",
    )
    report = _settle(
        tasks_dir,
        repo,
        [_merged("codex/parent", 41, OLD_FINISH + timedelta(hours=1), head_sha=parent)],
        apply=True,
    )
    row = _by_file(report)["stacked.json"]
    assert row["class"] == "C"
    assert row["outcome"] == "failed"
    assert "merged_pr" not in row
    assert json.loads((tasks_dir / "stacked.json").read_text())["status"] == "failed"


def test_rescue_ref_with_a_merged_pull_request_is_done(tasks_dir, repo):
    """Defect class: squash-merged work pinned by the reaper is settled failed as unpushed."""
    sha = _orphan_commit(repo, "landed")
    _git(repo, "update-ref", f"refs/reaper-rescue/stamp/landed-{sha[:12]}", sha)
    _record(tasks_dir, "landed", commits_ahead=1, worktree_dirty_on_exit=True, auto_finalize=_auto_finalized(sha))
    report = _settle(
        tasks_dir,
        repo,
        [_merged("codex/landed", 9, OLD_FINISH + timedelta(hours=1), head_sha=sha)],
        apply=True,
    )
    row = _by_file(report)["landed.json"]
    assert row["outcome"] == "done"
    assert row["action"] == "settled"
    assert json.loads((tasks_dir / "landed.json").read_text())["status"] == "done"
    assert _git(repo, "rev-parse", f"refs/reaper-rescue/stamp/landed-{sha[:12]}") == sha


def test_rescue_ref_is_not_written_when_the_pull_list_fails(tasks_dir, repo):
    """Defect class: a rescue pin is settled when the pull list that could show it landed is missing."""
    sha = _orphan_commit(repo, "unlisted")
    _git(repo, "update-ref", f"refs/reaper-rescue/stamp/unlisted-{sha[:12]}", sha)
    _record(tasks_dir, "unlisted", commits_ahead=1, worktree_dirty_on_exit=True, auto_finalize=_auto_finalized(sha))

    def broken(_slug, _page):
        raise RuntimeError("gh api failed: HTTP 502")

    report = str_mod.settle_stale(tasks_dir, repo_checkouts={SLUG: repo}, pager=broken, open_pager=FakePager([]), now=NOW, apply=True)
    row = _by_file(report)["unlisted.json"]
    assert row["class"] == "C"
    assert "outcome" not in row
    assert "pull request list unavailable" in row["skip_reason"]
    assert json.loads((tasks_dir / "unlisted.json").read_text())["status"] == "needs_finalize"
    assert _git(repo, "rev-parse", f"refs/reaper-rescue/stamp/unlisted-{sha[:12]}") == sha


def test_detached_worktree_head_with_a_rescue_ref_stays_class_b(tasks_dir, repo):
    """Defect class: a detached checkout that still holds a rescued commit, including one commit later, is settled."""
    sha = _orphan_commit(repo, "detached")
    _git(repo, "update-ref", f"refs/reaper-rescue/stamp/detached-{sha[:12]}", sha)
    worktree = repo.parent / "detached-wt-ä"
    _git(repo, "worktree", "add", "--detach", str(worktree), sha)
    (worktree / "newer.txt").write_text("newer\n")
    _git(worktree, "add", "newer.txt")
    _git(worktree, "commit", "-m", "newer")
    _record(tasks_dir, "detached", commits_ahead=1, worktree_dirty_on_exit=True, auto_finalize=_auto_finalized(sha))
    report = _settle(tasks_dir, repo, [], apply=True)
    row = _by_file(report)["detached.json"]
    assert row["class"] == "B"
    assert row["action"] == "report"
    assert json.loads((tasks_dir / "detached.json").read_text())["status"] == "needs_finalize"
    assert _git(repo, "rev-parse", f"refs/reaper-rescue/stamp/detached-{sha[:12]}") == sha


def test_record_changed_since_classification_is_skipped(tasks_dir, mixed):
    """Defect class: settlement overwrites a record a live writer changed after classification."""
    target = tasks_dir / "crashed.json"

    def live_writer(_candidates):
        record = json.loads(target.read_text())
        record["heartbeat"] = "live writer"
        target.write_text(json.dumps(record))
        stat = target.stat()
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    report = _run(tasks_dir, mixed, apply=True, before_apply=live_writer)
    assert _by_file(report)["crashed.json"]["action"] == "skipped"
    assert json.loads(target.read_text())["status"] == "needs_finalize"
    assert json.loads(target.read_text())["heartbeat"] == "live writer"
    assert _by_file(report)["merged.json"]["action"] == "settled"


def test_unavailable_repository_evidence_is_class_d(tasks_dir, mixed, tmp_path):
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    report = str_mod.settle_stale(tasks_dir, repo_checkouts={SLUG: not_a_repo}, pager=mixed["pager"], open_pager=FakePager([]), now=NOW)
    assert report["classes"] == {"A": 0, "B": 0, "C": 0, "D": 9}
    assert mixed["pager"].calls == []


def test_pull_index_pages_until_the_oldest_task_start():
    start = NOW - timedelta(days=5)
    pulls = [
        {**_merged(f"codex/b{i}", i, NOW - timedelta(hours=i)), "updated_at": (NOW - timedelta(hours=i)).isoformat()}
        for i in range(250)
    ]
    pager = FakePager(pulls)
    index = str_mod.build_pull_index(SLUG, oldest_start=start, pager=pager, max_pages=10)
    # Page 1 ends ~99h back (after the 5-day start); page 2 ends ~199h back (before it).
    assert [call[1] for call in pager.calls] == [1, 2]
    assert index.covered_since is None and index.error is None

    capped = str_mod.build_pull_index(SLUG, oldest_start=start, pager=FakePager(pulls), max_pages=1)
    assert capped.covered_since == NOW - timedelta(hours=99)


def _settle(tasks_dir: Path, repo: Path, pulls: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    return str_mod.settle_stale(tasks_dir, repo_checkouts={SLUG: repo}, pager=FakePager(pulls), open_pager=FakePager(pulls), now=NOW, **kwargs)


@pytest.mark.parametrize("status", ["failed", "cancelled", "done", "needs_finalize", "rate_limited", "unknown"])
def test_stale_settle_existing_interrupted_tree_preserves_bytes(tasks_dir, repo, status):
    import hashlib

    tree = repo.parent / "interrupted-tree"
    _git(repo, "worktree", "add", "-b", "codex/interrupted", str(tree), "main")
    (tree / "work.txt").write_bytes(b"unique work\x00\xff")
    _git(tree, "add", "work.txt")
    _git(tree, "commit", "-m", "work")
    _git(repo, "config", "core.excludesFile", str(repo.parent / "ignore"))
    (repo.parent / "ignore").write_text(".cache/\n")
    output = tree / ".cache/output.bin"
    output.parent.mkdir()
    output.write_bytes(b"ignored output\x00\xff")
    result = tasks_dir / "interrupted.result"
    result.write_text("Український звіт\u2028result\n", encoding="utf-8")
    record = _record(
        tasks_dir,
        "interrupted",
        status=status,
        run_nonce="attempt",
        worktree_path=str(tree),
        result_file=str(result),
        result_sha256=hashlib.sha256(result.read_bytes()).hexdigest(),
        final_branch_head_commit=_git(tree, "rev-parse", "HEAD"),
        commits_ahead=1,
    )
    before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (record, result, output)]
    for _ in range(2):
        report = _settle(tasks_dir, repo, [], apply=True)
        assert all(row["action"] == "report" for row in report["records"])
        assert tree.exists()
        assert [hashlib.sha256(p.read_bytes()).hexdigest() for p in (record, result, output)] == before


@pytest.mark.parametrize("head_field", ["final", "legacy", "neither"])
def test_recorded_final_head_classifies_orphaned_work(tasks_dir, repo, head_field):
    sha = _orphan_commit(repo, f"head-{head_field}")
    fields: dict[str, Any] = {"commits_ahead": 1, "worktree_dirty_on_exit": True}
    if head_field == "final":
        fields["final_branch_head_commit"] = sha
    elif head_field == "legacy":
        fields["auto_finalize"] = _auto_finalized(sha)
    _record(tasks_dir, f"head-{head_field}", **fields)

    row = _by_file(_settle(tasks_dir, repo, []))[f"head-{head_field}.json"]

    assert row["class"] == "C"
    assert row["evidence"]["recorded_commits"] == ([] if head_field == "neither" else [sha])


def test_zero_commit_final_head_is_not_work(tasks_dir, repo):
    base = _git(repo, "rev-parse", "HEAD")
    _record(tasks_dir, "no-work-head", worktree_base_sha=base, final_branch_head_commit=base)

    report = _settle(tasks_dir, repo, [], apply=True)
    row = _by_file(report)["no-work-head.json"]

    assert row["class"] == "C"
    assert row["outcome"] == "no_deliverable"
    assert row["evidence"]["recorded_commits"] == []
    assert json.loads((tasks_dir / "no-work-head.json").read_text())["status"] == "no_deliverable"


def test_final_head_differs_from_base_with_zero_ahead_count(tasks_dir, repo):
    base = _git(repo, "rev-parse", "HEAD")
    work_head = _orphan_commit(repo, "different-head")
    _record(
        tasks_dir,
        "different-head",
        worktree_base_sha=base,
        final_branch_head_commit=work_head,
    )

    row = _by_file(_settle(tasks_dir, repo, []))["different-head.json"]

    assert row["class"] == "C"
    assert row["evidence"]["recorded_commits"] == [work_head]


def test_soft_reset_final_base_head_keeps_legacy_work_commit(tasks_dir, repo):
    base = _git(repo, "rev-parse", "HEAD")
    reset_commit = _orphan_commit(repo, "reset-work")
    _record(
        tasks_dir,
        "reset-work",
        returncode=1,
        worktree_base_sha=base,
        final_branch_head_commit=base,
        auto_finalize=_auto_finalized(reset_commit),
    )

    row = _by_file(_settle(tasks_dir, repo, []))["reset-work.json"]

    assert row["class"] == "C"
    assert row["evidence"]["recorded_commits"] == [reset_commit]


def test_renamed_branch_preserves_work_when_abandoned_record_is_closed(tasks_dir, repo):
    """Defect class: closing an abandoned record loses a renamed branch or misses its merged PR."""
    _git(repo, "checkout", "-b", "codex/renamed")
    (repo / "work.txt").write_text("unpushed\n")
    _git(repo, "add", "work.txt")
    _git(repo, "commit", "-m", "unpushed")
    work_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    _git(repo, "branch", "-m", "codex/renamed", "rescue/renamed")
    # The renamed ref still carries the task id, so it remains discoverable.
    _record(tasks_dir, "renamed", commits_ahead=1)
    # The record names the commit, and the renamed branch still holds it.
    _record(
        tasks_dir,
        "renamed-named",
        commits_ahead=1,
        worktree_dirty_on_exit=True,
        auto_finalize=_auto_finalized(work_sha),
    )
    # The recorded commit was published on origin under another name only.
    pushed_sha = _orphan_commit(repo, "pushed")
    _git(repo, "push", "origin", f"{pushed_sha}:refs/heads/elsewhere/pushed")
    _record(tasks_dir, "pushed-elsewhere", worktree_dirty_on_exit=True, auto_finalize=_auto_finalized(pushed_sha))
    # A missing local object can still be delivered through a merged PR.
    _record(tasks_dir, "unknown-commit", worktree_dirty_on_exit=True, auto_finalize=_auto_finalized("abababa"))
    refs_before = _git(repo, "for-each-ref", "refs/heads", "refs/reaper-rescue")

    report = _settle(tasks_dir, repo, [
        _merged("codex/unknown-commit", 93, OLD_FINISH + timedelta(hours=1), head_sha="ab" * 20),
        _merged("codex/renamed-named", 94, OLD_FINISH + timedelta(hours=1)),
    ], apply=True)

    rows = _by_file(report)
    assert report["classes"] == {"A": 0, "B": 0, "C": 4, "D": 0}
    assert rows["renamed.json"]["outcome"] == "failed"
    assert "refs/heads/rescue/renamed" in rows["renamed.json"]["settle_reason"]
    assert rows["renamed-named.json"]["class"] == "C"
    assert rows["renamed-named.json"]["outcome"] == "failed"
    assert rows["renamed-named.json"]["evidence"]["merged_prs"] == []
    assert rows["renamed-named.json"]["evidence"]["refs_containing_commit"] == ["refs/heads/rescue/renamed"]
    assert rows["pushed-elsewhere.json"]["class"] == "C"
    assert rows["pushed-elsewhere.json"]["evidence"]["origin_heads_at_commit"] == ["elsewhere/pushed"]
    assert rows["unknown-commit.json"]["class"] == "C"
    assert rows["unknown-commit.json"]["evidence"]["merged_prs"][0]["number"] == 93
    assert rows["unknown-commit.json"]["outcome"] == "done"
    assert _git(repo, "for-each-ref", "refs/heads", "refs/reaper-rescue") == refs_before


def test_unfetched_remote_branch_descending_from_the_recorded_commit_is_preserved(tasks_dir, repo):
    """Sol r2: an origin branch this checkout never fetched has advanced past the recorded commit."""
    work_sha = _orphan_commit(repo, "advanced")
    _git(repo, "checkout", "-b", "tmp/descendant", work_sha)
    (repo / "later.txt").write_text("later\n")
    _git(repo, "add", "later.txt")
    _git(repo, "commit", "-m", "later")
    _git(repo, "push", "origin", "tmp/descendant:refs/heads/rescue/advanced")
    _git(repo, "checkout", "main")
    _git(repo, "branch", "-D", "tmp/descendant")
    # The checkout has not fetched it: no local or remote-tracking ref holds the work.
    _git(repo, "update-ref", "-d", "refs/remotes/origin/rescue/advanced")
    assert work_sha not in _git(repo, "rev-list", "--all").split()
    _record(tasks_dir, "advanced", worktree_dirty_on_exit=True, auto_finalize=_auto_finalized(work_sha))
    before = _snapshot(tasks_dir)

    row = _by_file(_settle(tasks_dir, repo, []))["advanced.json"]

    assert row["class"] == "C"
    assert row["outcome"] == "failed"
    assert row["evidence"]["origin_heads_matching_task"] == ["rescue/advanced"]
    assert "origin/rescue/advanced" in row["settle_reason"]
    # The scan namespace is private: the checkout's own remote-tracking refs are untouched.
    assert "rescue/advanced" not in _git(repo, "for-each-ref", "refs/remotes/origin")
    assert _snapshot(tasks_dir) == before


def _descending_branch_only_on_origin(repo: Path, name: str) -> str:
    """A recorded commit whose only holder is an origin branch that has advanced past it."""
    work_sha = _orphan_commit(repo, name)
    _git(repo, "checkout", "-b", "tmp/descendant", work_sha)
    (repo / "later.txt").write_text("later\n")
    _git(repo, "add", "later.txt")
    _git(repo, "commit", "-m", "later")
    _git(repo, "push", "origin", f"tmp/descendant:refs/heads/rescue/{name}")
    _git(repo, "checkout", "main")
    _git(repo, "branch", "-D", "tmp/descendant")
    _git(repo, "update-ref", "-d", f"refs/remotes/origin/rescue/{name}")
    assert work_sha not in _git(repo, "rev-list", "--all").split()
    return work_sha


def test_narrow_configured_refspec_still_finds_the_descending_branch(tasks_dir, repo):
    """Sol r3: a fetch through origin's own refspec would never bring rescue/* in."""
    work_sha = _descending_branch_only_on_origin(repo, "narrow")
    _git(repo, "config", "--replace-all", "remote.origin.fetch", "+refs/heads/main:refs/remotes/origin/main")
    _git(repo, "fetch", "--prune", "origin")
    assert work_sha not in _git(repo, "rev-list", "--all").split()
    _record(tasks_dir, "narrow", worktree_dirty_on_exit=True, auto_finalize=_auto_finalized(work_sha))

    report = _settle(tasks_dir, repo, [], apply=True)

    row = _by_file(report)["narrow.json"]
    assert row["class"] == "C"
    assert row["evidence"]["origin_heads_matching_task"] == ["rescue/narrow"]
    assert report["actions"] == {"settled": 1}
    assert _git(repo, "config", "--get-all", "remote.origin.fetch") == "+refs/heads/main:refs/remotes/origin/main"


def test_scan_namespace_is_preserved_and_never_used_as_current_remote_heads(tasks_dir, repo):
    _record(tasks_dir, "no-commits")
    _git(repo, "update-ref", f"{SCAN_PREFIX}/doomed", _git(repo, "rev-parse", "HEAD"))
    _git(repo, "push", "origin", "main:refs/heads/doomed")
    refs_before = _git(repo, "for-each-ref")
    _settle(tasks_dir, repo, [])
    assert _git(repo, "for-each-ref") == refs_before
    assert f"{SCAN_PREFIX}/doomed" in _git(repo, "for-each-ref", "--format=%(refname)", SCAN_PREFIX)
    _git(repo, "push", "origin", ":refs/heads/doomed")
    refs_before = _git(repo, "for-each-ref")
    _settle(tasks_dir, repo, [])
    assert _git(repo, "for-each-ref") == refs_before
    assert _git(repo, "for-each-ref", "--format=%(refname)", SCAN_PREFIX).split() == [f"{SCAN_PREFIX}/doomed"]
    facts = str_mod.collect_repo_facts(SLUG, repo, str_mod.fleet_fetch_target(SLUG))
    assert facts.remote_heads == {"main": _git(repo, "rev-parse", "HEAD")}


def test_git_config_env_redirect_is_ignored(tasks_dir, repo, tmp_path, monkeypatch):
    """Sol r3: inherited GIT_CONFIG_* rewrites the fetch URL to a repository without the rescue branch."""
    work_sha = _descending_branch_only_on_origin(repo, "redirected")
    decoy = tmp_path / "decoy.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(decoy))
    _git(repo, "push", str(decoy), "main")
    origin = str(tmp_path / "origin.git")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{decoy}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", origin)
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", f"'url.{decoy}.insteadof'='{origin}'")
    # Under the inherited environment, unscrubbed, the URL resolves to the decoy.
    unscrubbed = subprocess.run(
        ["git", "ls-remote", "--get-url", origin], cwd=repo, capture_output=True, text=True, check=True, timeout=30
    )
    assert unscrubbed.stdout.strip() == str(decoy)
    _record(tasks_dir, "redirected", worktree_dirty_on_exit=True, auto_finalize=_auto_finalized(work_sha))

    row = _by_file(_settle(tasks_dir, repo, [], apply=True))["redirected.json"]

    assert row["class"] == "C"
    assert row["evidence"]["origin_heads_matching_task"] == ["rescue/redirected"]


SECRET = "lu_synthetic_TOKEN_9f8e7d6c5b4a39281706"


def test_checkout_config_rewrite_refuses_remote_inventory_and_hides_credentials(tasks_dir, repo, tmp_path):
    """``url.*.insteadOf`` in the checkout's own config would silently change which repository is fetched."""
    origin = str(tmp_path / "origin.git")
    _git(repo, "config", f"url.https://x-access-token:{SECRET}@example.invalid/r.git.insteadOf", origin)
    _record(tasks_dir, "no-commits")

    report = _settle(tasks_dir, repo, [], apply=True)

    row = _by_file(report)["no-commits.json"]
    assert row["class"] == "D"
    assert "refusing remote inventory" in row["skip_reason"]
    assert "https://example.invalid/r.git" in row["evidence"]["fetch_failed"]
    assert SECRET not in json.dumps(report) and "x-access-token" not in json.dumps(report)


def test_git_error_text_never_carries_credentials(tasks_dir, mixed, monkeypatch, capsys):
    """Sol r3: the fetch error line was copied into the report verbatim."""
    real_git = str_mod._git
    stderr = (
        f"fatal: unable to access 'https://x-access-token:{SECRET}@github.com/owner/repo.git/': "
        f"The requested URL returned error: 403 token={SECRET}\n"
    )

    def leaky_fetch(args, **kwargs):
        if args[:2] == ["ls-remote", "--heads"]:
            return subprocess.CompletedProcess(["git", *args], 128, "", stderr)
        return real_git(args, **kwargs)

    monkeypatch.setattr(str_mod, "_git", leaky_fetch)
    report = _run(tasks_dir, mixed)
    assert "unable to access 'https://github.com/owner/repo.git/'" in report["records"][0]["skip_reason"]
    assert SECRET not in json.dumps(report)

    assert str_mod.main(["settle-stale", "--tasks-dir", str(tasks_dir)]) == 0
    assert SECRET not in capsys.readouterr().out


def test_gh_error_text_never_carries_credentials(monkeypatch):
    def failing_gh(*_args, **_kwargs):
        return subprocess.CompletedProcess(["gh"], 1, "", f"error: https://user:{SECRET}@api.github.com denied\n")

    monkeypatch.setattr(str_mod.github_client, "run", failing_gh)
    index = str_mod.build_pull_index(SLUG, oldest_start=None, pager=str_mod.gh_pull_page, max_pages=1)
    assert index.error and "https://api.github.com denied" in index.error
    assert SECRET not in index.error


def test_failed_remote_inventory_keeps_every_record_of_the_repository_out_of_class_c(tasks_dir, mixed, monkeypatch):
    real_git = str_mod._git

    def offline_fetch(args, **kwargs):
        if args[:2] == ["ls-remote", "--heads"]:
            return subprocess.CompletedProcess(["git", *args], 128, "", "fatal: unable to access origin\n")
        return real_git(args, **kwargs)

    monkeypatch.setattr(str_mod, "_git", offline_fetch)
    before = _snapshot(tasks_dir)

    report = _run(tasks_dir, mixed, apply=True)

    rows = _by_file(report)
    assert report["classes"] == {"A": 0, "B": 0, "C": 0, "D": 9}
    for name in ("published.json", "local-only.json", "merged.json", "no-commits.json", "crashed.json"):
        assert rows[name]["class"] == "D"
        assert rows[name]["skip_reason"].startswith("fetch_failed: git ls-remote ")
        assert "failed: fatal: unable to access origin" in rows[name]["skip_reason"]
    assert "settled" not in report["actions"]
    assert mixed["pager"].calls == []
    assert _snapshot(tasks_dir) == before


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"commits_ahead": 3}, "commits_ahead=3 but the record names no commit"),
        ({"commits_ahead": None}, "commits_ahead=None but the record names no commit"),
        ({"worktree_dirty_on_exit": True}, "uncommitted work at exit"),
        ({"worktree_dirty_on_exit": None}, "uncommitted work at exit"),
    ],
)
def test_lost_work_without_a_recorded_commit_settles_failed(tasks_dir, repo, fields, reason):
    _record(tasks_dir, "unnamed-work", **fields)
    row = _by_file(_settle(tasks_dir, repo, []))["unnamed-work.json"]
    assert row["class"] == "C"
    assert row["outcome"] == "failed"
    assert row["settle_reason"] == "work not recoverable: worktree and branch gone"


@pytest.mark.parametrize("worktree_path", [None, "missing"])
@pytest.mark.parametrize("branch", [None, "codex/lost"])
def test_unrecoverable_missing_work_settles_without_deleting_refs(tasks_dir, repo, worktree_path, branch):
    """Defect class: stale lost-work counters leave an empty record unfinished forever."""
    fields = {"worktree_path": None} if worktree_path is None else {}
    path = _record(tasks_dir, "lost", worktree_branch=branch, commits_ahead=3, worktree_dirty_on_exit=True, **fields)
    before = _snapshot(tasks_dir)
    report = _settle(tasks_dir, repo, [])
    assert _snapshot(tasks_dir) == before
    assert report["outcomes"] == {"failed": 1}
    assert report["needs_decision"] == 0
    assert report["keep_close"] == []
    refs = _git(repo, "for-each-ref")
    applied = _settle(tasks_dir, repo, [], apply=True)
    assert applied["actions"] == {"settled": 1}
    assert json.loads(path.read_text())["status"] == "failed"
    assert _git(repo, "for-each-ref") == refs


@pytest.mark.parametrize("holder", ["rescue", "custom", "worktree", "commit", "git-error", "open-pr"])
def test_preserved_work_settles_but_live_or_unknown_work_stays_reported(tasks_dir, repo, tmp_path, monkeypatch, holder):
    """Defect class: closure mistakes preserved refs for liveness or treats missing evidence as absence."""
    fields = {"commits_ahead": 2}
    pulls = []
    head = _git(repo, "rev-parse", "HEAD")
    if holder in {"rescue", "custom"}:
        ref = (f"refs/reaper-rescue/20261113/codex/kept-{head[:12]}" if holder == "rescue"
               else "refs/tmp/codex/kept")
        _git(repo, "update-ref", ref, head)
    elif holder == "worktree":
        tree = tmp_path / "present"
        tree.mkdir()
        fields["worktree_path"] = str(tree)
    elif holder == "commit":
        fields.update(worktree_path=None, auto_finalize=_auto_finalized(head))
    elif holder == "git-error":
        original = str_mod._git
        def broken(args, **kwargs):
            return None if args[0] == "for-each-ref" else original(args, **kwargs)
        monkeypatch.setattr(str_mod, "_git", broken)
    elif holder == "open-pr":
        pulls = [{"state": "open", "number": 42, "html_url": "https://example.invalid/pull/42",
                  "head": {"ref": "codex/kept", "sha": head, "repo": {"full_name": SLUG}}}]
    path = _record(tasks_dir, "kept", **fields)
    before = path.read_bytes()
    _settle(tasks_dir, repo, pulls)
    refs_before = _git(repo, "for-each-ref")
    report = _settle(tasks_dir, repo, pulls, apply=True)
    if holder in {"rescue", "custom", "commit"}:
        assert json.loads(path.read_text())["status"] == "failed"
        assert report["needs_decision"] == 0
        assert _git(repo, "for-each-ref") == refs_before
        assert "abandoned; work preserved at " in json.loads(path.read_text())["settle_reason"]
        return
    assert path.read_bytes() == before
    assert report["needs_decision"] == 1
    row = report["keep_close"][0]
    assert (row["task_id"], row["repo"], row["branch"], row["age_days"]) == ("kept", SLUG, "codex/kept", 10.0)
    assert row["where"]
    assert row["open_prs"] == ([] if holder != "open-pr" else [{
        "number": 42, "url": "https://example.invalid/pull/42", "merged_at": None,
        "head_sha": head, "merge_commit_sha": None}])


def test_ref_reappearing_before_settlement_preserves_the_record(tasks_dir, repo):
    """Defect class: classification-to-write race terminalizes newly recoverable work."""
    path = _record(tasks_dir, "returned", commits_ahead=3)
    before = path.read_bytes()
    def reappear(_candidates):
        _git(repo, "update-ref", "refs/tmp/codex/returned", _git(repo, "rev-parse", "HEAD"))
    report = _settle(tasks_dir, repo, [], apply=True, before_apply=reappear)
    assert report["actions"] == {"skipped": 1}
    assert path.read_bytes() == before
    assert report["needs_decision"] == 1


def test_abandoned_preserved_refs_settle_without_changing_refs(tasks_dir, repo):
    """Defect class: preserved refs keep abandoned records unfinished or are altered on closure."""
    sha = _orphan_commit(repo, "preserved")
    refs = ["refs/reaper-rescue/stamp/codex/preserved", "refs/original/codex/preserved",
            "refs/heads/codex/preserved"]
    for ref in refs:
        _git(repo, "update-ref", ref, sha)
    path = _record(tasks_dir, "preserved", commits_ahead=2)
    delivered = _record(tasks_dir, "delivered", commits_ahead=2, auto_finalize=_auto_finalized(sha))
    _record(tasks_dir, "unrelated-delivery")
    _git(repo, "update-ref", "refs/original/codex/unrelated-delivery", sha)
    _record(tasks_dir, "prefix", worktree_branch=None, commits_ahead=2)
    _git(repo, "update-ref", "refs/original/codex/prefix", sha)
    history = _git(repo, "rev-parse", "HEAD")
    _record(tasks_dir, "history", commits_ahead=1, final_branch_head_commit=history)
    newer = repo.parent / "unrelated-newer-checkout"
    _git(repo, "worktree", "add", "-b", "unrelated/newer", str(newer))
    (newer / "later.txt").write_text("a different task\n")
    _git(newer, "add", "later.txt")
    _git(newer, "commit", "-m", "unrelated newer work")
    pulls = [_merged("codex/delivered", 91, OLD_FINISH + timedelta(hours=1), head_sha=sha),
             _merged("codex/prefix-child", 92, OLD_FINISH + timedelta(hours=1), head_sha=sha),
             _merged("codex/unrelated-delivery", 93, OLD_FINISH + timedelta(days=2), head_sha="ff" * 20)]
    before = _snapshot(tasks_dir)
    dry = _settle(tasks_dir, repo, pulls)
    assert dry["outcomes"] == {"failed": 4, "done": 1}
    assert _snapshot(tasks_dir) == before
    ref_snapshot = _git(repo, "for-each-ref")
    applied = _settle(tasks_dir, repo, pulls, apply=True)
    assert applied["actions"] == {"settled": 5}
    failed = json.loads(path.read_text())
    assert failed["status"] == "failed"
    assert failed["settle_reason"] == "abandoned; work preserved at " + ", ".join(sorted(refs))
    assert json.loads(delivered.read_text())["merged_pr"]["url"] == f"https://github.com/{SLUG}/pull/91"
    assert json.loads(delivered.read_text())["status"] == "done"
    assert _git(repo, "for-each-ref") == ref_snapshot
    assert json.loads((tasks_dir / "history.json").read_text())["status"] == "failed"
    assert json.loads((tasks_dir / "prefix.json").read_text())["status"] == "failed"
    assert json.loads((tasks_dir / "unrelated-delivery.json").read_text())["status"] == "failed"
    assert newer.is_dir()


def test_open_pr_or_existing_worktree_keeps_abandoned_records_reported(tasks_dir, repo, tmp_path):
    """Defect class: abandoned-ref closure terminalizes an open PR, an existing directory, or a young record."""
    head = _git(repo, "rev-parse", "HEAD")
    tree = tmp_path / "interrupted-tree"
    tree.mkdir()
    opened = _record(tasks_dir, "opened", commits_ahead=2)
    existing = _record(tasks_dir, "existing", worktree_path=str(tree), commits_ahead=2)
    young = _record(tasks_dir, "young", finished_at=(NOW - timedelta(days=1)).isoformat())
    for name in ("opened", "existing", "young"):
        _git(repo, "update-ref", f"refs/reaper-rescue/stamp/codex/{name}", head)
    before = _snapshot(tasks_dir)
    pulls = [{"state": "open", "number": 42, "html_url": "https://example.invalid/pull/42",
              "head": {"ref": "codex/opened", "sha": head, "repo": {"full_name": SLUG}}}]
    report = _settle(tasks_dir, repo, pulls, apply=True)
    assert report["needs_decision"] == 2
    assert report["younger_left_alone"] == 1
    assert report["actions"] == {"report": 2}
    assert _snapshot(tasks_dir) == before
    assert all(json.loads(path.read_text())["status"] == "needs_finalize" for path in (opened, existing, young))
    assert tree.is_dir()
    # The same holders appearing after classification must also prevent writes
    # while the existing settlement lock is held.
    raced_open = _record(tasks_dir, "raced-open", commits_ahead=2)
    raced_tree_path = tmp_path / "reappeared-tree"
    raced_tree = _record(tasks_dir, "raced-tree", worktree_path=str(raced_tree_path), commits_ahead=2)
    for name in ("raced-open", "raced-tree"):
        _git(repo, "update-ref", f"refs/reaper-rescue/stamp/codex/{name}", head)
    before = _snapshot(tasks_dir)
    def reappear(_candidates):
        raced_tree_path.mkdir()
        pulls.append({"state": "open", "number": 43, "html_url": "https://example.invalid/pull/43",
                      "head": {"ref": "codex/raced-open", "sha": head, "repo": {"full_name": SLUG}}})
    raced = _settle(tasks_dir, repo, pulls, apply=True, before_apply=reappear)
    assert raced["actions"] == {"report": 2, "skipped": 2}
    assert _snapshot(tasks_dir) == before
    assert all(json.loads(path.read_text())["status"] == "needs_finalize" for path in (raced_open, raced_tree))


# ---------------------------------------------------------------------------
# archive / restore
# ---------------------------------------------------------------------------


def _terminal(tasks_dir: Path, name: str, *, status: str = "done", age_days: float = 20, **fields: Any) -> Path:
    path = tasks_dir / f"{name}.json"
    record = {
        "task_id": name,
        "status": status,
        "finished_at": (NOW - timedelta(days=age_days)).isoformat(),
        "result_file": str(tasks_dir / f"{name}.result"),
        **fields,
    }
    path.write_text(json.dumps(record))
    return path


def test_archive_round_trip_moves_record_with_sidecars(tasks_dir):
    _terminal(tasks_dir, "old-done")
    (tasks_dir / "old-done.result").write_text("reply\n")
    (tasks_dir / "old-done.snapshots").mkdir()
    (tasks_dir / "old-done.snapshots" / "read_only_checkout_pre.json").write_text("{}")
    # A --force-new archived record keeps its own stamped sidecars.
    stamp = "20260901T000000123456Z"
    _terminal(tasks_dir, f"redo.{stamp}.archived", status="failed")
    (tasks_dir / f"redo.{stamp}.archived.result").write_text("old reply\n")
    (tasks_dir / f"redo.snapshots.{stamp}.archived").mkdir()
    (tasks_dir / f"redo.snapshots.{stamp}.archived" / "read_only_checkout_post.json").write_text("{}")
    before = _snapshot(tasks_dir)

    dry = str_mod.archive_terminal(tasks_dir, now=NOW)
    assert dry["actions"] == {"would_archive": 2}
    assert _snapshot(tasks_dir) == before

    report = str_mod.archive_terminal(tasks_dir, now=NOW, apply=True)
    assert report["actions"] == {"archived": 2}
    archive = tasks_dir / "archive"
    assert sorted(path.name for path in tasks_dir.iterdir()) == ["archive"]
    assert (archive / "old-done.snapshots" / "read_only_checkout_pre.json").is_file()
    assert (archive / f"redo.snapshots.{stamp}.archived" / "read_only_checkout_post.json").is_file()
    assert task_record_store.locate_task_record(tasks_dir, "old-done") == archive / "old-done.json"

    restored = str_mod.restore_archived(tasks_dir, ["old-done", f"redo.{stamp}.archived.json"], apply=True)
    assert restored["actions"] == {"restored": 2}
    # The per-task lock file is not a task artifact; restore leaves it beside the archived name.
    after = {key: value[0] for key, value in _snapshot(tasks_dir).items() if not key.endswith(".lock")}
    assert after == {key: value[0] for key, value in before.items()}
    assert task_record_store.locate_task_record(tasks_dir, "old-done") == tasks_dir / "old-done.json"


def test_archive_preserves_stable_lock_anchors(tasks_dir):
    _terminal(tasks_dir, "old-done")
    (tasks_dir / "old-done.json.lock").write_text("")
    (tasks_dir / "orphan.json.lock").write_text("")
    _terminal(tasks_dir, "active", status="running")
    (tasks_dir / "active.json.lock").write_text("")

    dry = str_mod.archive_terminal(tasks_dir, now=NOW, apply=False)
    assert dry["actions"] == {"would_archive": 1}
    assert (tasks_dir / "old-done.json.lock").exists()
    assert (tasks_dir / "orphan.json.lock").exists()
    assert (tasks_dir / "active.json.lock").exists()

    report = str_mod.archive_terminal(tasks_dir, now=NOW, apply=True)
    assert report["actions"] == {"archived": 1}
    # Stable lock anchors must never be deleted to prevent flock split-brain (#8659)
    assert (tasks_dir / "old-done.json.lock").exists()
    assert (tasks_dir / "orphan.json.lock").exists()
    assert (tasks_dir / "active.json.lock").exists()
    assert (tasks_dir / "active.json").exists()


def test_task_state_lock_preserves_stable_lock_anchor_against_split_brain(tmp_path):
    record_path = tmp_path / "task-1.json"
    lock_path = tmp_path / "task-1.json.lock"

    # Acquire lock with task_state_lock
    with dead_worker_state.task_state_lock(record_path):
        assert lock_path.exists()
        inode_first = lock_path.stat().st_ino

        # Concurrent opener attempting to acquire non-blocking must block on the same anchor
        second_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(second_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(second_fd)

    # After releasing, next acquisition reuses the same inode
    with dead_worker_state.task_state_lock(record_path):
        inode_second = lock_path.stat().st_ino
        assert inode_first == inode_second


def test_archive_selects_only_old_terminal_records_without_a_live_worktree(tasks_dir, tmp_path):
    linked = tmp_path / "linked-wt"
    linked.mkdir()
    (linked / ".git").write_text("gitdir: /elsewhere\n")
    # A read-only task ran in a primary checkout (``.git`` directory), which no task owns.
    primary = tmp_path / "primary"
    (primary / ".git").mkdir(parents=True)
    _terminal(tasks_dir, "old-done")
    _terminal(tasks_dir, "young-done", age_days=3)
    _terminal(tasks_dir, "old-needs-finalize", status="needs_finalize")
    _terminal(tasks_dir, "old-running", status="running")
    _terminal(tasks_dir, "old-queued", status="queued")
    _terminal(tasks_dir, "old-with-worktree", worktree_path=str(linked))
    _terminal(tasks_dir, "old-read-only", status="failed", cwd=str(primary))

    report = str_mod.archive_terminal(tasks_dir, now=NOW, apply=True)

    assert sorted(row["file"] for row in report["records"]) == ["old-done.json", "old-read-only.json"]
    assert report["kept_hot"] == {"worktree path still exists": 1}
    assert report["younger_left_alone"] == 1
    hot = sorted(path.name for path in tasks_dir.glob("*.json"))
    assert hot == [
        "old-needs-finalize.json",
        "old-queued.json",
        "old-running.json",
        "old-with-worktree.json",
        "young-done.json",
    ]


def test_archive_never_overwrites_and_claim_scan_ignores_archive(tasks_dir, tmp_path):
    archive = tasks_dir / "archive"
    archive.mkdir()
    (archive / "dup.json").write_text('{"status": "done", "task_id": "dup", "keep": true}')
    _terminal(tasks_dir, "dup")
    (tasks_dir / "dup.result").write_text("new reply\n")

    report = str_mod.archive_terminal(tasks_dir, now=NOW, apply=True)

    moved = report["records"][0]["moved"]
    assert json.loads((archive / "dup.json").read_text())["keep"] is True
    assert moved[0].startswith("dup.") and moved[0].endswith(".archived.json")
    assert moved[1] == moved[0].removesuffix(".json") + ".result"
    # An unfinished record parked in archive/ (never produced by archive) is not a claim.
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (archive / "parked.json").write_text(
        json.dumps({"task_id": "parked", "status": "running", "worktree_path": str(worktree)})
    )
    assert worktree_claims.active_worktree_claim_refusal(worktree, tasks_dir=tasks_dir, repo_root=tmp_path) is None


def test_restore_refuses_to_overwrite_a_hot_record(tasks_dir):
    _terminal(tasks_dir, "again")
    str_mod.archive_terminal(tasks_dir, now=NOW, apply=True)
    _terminal(tasks_dir, "again", status="failed", age_days=1)
    report = str_mod.restore_archived(tasks_dir, ["again"], apply=True)
    assert report["records"][0]["action"] == "skipped"
    assert json.loads((tasks_dir / "again.json").read_text())["status"] == "failed"


def test_cli_defaults_to_dry_run_json(tasks_dir, capsys):
    _terminal(tasks_dir, "old-done")
    assert str_mod.main(["archive", "--tasks-dir", str(tasks_dir), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry-run"
    assert (tasks_dir / "old-done.json").is_file()
    assert str_mod.main(["archive", "--tasks-dir", str(tasks_dir / "missing")]) == str_mod.EXIT_USAGE


@pytest.mark.parametrize("names_checkout", [True, False])
def test_archive_skips_a_record_whose_lock_is_held(tasks_dir, tmp_path, names_checkout):
    """Archive takes the canonical task lock: the checkout's, else the record file's own."""
    checkout = tmp_path / "gone-wt"
    fields = {"worktree_path": str(checkout)} if names_checkout else {}
    record_path = _terminal(tasks_dir, "locked", **fields)
    held, release = threading.Event(), threading.Event()

    def holder():
        with str_mod.worktree_lock(str(checkout if names_checkout else record_path), timeout_s=1):
            held.set()
            release.wait(10)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert held.wait(5)
        report = str_mod.archive_terminal(tasks_dir, now=NOW, apply=True, lock_timeout_s=0.1)
    finally:
        release.set()
        thread.join()

    row = report["records"][0]
    assert (row["action"], row["skip_reason"]) == ("skipped", worktree_claims.LOCK_BUSY)
    assert record_path.is_file()


def test_archive_rechecks_the_record_under_its_lock(tasks_dir, tmp_path):
    reattached = tmp_path / "reattached-wt"
    _terminal(tasks_dir, "revived")
    _terminal(tasks_dir, "touched")
    _terminal(tasks_dir, "reattached", worktree_path=str(reattached))
    _terminal(tasks_dir, "untouched")

    def live_writer(path: Path) -> None:
        stat = path.stat()
        if path.name == "revived.json":
            # Rewritten as running with its mtime restored: only the status re-check sees it.
            path.write_text(json.dumps({**json.loads(path.read_text()), "status": "running"}))
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        elif path.name == "touched.json":
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        elif path.name == "reattached.json":
            reattached.mkdir()

    report = str_mod.archive_terminal(tasks_dir, now=NOW, apply=True, before_move=live_writer)

    rows = {row["file"]: (row["action"], row.get("skip_reason")) for row in report["records"]}
    assert rows == {
        "revived.json": ("skipped", "record no longer terminal"),
        "touched.json": ("skipped", "record changed since selection"),
        "reattached.json": ("skipped", "worktree path still exists"),
        "untouched.json": ("archived", None),
    }
    assert sorted(path.name for path in tasks_dir.glob("*.json")) == ["reattached.json", "revived.json", "touched.json"]


def test_archive_keeps_records_that_still_own_a_path(tasks_dir, tmp_path):
    half_removed = tmp_path / "half-removed"  # a checkout directory without its .git file
    half_removed.mkdir()
    acp_runtime = tmp_path / "acp-runtime"
    acp_runtime.mkdir()
    _terminal(tasks_dir, "half-removed", worktree_path=str(half_removed))
    _terminal(tasks_dir, "cwd-owner", cwd=str(half_removed))
    _terminal(
        tasks_dir,
        "acp-owner",
        worktree_path=str(tmp_path / "gone-wt"),
        acp_runtime_paths=[str(tmp_path / "acp-gone"), str(acp_runtime)],
    )
    _terminal(tasks_dir, "acp-gone", acp_runtime_paths=[str(tmp_path / "acp-gone")])

    report = str_mod.archive_terminal(tasks_dir, now=NOW, apply=True)

    assert [row["file"] for row in report["records"]] == ["acp-gone.json"]
    assert report["kept_hot"] == {"worktree path still exists": 2, "ACP runtime path still exists": 1}
    assert sorted(path.name for path in tasks_dir.glob("*.json")) == [
        "acp-owner.json",
        "cwd-owner.json",
        "half-removed.json",
    ]


def test_archive_puts_back_a_record_replaced_during_the_move(tasks_dir, monkeypatch):
    target = _terminal(tasks_dir, "raced")
    (tasks_dir / "raced.result").write_text("reply\n")
    real_rename = os.rename
    raced: list[bool] = []

    def racing_rename(src, dst):
        if Path(src) == target and not raced:
            # A writer replaces the record after the final re-check, before the rename.
            raced.append(True)
            store._write(target, {**json.loads(target.read_text()), "status": "running"})
        real_rename(src, dst)

    monkeypatch.setattr(str_mod.os, "rename", racing_rename)
    report = str_mod.archive_terminal(tasks_dir, now=NOW, apply=True)

    row = report["records"][0]
    assert (row["action"], row["skip_reason"]) == ("skipped", "record replaced during the move; put back")
    assert json.loads(target.read_text())["status"] == "running"
    assert (tasks_dir / "raced.result").is_file()
    assert not list((tasks_dir / "archive").iterdir())


def _link_after_writer(monkeypatch, target: Path, write: Callable[[], None]) -> list[bool]:
    """Make a writer create ``target`` just before the tool links a file onto it."""
    real_link = os.link
    raced: list[bool] = []

    def racing_link(src, dst, **kwargs):
        if Path(dst) == target and not raced:
            raced.append(True)
            write()
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(str_mod.os, "link", racing_link)
    return raced


def test_restore_keeps_a_hot_record_a_writer_created_after_the_check(tasks_dir, monkeypatch):
    _terminal(tasks_dir, "revived")
    (tasks_dir / "revived.result").write_text("old reply\n")
    str_mod.archive_terminal(tasks_dir, now=NOW, apply=True)
    hot = tasks_dir / "revived.json"
    raced = _link_after_writer(monkeypatch, hot, lambda: store._write(hot, {"task_id": "revived", "status": "running"}))

    report = str_mod.restore_archived(tasks_dir, ["revived"], apply=True)

    row = report["records"][0]
    assert raced and row["action"] == "skipped"
    assert "a writer created revived.json" in row["skip_reason"]
    assert json.loads(hot.read_text())["status"] == "running"
    assert json.loads((tasks_dir / "archive" / "revived.json").read_text())["status"] == "done"
    assert (tasks_dir / "archive" / "revived.result").read_text() == "old reply\n"
    assert not (tasks_dir / "revived.result").exists()


def _rename_after_writer(monkeypatch, target: Path, write: Callable[[], None]) -> list[bool]:
    """Make a writer act on ``target`` just before the tool renames a directory onto it."""
    real_rename = os.rename
    raced: list[bool] = []

    def racing_rename(src, dst):
        if Path(dst) == target and not raced:
            raced.append(True)
            write()
        return real_rename(src, dst)

    monkeypatch.setattr(str_mod.os, "rename", racing_rename)
    return raced


SNAPSHOT_FILES = {"read_only_checkout_pre.json": '{"old": "pre"}', "read_only_checkout_post.json": '{"old": "post"}'}


def _with_snapshots(tasks_dir: Path, name: str) -> Path:
    _terminal(tasks_dir, name)
    (tasks_dir / f"{name}.result").write_text("old reply\n")
    snapshots = tasks_dir / f"{name}.snapshots"
    snapshots.mkdir()
    for file_name, text in SNAPSHOT_FILES.items():
        (snapshots / file_name).write_text(text)
    return snapshots


def _contents(directory: Path) -> dict[str, str]:
    return {path.name: path.read_text() for path in directory.iterdir()}


def _writer_child(directory: Path) -> Callable[[], None]:
    """A writer that creates ``directory`` holding one child named like an original snapshot."""

    def write() -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "read_only_checkout_post.json").write_text("writer\n")

    return write


def test_restore_keeps_a_result_a_writer_created_after_the_check(tasks_dir, monkeypatch):
    _with_snapshots(tasks_dir, "revived")
    str_mod.archive_terminal(tasks_dir, now=NOW, apply=True)
    target = tasks_dir / "revived.result"
    raced = _link_after_writer(monkeypatch, target, lambda: target.write_text("writer\n"))

    report = str_mod.restore_archived(tasks_dir, ["revived"], apply=True)

    row = report["records"][0]
    assert raced and row["action"] == "restored"
    assert row["kept_in_archive"] == [str(tasks_dir / "archive" / "revived.result")]
    assert target.read_text() == "writer\n"
    assert (tasks_dir / "archive" / "revived.result").read_text() == "old reply\n"
    assert json.loads((tasks_dir / "revived.json").read_text())["status"] == "done"
    assert _contents(tasks_dir / "revived.snapshots") == SNAPSHOT_FILES


def test_restore_keeps_a_snapshot_directory_whole_when_a_writer_fills_its_hot_name(tasks_dir, monkeypatch):
    """A writer's child in the hot snapshot directory keeps the archived one whole, never split."""
    _with_snapshots(tasks_dir, "revived")
    str_mod.archive_terminal(tasks_dir, now=NOW, apply=True)
    hot = tasks_dir / "revived.snapshots"
    raced = _rename_after_writer(monkeypatch, hot, _writer_child(hot))

    report = str_mod.restore_archived(tasks_dir, ["revived"], apply=True)

    row = report["records"][0]
    archived = tasks_dir / "archive" / "revived.snapshots"
    assert raced and row["action"] == "restored"
    assert row["kept_in_archive"] == [str(archived)]
    assert _contents(archived) == SNAPSHOT_FILES
    assert _contents(hot) == {"read_only_checkout_post.json": "writer\n"}
    assert (tasks_dir / "revived.result").read_text() == "old reply\n"


def test_restore_moves_a_snapshot_directory_onto_an_empty_one(tasks_dir, monkeypatch):
    _with_snapshots(tasks_dir, "revived")
    str_mod.archive_terminal(tasks_dir, now=NOW, apply=True)
    hot = tasks_dir / "revived.snapshots"
    raced = _rename_after_writer(monkeypatch, hot, hot.mkdir)

    report = str_mod.restore_archived(tasks_dir, ["revived"], apply=True)

    row = report["records"][0]
    assert raced and row["action"] == "restored" and "kept_in_archive" not in row
    assert _contents(hot) == SNAPSHOT_FILES
    assert not (tasks_dir / "archive" / "revived.snapshots").exists()


def test_archive_moves_a_snapshot_directory_whole_past_a_writer_filled_name(tasks_dir, monkeypatch):
    monkeypatch.setattr(str_mod, "_archive_stamp", lambda: STAMP)
    _with_snapshots(tasks_dir, "clash")
    foreign = tasks_dir / "archive" / "clash.snapshots"
    raced = _rename_after_writer(monkeypatch, foreign, _writer_child(foreign))

    report = str_mod.archive_terminal(tasks_dir, now=NOW, apply=True)

    row = report["records"][0]
    stamped = tasks_dir / "archive" / f"clash.{STAMP}.archived.snapshots"
    assert raced and row["action"] == "archived"
    assert row["moved"] == ["clash.json", "clash.result", stamped.name]
    assert _contents(stamped) == SNAPSHOT_FILES
    assert _contents(foreign) == {"read_only_checkout_post.json": "writer\n"}
    assert sorted(path.name for path in tasks_dir.iterdir()) == ["archive"]
    assert _no_staging_left(tasks_dir)


def test_archive_never_splits_a_snapshot_directory(tasks_dir, monkeypatch):
    """Sol r4 probe: a writer fills the snapshot directory's archive name mid-move, its stamped names are taken.

    Child-by-child moves left one original snapshot in the archive and returned
    the other hot. The directory now goes back whole to its hot name.
    """
    monkeypatch.setattr(str_mod, "_archive_stamp", lambda: STAMP)
    hot = _with_snapshots(tasks_dir, "split")
    archive = tasks_dir / "archive"
    taken = [archive / f"split.{STAMP}.archived.snapshots", archive / f"split.{STAMP}.{os.getpid()}.archived.snapshots"]
    for directory in taken:
        _writer_child(directory)()
    foreign = archive / "split.snapshots"
    raced = _rename_after_writer(monkeypatch, foreign, _writer_child(foreign))

    report = str_mod.archive_terminal(tasks_dir, now=NOW, apply=True)

    row = report["records"][0]
    assert raced and row["action"] == "error" and row["moved"] == ["split.json", "split.result"]
    assert "split.snapshots not archived (archive already holds split.snapshots and its stamped name)" in row["error"]
    assert row["error"].endswith(f"left at {hot}")
    assert _contents(hot) == SNAPSHOT_FILES
    for directory in (foreign, *taken):
        assert _contents(directory) == {"read_only_checkout_post.json": "writer\n"}
    assert json.loads((archive / "split.json").read_text())["status"] == "done"
    assert _no_staging_left(tasks_dir)


def test_archive_parks_a_snapshot_directory_whole_when_its_hot_name_is_retaken(tasks_dir, monkeypatch):
    monkeypatch.setattr(str_mod, "_archive_stamp", lambda: STAMP)
    hot = _with_snapshots(tasks_dir, "parked")
    archive = tasks_dir / "archive"
    for name in (
        "parked.snapshots",
        f"parked.{STAMP}.archived.snapshots",
        f"parked.{STAMP}.{os.getpid()}.archived.snapshots",
    ):
        _writer_child(archive / name)()
    # Every archive name is taken, so the directory heads home; a writer fills that name first.
    raced = _rename_after_writer(monkeypatch, hot, _writer_child(hot))

    report = str_mod.archive_terminal(tasks_dir, now=NOW, apply=True)

    row = report["records"][0]
    assert raced and row["action"] == "error"
    parked = Path(row["error"].rsplit("left at ", 1)[1])
    assert parked.parent == archive and parked.name.startswith("parked.snapshots.unplaced-")
    assert _contents(parked) == SNAPSHOT_FILES
    assert _contents(hot) == {"read_only_checkout_post.json": "writer\n"}
    assert _no_staging_left(tasks_dir)


def test_archive_never_replaces_a_destination_created_during_the_move(tasks_dir, monkeypatch):
    _terminal(tasks_dir, "clash")
    (tasks_dir / "clash.result").write_text("reply\n")
    foreign = tasks_dir / "archive" / "clash.json"
    raced = _link_after_writer(monkeypatch, foreign, lambda: foreign.write_text("foreign\n"))

    report = str_mod.archive_terminal(tasks_dir, now=NOW, apply=True)

    row = report["records"][0]
    assert raced and row["action"] == "archived"
    assert foreign.read_text() == "foreign\n"
    record_name, result_name = row["moved"]
    assert record_name.endswith(".archived.json") and result_name == record_name.removesuffix(".json") + ".result"
    assert json.loads((foreign.parent / record_name).read_text())["status"] == "done"
    assert (foreign.parent / result_name).read_text() == "reply\n"
    assert sorted(path.name for path in tasks_dir.iterdir()) == ["archive"]
    assert not list(foreign.parent.glob(".*.moving"))


STAMP = "20260924T120000000000Z"


def _fill_archive_names(archive: Path, file_name: str) -> dict[str, bytes]:
    """Take a file's archive name and both stamped fallbacks, as a racing run could."""
    archive.mkdir(exist_ok=True)
    stem, suffix = file_name.rsplit(".", 1)
    names = [file_name, f"{stem}.{STAMP}.archived.{suffix}", f"{stem}.{STAMP}.{os.getpid()}.archived.{suffix}"]
    for name in names:
        (archive / name).write_bytes(f"foreign {name}\n".encode())
    return {name: (archive / name).read_bytes() for name in names}


def _no_staging_left(tasks_dir: Path) -> bool:
    return not list(tasks_dir.rglob(".*.moving"))


def test_archive_double_collision_puts_the_record_back(tasks_dir, monkeypatch):
    """Sol r3: both archive names taken used to raise with the record only at its hidden staging name."""
    monkeypatch.setattr(str_mod, "_archive_stamp", lambda: STAMP)
    record = _terminal(tasks_dir, "twice")
    (tasks_dir / "twice.result").write_text("reply\n")
    original = record.read_bytes()
    foreign = _fill_archive_names(tasks_dir / "archive", "twice.json")

    report = str_mod.archive_terminal(tasks_dir, now=NOW, apply=True)

    row = report["records"][0]
    assert row["action"] == "skipped"
    assert row["skip_reason"] == "archive already holds twice.json and its stamped name; record left in place"
    assert record.read_bytes() == original
    assert (tasks_dir / "twice.result").read_text() == "reply\n"
    assert {name: (tasks_dir / "archive" / name).read_bytes() for name in foreign} == foreign
    assert _no_staging_left(tasks_dir)
    assert report["actions"] == {"skipped": 1}


def test_archive_double_collision_with_the_hot_name_retaken_parks_the_record_visibly(tasks_dir, monkeypatch):
    monkeypatch.setattr(str_mod, "_archive_stamp", lambda: STAMP)
    record = _terminal(tasks_dir, "twice")
    original = record.read_bytes()
    _fill_archive_names(tasks_dir / "archive", "twice.json")
    raced = _link_after_writer(
        monkeypatch, record, lambda: store._write(record, {"task_id": "twice", "status": "running"})
    )

    report = str_mod.archive_terminal(tasks_dir, now=NOW, apply=True)

    row = report["records"][0]
    parked = Path(row["recover_path"])
    assert raced and row["action"] == "error"
    assert row["error"] == f"archive already holds twice.json and its stamped name; record left at {parked}"
    assert parked.parent == tasks_dir / "archive" and parked.name.startswith("twice.json.unplaced-")
    assert parked.read_bytes() == original
    assert json.loads(record.read_text())["status"] == "running"
    assert _no_staging_left(tasks_dir)
    assert str_mod.main(["archive", "--tasks-dir", str(tasks_dir)]) == 0  # the parked file is no record


def test_archive_double_collision_on_a_sidecar_keeps_it_hot_and_reports_it(tasks_dir, monkeypatch):
    monkeypatch.setattr(str_mod, "_archive_stamp", lambda: STAMP)
    _terminal(tasks_dir, "split")
    (tasks_dir / "split.result").write_text("reply\n")
    _fill_archive_names(tasks_dir / "archive", "split.result")

    report = str_mod.archive_terminal(tasks_dir, now=NOW, apply=True)

    row = report["records"][0]
    assert row["action"] == "error" and row["moved"] == ["split.json"]
    assert "split.result not archived (archive already holds split.result and its stamped name)" in row["error"]
    assert row["error"].endswith(f"left at {tasks_dir / 'split.result'}")
    assert (tasks_dir / "split.result").read_text() == "reply\n"
    assert json.loads((tasks_dir / "archive" / "split.json").read_text())["status"] == "done"
    assert _no_staging_left(tasks_dir)

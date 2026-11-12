"""Merge rescue of a write task whose remote branch was deleted after its pull request merged (machinery #118)."""

import json
import subprocess
from contextlib import contextmanager
from functools import partial
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lu_machinery.common import github_client
from lu_machinery.dispatcher import git, ownership, worker
from lu_machinery.orchestration import pr_ownership


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.mark.parametrize(("error", "marker"), [
    ("claude_advisor_executor_unverified", None), ("agy_model_identity_unverified", False),
    ("review_gate_error", False), ("structured_output_invalid", False),
    ("agy_child_still_running: child-1", True), ("agy: incomplete run", True),
])
def test_merged_delivery_never_waives_dispatcher_integrity_refusals(monkeypatch, error, marker):
    """Defect class: merged PR evidence erased dispatcher identity, receipt and schema refusals as if
    they were provider-terminal errors. Neither an absent nor a false provider-origin marker permits rescue, and
    an AGY run that closed with its child task or background work still open is unfinished, not a rescuable failure.
    """
    record = dict(mode="workspace-write", status="failed", error=error, owned_paths=[],
                  commits_ahead=1, push_state="pushed", worktree_dirty_on_exit=False)
    if marker is not None:
        record["provider_terminal_failure"] = marker
    monkeypatch.setattr(git, "merged_delivery", Mock(return_value={
        "repo": "org/repo", "number": 7, "merge_sha": "a" * 40, "paths": ["docs/owned.md"],
    }))
    worker._enforce_delivery(record)
    assert (record["status"], record["error"]) == ("failed", error)
    assert "delivery_pr" not in record
    assert "provider_terminal_error" not in record


@pytest.mark.parametrize(("task", "number", "head", "merge", "status", "verification_error"), [
    ("cto-1949-canary-opus", 336, "18ebfefd3431ffe6938d572439ab54c490bce033",
     "20eee9cf52219bb9d0a2ddaab3bb59fa7a29f100", "done", ValueError("remote delivery tip does not match snapshot")),
    ("cto-1949-pcon-sonnet", 335, "242494b0b835b2f308c12fc9c25679f0b4619416",
     "05ab87d270a5a2c5b98251b2f96541db718de9d0", "done", subprocess.CalledProcessError(128, ["git", "fetch"])),
    ("cto-c69c-allowsync-carbon", 338, "d077d37e7b46620c546f7ada793e12e9aa72ff8c",
     "1e6c84e7d4fcb8680483f14ccae1dddac76b6094", "failed", subprocess.CalledProcessError(128, ["git", "fetch"])),
], ids=["336-replacement-tip-mismatch", "335-deleted-replacement-branch", "338-provider-error-after-merge"])
def test_merged_task_survives_missing_branch_search_and_provider_error(
        tmp_path, monkeypatch, task, number, head, merge, status, verification_error):
    """Merged workers settled as failures when a replacement branch was absent from the record and indexed
    SHA search found nothing, or when a provider reported an error after merge. Direct commit association,
    the merged PR and its task trailer prove delivery without either branch or search visibility.
    Historical records retain no lookup trace; absent search models the failed discovery, not its hidden cause.
    """
    agent = "agy" if number == 338 else "claude"
    provider_error = "agy: terminal result unsuccessful (ERROR): unclassified error" if status == "failed" else None
    record = {
        "task_id": task, "agent": agent, "mode": "workspace-write", "status": status, "error": provider_error,
        "owned_paths": ["docs/owned.md"], "worktree_remote_url": "https://github.com/org/repo.git",
        "worktree_base_ref": "origin/main", "worktree_branch": f"{agent}/{task}",
        "started_at": "2026-11-11T15:00:00+00:00", "worktree_base_sha": "c" * 40, "head_sha": head, "commits_ahead": 1,
        "push_state": "unpushed", "worktree_dirty_on_exit": False,
        "provider_terminal_failure": status == "failed",
    }

    def gh(args, **_):
        words = args[1:]
        if words[:2] == ["pr", "list"]:
            out = "[]"  # Neither the recorded branch nor indexed SHA search discovers the replacement PR.
        elif words[:2] == ["pr", "view"]:
            out = json.dumps({"state": "MERGED", "baseRefName": "main", "mergedAt": "2026-11-11T18:55:00Z",
                              "headRefOid": head, "mergeCommit": {"oid": merge}, "isCrossRepository": False})
        elif f"commits/{head}/pulls" in words[2]:
            out = json.dumps([{"number": number, "head": {"sha": head, "repo": {"full_name": "org/repo"}},
                               "base": {"repo": {"full_name": "org/repo"}}}])
        elif words[2].endswith("/commits"):
            out = json.dumps(f"deliver\n\nX-Agent: {agent}/{task}\n")
        else:
            assert words[2].endswith("/files")
            out = json.dumps("docs/owned.md")
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    @contextmanager
    def ownership_git(walked, head=None):
        if head is None:
            raise verification_error  # the branch walk fails; the merged PR's head is still served by id
        assert head == walked["head_sha"]
        yield Mock(return_value=merge)

    monkeypatch.setattr(github_client, "run", gh)
    monkeypatch.setattr(pr_ownership, "state_path", lambda: tmp_path / "ownership.json")
    monkeypatch.setattr(git, "ownership_git", ownership_git)
    monkeypatch.setattr(ownership, "delivered_paths", Mock(return_value=["docs/owned.md"]))
    worker._enforce_delivery(record)
    assert (record["status"], record["error"], record.get("delivery_pr")) == ("done", None, f"org/repo#{number}")
    assert record["delivery_merge_sha"] == merge
    if provider_error:
        assert record["provider_terminal_status"] == "failed"
        assert record["provider_terminal_error"] == provider_error


@pytest.mark.parametrize("replacement", [False, True])
@pytest.mark.parametrize("provider_status", ["done", "failed"])
@pytest.mark.parametrize(("case", "tree"), [
    ("merged", "clean"), ("open", "clean"), ("merged-other-head", "clean"),
    ("outside", "clean"), ("rename-outside", "clean"), ("wrong-base", "clean"), ("before-start", "clean"),
    ("merged", "untracked"), ("merged", "tracked"), ("merged", "staged"), ("merged", "late-commit"),
    ("foreign-trailer", "clean"), ("accepted-foreign-trailer", "clean"), ("foreign-repository", "clean"),
    ("missing-merge", "clean"),
    ("history-outside", "clean"),
    ("commit-unavailable", "clean"),
])
def test_merged_pr_proves_delivery_after_branch_and_pull_ref_are_gone(
        tmp_path, monkeypatch, case, tree, replacement, provider_status):
    """Defect class: a squash-merged task whose branch closeout deleted, and whose refs/pull/N/head the remote
    no longer advertises, or whose tree held only untracked scratch, settled no_deliverable. An open PR, a
    merged PR whose head is not the task's head, a tracked or staged change left in the tree, or one committed
    after the exit snapshot still refuses."""
    remote, work = tmp_path / "remote.git", tmp_path / "work"
    _git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    # GitHub still serves a merged pull request's commits by id after its branch is deleted.
    _git(remote, "config", "uploadpack.allowAnySHA1InWant", "true")
    _git(tmp_path, "clone", str(remote), str(work))
    for key, value in (("user.name", "t"), ("user.email", "t@example.invalid"), ("commit.gpgsign", "false")):
        _git(work, "config", key, value)
    _git(work, "commit", "--allow-empty", "-m", "base")
    base = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", "main")
    _git(work, "switch", "-c", "claude/rescue-probe")
    branch = "claude/rescue-probe-v3" if replacement else "claude/rescue-probe"
    if replacement:
        _git(work, "push", "origin", "claude/rescue-probe")
        _git(work, "switch", "-c", branch)
    (work / "docs").mkdir()
    (work / "docs" / "owned.md").write_text("delivered\n")
    _git(work, "add", "docs/owned.md")
    _git(work, "commit", "-m", "deliver", "--trailer", "X-Agent: claude/rescue-probe")
    if case == "history-outside":
        # The ownership walk sees every commit; the final PR diff omits this reverted change.
        (work / "outside.md").write_text("outside scope\n")
        _git(work, "add", "outside.md")
        _git(work, "commit", "-m", "outside", "--trailer", "X-Agent: claude/rescue-probe")
        _git(work, "rm", "outside.md")
        _git(work, "commit", "-m", "revert outside", "--trailer", "X-Agent: claude/rescue-probe")
    head = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", branch)
    # The queue squash-merges the work as a new commit on main, then closeout deletes the task branch.
    _git(work, "switch", "main")
    _git(work, "merge", "--squash", branch)
    _git(work, "commit", "-m", "deliver (#7)")
    _git(work, "push", "origin", "main", f":{branch}")
    # The task worktree stays on its branch, with the canonical origin recorded at dispatch.
    _git(work, "switch", branch)
    _git(work, "remote", "set-url", "origin", "https://github.com/org/repo.git")

    @contextmanager
    def private_remote(task_id, url):
        namespace = tmp_path / f"ns-{task_id}-{len(list(tmp_path.glob('ns-*')))}"
        namespace.mkdir()
        run = partial(git._verification_git, namespace, git_dir=namespace)
        run("init", "--bare", "--template=")
        run("config", "remote.origin.url", str(remote))
        yield run, namespace

    pr = {"state": "OPEN" if case == "open" else "MERGED", "baseRefName": "other" if case == "wrong-base" else "main",
          "mergedAt": "2026-11-08T12:00:00Z" if case == "before-start" else "2026-11-08T12:50:00Z",
          "headRefOid": "b" * 40 if case == "merged-other-head" else head,
          "mergeCommit": None if case == "missing-merge" else {"oid": _git(work, "rev-parse", "main")},
          "isCrossRepository": case == "foreign-repository"}

    real_run = github_client.run
    association_requests = []

    def transport(method, endpoint, headers, body, timeout):
        association_requests.append(endpoint)
        return github_client.Response(422, {}, b'{"message":"No commit found"}')

    client = github_client.GitHubClient(cache_dir=tmp_path / "github-cache", transport=transport,
                                        env={"GH_HOST": "github.com"})

    def gh(args, **kwargs):
        words = args[1:]
        if words[:2] == ["pr", "list"]:
            if "--search" in words:
                assert words[words.index("--search") + 1] == head
                assert words[words.index("--state") + 1] == "merged"
                out = json.dumps([{"number": 7, "isCrossRepository": False, "headRefOid": pr["headRefOid"]}])
            else:
                out = json.dumps([] if replacement else [
                    {"number": 7, "isCrossRepository": False, "headRefName": branch}])
        elif words[:2] == ["pr", "view"]:
            out = json.dumps(pr)
        elif words[2].endswith("/pulls"):
            if case == "commit-unavailable":
                return real_run(args, client=client, runner=Mock(side_effect=AssertionError("native fallthrough")),
                                **kwargs)
            out = json.dumps([{"number": 7, "head": {"sha": pr["headRefOid"], "repo": {"full_name": "org/repo"}},
                               "base": {"repo": {"full_name": "org/repo"}}}])
        elif words[2].endswith("/commits"):
            out = json.dumps("deliver\n\nX-Agent: claude/" + ("foreign" if "foreign-trailer" in case else "rescue-probe"))
        else:
            out = json.dumps("outside.md" if case == "outside" else "docs/owned.md")
            if case == "rename-outside":
                out += "\n" + json.dumps("outside.md")
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr(git, "_private_remote_git", private_remote)
    monkeypatch.setattr(github_client, "run", gh)
    monkeypatch.setattr(pr_ownership, "state_path", lambda: tmp_path / "ownership.json")
    if case == "accepted-foreign-trailer":
        # A takeover accepted the PR; its commits carry only the predecessor's trailer.
        (tmp_path / "ownership.json").write_text(json.dumps({
            "schema": 1, "last_tick": None, "episodes": {}, "handoffs": {}, "outbox": [],
            "history": [{"event": "accepted", "pr": "org/repo#7", "successor": "rescue-probe"}]}))
    # The ownership walk fetches the deleted branch, as the recorded failures did; the merged head is fetched by id.
    if not replacement:
        walk = git.ownership_git

        def ownership_git(walked, head=None):
            if head is None:
                raise subprocess.CalledProcessError(128, ["git", "fetch"])
            return walk(walked, head=head)

        monkeypatch.setattr(git, "ownership_git", ownership_git)
    assert not _git(remote, "for-each-ref", "refs/pull", f"refs/heads/{branch}")
    if replacement:
        assert _git(remote, "rev-parse", "refs/heads/claude/rescue-probe") == base
    if tree == "untracked":
        (work / ".pytest_breadcrumbs").mkdir()
        (work / ".pytest_breadcrumbs" / "breadcrumb_master.txt").write_text("scratch\n")
    elif tree in {"tracked", "staged", "late-commit"}:
        (work / "docs" / "owned.md").write_text("further work\n")
        if tree == "staged":
            _git(work, "add", "docs/owned.md")
    record = {
        "task_id": "rescue-probe", "agent": "claude", "mode": "workspace-write", "status": provider_status,
        "error": "provider failed" if provider_status == "failed" else None,
        "provider_terminal_failure": provider_status == "failed",
        "owned_paths": ["docs/owned.md"], "worktree_remote_url": "https://github.com/org/repo.git",
        "worktree_base_ref": "origin/main", "worktree_branch": "claude/rescue-probe", "publish_branch": None,
        "started_at": "2026-11-08T12:30:00+00:00", "worktree_base_sha": base, "worktree_path": str(work),
    }
    record.update(git.snapshot_delivery(SimpleNamespace(worktree=work, mode="workspace-write"), record))
    assert (record["head_sha"], record["commits_ahead"], record["worktree_dirty_on_exit"]) == (
        head, 3 if case == "history-outside" else 1, tree != "clean")
    if tree == "late-commit":
        # A surviving descendant commits after the exit snapshot: the gate reads the snapshot, not the tree.
        _git(work, "commit", "-am", "further work")
    worker._enforce_delivery(record)
    if case == "commit-unavailable":
        assert association_requests == ([f"repos/org/repo/commits/{head}/pulls"] if replacement else [])
    if (case in {"merged", "accepted-foreign-trailer"} or (case == "commit-unavailable" and not replacement)
            ) and tree in {"clean", "untracked"}:
        assert (record["status"], record["error"], record["delivery_pr"]) == ("done", None, "org/repo#7")
    elif case in {"outside", "rename-outside", "history-outside"}:
        assert (record["status"], record["error"]) == ("no_deliverable", "outside_owned_paths: outside.md")
    else:
        reason = "remote delivery tip does not match snapshot" if replacement else "git fetch failed"
        expected = (("failed", "provider failed") if provider_status == "failed" else
                    ("no_deliverable", f"owned_paths_verification_failed: {reason}"))
        assert (record["status"], record["error"]) == expected
        assert "delivery_pr" not in record
        if tree == "clean":
            assert record["merged_delivery_lookup"] == "no_match"

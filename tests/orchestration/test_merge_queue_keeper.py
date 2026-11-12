"""Keeper admission/revocation cases ported from the product suite for F2."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lu_machinery.ci.deploy_paths import DeployPaths
from lu_machinery.orchestration import integration_sweep as sweep
from lu_machinery.orchestration import merge_queue_keeper as keeper
from lu_machinery.orchestration.integration_sweep import Verdict
from lu_machinery.review.record_cf_verdict import build_comment

from tests.helpers.product_tree import configured_roots as configured_roots

HEAD_A = "a" * 40
HEAD_B = "b" * 40



@pytest.fixture(autouse=True)
def product_policy(monkeypatch: pytest.MonkeyPatch, configured_roots) -> None:
    """Return the keeper policy, read through the hermetic product tree."""
    policy = DeployPaths(
        content_path_prefixes=("curriculum/",),
        site_code_path_exceptions=frozenset(),
        site_code_path_prefixes=("site/",),
    )
    product = Path(configured_roots.product)

    def load(product_root: Path | None = None) -> DeployPaths:
        if product_root != product:
            raise AssertionError("deploy policy was not read from the hermetic product tree")
        return policy

    monkeypatch.setattr(sweep, "load_deploy_paths", load)


def pr(**changes: Any) -> dict[str, Any]:
    row = {
        "id": "PR_node_42",
        "number": 42,
        "title": "A change",
        "isDraft": False,
        "headRefOid": HEAD_A,
        "baseRefName": "main",
        "isInMergeQueue": False,
        "mergeStateStatus": "CLEAN",
        "autoMergeRequest": None,
        "labels": {"totalCount": 0, "pageInfo": {"hasNextPage": False}, "nodes": []},
    }
    row.update(changes)
    return row


def checks(head: str = HEAD_A, conclusion: str = "success", status: str = "completed") -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "head_sha": head,
            "status": status,
            "conclusion": conclusion,
            "started_at": "2026-09-23T00:00:00Z",
        }
        for name in ("CI Gate", "Analyze (python)")
    ]


def recovery_comment(head: str = HEAD_A) -> dict[str, Any]:
    data = {
        "pr": 42,
        "head": head,
        "run_id": 123,
        "job_ids": [456],
        "tested_sha": "c" * 40,
        "queue_group_diff": {"base_sha": "d" * 40, "head_sha": "c" * 40, "paths": ["scripts/earlier-pr.py", "scripts/this-pr.py"]},
        "failure_evidence": "Runner outage in linked job log",
        "unrelated_to_diff": "Runner failed before checkout",
    }
    return {
        "id": 789,
        "body": "<!-- ci-recovery-evidence " + json.dumps(data) + " -->",
        "user": {"login": "driver"},
        "author_association": "MEMBER",
    }


class FakeGitHub:
    root = Path.cwd()
    repository = "github.com/unit/public"

    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self.row = row or pr()
        self.fresh = dict(self.row)
        self.fresh["state"] = "OPEN"
        self.fresh["labels"] = []
        self.check_rows = checks(self.row["headRefOid"])
        self.queue_enabled = True
        self.remaining = 3000
        self.membership_result = True
        self.comments_rows: list[dict[str, Any]] = [
            recorded("APPROVED", "2026-09-23T12:00:00.000001+00:00", self.row["headRefOid"]),
            recovery_comment(self.row["headRefOid"]),
        ]
        self.actions: list[tuple[str, Any]] = []
        self.fail_removal = False
        self.removal_read_failure = False
        self.events: list[dict[str, Any]] = []
        self.run_rows: list[dict[str, Any]] = []
        self.job_rows: list[dict[str, Any]] = []
        self.issue_rows: list[dict[str, Any]] = []
        self.squash: bool | None = False
        self.files = ["scripts/example.py"]

    def squash_blocked(self, number: int, head: str) -> bool | None:
        self.actions.append(("squash-read", (number, head)))
        return self.squash

    def branch_names(self) -> set[str]:
        return {self.row["baseRefName"]}

    def snapshot(self, branches: set[str]) -> dict[str, Any]:
        return {
            "prs": [self.row],
            "queues": {self.row["baseRefName"]: self.queue_enabled},
            "remaining": self.remaining,
            "cost": 10,
        }

    def identity(self) -> str:
        return "driver"

    def comments(self, number: int) -> list[dict[str, Any]]:
        return list(self.comments_rows)

    def pr_file_rows(self, number: int) -> list[dict[str, Any]]:
        return [{"filename": path, "status": "modified"} for path in self.files]

    def pr_files(self, number: int) -> list[str]:
        return keeper.pr_file_paths(self.pr_file_rows(number))

    def checks(self, head: str) -> list[dict[str, Any]]:
        return self.check_rows

    def current(self, number: int) -> dict[str, Any]:
        return self.fresh

    def enqueue(self, number: int, head: str) -> None:
        if self.row.get("isInMergeQueue") is True:
            self.actions.append(("cleanup", (number, head)))
            return
        self.actions.append(("enqueue", (number, head)))

    def membership(self, number: int) -> bool:
        return self.membership_result

    def dequeue(self, node_id: str) -> None:
        self.actions.append(("dequeue", node_id))
        if self.fail_removal:
            raise keeper.KeeperError("remove failed")

    def disarm(self, number: int) -> None:
        self.actions.append(("disarm", number))

    def comment(self, number: int, body: str) -> None:
        self.actions.append(("comment", body))
        self.comments_rows.append({"body": body, "user": {"login": "driver"}})

    def timeline(self, number: int) -> list[dict[str, Any]]:
        return self.events

    def created_at(self, number: int) -> str:
        return "2026-09-01T00:00:00Z"

    def runs(self, since: str) -> list[dict[str, Any]]:
        return self.run_rows

    def jobs(self, run_id: int) -> list[dict[str, Any]]:
        return self.job_rows

    def issues(self, title: str) -> list[dict[str, Any]]:
        return self.issue_rows

    def issue_comment(self, number: int, body: str) -> None:
        self.actions.append(("issue-comment", (number, body)))

    def create_issue(self, title: str, body: str) -> int:
        self.actions.append(("issue", title))
        number = 1000 + len(self.issue_rows)
        self.issue_rows.append({"title": title, "body": body, "number": number})
        return number


def run(
    fake: FakeGitHub, path: Path, monkeypatch: pytest.MonkeyPatch, verdict: str = "APPROVED", *, apply: bool = True
) -> tuple[list[str], bool]:
    reason = "comments-unavailable" if verdict == "unknown" else None
    monkeypatch.setattr(keeper, "lookup_verdict", lambda comments, head, login, **_: Verdict(verdict, reason=reason))
    monkeypatch.setattr(keeper, "_ever_approved", lambda comments, login: verdict == "APPROVED")
    return keeper.run(fake, path, apply=apply)


def mutations(fake: FakeGitHub) -> list[str]:
    return [kind for kind, _ in fake.actions if kind not in {"squash-read", "cleanup"}]


def recorded(verdict: str, started: str, head: str = HEAD_A, *, review_mode: str = "cross_family") -> dict[str, Any]:
    return {
        "body": build_comment(
            sha=head,
            task_id="review",
            started=started,
            verdict=verdict,
            model="gpt-6.1-sol",
            family="openai",
            reply="VERDICT: " + verdict,
            review_mode=review_mode,
        ),
        "user": {"login": "driver"},
        "author_association": "MEMBER",
        "created_at": "2026-09-23T13:00:00Z",
        "updated_at": "2026-09-23T13:00:00Z",
    }


def _keeper_comment(fake: FakeGitHub) -> str:
    bodies = [body for kind, body in fake.actions if kind == "comment"]
    assert len(bodies) == 1
    return bodies[0]


def test_queued_with_cf_at_head_is_kept(tmp_path: Path) -> None:
    fake = FakeGitHub(pr(isInMergeQueue=True))
    fake.check_rows.extend([
        {"name": "Entire Gates", "head_sha": HEAD_A, "status": "completed", "conclusion": "failure",
         "app": {"id": 2626939, "slug": "entire"}},
        {"name": "Entire Gates", "head_sha": HEAD_A, "status": "in_progress", "conclusion": None,
         "app": {"id": 2626939, "slug": "entire"}},
        {"name": "Reject negated closing references", "head_sha": HEAD_A,
         "status": "completed", "conclusion": "failure"},
    ])
    fake.comments_rows = [recorded("APPROVED", "2026-09-23T12:00:00.000001+00:00")]
    path = tmp_path / "state.json"
    lines, failed = keeper.run(fake, path, apply=True)
    assert not failed
    assert mutations(fake) == []
    assert ("cleanup", (42, HEAD_A)) not in fake.actions
    assert "reason=ready" in lines[0]
    assert not any("revoked" in line for line in lines)
    assert json.loads(path.read_text())["queued"]["42"] == HEAD_A


def test_queued_without_cf_at_head_is_revoked(tmp_path: Path) -> None:
    fake = FakeGitHub(pr(isInMergeQueue=True))
    fake.comments_rows = []
    path = tmp_path / "state.json"
    lines, failed = keeper.run(fake, path, apply=True)
    assert not failed
    assert mutations(fake) == ["dequeue", "comment"]
    assert ("dequeue", "PR_node_42") in fake.actions
    assert "#42 revoked: needs-CF" in lines
    assert "reason=needs-CF" in lines[0]
    body = _keeper_comment(fake)
    assert body == (
        "Merge queue keeper: #42 was not queued because needs-CF.\n\n"
        f"<!-- mq-keeper head={HEAD_A} reason=needs-CF -->"
    )
    state = json.loads(path.read_text())
    assert "42" not in state["queued"]
    assert state["drops"][f"42:{HEAD_A}"] == 1
    assert f"42:{HEAD_A}" not in state.get("pending_comments", {})
    assert f"42:{HEAD_A}" not in state.get("squash_revoked", {})


def test_queued_cf_only_at_old_head_is_revoked(tmp_path: Path) -> None:
    fake = FakeGitHub(pr(isInMergeQueue=True, headRefOid=HEAD_B))
    fake.fresh["headRefOid"] = HEAD_B
    fake.check_rows = checks(HEAD_B)
    fake.comments_rows = [recorded("APPROVED", "2026-09-23T12:00:00.000001+00:00", head=HEAD_A)]
    lines, failed = keeper.run(fake, tmp_path / "state.json", apply=True)
    assert not failed
    assert mutations(fake) == ["dequeue", "comment"]
    assert "#42 revoked: needs-CF" in lines
    assert "reason=needs-CF" in lines[0]
    body = _keeper_comment(fake)
    assert f"head={HEAD_B} reason=needs-CF" in body
    assert "was not queued because needs-CF." in body


def test_queued_recorded_rejection_revokes(tmp_path: Path) -> None:
    fake = FakeGitHub(pr(isInMergeQueue=True))
    fake.comments_rows = [
        recorded("APPROVED", "2026-09-23T12:00:00.000001+00:00"),
        recorded("CHANGES_REQUESTED", "2026-09-23T12:00:01.000001+00:00"),
    ]
    approval = recorded("APPROVED", "2026-09-23T12:00:02.000001+00:00")
    approval.update(created_at="2026-09-23T13:10:00Z", updated_at="2026-09-23T13:10:00Z")
    fake.comments_rows.append(approval)
    lines, failed = keeper.run(fake, tmp_path / "state.json", apply=True)
    assert not failed
    assert ("dequeue", "PR_node_42") in fake.actions
    assert "CF-changes_requested" in lines[0]


def test_queued_recorded_approval_then_hold_revokes(tmp_path: Path) -> None:
    fake = FakeGitHub(pr(isInMergeQueue=True))
    fake.comments_rows = [recorded("APPROVED", "2026-09-23T12:00:00.000001+00:00")]
    fake.fresh["labels"] = [{"name": "hold"}]
    keeper.run(fake, tmp_path / "state.json", apply=True)
    assert ("dequeue", "PR_node_42") in fake.actions


def test_queued_verdict_lookup_error_reports_without_revocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGitHub(pr(isInMergeQueue=True))
    monkeypatch.setattr(
        keeper, "lookup_verdict", lambda *args, **_: (_ for _ in ()).throw(keeper.KeeperError("lookup failed"))
    )
    lines, failed = keeper.run(fake, tmp_path / "state.json", apply=True)
    assert not failed
    assert mutations(fake) == []
    assert "reason=CF-unknown" in lines[0]


def test_queued_approval_then_unknown_marker_revokes(tmp_path: Path) -> None:
    fake = FakeGitHub(pr(isInMergeQueue=True))
    fake.comments_rows = [recorded("APPROVED", "2026-09-23T12:00:00.000001+00:00")]
    path = tmp_path / "state.json"
    keeper.run(fake, path, apply=True)
    assert mutations(fake) == []
    assert ("cleanup", (42, HEAD_A)) not in fake.actions
    fake.comments_rows[0]["updated_at"] = "2026-09-23T13:00:01Z"
    keeper.run(fake, path, apply=True)
    assert ("dequeue", "PR_node_42") in fake.actions


def test_cf_unknown_lookup_failure_names_reason(tmp_path: Path) -> None:
    fake = FakeGitHub()

    def broken(number: int) -> list[dict[str, Any]]:
        raise keeper.KeeperError("comments unavailable")

    fake.comments = broken  # type: ignore[method-assign]
    lines, _failed = keeper.run(fake, tmp_path / "state.json", apply=False)
    assert lines[0].endswith(" cf-unknown=comments-unavailable comment=none")


def test_queued_stale_recorded_approval_is_revoked(tmp_path: Path) -> None:
    fake = FakeGitHub(pr(isInMergeQueue=True, headRefOid=HEAD_B))
    fake.check_rows = checks(HEAD_B)
    fake.comments_rows = [recorded("APPROVED", "2026-09-23T12:00:00.000001+00:00")]
    lines, failed = keeper.run(fake, tmp_path / "state.json", apply=True)
    assert not failed
    assert ("dequeue", "PR_node_42") in fake.actions
    assert "#42 revoked: needs-CF" in lines
    assert "reason=needs-CF" in lines[0]


def test_enqueue_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGitHub()
    lines, failed = run(fake, tmp_path / "state.json", monkeypatch)
    assert not failed
    assert mutations(fake) == ["enqueue"]
    assert fake.actions[0][1] == (42, HEAD_A)
    assert "#42 enqueued" in lines


def test_low_scoped_pair_cannot_replace_a_whole_head_non_author_approval(tmp_path: Path) -> None:
    """Scoped co-author reviews must never admit or keep a Low head queued."""
    for queued in (False, True):
        fake = FakeGitHub(pr(isInMergeQueue=queued))
        fake.files = ["docs/example.md"]
        whole = recorded("APPROVED", "2026-09-23T12:00:00.000001+00:00")
        whole["body"] = build_comment(
            sha=HEAD_A, task_id="non-author", started="2026-09-23T12:00:00.000001+00:00",
            verdict="APPROVED", model="gpt-6.1-sol", family="openai",
            author_models=["gpt-6.1-sol"], reply="VERDICT: APPROVE",
        )
        fake.comments_rows = [whole]
        lines, failed = keeper.run(fake, tmp_path / f"whole-{queued}.json", apply=True)
        assert not failed
        assert "reason=ready" in lines[0]
        assert mutations(fake) == ([] if queued else ["enqueue"])

        fake = FakeGitHub(pr(isInMergeQueue=queued))
        fake.files = ["docs/example.md"]
        fake.comments_rows = []
        for family, model, scope in (
            ("openai", "gpt-6.1-sol", "c" * 40),
            ("anthropic", "claude-opus-5-5", "d" * 40),
        ):
            scoped = dict(whole)
            scoped["body"] = build_comment(
                sha=HEAD_A, task_id=f"co-author-{family}", started="2026-09-23T12:00:00.000001+00:00",
                verdict="APPROVED", model=model, family=family,
                author_models=["gpt-6.1-sol", "claude-opus-5-5"],
                scope_commits=[scope], reply="VERDICT: APPROVE",
            )
            fake.comments_rows.append(scoped)
        lines, failed = keeper.run(fake, tmp_path / f"scoped-{queued}.json", apply=True)
        assert not failed
        assert "reason=needs-CF" in lines[0]
        assert "enqueue" not in mutations(fake)
        assert ("dequeue" in mutations(fake)) is queued
        assert "42" not in json.loads((tmp_path / f"scoped-{queued}.json").read_text())["approved"]

        fake = FakeGitHub(pr(isInMergeQueue=queued))
        fake.comments_rows = [whole]
        lines, failed = keeper.run(fake, tmp_path / f"medium-{queued}.json", apply=True)
        assert not failed
        assert "reason=needs-CF" in lines[0]
        assert "enqueue" not in mutations(fake)
        assert ("dequeue" in mutations(fake)) is queued


def test_queued_security_pr_with_one_review_stays_queued(tmp_path: Path) -> None:
    """Defect class: the keeper revokes a security-sensitive PR that has its one review of record."""
    fake = FakeGitHub(pr(isInMergeQueue=True))
    fake.files = ["lu_machinery/publish/github.py"]
    fake.comments_rows = [recorded("APPROVED", "2026-09-23T12:00:00.000001+00:00")]
    lines, failed = keeper.run(fake, tmp_path / "state.json", apply=True)
    assert not failed
    assert ("dequeue", "PR_node_42") not in fake.actions
    assert not any("revoked" in line for line in lines)


@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize("rejected", [False, True])
def test_machinery_carbon_debt_admission_preserves_rejections(tmp_path: Path, queued, rejected) -> None:
    """Defect class: machinery missing Carbon is revoked, or Carbon rejection is waived as debt."""
    fake = FakeGitHub(pr(isInMergeQueue=queued))
    fake.repository = "github.com/learn-ukrainian/machinery"
    fake.files = ["lu_machinery/publish/github.py"]
    if rejected:
        fake.comments_rows.append({
            **fake.comments_rows[0],
            "body": build_comment(
                sha=HEAD_A, task_id="review-carbon", started="2026-09-23T12:00:00.000001+00:00",
                verdict="CHANGES_REQUESTED", model="gemini-4-carbon-medium", family="google",
                reply="VERDICT: REQUEST_CHANGES", second_review=True, author_models=("gpt-6.1-sol",),
            ),
        })
    _lines, failed = keeper.run(fake, tmp_path / "state.json", apply=True)
    assert not failed
    if rejected:
        assert "enqueue" not in mutations(fake)
        assert ("dequeue" in mutations(fake)) is queued
    else:
        assert "dequeue" not in mutations(fake)
        assert ("enqueue" in mutations(fake)) is (not queued)

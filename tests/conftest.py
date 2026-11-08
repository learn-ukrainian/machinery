"""Focused recorder/publisher fixtures ported from the product test suite."""

import json
import subprocess

import pytest

from lu_machinery.orchestration import integration_sweep as sweep
from lu_machinery.publish import github as publisher
from lu_machinery.review import record_cf_verdict as recorder

HEAD = "a" * 40
BASE = "b" * 40
REPOSITORY = "unit/public"
BRANCH = "codex/fixture"


@pytest.fixture
def low_review(monkeypatch, tmp_path):
    """Keep real verdict parsing/qualification; replace GitHub and branch facts."""
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    task = {
        "task_id": "review-one", "repository": REPOSITORY,
        "worktree_branch": BRANCH, "worktree_base_sha": HEAD,
        "model": "gpt-6.1-sol", "agent": "codex", "status": "done",
        "started_at": "2026-09-23T12:00:00.000001+00:00",
    }
    (tasks / "review-one.json").write_text(json.dumps(task))
    (tasks / "review-one.result").write_text("VERDICT: APPROVE")
    files = [{"filename": "docs/example.md", "status": "modified"}]
    facts = recorder.BranchReviewFacts(
        repository=REPOSITORY, base_tip_sha=BASE, head_sha=HEAD, merge_base_sha=BASE,
        commits=(recorder.CommitAttribution(
            HEAD, "openai", "trailer-model", task_id="codex/author-one", author_models=("gpt-6.1-sol",),
        ),),
        existing_families=frozenset({"openai"}), incoming_writer=None, incoming_family=None,
        changed_paths=("docs/example.md",), owned_paths=(), subject_seats=frozenset(),
        subject_families=frozenset(), subject_evidence=(),
    )
    comments = []
    statuses = []
    monkeypatch.setattr(recorder, "_review_repository_root", lambda *_: tmp_path)
    monkeypatch.setattr(recorder, "pr_review_facts", lambda *_, **__: facts)
    monkeypatch.setattr(recorder, "budget_admission", lambda **_: {})
    monkeypatch.setattr(recorder, "check_texts", lambda *_, **__: None)
    monkeypatch.setattr(recorder, "absolute_path_spans", lambda _: ())
    monkeypatch.setattr(recorder, "post_commit_status", lambda **fields: statuses.append(fields))
    monkeypatch.setattr(recorder.GitHubAdapter, "identity", lambda _: "fleet")
    monkeypatch.setattr(recorder.GitHubAdapter, "comments", lambda *_, **__: list(comments))
    monkeypatch.setattr(recorder.GitHubAdapter, "pr_file_rows", lambda *_, **__: files)
    monkeypatch.setattr(recorder, "_pr", lambda *_: {
        "number": 1, "headRefOid": HEAD, "headRefName": BRANCH,
    })
    monkeypatch.setattr(recorder, "_run_json", lambda *_a, **_kw: {
        "number": 1, "headRefOid": HEAD, "headRefName": BRANCH,
    })

    def post(_repository, _number, body, _login):
        comment = {
            "id": 1, "body": body, "user": {"login": "fleet"}, "author_association": "MEMBER",
            "created_at": "2026-09-23T13:00:00Z", "updated_at": "2026-09-23T13:00:00Z",
        }
        comments.append(comment)
        return comment

    monkeypatch.setattr(recorder, "_post_verdict", post)
    monkeypatch.setattr(publisher.gate, "catalog", lambda: {
        "public": {"github": REPOSITORY, "verdict_recorder": True},
    })
    # These fixtures contain no learner paths. Product content policy is independent.
    monkeypatch.setattr(sweep, "changes_learner_content", lambda _: False)

    def send(args, **_kwargs):
        if args[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(args, 0, json.dumps({"headRefOid": HEAD, "statusCheckRollup": []}), "")
        endpoint = args[4]
        value = {"login": "fleet"} if endpoint.startswith("user") else (
            [comments] if "/comments?" in endpoint else [files]
        )
        return subprocess.CompletedProcess(args, 0, json.dumps(value), "")

    def admit():
        publisher._refuse_incomplete_seats(
            "github.com/" + REPOSITORY, 1, HEAD, send, tmp_path, {},
        )

    return tasks, comments, statuses, files, admit

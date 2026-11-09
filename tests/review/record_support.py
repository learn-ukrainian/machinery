"""Verbatim fixture closure from LU 5aba2adb:tests/test_record_cf_verdict.py.

Only imports are adapted with the package-3 codemods; no test functions are copied.
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest
from lu_machinery.orchestration.fleet_repos import FleetRepo
from lu_machinery.review import record_cf_verdict as recorder












SHA = "a" * 40


OTHER = "b" * 40


BRANCH = "codex/42"


REPOSITORY = "owner/repo"


@pytest.fixture(autouse=True)
def recorder_matcher(synthetic_opsec):
    from tests.opsec_fixtures import synthetic_rules

    rules = synthetic_rules(rule="3-absolute-path", level=3, pattern=r"(?<![<\w:])/[A-Za-z][^\s`'\"<>),;\]}|*]*")
    (synthetic_opsec / "rules.json").write_text(json.dumps(rules))


def task(**updates):
    data = {
        "repository": REPOSITORY,
        "worktree_branch": BRANCH,
        "worktree_base_sha": SHA,
        "model": "gpt-6.1-sol",
        "agent": "codex",
        "started_at": "2026-09-23T12:00:00.000001+00:00",
        "status": "done",
    }
    data.update(updates)
    return data


def write_task(root: Path, *, task_id="review-one", reply="VERDICT: APPROVE", **updates):
    root.mkdir(parents=True, exist_ok=True)
    (root / task_id).parent.mkdir(parents=True, exist_ok=True)
    record = task(**updates)
    if record.get("schema_version") == 2:
        record["task_id"] = task_id
    (root / f"{task_id}.json").write_text(json.dumps(record))
    (root / f"{task_id}.result").write_text(reply)


def facts_for(families, *, changed_paths=(), subject_seats=frozenset()):
    """Branch facts for recorder tests that do not exercise Git enumeration."""
    return recorder.BranchReviewFacts(
        repository=REPOSITORY,
        base_tip_sha=OTHER,
        head_sha=SHA,
        merge_base_sha=OTHER,
        commits=tuple(
            recorder.CommitAttribution(
                None,
                family,
                "trailer-model",
                author_models=(
                    {"openai": "gpt-6-luna", "google": "gemini-3.8-flash-high", "anthropic": "claude-opus-5-5"}.get(
                        family, "gpt-6-luna"
                    ),
                ),
            )
            for family in sorted(families)
        ),
        existing_families=frozenset(families),
        incoming_writer=None,
        incoming_family=None,
        changed_paths=tuple(changed_paths),
        owned_paths=(),
        subject_seats=frozenset(subject_seats),
        subject_families=frozenset(),
        subject_evidence=(),
    )


def setup_record(monkeypatch, tmp_path, *, head=SHA, branch=BRANCH, families=None, status_error=False):
    tasks = tmp_path / "tasks"
    write_task(tasks)
    comments = []
    calls = {"posts": 0, "statuses": 0}
    # These unit tests replace Git facts; integration coverage uses real catalog
    # resolution and independent object stores in authoring_review_feasibility.
    catalog = {**recorder.load_fleet_repos(), "unit": FleetRepo("unit", REPOSITORY, "unit", "test")}
    monkeypatch.setattr(recorder, "load_fleet_repos", lambda: catalog)
    monkeypatch.setattr(recorder, "resolve_review_repository", lambda root, _key, **_kwargs: root)

    def fake_json(args, *, input_text=None):
        from lu_machinery.publish.github import Request

        if isinstance(args, Request):
            if args.verb == "issue-comment-json":
                input_text = json.dumps({"body": args.fields["body"]})
                args = ["gh", "api", "-X", "POST"]
            elif args.verb == "read-comment":
                args = ["gh", "api", "issues/comments/" + str(args.fields["number"])]
            else:
                raise AssertionError(args.verb)
        if args[:3] == ["gh", "pr", "view"]:
            return {"number": 42, "headRefOid": head, "headRefName": branch, "state": "OPEN"}
        if args[:3] == ["gh", "pr", "list"]:
            return [{"number": 42, "headRefOid": head, "headRefName": branch}]
        if args[:4] == ["gh", "api", "-X", "POST"]:
            calls["posts"] += 1
            item = {
                "id": calls["posts"],
                "body": json.loads(input_text)["body"],
                "user": {"login": "fleet"},
                "author_association": "MEMBER",
                "created_at": "2026-09-23T13:00:00Z",
                "updated_at": "2026-09-23T13:00:00Z",
            }
            comments.append(item)
            return item
        if args[:2] == ["gh", "api"] and "issues/comments/" in args[2]:
            return comments[-1]
        raise AssertionError(args)

    monkeypatch.setattr(recorder, "_run_json", fake_json)
    monkeypatch.setattr(
        recorder,
        "pr_review_facts",
        lambda repository, number, **kwargs: facts_for(families if families is not None else {"google"}),
    )
    monkeypatch.setattr(recorder.GitHubAdapter, "identity", lambda self: "fleet")
    monkeypatch.setattr(recorder.GitHubAdapter, "comments", lambda self, repository, number: list(comments))
    # No repair-verification round applies unless a test builds a repository (repair_case).
    monkeypatch.setattr(recorder, "_repair_round", lambda **_kwargs: None)
    # No head proves a clean base merge descent unless a test builds a repository (exemption_case).
    monkeypatch.setattr(recorder, "_clean_merge_descent_prover", lambda *_a, **_k: lambda *_b: None)

    def fake_status(**kwargs):
        calls["statuses"] += 1
        if status_error:
            raise RuntimeError("status write failed")

    monkeypatch.setattr(recorder, "post_commit_status", fake_status)
    return tasks, comments, calls


def recorded_verdict(sha, verdict, *, task_id="prior-review", mode="cross_family", edited=False):
    """A trusted verdict comment the keeper reads, as the recorder posts it (``edited`` changes it later)."""
    body = recorder.build_comment(
        sha=sha,
        task_id=task_id,
        started="2026-09-23T11:00:00.000001+00:00",
        verdict=verdict,
        model="claude-opus-5-5",
        family="anthropic",
        reply=f"VERDICT: {verdict}",
        review_mode=mode,
    )
    stamp = "2026-09-23T11:30:00Z"
    return {
        "body": body,
        "user": {"login": "fleet"},
        "author_association": "MEMBER",
        "created_at": stamp,
        "updated_at": "2026-09-23T11:45:00Z" if edited else stamp,
    }

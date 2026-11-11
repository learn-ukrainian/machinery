"""Tiered landing regressions, ported from the recorder and publisher tests.

The source tests live in learn-ukrainian's test_record_cf_verdict.py and
publish/test_pr_merge_squash_scan.py. Machinery has no test suite yet.
"""

from __future__ import annotations

import pytest

from lu_machinery.publish import github as publisher
from lu_machinery.review import record_cf_verdict as recorder
from lu_machinery.review.tiers import classify_pr_files


def test_low_same_family_non_author_approve_passes_recorder_and_landing(low_review, tmp_path):
    """A qualified second Sol task must approve a Low head without another family."""
    tasks, comments, statuses, _files, admit = low_review
    result = recorder.record("review-one", pr_number=1, task_root=tasks, lock_root=tmp_path / "locks")
    assert result["verdict"] == "APPROVED"
    assert len(comments) == 1
    assert statuses[-1]["state"] == "success"
    assert admit() is None


def test_low_authors_own_approve_is_refused(low_review, tmp_path):
    """A different spelling of the author task must not bypass non-authorship."""
    tasks, comments, statuses, _files, _admit = low_review
    (tasks / "author-one.json").write_bytes((tasks / "review-one.json").read_bytes().replace(
        b'"review-one"', b'"author-one"',
    ))
    (tasks / "author-one.result").write_text("VERDICT: APPROVE")
    with pytest.raises(recorder.RecordError, match="SELF_REVIEW_DETECTED"):
        recorder.record("author-one", pr_number=1, task_root=tasks, lock_root=tmp_path / "locks")
    assert comments == [] and statuses == []


def test_low_without_approve_is_refused_at_landing(low_review):
    """Low never waives the recorded exact-head approval requirement."""
    _tasks, comments, _statuses, _files, admit = low_review
    assert comments == []
    with pytest.raises(publisher.gate.PublishBlocked, match="no non-author APPROVE"):
        admit()


def test_deleted_github_path_keeps_high_tier():
    """Removing a workflow must not receive the ordinary unused-code deletion waiver."""
    assert classify_pr_files([{"filename": ".github/workflows/old.yml", "status": "removed"}]) == "high"


def test_mixed_pr_takes_highest_tier():
    """Docs cannot dilute an ordinary code, learner-content, or protected change."""
    files = [
        {"filename": "docs/example.md", "status": "modified"},
        {"filename": "lu_machinery/common/unused.py", "status": "removed"},
    ]
    assert classify_pr_files(files) == "low"
    files.append({"filename": "lu_machinery/common/example.py", "status": "modified"})
    assert classify_pr_files(files) == "medium"
    files.append({"filename": "curriculum/l2-uk-en/a1/example.md", "status": "removed"})
    assert classify_pr_files(files) == "ukrainian-content"
    files.append({"filename": ".github/workflows/ci.yml", "status": "modified"})
    assert classify_pr_files(files) == "high"


# Binding Carbon second review (operator 2026-11-11).
SECURITY_FILES = (
    "scripts/audit/secret_scan_local.py",
    "scripts/ocr/_credentials.py",
    "packages/v4-runtime/src/learn_ukrainian_v4_runtime/credential_custody.py",
)


@pytest.mark.parametrize("path", SECURITY_FILES)
def test_security_path_outside_the_high_table_requires_second_review(path):
    """Defect class: a security-sensitive file the High table misses lands without Carbon."""
    from lu_machinery.review.tiers import requires_second_review

    rows = [{"filename": path, "status": "modified"}]
    assert requires_second_review(rows) is True
    if path.startswith("packages/"):
        assert classify_pr_files(rows) == "medium"
    assert requires_second_review([{"filename": "docs/example.md", "status": "modified"}]) is False


def _carbon_task(tasks, name="review-carbon", *, agent="agy", model="gemini-4-carbon-medium"):
    import json

    task = json.loads((tasks / "review-one.json").read_text())
    task.update(task_id=name, agent=agent, model=model)
    (tasks / f"{name}.json").write_text(json.dumps(task))
    (tasks / f"{name}.result").write_text("VERDICT: APPROVE")
    return name


def _record_of_record(comments):
    comments.append({
        "id": 2, "user": {"login": "fleet"}, "author_association": "MEMBER",
        "created_at": "2026-09-23T13:00:00Z", "updated_at": "2026-09-23T13:00:00Z",
        "body": recorder.build_comment(
            sha="a" * 40, task_id="review-opus", started="2026-09-23T12:00:00.000001+00:00",
            verdict="APPROVED", model="claude-opus-5-5", family="anthropic", reply="VERDICT: APPROVE",
            author_models=("gpt-6.1-sol",),
        ),
    })


def test_carbon_is_refused_as_the_reviewer_of_record(low_review, tmp_path):
    """Defect class: Carbon is recorded as the reviewer of record and so replaces it."""
    tasks, comments, statuses, files, _admit = low_review
    files[:] = [{"filename": SECURITY_FILES[0], "status": "modified"}]
    name = _carbon_task(tasks)
    with pytest.raises(recorder.RecordError, match="never the reviewer of record"):
        recorder.record(name, pr_number=1, task_root=tasks, lock_root=tmp_path / "locks")
    assert comments == [] and statuses == []


@pytest.mark.parametrize(
    ("agent", "model", "paths", "reason"),
    [
        ("claude", "claude-opus-5-5", SECURITY_FILES[:1], "not the catalog's second reviewer"),
        ("agy", "gemini-4-argon-medium", SECURITY_FILES[:1], "holds no review"),
        ("claude", "gemini-4-carbon-medium", SECURITY_FILES[:1], "runs only through agy"),
        ("agy", "gemini-4-carbon-medium", ("docs/example.md",), "neither High tier nor security-sensitive"),
    ],
)
def test_second_review_token_needs_completed_task_provenance(low_review, tmp_path, agent, model, paths, reason):
    """Defect class: --second-review issues the Carbon token for another model, route or an unrequired PR."""
    tasks, comments, statuses, files, _admit = low_review
    files[:] = [{"filename": path, "status": "modified"} for path in paths]
    name = _carbon_task(tasks, agent=agent, model=model)
    with pytest.raises(recorder.RecordError, match=reason):
        recorder.record(name, pr_number=1, task_root=tasks, lock_root=tmp_path / "locks", second_review=True)
    assert comments == [] and statuses == []


def test_unreadable_pr_files_keep_the_second_review_required(low_review, tmp_path, monkeypatch):
    """Defect class: a file-list failure defaults the requirement to false and waives Carbon."""
    tasks, comments, statuses, _files, _admit = low_review

    def unavailable(*_args, **_kwargs):
        raise recorder.SweepError("files unavailable")

    monkeypatch.setattr(recorder.GitHubAdapter, "pr_file_rows", unavailable)
    name = _carbon_task(tasks)
    result = recorder.record(name, pr_number=1, task_root=tasks, lock_root=tmp_path / "locks", second_review=True)
    assert result["verdict"] == "APPROVED" and "second_review=carbon" in comments[-1]["body"]
    # Carbon alone never approves: the status waits for the reviewer of record.
    assert statuses[-1]["state"] == "pending"


def test_enqueue_refuses_a_security_pr_without_the_carbon_second_review(low_review):
    """Defect class: a security-sensitive PR enqueues on the reviewer of record alone."""
    _tasks, comments, _statuses, files, admit = low_review
    files[:] = [{"filename": SECURITY_FILES[0], "status": "modified"}]
    _record_of_record(comments)
    with pytest.raises(publisher.gate.PublishBlocked, match="Carbon second review has not approved"):
        admit()


def test_enqueue_admits_a_security_pr_with_both_reviews(low_review, tmp_path):
    """Defect class: a correctly double-reviewed security PR is refused at enqueue."""
    tasks, comments, statuses, files, admit = low_review
    files[:] = [{"filename": SECURITY_FILES[0], "status": "modified"}]
    _record_of_record(comments)
    name = _carbon_task(tasks)
    result = recorder.record(name, pr_number=1, task_root=tasks, lock_root=tmp_path / "locks", second_review=True)
    assert result["verdict"] == "APPROVED"
    assert statuses[-1]["state"] == "success"
    assert admit() is None

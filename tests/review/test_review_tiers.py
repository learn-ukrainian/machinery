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
    files = [{"filename": "docs/example.md", "status": "modified"}]
    assert classify_pr_files(files) == "low"
    files.append({"filename": "lu_machinery/common/example.py", "status": "modified"})
    assert classify_pr_files(files) == "medium"
    files.append({"filename": "curriculum/l2-uk-en/a1/example.md", "status": "removed"})
    assert classify_pr_files(files) == "ukrainian-content"
    files.append({"filename": ".github/workflows/ci.yml", "status": "modified"})
    assert classify_pr_files(files) == "high"

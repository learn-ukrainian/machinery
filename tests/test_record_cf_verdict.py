"""Existing recorder invariants ported from the product file at the same path."""

import pytest

from lu_machinery.review import record_cf_verdict as recorder


def test_same_family_refused(monkeypatch, low_review, tmp_path):
    tasks, comments, statuses, files, _admit = low_review
    files[:] = [{"filename": "lu_machinery/common/example.py", "status": "modified"}]
    with pytest.raises(recorder.RecordError, match="equals an author family"):
        recorder.record("review-one", pr_number=1, task_root=tasks, lock_root=tmp_path / "locks")
    assert comments == [] and statuses == []


@pytest.mark.parametrize("model,family", [("claude-opus-5-5", "anthropic"), ("gpt-6.1-sol", "openai")])
def test_formal_reviewer_still_admits_opus_and_sol(model, family):
    recorder._require_formal_reviewer(cursor=False, reported=model, model=model, family=family)


def test_head_and_branch_mismatch_refused(monkeypatch, low_review, tmp_path):
    tasks, comments, statuses, _files, _admit = low_review
    monkeypatch.setattr(recorder, "_pr", lambda *_: {
        "number": 1, "headRefOid": "b" * 40, "headRefName": "codex/fixture",
    })
    with pytest.raises(recorder.RecordError, match="head moved"):
        recorder.record("review-one", pr_number=1, task_root=tasks, lock_root=tmp_path / "locks")
    monkeypatch.setattr(recorder, "_pr", lambda *_: {
        "number": 1, "headRefOid": "a" * 40, "headRefName": "other/branch",
    })
    with pytest.raises(recorder.RecordError, match="branch does not match"):
        recorder.record("review-one", pr_number=1, task_root=tasks, lock_root=tmp_path / "locks")
    assert comments == [] and statuses == []

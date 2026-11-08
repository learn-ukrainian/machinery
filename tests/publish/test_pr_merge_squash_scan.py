"""Existing recorded-approval invariant ported from the product's same path."""

import pytest

from lu_machinery.publish import github as publisher


@pytest.mark.parametrize("recorder", [True, False])
def test_head_without_a_recorded_verdict_is_refused_only_where_the_recorder_is_used(
    monkeypatch, low_review, recorder,
):
    _tasks, _comments, _statuses, files, admit = low_review
    files[:] = [{"filename": "lu_machinery/common/example.py", "status": "modified"}]
    monkeypatch.setattr(publisher.gate, "catalog", lambda: {
        "public": {"github": "unit/public", "verdict_recorder": recorder},
    })
    if recorder:
        with pytest.raises(publisher.gate.PublishBlocked, match="no cross-family APPROVE"):
            admit()
    else:
        assert admit() is None

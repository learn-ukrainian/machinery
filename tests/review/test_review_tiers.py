"""Tiered landing regressions, ported from the recorder and publisher tests.

The source tests live in learn-ukrainian's test_record_cf_verdict.py and
publish/test_pr_merge_squash_scan.py. Machinery has no test suite yet.
"""

from __future__ import annotations

import pytest

from lu_machinery.publish import github as publisher
from lu_machinery.review import record_cf_verdict as recorder
from lu_machinery.review.tiers import classify_pr_files

_PR_READER = recorder._pr


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


def test_enqueue_refuses_an_unreviewed_security_pr_where_the_recorder_is_optional(low_review, monkeypatch):
    """Defect class: a repository's verdict_recorder opt-out waives the Carbon second review."""
    _tasks, comments, _statuses, files, admit = low_review
    monkeypatch.setattr(publisher.gate, "catalog", lambda: {
        "public": {"github": "unit/public", "verdict_recorder": False},
    })
    assert comments == []
    assert admit() is None  # an ordinary PR stays outside the optional recorder's regime
    files[:] = [{"filename": SECURITY_FILES[0], "status": "modified"}]
    with pytest.raises(publisher.gate.PublishBlocked, match="no cross-family APPROVE"):
        admit()


@pytest.mark.parametrize("repository,expected", [
    ("learn-ukrainian/machinery", "success"),
    ("learn-ukrainian/learn-ukrainian.github.io", "pending"),
])
def test_recorded_status_allows_carbon_debt_only_in_machinery(low_review, tmp_path, monkeypatch, repository, expected):
    """Defect class: a pending Carbon status stalls machinery before its debt admission can run."""
    import json
    from dataclasses import replace

    tasks, _comments, statuses, files, _admit = low_review
    path = tasks / "review-one.json"
    task = json.loads(path.read_text())
    task.update(repository=repository, model="claude-opus-5-5", agent="claude")
    path.write_text(json.dumps(task))
    original = recorder.pr_review_facts(None)
    monkeypatch.setattr(recorder, "pr_review_facts", lambda *_, **__: replace(original, repository=repository))
    files[:] = [{"filename": "lu_machinery/publish/github.py", "status": "modified"}]
    assert recorder.record("review-one", pr_number=1, task_root=tasks, lock_root=tmp_path / "locks")["verdict"] == "APPROVED"
    assert statuses[-1]["state"] == expected
    # Carbon rejection must still replace success/pending with failure in either repository.
    name = _carbon_task(tasks)
    path = tasks / (name + ".json")
    task = json.loads(path.read_text())
    task["repository"] = repository
    path.write_text(json.dumps(task))
    (tasks / (name + ".result")).write_text("VERDICT: REQUEST_CHANGES")
    assert recorder.record(name, pr_number=1, task_root=tasks, lock_root=tmp_path / "locks", second_review=True)["verdict"] == "CHANGES_REQUESTED"
    assert statuses[-1]["state"] == "failure"


@pytest.mark.parametrize("repository", ["learn-ukrainian/machinery", "learn-ukrainian/learn-ukrainian.github.io"])
@pytest.mark.parametrize("state", ["OPEN", "MERGED", "CLOSED"])
@pytest.mark.parametrize("second", [False, True])
def test_recording_merged_pr_is_carbon_machinery_only(monkeypatch, repository, state, second):
    """Defect class: debt cannot be recorded after merge, or merged recording escapes its repository and seat."""
    payload = {"number": 1, "headRefOid": "a" * 40, "headRefName": "codex/fixture", "state": state}
    monkeypatch.setattr(recorder, "_run_json", lambda *_: payload)
    if state == "OPEN" or (state == "MERGED" and second and repository == "learn-ukrainian/machinery"):
        assert recorder._pr(repository, "codex/fixture", 1, second) == payload
    else:
        with pytest.raises(recorder.RecordError, match="PR is not open"):
            recorder._pr(repository, "codex/fixture", 1, second)


def test_carbon_debt_record_uses_merged_pr_head_and_rechecks_under_lock(low_review, tmp_path, monkeypatch):
    """Defect class: post-merge Carbon recording fails, uses the squash SHA, or misses a moved head under lock."""
    import json

    tasks, comments, statuses, files, _admit = low_review
    files[:] = [{"filename": "lu_machinery/publish/github.py", "status": "modified"}]
    _record_of_record(comments)
    name = _carbon_task(tasks)
    path = tasks / (name + ".json")
    task = json.loads(path.read_text())
    task["repository"] = "learn-ukrainian/machinery"
    path.write_text(json.dumps(task))
    from dataclasses import replace

    facts = recorder.pr_review_facts(None)
    monkeypatch.setattr(recorder, "pr_review_facts", lambda *_, **__: replace(facts, repository="learn-ukrainian/machinery"))
    monkeypatch.setattr(recorder, "_pr", _PR_READER)
    payload = {"number": 1, "headRefOid": "a" * 40, "headRefName": "codex/fixture", "state": "MERGED"}
    reads = []

    def read(*_):
        reads.append(True)
        return payload

    monkeypatch.setattr(recorder, "_run_json", read)
    result = recorder.record(name, pr_number=1, task_root=tasks, lock_root=tmp_path / "locks", second_review=True)
    assert result["verdict"] == "APPROVED" and result["head"] == "a" * 40
    assert len(reads) == 2 and "second_review=carbon" in comments[-1]["body"]
    assert statuses[-1]["state"] == "success"
    comments.clear()
    reads.clear()

    def moved(*_):
        reads.append(True)
        return {**payload, "headRefOid": "b" * 40} if len(reads) == 2 else payload

    monkeypatch.setattr(recorder, "_run_json", moved)
    with pytest.raises(recorder.RecordError, match="PR head moved before publication"):
        recorder.record(name, pr_number=1, task_root=tasks, lock_root=tmp_path / "locks", second_review=True)
    assert comments == []

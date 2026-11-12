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


# Binding second review (operator 2026-11-11; ordered list Muse, Carbon, Sol, Opus since 2026-11-12).
SECURITY_FILES = (
    "scripts/audit/secret_scan_local.py",
    "scripts/ocr/_credentials.py",
    "packages/v4-runtime/src/learn_ukrainian_v4_runtime/credential_custody.py",
)


@pytest.mark.parametrize("path", SECURITY_FILES)
def test_security_paths_still_classify_without_a_second_review(path):
    """Defect class: removing the second-review gate changes how a security path is tiered."""
    rows = [{"filename": path, "status": "modified"}]
    assert classify_pr_files(rows) in {"medium", "high"}
    assert classify_pr_files([{"filename": "docs/example.md", "status": "modified"}]) != "high"


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
    with pytest.raises(recorder.RecordError, match="holds no review"):
        recorder.record(name, pr_number=1, task_root=tasks, lock_root=tmp_path / "locks")
    assert comments == [] and statuses == []


def test_second_review_is_not_recorded(low_review, tmp_path):
    """Defect class: a new record still issues the historical second-review token."""
    tasks, comments, statuses, files, _admit = low_review
    files[:] = [{"filename": SECURITY_FILES[0], "status": "modified"}]
    name = _carbon_task(tasks)
    with pytest.raises(recorder.RecordError, match="second review is not recorded"):
        recorder.record(name, pr_number=1, task_root=tasks, lock_root=tmp_path / "locks", second_review=True)
    assert comments == [] and statuses == []


def test_unreadable_pr_files_do_not_record_a_second_review(low_review, tmp_path, monkeypatch):
    """Defect class: a file-list failure still records a second review."""
    tasks, comments, statuses, _files, _admit = low_review

    def unavailable(*_args, **_kwargs):
        raise recorder.SweepError("files unavailable")

    monkeypatch.setattr(recorder.GitHubAdapter, "pr_file_rows", unavailable)
    name = _carbon_task(tasks)
    with pytest.raises(recorder.RecordError, match="second review is not recorded"):
        recorder.record(name, pr_number=1, task_root=tasks, lock_root=tmp_path / "locks", second_review=True)
    assert comments == [] and statuses == []


def test_enqueue_admits_a_security_pr_on_one_cross_family_review(low_review):
    """Defect class: a security-sensitive PR is held for a second review."""
    _tasks, comments, _statuses, files, admit = low_review
    files[:] = [{"filename": SECURITY_FILES[0], "status": "modified"}]
    _record_of_record(comments)
    assert admit() is None


def test_recording_a_second_review_does_not_add_a_seat(low_review, tmp_path):
    """Defect class: a second-review record is still accepted beside the reviewer of record."""
    tasks, comments, statuses, files, admit = low_review
    files[:] = [{"filename": SECURITY_FILES[0], "status": "modified"}]
    _record_of_record(comments)
    name = _carbon_task(tasks)
    before = len(comments)
    with pytest.raises(recorder.RecordError, match="second review is not recorded"):
        recorder.record(name, pr_number=1, task_root=tasks, lock_root=tmp_path / "locks", second_review=True)
    assert len(comments) == before and statuses == []
    assert admit() is None


def test_archived_task_path_alias_cannot_supply_both_review_runs(low_review, tmp_path, monkeypatch):
    """Defect class: one sealed provider run fills both seats through an archive path alias."""
    import hashlib
    import json
    from dataclasses import replace

    from lu_machinery.orchestration import integration_sweep as sweep

    tasks, comments, _statuses, files, admit = low_review
    facts = recorder.pr_review_facts(None)
    monkeypatch.setattr(recorder, "pr_review_facts", lambda *_, **__: replace(
        facts, commits=(replace(facts.commits[0], family="anthropic", author_models=("claude-opus-5-5",)),),
        existing_families=frozenset({"anthropic"}),
    ))
    files[:] = [{"filename": SECURITY_FILES[0], "status": "modified"}]
    path = tasks / "review-one.json"
    task = json.loads(path.read_text())
    task.update(run_nonce="one-provider-run", result_sha256=hashlib.sha256(
        (tasks / "review-one.result").read_bytes(),
    ).hexdigest())
    path.write_text(json.dumps(task))
    archive = tasks / "archive"
    archive.mkdir()
    path.rename(archive / path.name)
    (tasks / "review-one.result").rename(archive / "review-one.result")
    assert recorder.record("review-one", pr_number=1, task_root=tasks, lock_root=tmp_path / "locks")["verdict"] == "APPROVED"

    with pytest.raises(recorder.RecordError, match="second review is not recorded"):
        recorder.record("archive/review-one", pr_number=1, task_root=tasks,
                        lock_root=tmp_path / "locks", second_review=True)
    assert len(comments) == 1

    # A historical second-review approval is not itself the reviewer of record.
    alias = {**comments[0], "body": recorder.build_comment(
        sha="a" * 40, task_id="archive/review-one", started=task["started_at"],
        verdict="APPROVED", model=task["model"], family="openai", reply="VERDICT: APPROVE",
        author_models=("claude-opus-5-5",), second_review=True,
    )}
    assert sweep.lookup_verdict([alias], "a" * 40, "fleet").state != "APPROVED"
    assert sweep.lookup_verdict(comments, "a" * 40, "fleet").state == "APPROVED"
    with pytest.raises(recorder.RecordError, match="second review is not recorded"):
        recorder.record("review-two", pr_number=1, task_root=tasks,
                        lock_root=tmp_path / "locks", second_review=True)
    assert admit() is None


def test_optional_recorder_still_requires_one_review_on_a_security_path(low_review, monkeypatch):
    """Defect class: turning the verdict recorder off admits an unreviewed security PR."""
    _tasks, comments, _statuses, files, admit = low_review
    monkeypatch.setattr(publisher.gate, "catalog", lambda: {
        "public": {"github": "unit/public", "verdict_recorder": False},
    })
    assert comments == []
    assert admit() is None
    for path in (SECURITY_FILES[0], ".github/workflows/ci.yml"):
        files[:] = [{"filename": path, "status": "modified"}]
        with pytest.raises(publisher.gate.PublishBlocked, match="no cross-family APPROVE"):
            admit()


@pytest.mark.parametrize("repository", [
    "learn-ukrainian/machinery",
    "learn-ukrainian/learn-ukrainian.github.io",
])
def test_recorded_approval_is_success_without_a_second_seat(low_review, tmp_path, monkeypatch, repository):
    """Defect class: one exact-head approval stays pending until a second review is recorded."""
    import json
    from dataclasses import replace

    tasks, comments, statuses, files, _admit = low_review
    path = tasks / "review-one.json"
    task = json.loads(path.read_text())
    task.update(repository=repository, model="claude-opus-5-5", agent="claude")
    path.write_text(json.dumps(task))
    original = recorder.pr_review_facts(None)
    monkeypatch.setattr(recorder, "pr_review_facts", lambda *_, **__: replace(original, repository=repository))
    files[:] = [{"filename": "lu_machinery/publish/github.py", "status": "modified"}]
    assert recorder.record("review-one", pr_number=1, task_root=tasks, lock_root=tmp_path / "locks")["verdict"] == "APPROVED"
    assert statuses[-1]["state"] == "success"
    name = _carbon_task(tasks)
    with pytest.raises(recorder.RecordError, match="second review is not recorded"):
        recorder.record(name, pr_number=1, task_root=tasks, lock_root=tmp_path / "locks", second_review=True)
    assert len(comments) == 1 and statuses[-1]["state"] == "success"


@pytest.mark.parametrize("repository", ["learn-ukrainian/machinery", "learn-ukrainian/learn-ukrainian.github.io"])
@pytest.mark.parametrize("state", ["OPEN", "MERGED", "CLOSED"])
def test_recording_requires_an_open_pr(monkeypatch, repository, state):
    """Defect class: a merged or closed PR can still be recorded."""
    payload = {"number": 1, "headRefOid": "a" * 40, "headRefName": "codex/fixture", "state": state}
    monkeypatch.setattr(recorder, "_run_json", lambda *_: payload)
    if state == "OPEN":
        assert recorder._pr(repository, "codex/fixture", 1) == payload
    else:
        with pytest.raises(recorder.RecordError, match="PR is not open"):
            recorder._pr(repository, "codex/fixture", 1)


def test_second_review_is_refused_before_a_merged_pr_is_read(low_review, tmp_path):
    """Defect class: a second review is still recorded against a merged PR."""
    import json

    tasks, comments, statuses, files, _admit = low_review
    files[:] = [{"filename": "lu_machinery/publish/github.py", "status": "modified"}]
    _record_of_record(comments)
    name = _carbon_task(tasks)
    path = tasks / (name + ".json")
    task = json.loads(path.read_text())
    task["repository"] = "learn-ukrainian/machinery"
    path.write_text(json.dumps(task))
    before = len(comments)
    with pytest.raises(recorder.RecordError, match="second review is not recorded"):
        recorder.record(name, pr_number=1, task_root=tasks, lock_root=tmp_path / "locks", second_review=True)
    assert len(comments) == before and statuses == []

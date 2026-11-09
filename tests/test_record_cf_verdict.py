"""Existing recorder invariants ported from the product file at the same path."""

import json
import os
import subprocess

import pytest

from lu_machinery.review import record_cf_verdict as recorder
from lu_machinery.review import repair_round
from tests.review.record_support import write_task


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


def test_ci_fix_uses_the_repository_required_workflow(monkeypatch, tmp_path):
    """A repair round ties the row's required workflow, not a hardcoded ci.yml.

    Defect class: a failed ci.yml run counted as CI-fix evidence for a repository
    whose required workflow is another file (machinery's ci-gate.yml).
    Same cases as learn-ukrainian tests/test_record_cf_verdict.py at f6bb1346.
    """
    REPOSITORY = "owner/repo"
    CI_RUN = {"path": ".github/workflows/ci.yml", "conclusion": "failure"}

    def fake_github_reads(*, failed=(), queue=(), trees=None, issues=()):
        return repair_round.GitHubReads(
            failed_runs=lambda sha: [dict(run) for run in failed if run["head_sha"] == sha],
            queue_runs=lambda: [dict(run) for run in queue],
            commit_tree=lambda sha: (trees or {}).get(sha),
            closing_issues=lambda: list(issues),
        )

    repo = tmp_path / "repair-repo"
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
    )

    def run(*args):
        command = ["git", "-C", str(repo), *args]
        return subprocess.run(command, check=True, capture_output=True, text=True, env=env, timeout=30).stdout.strip()

    def commit(path, text, message):
        (repo / path).write_text(text)
        run("add", path)
        run("commit", "-q", "-m", message)
        return run("rev-parse", "HEAD")

    repo.mkdir()
    run("init", "-q", "-b", "main")
    commit("shared.txt", "base\n", "root")
    run("checkout", "-q", "-b", "feature")
    approved = commit("feature.txt", "feature\n", "feature")
    run("checkout", "-q", "main")
    commit("other.txt", "other\n", "base change")
    base = run("rev-parse", "main")
    tree = run("merge-tree", "--write-tree", base, approved)
    queue = [
        {
            **CI_RUN,
            "id": 8,
            "event": "merge_group",
            "head_branch": f"gh-readonly-queue/main/pr-42-{base}",
            "head_sha": "c" * 40,
        },
        {
            **CI_RUN,
            "id": 9,
            "event": "merge_group",
            "head_branch": f"gh-readonly-queue/main/pr-41-{base}",
            "head_sha": "c" * 40,
        },
        {
            **CI_RUN,
            "id": 10,
            "event": "merge_group",
            "head_branch": f"gh-readonly-queue/main/pr-42-{base}",
            "head_sha": "d" * 40,
        },
    ]
    reads = fake_github_reads(queue=queue, trees={"c" * 40: tree, "d" * 40: "e" * 40})
    gate = ".github/workflows/ci-gate.yml"
    row = {"github": REPOSITORY.upper(), "required_workflow": gate}
    monkeypatch.setattr(repair_round.prepublish, "catalog", lambda: {"unit": row})
    kwargs = dict(number=42, repo_root=repo, github=reads)
    assert repair_round.failed_runs_for(approved, **kwargs, workflow=repair_round.required_workflow(REPOSITORY)) == []
    gate_queue = [{**item, "path": gate} for item in queue]
    found = repair_round.failed_runs_for(
        approved, **{**kwargs, "github": fake_github_reads(queue=gate_queue, trees={"c" * 40: tree})}, workflow=gate
    )
    assert found == [{"run_id": 8, "event": "merge_group", "conclusion": "failure"}]
    for bad in ({"github": REPOSITORY, "required_workflow": "ci.yml"}, {"github": REPOSITORY, "required_workflow": 1}):
        monkeypatch.setattr(repair_round.prepublish, "catalog", lambda bad=bad: {"unit": bad})
        with pytest.raises(repair_round.RepairRefused, match=r"\[evidence\].*required_workflow"):
            repair_round.required_workflow(REPOSITORY)
    monkeypatch.setattr(repair_round.prepublish, "catalog", lambda: {"a": row, "b": row})
    with pytest.raises(repair_round.RepairRefused, match=r"\[evidence\].*registered more than once"):
        repair_round.required_workflow(REPOSITORY)
    # Every registered repository without the key keeps ci.yml; machinery's row names ci-gate.yml.
    monkeypatch.undo()
    workflows = {key: repair_round.required_workflow(repo.github) for key, repo in recorder.load_fleet_repos().items()}
    assert workflows.pop("machinery") == gate and set(workflows.values()) == {repair_round.DEFAULT_REQUIRED_WORKFLOW}


def _misnamed_copy(tasks, name, **updates):
    record = json.loads((tasks / "review-one.json").read_text())
    (tasks / name).write_text(json.dumps({**record, **updates}))


def test_saved_wait_output_beside_its_record_neither_blocks_nor_counts(tmp_path):
    """Defect class: a worker's saved `dispatch.py wait` stdout stopped every review fleet-wide."""
    from lu_machinery.review.review_lineage import LineageRefused, review_records

    tasks = tmp_path / "tasks"
    write_task(tasks, schema_version=2, pr=42, run_nonce="nonce-1")
    _misnamed_copy(tasks, "review-one.review-wait.json")
    assert [path.name for path, _ in review_records(tasks, repository="owner/repo")] == ["review-one.json"]
    # A copy saved mid-run differs from its named record, so it refuses rather than being skipped.
    _misnamed_copy(tasks, "review-one.review-wait.json", status="running")
    with pytest.raises(LineageRefused, match=r"review-one\.review-wait\.json has invalid task identity.*move it out"):
        review_records(tasks, repository="owner/repo")


def test_renamed_review_record_with_no_correctly_named_record_refuses(tmp_path):
    """Defect class: a renamed or orphaned review record silently drops out of the lineage."""
    from lu_machinery.review.review_lineage import LineageRefused, review_records

    tasks = tmp_path / "tasks"
    write_task(tasks, schema_version=2, pr=42, run_nonce="nonce-1")
    _misnamed_copy(tasks, "review-one.saved.json", run_nonce="nonce-2")
    with pytest.raises(LineageRefused, match=r"review-one\.saved\.json has invalid task identity"):
        review_records(tasks, repository="owner/repo")
    (tasks / "review-one.saved.json").unlink()
    (tasks / "review-one.json").rename(tasks / "review-one.saved.json")
    with pytest.raises(LineageRefused, match=r"review-one\.saved\.json has invalid task identity"):
        review_records(tasks, repository="owner/repo")
    # A legacy or schema-1 record under another name never vouches for the copy (round-1 F1).
    for anchor in ({}, {"schema_version": 1}):
        (tasks / "unrelated.json").write_text(json.dumps({**anchor, "task_id": "review-one", "run_nonce": "nonce-1"}))
        with pytest.raises(LineageRefused, match=r"review-one\.saved\.json has invalid task identity"):
            review_records(tasks, repository="owner/repo")


def test_copies_of_other_repository_and_non_review_records_do_not_refuse(tmp_path):
    """Defect class: a stray copy of any record beside its original blocks this repository's reviews."""
    from lu_machinery.review.review_lineage import review_records

    tasks = tmp_path / "tasks"
    write_task(tasks, task_id="other-review", schema_version=2, pr=7, run_nonce="nonce-2", repository="owner/other")
    (tasks / "other-review.wait.json").write_text((tasks / "other-review.json").read_text())
    worker = json.dumps({"schema_version": 2, "task_id": "worker", "run_nonce": "nonce-3", "status": "done"})
    (tasks / "worker.json").write_text(worker)
    (tasks / "worker.status.json").write_text(worker)
    # A list or object nonce in any record is never hashed into a crash (round-1 F2).
    (tasks / "listed.json").write_text(json.dumps({"schema_version": 2, "task_id": "listed", "run_nonce": []}))
    write_task(tasks, task_id="mapped", schema_version=2, pr=8, run_nonce={}, repository="owner/other")
    write_task(tasks, schema_version=2, pr=42, run_nonce="nonce-1")
    assert [path.name for path, _ in review_records(tasks, repository="owner/repo")] == ["review-one.json"]
